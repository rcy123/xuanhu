"""M1 关键配置快照与偏离告警测试。

- ``build_config_snapshot``：字段白名单 + base_url 只取域名（不含密钥）。
- 启动快照写入 audit_events；关键项变化 → config.drift 记录 + 指标。
- 版本等非关键项变化不触发偏离（防告警风暴）。
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.metrics import observe_config_drift
from app.core.redis import reset_redis
from app.models.audit import AuditEvent
from app.services.config_drift import (
    CONFIG_DRIFT_EVENT,
    CONFIG_SNAPSHOT_EVENT,
    _base_url_domain,
    build_config_snapshot,
    record_startup_config_snapshot,
)

pytestmark = [pytest.mark.integration]


def _settings(**overrides):
    base = dict(
        app_env="staging",
        agent_runtime_version="langgraph",
        langgraph_public_enabled=True,
        langgraph_product_ready=False,
        agent_runtime_rollout_phase="rampup",
        model_gateway_base_url="http://gateway.internal:8080/v1",
        cors_allowed_origins=["http://localhost:5173"],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_build_config_snapshot_allowlist() -> None:
    snapshot = build_config_snapshot(_settings())
    assert snapshot["app_env"] == "staging"
    assert snapshot["agent_runtime_version"] == "langgraph"
    assert snapshot["langgraph_product_ready"] is False
    assert snapshot["agent_runtime_rollout_phase"] == "rampup"
    assert snapshot["model_gateway_base_url"] == "gateway.internal:8080"  # 域名部分
    assert snapshot["cors_allowed_origins"] == ["http://localhost:5173"]
    # 白名单之外没有多余字段
    assert set(snapshot) == {
        "app_env",
        "agent_runtime_version",
        "langgraph_public_enabled",
        "langgraph_product_ready",
        "agent_runtime_rollout_phase",
        "model_gateway_base_url",
        "cors_allowed_origins",
    }


def test_base_url_domain_strips_path_and_scheme() -> None:
    assert _base_url_domain("https://gw.example.com:8443/v1/chat") == "gw.example.com:8443"
    assert _base_url_domain("http://127.0.0.1:8080/v1") == "127.0.0.1:8080"
    assert _base_url_domain("internal-only") == "internal-only"


@pytest_asyncio.fixture(loop_scope="module")
async def db() -> AsyncIterator[object]:
    from app.db.session import get_session_factory

    async with get_session_factory()() as session:
        yield session


@pytest_asyncio.fixture(loop_scope="module", autouse=True)
async def _cleanup() -> AsyncIterator[None]:
    yield
    with contextlib.suppress(Exception):
        await reset_redis()


async def _count_events(db, event_type: str) -> int:
    from sqlalchemy import func

    return int(
        (await db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.event_type == event_type))) or 0
    )


@pytest.mark.asyncio(loop_scope="module")
async def test_first_startup_writes_snapshot_no_drift(db) -> None:
    drifted, snapshot = await record_startup_config_snapshot(
        db,
        _settings(),
        trace_id="cfg-first",
    )
    assert drifted == []
    assert snapshot["agent_runtime_rollout_phase"] == "rampup"
    assert await _count_events(db, CONFIG_SNAPSHOT_EVENT) == 1
    assert await _count_events(db, CONFIG_DRIFT_EVENT) == 0


@pytest.mark.asyncio(loop_scope="module")
async def test_critical_key_change_emits_drift(db) -> None:
    await record_startup_config_snapshot(db, _settings(), trace_id="cfg-a")
    drifted, snapshot = await record_startup_config_snapshot(
        db,
        _settings(langgraph_product_ready=True),
        trace_id="cfg-b",
    )
    assert drifted == ["langgraph_product_ready"]
    assert snapshot["langgraph_product_ready"] is True
    assert await _count_events(db, CONFIG_DRIFT_EVENT) == 1


@pytest.mark.asyncio(loop_scope="module")
async def test_non_critical_change_does_not_drift(db) -> None:
    """版本号 / CORS 等正常变更不触发偏离（防告警风暴）。"""
    await record_startup_config_snapshot(db, _settings(), trace_id="cfg-c")
    drifted, _ = await record_startup_config_snapshot(
        db,
        _settings(cors_allowed_origins=["http://new.example"], app_env="production"),
        trace_id="cfg-d",
    )
    assert drifted == []


@pytest.mark.asyncio(loop_scope="module")
async def test_metrics_observer_bounded(db) -> None:
    """observe_config_drift 对允许列表之外的 type 收敛到 unknown（无副作用）。"""
    observe_config_drift("langgraph_product_ready")
    observe_config_drift("agent_runtime_rollout_phase")
    observe_config_drift("model_gateway_base_url")
    observe_config_drift("random-type")  # fail-closed
