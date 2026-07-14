"""P4-2 BaseAgent 与 Prompt 版本机制测试。"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import BaseAgentImpl
from app.agents.errors import AgentRunError, PromptManifestError
from app.agents.prompt_loader import PromptLoader
from app.core.exceptions import ModelGatewayTimeoutError, ModelGatewayUnavailableError
from app.models.agent import AgentRun
from app.models.audit import AuditEvent
from app.models.consult import ConsultSession
from app.schemas.agent import XuanhuState
from app.schemas.types import Stage


class FakeOutput(BaseModel):
    """测试用结构化输出。"""

    answer: str
    confidence: float = Field(ge=0.0, le=1.0)


class FakeGateway:
    """可控 fake gateway，不依赖真实模型网关。"""

    def __init__(self, responses: list[BaseModel | dict[str, Any] | Exception]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def chat_structured(
        self,
        messages: list[dict[str, Any]],
        output_schema: type[BaseModel],
        *,
        trace_id: str,
        session_id: str | None = None,
        agent_name: str | None = None,
    ) -> BaseModel | dict[str, Any]:
        self.calls.append(
            {
                "messages": messages,
                "output_schema": output_schema,
                "trace_id": trace_id,
                "session_id": session_id,
                "agent_name": agent_name,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeAgent(BaseAgentImpl):
    """测试用 Agent，仅组装 prompt，不包含业务逻辑。"""

    name = "test_agent"
    stage = Stage.INQUIRY
    output_schema = FakeOutput
    next_stage = Stage.SUFFICIENCY

    async def _build_prompt(
        self,
        state: XuanhuState,
        evidences: list[Any],
    ) -> list[dict[str, Any]]:
        del evidences
        return [
            {"role": "system", "content": self.prompt_template.content},
            {"role": "user", "content": f"session={state.session_id}"},
        ]


def _write_prompt_files(tmp_path: Path, *, content: str = "TEST_PROMPT") -> Path:
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "manifest.yaml").write_text("test_agent: test_agent_v1.jinja2\n", encoding="utf-8")
    (prompt_dir / "test_agent_v1.jinja2").write_text(content, encoding="utf-8")
    return prompt_dir / "manifest.yaml"


def test_prompt_loader_loads_manifest_entry(tmp_path: Path) -> None:
    """PromptLoader 从 manifest 加载当前版本文件。"""
    manifest = _write_prompt_files(tmp_path, content="hello prompt")
    template = PromptLoader(manifest).load("test_agent")

    assert template.agent_name == "test_agent"
    assert template.prompt_version == "test_agent_v1.jinja2"
    assert template.content == "hello prompt"


def test_prompt_loader_missing_agent_raises(tmp_path: Path) -> None:
    """manifest 未配置 agent 时返回明确错误。"""
    manifest = _write_prompt_files(tmp_path)

    with pytest.raises(PromptManifestError):
        PromptLoader(manifest).load("unknown_agent")


def test_prompt_loader_missing_manifest_raises(tmp_path: Path) -> None:
    """manifest 文件缺失时返回明确错误。"""
    missing_manifest = tmp_path / "missing" / "manifest.yaml"

    with pytest.raises(PromptManifestError):
        PromptLoader(missing_manifest).load("test_agent")


def test_prompt_loader_missing_prompt_file_raises(tmp_path: Path) -> None:
    """manifest 指向不存在的 prompt 文件时返回明确错误。"""
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    manifest = prompt_dir / "manifest.yaml"
    manifest.write_text("test_agent: missing_prompt.jinja2\n", encoding="utf-8")

    with pytest.raises(PromptManifestError):
        PromptLoader(manifest).load("test_agent")


def test_prompt_loader_rejects_path_traversal(tmp_path: Path) -> None:
    """manifest 不允许 prompt 路径越界。"""
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (tmp_path / "outside.jinja2").write_text("outside", encoding="utf-8")
    manifest = prompt_dir / "manifest.yaml"
    manifest.write_text("test_agent: ../outside.jinja2\n", encoding="utf-8")

    with pytest.raises(PromptManifestError):
        PromptLoader(manifest).load("test_agent")


@pytest.mark.asyncio
async def test_base_agent_success_with_fake_gateway(tmp_path: Path) -> None:
    """BaseAgentImpl 成功路径：加载 prompt、调用 fake gateway、校验输出。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([{"answer": "ok", "confidence": 0.8}])
    agent = FakeAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
    )

    result = await agent.run(XuanhuState(session_id=str(uuid.uuid4())), "trace-success")

    assert isinstance(result.output, FakeOutput)
    assert result.output.answer == "ok"
    assert result.next_stage == Stage.SUFFICIENCY
    assert result.prompt_version == "test_agent_v1.jinja2"
    assert result.agent_run_id is None
    assert gateway.calls[0]["trace_id"] == "trace-success"
    assert gateway.calls[0]["agent_name"] == "test_agent"
    assert gateway.calls[0]["messages"][0]["content"] == "TEST_PROMPT"


@pytest.mark.asyncio
async def test_base_agent_retries_schema_validation_failure(tmp_path: Path) -> None:
    """fake gateway 首次返回非法结构时，BaseAgentImpl 会重试并成功。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            {"answer": "bad", "confidence": 2.0},
            {"answer": "fixed", "confidence": 0.6},
        ]
    )
    agent = FakeAgent(gateway=gateway, prompt_loader=PromptLoader(manifest), max_retries=1)

    result = await agent.run(XuanhuState(session_id=str(uuid.uuid4())), "trace-retry")

    assert result.output.answer == "fixed"
    assert len(gateway.calls) == 2


@pytest.mark.asyncio
async def test_base_agent_timeout_failure_is_sanitized(tmp_path: Path) -> None:
    """模型超时最终失败时返回脱敏 AgentRunError。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            ModelGatewayTimeoutError("timeout with sk-should-not-leak"),
            ModelGatewayTimeoutError("timeout with prompt text"),
        ]
    )
    agent = FakeAgent(gateway=gateway, prompt_loader=PromptLoader(manifest), max_retries=1)

    with pytest.raises(AgentRunError) as exc_info:
        await agent.run(XuanhuState(session_id=str(uuid.uuid4())), "trace-timeout")

    error = exc_info.value
    assert error.code == "AGENT_MODEL_TIMEOUT"
    assert error.retryable is True
    assert "sk-should-not-leak" not in error.message
    assert "prompt text" not in error.message
    assert len(gateway.calls) == 2


@pytest.mark.asyncio
async def test_base_agent_gateway_unavailable_failure(tmp_path: Path) -> None:
    """模型网关不可用时归一化为 AGENT_MODEL_UNAVAILABLE。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            ModelGatewayUnavailableError("gateway unavailable sk-nope", retryable=False),
        ]
    )
    agent = FakeAgent(gateway=gateway, prompt_loader=PromptLoader(manifest), max_retries=2)

    with pytest.raises(AgentRunError) as exc_info:
        await agent.run(XuanhuState(session_id=str(uuid.uuid4())), "trace-unavailable")

    error = exc_info.value
    assert error.code == "AGENT_MODEL_UNAVAILABLE"
    assert error.retryable is False
    assert error.retry_count == 0
    assert "sk-nope" not in error.message
    assert len(gateway.calls) == 1


pytestmark_integration = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


@pytest_asyncio.fixture(loop_scope="module")
async def db() -> AsyncSession:
    """提供集成测试数据库会话。"""
    from app.db.session import get_session_factory, reset_session_factory

    await reset_session_factory()
    factory = get_session_factory()
    try:
        async with factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"PostgreSQL integration dependency unavailable: {type(exc).__name__}: {exc}")

    async with factory() as session:
        yield session


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_base_agent_writes_agent_run_and_audit(
    db: AsyncSession,
    tmp_path: Path,
) -> None:
    """有 DB session 时写入 agent_runs 和 audit_events，且审计 payload 不含 prompt 原文。"""
    session_id = uuid.uuid4()
    db.add(
        ConsultSession(
            id=session_id,
            patient_ref="P4-2-BASEAGENT",
            patient_info={"patient_ref": "P4-2-BASEAGENT"},
            current_stage="inquiry",
            status="active",
        )
    )
    await db.commit()

    manifest = _write_prompt_files(tmp_path, content="SECRET_PROMPT_DO_NOT_AUDIT")
    gateway = FakeGateway([FakeOutput(answer="ok", confidence=0.9)])
    agent = FakeAgent(
        gateway=gateway,
        db=db,
        prompt_loader=PromptLoader(manifest),
        max_retries=2,
        model_name="fake-chat-model",
    )

    try:
        result = await agent.run(XuanhuState(session_id=str(session_id)), "trace-db")
        await db.commit()

        run_result = await db.execute(select(AgentRun).where(AgentRun.session_id == session_id))
        run = run_result.scalar_one()
        assert result.agent_run_id == str(run.id)
        assert run.agent_name == "test_agent"
        assert run.stage == "inquiry"
        assert run.prompt_version == "test_agent_v1.jinja2"
        assert run.model == "fake-chat-model"
        assert run.status == "success"
        assert run.retry_count == 0
        assert run.output_snapshot == {"answer": "ok", "confidence": 0.9}

        audit_result = await db.execute(
            select(AuditEvent)
            .where(AuditEvent.session_id == session_id)
            .where(AuditEvent.event_type.in_(["agent.started", "agent.finished"]))
            .order_by(AuditEvent.created_at, AuditEvent.id)
        )
        audits = audit_result.scalars().all()
        # agent.started 与 agent.finished 在同一事务内连续写入，created_at
        # 可能落在同一毫秒（server_default=func.now() 为事务时间戳）。
        # UUID v4 不保证插入序，仅校验事件集合存在即可覆盖审计完整性。
        assert {event.event_type for event in audits} == {"agent.started", "agent.finished"}
        audit_text = " ".join(str(event.payload) for event in audits)
        assert "SECRET_PROMPT_DO_NOT_AUDIT" not in audit_text
        assert "sk-" not in audit_text
    finally:
        await db.rollback()
        await db.execute(delete(AuditEvent).where(AuditEvent.session_id == session_id))
        await db.execute(delete(AgentRun).where(AgentRun.session_id == session_id))
        await db.execute(delete(ConsultSession).where(ConsultSession.id == session_id))
        await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_base_agent_failure_writes_failed_run_and_audit(
    db: AsyncSession,
    tmp_path: Path,
) -> None:
    """失败路径写入 failed agent_run 和 agent.failed 审计事件。"""
    session_id = uuid.uuid4()
    db.add(
        ConsultSession(
            id=session_id,
            patient_ref="P4-2-BASEAGENT-FAIL",
            patient_info={"patient_ref": "P4-2-BASEAGENT-FAIL"},
            current_stage="inquiry",
            status="active",
        )
    )
    await db.commit()

    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([{"answer": "bad", "confidence": 2.0}, {"answer": "bad", "confidence": 2.0}])
    agent = FakeAgent(gateway=gateway, db=db, prompt_loader=PromptLoader(manifest), max_retries=1)

    try:
        with pytest.raises(AgentRunError) as exc_info:
            await agent.run(XuanhuState(session_id=str(session_id)), "trace-db-failed")
        await db.commit()

        assert exc_info.value.code == "AGENT_SCHEMA_INVALID"
        run_result = await db.execute(select(AgentRun).where(AgentRun.session_id == session_id))
        run = run_result.scalar_one()
        assert run.status == "failed"
        assert run.error_code == "AGENT_SCHEMA_INVALID"
        assert run.prompt_version == "test_agent_v1.jinja2"
        assert run.retry_count == 1

        audit_result = await db.execute(
            select(AuditEvent)
            .where(AuditEvent.session_id == session_id)
            .where(AuditEvent.event_type == "agent.failed")
        )
        audit = audit_result.scalar_one()
        assert audit.payload["error_code"] == "AGENT_SCHEMA_INVALID"
        assert "SECRET" not in str(audit.payload)
    finally:
        await db.rollback()
        await db.execute(delete(AuditEvent).where(AuditEvent.session_id == session_id))
        await db.execute(delete(AgentRun).where(AgentRun.session_id == session_id))
        await db.execute(delete(ConsultSession).where(ConsultSession.id == session_id))
        await db.commit()
