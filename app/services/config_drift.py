"""M1 关键配置快照与偏离告警。

应用启动时把「安全开关类」配置的当前值写入一条 ``config.snapshot`` 审计
事件；随后与上一次快照对比，若关键项（``langgraph_product_ready``、
``agent_runtime_rollout_phase``、``model_gateway_base_url``）发生变化，
写一条 ``config.drift`` 审计事件并升级 ``xuanhu_config_drift_total`` 指标
（配合 Prometheus 规则告警），确保「误把安全开关推到生产」留下不可抵赖
的痕迹。

本模块全部为 best-effort：审计写入失败只记 warning，绝不阻断启动。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.metrics import observe_config_drift
from app.models.audit import AuditEvent

logger = logging.getLogger("xuanhu.config_drift")

CONFIG_SNAPSHOT_EVENT = "config.snapshot"
CONFIG_DRIFT_EVENT = "config.drift"

# 参与偏离对比的关键项：只有这些项的变更允许触发 config.drift 告警。
# 版本号、CORS 域名等正常变更不在其列，防止每次部署都误报（告警风暴）。
DRIFT_KEYS: tuple[str, ...] = (
    "langgraph_product_ready",
    "agent_runtime_rollout_phase",
    "model_gateway_base_url",
)

# 快照完整字段（与 docs/04_生产环境加固/04-运行态安全加固.md §7.1 一致）。
SNAPSHOT_KEYS: tuple[str, ...] = (
    "app_env",
    "agent_runtime_version",
    "langgraph_public_enabled",
    "langgraph_product_ready",
    "agent_runtime_rollout_phase",
    "model_gateway_base_url",
    "cors_allowed_origins",
)


def _base_url_domain(base_url: str) -> str:
    """只取 base_url 的域名部分（含端口），避免快照携带路径/密钥形态差异。"""
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    if parsed.hostname:
        return f"{parsed.hostname}:{parsed.port}" if parsed.port else parsed.hostname
    return base_url.strip().rstrip("/").split("/")[-1]


def build_config_snapshot(settings: Any) -> dict[str, Any]:
    """从 Settings 构建允许列表快照 payload（不携带任何密钥）。"""
    return {
        "app_env": settings.app_env,
        "agent_runtime_version": settings.agent_runtime_version,
        "langgraph_public_enabled": bool(settings.langgraph_public_enabled),
        "langgraph_product_ready": bool(settings.langgraph_product_ready),
        "agent_runtime_rollout_phase": settings.agent_runtime_rollout_phase,
        "model_gateway_base_url": _base_url_domain(str(settings.model_gateway_base_url)),
        "cors_allowed_origins": list(settings.cors_allowed_origins),
    }


async def _latest_snapshot(db: AsyncSession) -> dict[str, Any] | None:
    row = await db.scalar(
        select(AuditEvent)
        .where(AuditEvent.event_type == CONFIG_SNAPSHOT_EVENT)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(1)
    )
    if row is None:
        return None
    payload = row.payload
    return payload if isinstance(payload, dict) else None


async def record_startup_config_snapshot(
    db: AsyncSession,
    settings: Any,
    *,
    trace_id: str | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """写入本次启动快照并检测相对上一次快照的关键项偏离。

    Returns:
        (drifted_keys, current_snapshot)：drifted_keys 为发生变化的
        DRIFT_KEYS 子集（空列表=无偏离）。best-effort：任何异常只记
        warning 并返回空偏离，不阻断应用启动。
    """
    current = build_config_snapshot(settings)
    drifted: list[str] = []
    try:
        previous = await _latest_snapshot(db)
        if previous is not None:
            drifted = [key for key in DRIFT_KEYS if previous.get(key) != current.get(key)]
        db.add(
            AuditEvent(
                session_id=None,
                event_type=CONFIG_SNAPSHOT_EVENT,
                actor_type="system",
                actor_id=None,
                payload=current,
                trace_id=trace_id,
            )
        )
        await db.commit()
        for key in drifted:
            logger.warning(
                "config.drift: 关键配置偏离 %s=%r（上一次=%r）",
                key,
                current.get(key),
                previous.get(key) if previous is not None else None,
            )
            observe_config_drift(key)
        if drifted:
            db.add(
                AuditEvent(
                    session_id=None,
                    event_type=CONFIG_DRIFT_EVENT,
                    actor_type="system",
                    actor_id=None,
                    payload={"drifted_keys": drifted, "snapshot": current},
                    trace_id=trace_id,
                )
            )
            await db.commit()
    except Exception:  # noqa: BLE001 - best-effort，审计失败不阻断启动
        logger.warning("config.snapshot 写入失败（best-effort 忽略）", exc_info=True)
        return [], current
    return drifted, current


async def record_manual_snapshot(
    db: AsyncSession,
    settings: Any,
    *,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """运维手动触发一次快照（无偏离检测语义，仅留痕）。

    供诊断脚本/管理命令使用；正常启动流程走
    :func:`record_startup_config_snapshot`。
    """
    current = build_config_snapshot(settings)
    db.add(
        AuditEvent(
            session_id=None,
            event_type=CONFIG_SNAPSHOT_EVENT,
            actor_type="system",
            actor_id=None,
            payload={**current, "manual": True},
            trace_id=trace_id or str(uuid.uuid4()),
        )
    )
    await db.commit()
    return current
