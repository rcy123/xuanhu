"""澄清/对话 Agent（L3-6）测试：输出校验、强信号检测、图节点短路。"""

from __future__ import annotations

import uuid

import pytest
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent_runtime.runtime import AgentRuntime
from app.agents.clarification import (
    ClarificationOutputBoundaryError,
    canonicalize_clarification_output,
)
from app.models.consult import ConsultSession
from app.schemas.clarification import ClarificationOutput
from app.schemas.intake import IntakeExtractionOutput
from app.services import langgraph_intake as langgraph_intake_module
from app.services.langgraph_intake import IntakeCommandClaim, LangGraphIntakeMessageRunner
from tests._database_safety import destructive_database_environment

# ---------------------------------------------------------------------------
# 输出校验
# ---------------------------------------------------------------------------


def test_canonicalize_accepts_normal_reply() -> None:
    out = canonicalize_clarification_output(
        ClarificationOutput(reply="'头身感受'指头痛、头晕等不适。请继续回答：患者近期头身感受怎样？")
    )
    assert "头身感受" in out.reply


def test_canonicalize_rejects_identity_data() -> None:
    for bad in ("请提供患者手机号 13812345678", "患者身份证 110101199001011234", "请问患者姓名是？"):
        with pytest.raises(ClarificationOutputBoundaryError):
            canonicalize_clarification_output(ClarificationOutput(reply=bad))


def test_canonicalize_rejects_medical_advice() -> None:
    for bad in ("建议服用阿莫西林每日三次", "诊断为上呼吸道感染，治疗方案为…", "推荐服用剂量为每次2片"):
        with pytest.raises(ClarificationOutputBoundaryError):
            canonicalize_clarification_output(ClarificationOutput(reply=bad))


def test_canonicalize_rejects_blank_and_oversize() -> None:
    with pytest.raises(ClarificationOutputBoundaryError):
        canonicalize_clarification_output(ClarificationOutput(reply="   "))
    # oversize 走原始 dict 形态，验证 canonicalize 层（而非 Pydantic 构造层）拦截
    with pytest.raises(ClarificationOutputBoundaryError):
        canonicalize_clarification_output({"reply": "答" * 501})


# ---------------------------------------------------------------------------
# 强信号检测
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "什么是头身感受",
        "头身感受是什么意思？",
        "没听懂这个问题",
        "我不明白为什么要问睡眠",
        "能解释一下吗",
        "为什么问这个问题",
    ],
)
def test_strong_signal_detected(text: str) -> None:
    assert langgraph_intake_module._is_clarification_strong_signal(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "有时会发热出汗",
        "没有",
        "正常",
        "睡眠质量一般",
        "一直咳嗽，有痰",
        "逐渐加重",
    ],
)
def test_normal_answers_not_flagged(text: str) -> None:
    assert langgraph_intake_module._is_clarification_strong_signal(text) is False


# ---------------------------------------------------------------------------
# E2E：强信号消息走澄清分支（fake gateway + 真实 PostgreSQL）
# ---------------------------------------------------------------------------


class _ClarifyTestGateway:
    """Fake gateway：intake 返回 abstained，澄清返回固定解释。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def chat_structured(
        self,
        messages: list[dict[str, object]],
        output_schema: type[BaseModel],
        **kwargs: object,
    ) -> BaseModel:
        self.calls.append({"agent_name": kwargs.get("agent_name"), "output_schema": output_schema})
        if output_schema is IntakeExtractionOutput:
            return IntakeExtractionOutput(decision="abstained")
        if output_schema is ClarificationOutput:
            return ClarificationOutput(
                reply="“头身感受”指头痛、头晕、头重、身体酸痛或乏力等不适。请继续回答：患者近期头身感受怎样？"
            )
        raise AssertionError(f"unexpected output schema: {output_schema}")

    @property
    def clarify_calls(self) -> int:
        return sum(1 for item in self.calls if item["output_schema"] is ClarificationOutput)


class _TestIntakeRunner(LangGraphIntakeMessageRunner):
    """生产 runner 的测试子类：允许 request-local runtime（与 test_l3_5 一致）。"""

    def __init__(self, db: object) -> None:
        super().__init__(db, allow_request_local_runtime=True)


def _install_fake_runtime(monkeypatch: pytest.MonkeyPatch, gateway: _ClarifyTestGateway) -> None:
    monkeypatch.setattr(
        langgraph_intake_module,
        "AgentRuntime",
        lambda: AgentRuntime(gateway, recorder=None),
    )


@pytest.fixture(scope="module")
def migrated_database() -> str:
    from alembic import command
    from alembic.config import Config

    with destructive_database_environment() as db_url:
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", db_url.replace("%", "%%"))
        try:
            command.downgrade(config, "20260711_0004")
            command.upgrade(config, "20260712_0006")
            command.downgrade(config, "20260711_0004")
            command.upgrade(config, "20260712_0006")
            command.upgrade(config, "head")
            yield db_url
        finally:
            command.upgrade(config, "head")


@pytest.fixture
async def db_factory(migrated_database: str) -> object:
    from app.db.session import _build_async_pg_url, reset_session_factory

    await reset_session_factory()
    engine = create_async_engine(_build_async_pg_url(migrated_database), pool_size=3, max_overflow=3)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE http_command_claims, intake_command_claims, domain_command_commits, "
                "outbox_events, gate_results, "
                "artifact_revisions, graph_run_steps, graph_runs, safety_profiles, observations, "
                "consult_messages, consult_sessions CASCADE"
            )
        )
    try:
        yield factory
    finally:
        await engine.dispose()
        await reset_session_factory()


@pytest.mark.asyncio
async def test_strong_signal_message_replies_with_clarification_without_intake_extraction(
    db_factory: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import select

    session_id = uuid.uuid4()
    gateway = _ClarifyTestGateway()
    _install_fake_runtime(monkeypatch, gateway)
    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    async with db_factory() as db:
        response = await _TestIntakeRunner(db).submit_message(
            str(session_id),
            langgraph_intake_module.MessageCreateRequest(role="doctor", content="什么是头身感受？"),
            doctor_id="doctor-a",
            trace_id="clarify-e2e-strong",
            x_state_version=1,
        )

    assert response.agent_message is not None
    assert response.agent_message.agent_name == "clarification"
    assert "头身感受" in response.agent_message.content
    # 强信号短路：不得调用 intake_extraction 抽取模型
    assert gateway.clarify_calls == 1
    assert sum(1 for item in gateway.calls if item["output_schema"] is IntakeExtractionOutput) == 0

    async with db_factory() as db:
        claim = await db.scalar(
            select(IntakeCommandClaim).where(IntakeCommandClaim.session_id == session_id)
        )
    assert claim is not None
    assert claim.status == "completed"
    assert "clarify_precheck" in claim.intermediate_payload["steps"]
