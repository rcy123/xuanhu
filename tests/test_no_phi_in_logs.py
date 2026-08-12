"""阶段 2 认证测试：日志 PHI 脱敏（T2.5 / T2.9 / 验收清单）。

- 单元：``redact_phi`` / ``PHIRedactingFilter`` 对 session_id / patient_* /
  手机号 / 身份证号等字段形态做替换。
- 集成：跑一次真实会话创建 + 消息提交，捕获全量日志，断言不含患者姓名与
  症状原文 token（hard gate，防后续回归）。
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.log_filter import PHIRedactingFilter, redact_phi
from app.core.redis import reset_redis
from app.main import app
from app.models.audit import AuditEvent
from app.models.consult import ConsultMessage, ConsultSession
from app.models.doctor import Doctor
from app.models.domain import Observation

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

_PATIENT_REF_PREFIX = "NO-PHI-"
_PATIENT_NAME = "测试患者张三丰"
_SYMPTOM_TOKEN = "怕冷四肢凉痛经加重"


# ---------------------------------------------------------------------------
# 单元：脱敏模式
# ---------------------------------------------------------------------------


def test_redact_session_id_uuid() -> None:
    sid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    msg = f"session_id={sid} session is busy"
    out = redact_phi(msg)
    assert sid not in out
    assert "session_id=[REDACTED]" in out


def test_redact_session_id_quoted() -> None:
    sid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    msg = f"'session_id': '{sid}' not found"
    out = redact_phi(msg)
    assert sid not in out


def test_redact_patient_ref() -> None:
    msg = "patient_ref=NO-PHI-abc123 created"
    out = redact_phi(msg)
    assert "NO-PHI-abc123" not in out
    assert "patient_ref=[REDACTED]" in out


def test_redact_phone_and_id_card() -> None:
    msg = "联系手机 13812345678 身份证 110101199003078888"
    out = redact_phi(msg)
    assert "13812345678" not in out
    assert "110101199003078888" not in out


def test_redact_plain_technical_log_unchanged() -> None:
    msg = "gate evaluation passed policy_version=3 trace=abc-123"
    assert redact_phi(msg) == msg


def test_filter_transforms_record() -> None:
    filt = PHIRedactingFilter()
    record = logging.LogRecord(
        name="xuanhu",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="session_id=f47ac10b-58cc-4372-a567-0e02b2c3d479 failed",
        args=(),
        exc_info=None,
    )
    assert filt.filter(record) is True
    assert "f47ac10b-58cc-4372-a567-0e02b2c3d479" not in record.getMessage()


# ---------------------------------------------------------------------------
# 集成：端到端流程日志 PHI 扫描
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _cleanup() -> AsyncIterator[None]:
    from app.db.session import get_session_factory, reset_session_factory

    await reset_session_factory()
    try:
        # 与 test_sessions_api 一致：先落一条 durable runtime.switched 审计，
        # 统一后端要求默认运行时(langgraph)与最近一次 switch 审计一致。
        from datetime import UTC, datetime

        from app.services.runtime_switch_audit import (
            PostgresRuntimeSwitchAuditRepository,
            RuntimeSwitchAuditService,
            RuntimeSwitchRecord,
        )

        factory = get_session_factory()
        async with factory() as session:
            service = RuntimeSwitchAuditService(PostgresRuntimeSwitchAuditRepository(session))
            if (await service.status("langgraph")).status != "ok":
                await service.record_switch(
                    RuntimeSwitchRecord(
                        from_runtime="legacy",
                        to_runtime="langgraph",
                        operator="test-operator",
                        reason="integration tests authorize the langgraph default runtime",
                        deployment_id="no-phi-test-deploy-0001",
                        timestamp=datetime.now(UTC),
                    ),
                    configured_runtime="langgraph",
                )
                await session.commit()
        yield
    finally:
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(ConsultSession.id).where(ConsultSession.patient_ref.like(f"{_PATIENT_REF_PREFIX}%"))
            )
            ids = [row[0] for row in result.all()]
            if ids:
                await session.execute(delete(Observation).where(Observation.session_id.in_(ids)))
                await session.execute(delete(ConsultMessage).where(ConsultMessage.session_id.in_(ids)))
                await session.execute(delete(AuditEvent).where(AuditEvent.session_id.in_(ids)))
                await session.execute(delete(ConsultSession).where(ConsultSession.id.in_(ids)))
            await session.execute(delete(Doctor).where(Doctor.name == "日志PHI测试医师"))
            await session.commit()
        with contextlib.suppress(Exception):
            await reset_redis()


@pytest_asyncio.fixture(loop_scope="module")
async def db() -> AsyncSession:
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        yield session


@pytest_asyncio.fixture(loop_scope="module")
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_consultation_flow_logs_contain_no_phi(
    client: AsyncClient,
    db: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """完整问诊流程的日志不含患者姓名与症状原文 token。"""
    from app.api.auth import hash_password
    from app.core.auth import create_access_token

    doctor = Doctor(name="日志PHI测试医师", password_hash=hash_password("p"), enabled=True)
    db.add(doctor)
    await db.commit()
    await db.refresh(doctor)
    token, _ = create_access_token(str(doctor.id), name=doctor.name)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with caplog.at_level(logging.INFO, logger="xuanhu"):
            # 创建会话（patient_info 含 PHI 样例）
            resp = await client.post(
                "/api/v1/consult/sessions",
                headers=headers,
                json={
                    "chief_complaint": _SYMPTOM_TOKEN,
                    "patient_info": {"patient_ref": f"{_PATIENT_REF_PREFIX}phi", "name": _PATIENT_NAME, "age": 35},
                },
            )
            assert resp.status_code == 201, resp.text
            session_id = resp.json()["data"]["session_id"]

            # 提交一条消息（内容含 PHI 样例）
            msg = await client.post(
                f"/api/v1/consult/sessions/{session_id}/messages",
                headers=headers,
                json={"content": f"患者自述：{_SYMPTOM_TOKEN}，姓名 {_PATIENT_NAME}"},
            )
            # 消息可能触发 agent 调用（mock 环境 503/422 均可），关键是日志无 PHI
            assert msg.status_code in (200, 202, 422, 503), msg.text

            # 触发一次必然落日志的操作（非法 session_id → 校验错误）
            await client.get("/api/v1/consult/sessions/../../etc/stream")

        captured = caplog.text
        assert _PATIENT_NAME not in captured, "患者姓名出现在日志中"
        assert _SYMPTOM_TOKEN not in captured, "症状原文出现在日志中"
        assert f"{_PATIENT_REF_PREFIX}phi" not in captured, "patient_ref 出现在日志中"
    finally:
        # 先删该医师名下的会话（doctor_id FK 阻止先删医师），再删医师
        result = await db.execute(
            select(ConsultSession.id).where(ConsultSession.doctor_id == doctor.id)
        )
        session_ids = [row[0] for row in result.all()]
        if session_ids:
            await db.execute(delete(Observation).where(Observation.session_id.in_(session_ids)))
            await db.execute(delete(ConsultMessage).where(ConsultMessage.session_id.in_(session_ids)))
            await db.execute(delete(AuditEvent).where(AuditEvent.session_id.in_(session_ids)))
            await db.execute(delete(ConsultSession).where(ConsultSession.id.in_(session_ids)))
        await db.execute(delete(Doctor).where(Doctor.id == doctor.id))
        await db.commit()
