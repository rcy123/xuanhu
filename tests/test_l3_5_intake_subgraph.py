from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import func, select, text
from sqlalchemy import null as sql_null
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.services.langgraph_intake as langgraph_intake_module
from app.agent_runtime.commands import NODE_INTAKE_SUBGRAPH_V1, XuanhuCommand
from app.agent_runtime.config import DEFAULT_GRAPH_VERSION, make_run_config
from app.agent_runtime.graph import build_main_graph
from app.agent_runtime.runner import GraphRunner
from app.agent_runtime.runtime import AgentRuntime
from app.agent_runtime.state import XuanhuGraphState, default_state, validate_state_json_safe
from app.api.advance import _run_langgraph_advance as _production_run_langgraph_advance
from app.core.config import get_settings
from app.core.exceptions import InsufficientInquiryError, ModelGatewayUnavailableError
from app.core.exceptions import ValidationError as XuanhuValidationError
from app.db.session import _build_async_pg_url
from app.main import app
from app.models.consult import ConsultMessage, ConsultSession
from app.models.domain import (
    DomainCommandCommit,
    GateResult,
    GraphRun,
    IntakeCommandClaim,
    Observation,
    OutboxEvent,
    SafetyFactAssertion,
    SafetyProfile,
)
from app.models.http_command import HttpCommandClaim
from app.schemas.completeness import (
    COMPLETENESS_GATE_NAME,
    COMPLETENESS_POLICY_VERSION,
    ComplaintCategory,
    InquiryDimension,
)
from app.schemas.domain import CollectionStatus
from app.schemas.intake import (
    CandidateSeverity,
    ComplaintClassificationOutput,
    EvidenceSpan,
    IntakeExtractionDecision,
    IntakeExtractionOutput,
    LactationDelta,
    ObservationDelta,
    PatientSafetyDelta,
    PregnancyDelta,
    RedFlagCandidate,
    RedFlagCategory,
    SafetyListDelta,
)
from app.schemas.message import MessageCreateRequest, MessageCreateResponse
from app.schemas.question import QuestionComposerModelOutput
from app.schemas.triage import TRIAGE_GATE_NAME, TRIAGE_POLICY_VERSION
from app.services.langgraph_intake import (
    LangGraphIntakeMessageRunner as _ProductionLangGraphIntakeMessageRunner,
)
from app.services.langgraph_intake import _payload_digest
from tests._database_safety import destructive_database_environment

pytestmark = pytest.mark.integration


class LangGraphIntakeMessageRunner(_ProductionLangGraphIntakeMessageRunner):
    """Test subclass explicitly opting direct calls into request-local runtime."""

    def __init__(self, db: AsyncSession) -> None:
        super().__init__(db, allow_request_local_runtime=True)


async def _run_langgraph_advance(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Explicitly opt direct integration calls into request-local runtime."""

    kwargs["allow_request_local_runtime"] = True
    return await _production_run_langgraph_advance(*args, **kwargs)


def _state() -> XuanhuGraphState:
    session_id = str(uuid.uuid4())
    return default_state(
        session_id=session_id,
        command=XuanhuCommand.MESSAGE.value,
        command_id="cmd-intake-test",
        graph_version=DEFAULT_GRAPH_VERSION,
        run_id=str(uuid.uuid4()),
    )


# composer 模型 fake：每个维度一张「单问句」题文，末句必含该维度规范关键词
# （_question_targets_dimension 只检查最后一个分句），且恰好一个问号、以问号结尾、
# 不含二次问句/身份/权威/机密标记（validate_single_question_text 全部通过）。
# 逐个维度定制而不是静态复读，是因为 composer 2.8 在题文未命中 selected_dimension
# 或与最近问句重复时会重试一次（question_model_calls +1），跨轮次维度滚动后静态
# 问句必触发重试。
_QUESTION_TEXT_BY_DIMENSION: dict[str, str] = {
    InquiryDimension.CHIEF_COMPLAINT_SYMPTOM.value: "您主要有什么不舒服的症状？",
    InquiryDimension.CHIEF_COMPLAINT_CATEGORY.value: "请问您的主诉属于哪种类别？",
    InquiryDimension.BASIC_COURSE.value: "这个症状持续多久了？",
    InquiryDimension.PRESENT_ILLNESS_CHANGE.value: "病情有没有变化或者加重？",
    InquiryDimension.TEN_COLD_HEAT.value: "您有没有发热或者怕冷的情况？",
    InquiryDimension.TEN_SWEAT.value: "您最近有出汗多的情况吗？",
    InquiryDimension.TEN_HEAD_BODY.value: "您有没有头痛或者头晕的情况？",
    InquiryDimension.TEN_STOOL_URINE.value: "您的大便小便情况怎么样？",
    InquiryDimension.TEN_DIET.value: "您的食欲和饮食情况怎么样？",
    InquiryDimension.TEN_CHEST_ABDOMEN.value: "您有没有胸闷或者恶心的感觉？",
    InquiryDimension.TEN_THIRST.value: "您有口渴的情况吗？",
    InquiryDimension.TEN_SLEEP.value: "您的睡眠情况怎么样？",
    InquiryDimension.TEN_MENSES_LEUKORRHEA.value: "您的月经情况怎么样？",
    InquiryDimension.TEN_PAIN.value: "您有没有疼痛的感觉？",
    InquiryDimension.TEN_RESPIRATORY.value: "您有没有咳嗽或者咳痰的情况？",
    InquiryDimension.ALLERGY_STATUS.value: "您有没有药物或者食物过敏的情况？",
    InquiryDimension.MEDICATION_STATUS.value: "您最近有没有在服用什么药物？",
    InquiryDimension.MAJOR_CONDITION_STATUS.value: "您有没有既往的慢性疾病史？",
    InquiryDimension.PREGNANCY_STATUS.value: "您目前是否处于妊娠状态？",
    InquiryDimension.LACTATION_STATUS.value: "您目前是否在哺乳？",
    InquiryDimension.PAST_HISTORY.value: "您有没有既往病史？",
    InquiryDimension.FOUR_DIAGNOSIS.value: "您能描述一下舌苔和脉象情况吗？",
    InquiryDimension.PATIENT_SEX.value: "请问您的性别是男还是女？",
    InquiryDimension.PATIENT_AGE.value: "请问您的年龄多大了？",
    InquiryDimension.MENOPAUSE_STATUS.value: "您是否已经绝经？",
    InquiryDimension.PREGNANCY_APPLICABILITY_FLAG.value: "您目前是否可能怀孕？",
    InquiryDimension.LACTATION_APPLICABILITY_FLAG.value: "您目前是否在哺乳期？",
}


def _composer_selected_dimension(messages: list[dict[str, Any]]) -> str | None:
    """从 composer 的 user 层 JSON 里读出本轮 selected_dimension。

    build_question_context 的 user 层是 ``json.dumps({"selected_dimension": ...})``，
    作为最后一层消息传给网关。跨所有消息扫一遍，避免依赖消息顺序。
    """
    for message in messages:
        content = message.get("content")
        if not isinstance(content, str) or "selected_dimension" not in content:
            continue
        try:
            payload = json.loads(content)
        except (ValueError, TypeError):
            continue
        value = payload.get("selected_dimension")
        if isinstance(value, str):
            return value
    return None


class _E2EFakeGateway:
    def __init__(self, mode: str, *, delay: float = 0) -> None:
        self.mode = mode
        self.delay = delay
        self.calls: list[dict[str, Any]] = []

    async def chat_structured(
        self,
        messages: list[dict[str, Any]],
        output_schema: type[BaseModel],
        **kwargs: Any,
    ) -> Any:
        self.calls.append({"agent_name": kwargs.get("agent_name"), "output_schema": output_schema})
        # Delay AFTER the append so only the request that actually executes the
        # node is counted. Keeps a claim "running" long enough for a concurrent
        # same-key replay to observe the in-flight claim and wait on it, instead
        # of racing to completion — widens the race exercised by the concurrency test.
        if self.delay:
            await asyncio.sleep(self.delay)
        if output_schema is IntakeExtractionOutput:
            source_id, content = _source_message(messages)
            return _intake_output(self.mode, source_id, content)
        if output_schema is QuestionComposerModelOutput:
            # The 2.8 _model_result retry fires when the model question misses
            # _question_targets_dimension for the selected dimension or repeats a
            # recent question (SINGLE_QUESTION_INVALID → one extra question call).
            # A static fake question only ever targets one dimension, so once the
            # intake progresses to a new dimension the composer falls into its
            # retry and question_model_calls balloons past the per-round count.
            # Emulate a bounded model: read the selected dimension from the
            # user-layer context JSON and emit a single matching question.
            dimension = _composer_selected_dimension(messages)
            question = _QUESTION_TEXT_BY_DIMENSION.get(dimension)
            if question is None:
                raise AssertionError(f"fake composer: unknown selected_dimension {dimension!r}")
            # R1 跨轮次：gap selector 可能跨轮重选同一未答维度，此时静态题文会被
            # _question_repeats_recent 判重 → composer 2.8 重试一次（question_model_calls
            # +1）→ 多轮测试的 per-round 计数失准。给每次 composer 调用的题文追加一个
            # 轮次序号（不改变维度关键词、保持单问号且以问号结尾），使重复维度也能产出
            # 非重复题文，杜绝重试。
            self._composer_seq = getattr(self, "_composer_seq", 0) + 1
            if self._composer_seq > 1:
                question = question[:-1] + f"（第{self._composer_seq}次）？"
            return QuestionComposerModelOutput(question=question)
        if output_schema is ComplaintClassificationOutput:
            # 1a 主诉大类归集 fake：默认归 respiratory，evidence 引用 chief_complaint_text 首段。
            # complaint_classifier 的消息最后一层是 user 层，content 即主诉纯文本
            # （system/developer/context/user 四层，非 JSON payload）。
            chief_text = str(messages[-1].get("content") or "")
            quote = chief_text[: min(4, len(chief_text))] if chief_text else ""
            return ComplaintClassificationOutput(
                category=ComplaintCategory.RESPIRATORY,
                evidence=EvidenceSpan(
                    source_message_id=uuid.uuid4(),
                    start_char=0,
                    end_char=len(quote),
                    quote=quote,
                ),
                confidence=0.9,
            )
        raise AssertionError(f"unexpected output schema: {output_schema}")

    @property
    def intake_calls(self) -> int:
        return sum(1 for item in self.calls if item["output_schema"] is IntakeExtractionOutput)

    @property
    def question_model_calls(self) -> int:
        return sum(1 for item in self.calls if item["output_schema"] is QuestionComposerModelOutput)

    @property
    def classify_calls(self) -> int:
        return sum(1 for item in self.calls if item["output_schema"] is ComplaintClassificationOutput)


class _UnavailableOnceGateway(_E2EFakeGateway):
    def __init__(self) -> None:
        super().__init__("incomplete")
        self._failed = False

    async def chat_structured(
        self,
        messages: list[dict[str, Any]],
        output_schema: type[BaseModel],
        **kwargs: Any,
    ) -> Any:
        if output_schema is IntakeExtractionOutput and not self._failed:
            self._failed = True
            self.calls.append({"agent_name": kwargs.get("agent_name"), "output_schema": output_schema})
            raise ModelGatewayUnavailableError()
        return await super().chat_structured(messages, output_schema, **kwargs)


def _source_message(messages: list[dict[str, Any]]) -> tuple[uuid.UUID, str]:
    raw = messages[-1]["content"]
    payload = json.loads(raw)
    return uuid.UUID(payload[0]["message_id"]), str(payload[0]["content"])


def _evidence_span(source: uuid.UUID, text: str, quote: str) -> EvidenceSpan:
    start = text.index(quote)
    return EvidenceSpan(
        source_message_id=source,
        start_char=start,
        end_char=start + len(quote),
        quote=quote,
    )


def _observation(source: uuid.UUID, fact_key: str, value: str) -> ObservationDelta:
    return ObservationDelta(
        fact_key=fact_key,
        value=value,
        normalized_value=value,
        source_message_id=source,
        confidence=0.95,
    )


def _safety_none(source: uuid.UUID, text: str) -> PatientSafetyDelta:
    return PatientSafetyDelta(
        allergy=SafetyListDelta(
            status=CollectionStatus.EXPLICITLY_NONE,
            source_message_id=source,
            negation_span=_evidence_span(source, text, "no drug allergies"),
        ),
        pregnancy=PregnancyDelta(
            status=CollectionStatus.EXPLICITLY_NONE,
            source_message_id=source,
            span=_evidence_span(source, text, "not pregnant"),
        ),
        lactation=LactationDelta(
            status=CollectionStatus.EXPLICITLY_NONE,
            source_message_id=source,
            span=_evidence_span(source, text, "not breastfeeding"),
        ),
        medications=SafetyListDelta(
            status=CollectionStatus.EXPLICITLY_NONE,
            source_message_id=source,
            negation_span=_evidence_span(source, text, "no current medications"),
        ),
        major_conditions=SafetyListDelta(
            status=CollectionStatus.EXPLICITLY_NONE,
            source_message_id=source,
            negation_span=_evidence_span(source, text, "no major conditions"),
        ),
    )


def _intake_output(mode: str, source: uuid.UUID, text: str) -> IntakeExtractionOutput:
    if mode in ("ready", "ready_full"):
        observations = (
            _observation(source, "chief_complaint.symptom", "headache"),
            _observation(source, "chief_complaint.course", "three_days"),
            _observation(source, "present_illness.change", "stable"),
            _observation(source, "ten_questions.cold_heat", "none"),
            _observation(source, "ten_questions.sweat", "none"),
            _observation(source, "ten_questions.head_body", "none"),
            _observation(source, "ten_questions.diet", "normal"),
            _observation(source, "ten_questions.sleep", "normal"),
            _observation(source, "ten_questions.stool_urine", "normal"),
            _observation(source, "ten_questions.chest_abdomen", "none"),
            _observation(source, "ten_questions.thirst", "none"),
            # 1a 主诉大类归集后 fake 恒定归 respiratory → 动态十问激活
            # (cold_heat, respiratory, sleep)，必须采齐 respiratory 档才维持
            # "只差 safety 确认、不再追问"的测试语义。
            _observation(source, "ten_questions.respiratory", "normal"),
        )
        if mode == "ready_full":
            # FOUR_DIAGNOSIS 是 required_by_default 维度。共享的 "ready" 模式缺此
            # 维度 → completeness 判 INCOMPLETE 并追问四诊，只能配合 recover-after-commit
            # 测试（monkeypatch 不落盘问题消息）使用。R1 ready 出口测试要触发真实
            # READY disposition，必须采齐四诊，故用 ready_full。
            observations += (_observation(source, "four_diagnosis.inspection", "red_tongue"),)
        return IntakeExtractionOutput(
            decision=IntakeExtractionDecision.EXTRACTED,
            observations=observations,
            patient_safety_delta=_safety_none(source, text),
        )
    if mode == "red_flag":
        return IntakeExtractionOutput(
            decision=IntakeExtractionDecision.EXTRACTED,
            red_flag_candidates=(
                RedFlagCandidate(
                    category=RedFlagCategory.HIGH_FEVER,
                    source_message_id=source,
                    span=_evidence_span(source, text, "39.2°C"),
                    severity=CandidateSeverity.HIGH,
                    evidence="high fever",
                    confidence=0.96,
                ),
            ),
        )
    if mode == "privacy":
        return IntakeExtractionOutput(
            decision=IntakeExtractionDecision.EXTRACTED,
            observations=(_observation(source, "chief_complaint.symptom", "privacy_fact_value_778899"),),
        )
    # incomplete 模式：跨轮次 interrupt/resume 时，模型输出必须只含"本轮新事实"。
    # 若把首轮已落库的 chief_complaint.symptom=headache 再次以 ADD 返回，intake_verifier
    # 会判 INTAKE_HISTORICAL_FACT_REEXTRACTED → _execute_intake_extraction_with_retry 重试一次
    # （intake 调用 +1）→ 仍失败后降级 ABSTAINED → 误入澄清而非继续追问。
    # 故按消息内容增量抽取：第 2 轮 "三天了" → course；第 3 轮 "没有发烧" → cold_heat=none；
    # 其余（首轮 symptom）走默认。每轮恰好 1 次 intake 调用，维持 intake_calls==N 断言。
    if "三天" in text:
        return IntakeExtractionOutput(
            decision=IntakeExtractionDecision.EXTRACTED,
            observations=(_observation(source, "chief_complaint.course", "three_days"),),
        )
    if "发烧" in text or "发热" in text or "fever" in text.casefold():
        return IntakeExtractionOutput(
            decision=IntakeExtractionDecision.EXTRACTED,
            observations=(_observation(source, "ten_questions.cold_heat", "none"),),
        )
    return IntakeExtractionOutput(
        decision=IntakeExtractionDecision.EXTRACTED,
        observations=(_observation(source, "chief_complaint.symptom", "headache"),),
    )


def _install_fake_runtime(monkeypatch: pytest.MonkeyPatch, gateway: _E2EFakeGateway) -> None:
    monkeypatch.setattr(
        langgraph_intake_module,
        "AgentRuntime",
        lambda: AgentRuntime(gateway, recorder=None),
    )


def _install_fake_advance_graph(
    monkeypatch: pytest.MonkeyPatch,
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def fake_invoke_reasoning_graph(
        *,
        session_id: str,
        command_key: str,
        run_id: uuid.UUID,
        command: XuanhuCommand,
    ) -> None:
        assert command is XuanhuCommand.ADVANCE
        sid = uuid.UUID(session_id)
        async with db_factory() as db, db.begin():
            claim = await db.scalar(
                select(IntakeCommandClaim)
                .where(
                    IntakeCommandClaim.session_id == sid,
                    IntakeCommandClaim.idempotency_key == command_key,
                )
                .with_for_update()
            )
            session = await db.get(ConsultSession, sid, with_for_update=True)
            graph_run = await db.get(GraphRun, run_id, with_for_update=True)
            assert claim is not None and session is not None and graph_run is not None
            advance = claim.intermediate_payload.get("advance", {}) if claim.intermediate_payload else {}
            response_payload = {
                "session_id": session_id,
                "current_stage": session.current_stage,
                "from_stage": advance.get("from_stage", "inquiry"),
                "state_version": session.state_version,
                "blocked_reason": session.blocked_reason,
                "agent_name": None,
                "trace_id": advance.get("trace_id"),
            }
            claim.status = "completed"
            claim.output_state_version = session.state_version
            claim.response_payload = response_payload
            claim.updated_at = func.now()
            graph_run.status = "completed"
            graph_run.completed_at = func.now()
            existing_completed = await db.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.session_id == sid, OutboxEvent.event_type == "advance.command_completed.v1")
            )
            if existing_completed == 0:
                db.add(
                    OutboxEvent(
                        id=uuid.uuid4(),
                        event_type="advance.command_completed.v1",
                        session_id=sid,
                        graph_run_id=run_id,
                        state_version=session.state_version,
                        trace_id="trace:advance-test",
                        payload=response_payload,
                    )
                )

    monkeypatch.setattr("app.api.advance._invoke_reasoning_graph", fake_invoke_reasoning_graph)


@pytest.fixture(scope="module")
def migrated_database() -> str:
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
async def db_factory(migrated_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    from app.db.session import reset_session_factory

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
async def test_langgraph_messages_e2e_incomplete_uses_model_question_and_one_intake_call(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    gateway = _E2EFakeGateway("incomplete")
    _install_fake_runtime(monkeypatch, gateway)
    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    async with db_factory() as db:
        response = await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            MessageCreateRequest(role="patient_proxy", content="headache"),
            doctor_id="doctor-a",
            trace_id="messages-e2e-incomplete",
            x_state_version=1,
        )

    async with db_factory() as db:
        claim = await db.scalar(select(IntakeCommandClaim).where(IntakeCommandClaim.session_id == session_id))
        outbox_count = await db.scalar(
            text("SELECT count(*) FROM outbox_events WHERE event_type = 'intake.command_completed.v1'")
        )

    assert response.agent_message is not None
    assert response.current_stage == "inquiry"
    assert gateway.intake_calls == 1
    assert gateway.question_model_calls == 1
    assert outbox_count == 1
    assert claim is not None
    assert claim.status == "completed"
    assert claim.intermediate_payload is not None
    assert set(claim.intermediate_payload["steps"]) >= {
        "persist_message",
        "triage_precheck",
        "classify_complaint",
        "build_intake_context",
        "extract_intake",
        "verify_intake",
        "reduce_observations",
        "gates_and_route",
    }
    assert claim.intermediate_payload["gates"]["route"] == "incomplete"
    # 0a 模板兜底留痕：question_composer 顶层 key 必存在，source 与 degraded 字段类型正确
    composer_trace = claim.intermediate_payload["question_composer"]
    assert composer_trace["source"] in {"template", "model"}
    assert isinstance(composer_trace["degraded"], bool)
    assert composer_trace["source_kind"] == "question_composer"
    # 1a 主诉大类归集：respiratory 已落库，呼吸维度被激活而非 general 档 4 维
    assert gateway.classify_calls == 1
    classify_trace = claim.intermediate_payload["classify_complaint"]
    assert classify_trace["category"] == "respiratory"
    assert classify_trace["source"] == "model"
    assert classify_trace["degraded"] is False
    async with db_factory() as db:
        category_obs = await db.scalar(
            text(
                "SELECT normalized_value FROM observations "
                "WHERE session_id = :sid AND fact_key = 'chief_complaint.category' "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            params={"sid": session_id},
        )
    assert category_obs == "respiratory"


@pytest.mark.asyncio
async def test_bound_bare_negative_creates_pending_safety_fact_without_repeating_allergy(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    question_id = uuid.uuid4()
    gateway = _E2EFakeGateway("incomplete")
    _install_fake_runtime(monkeypatch, gateway)
    async with db_factory() as db, db.begin():
        db.add(
            ConsultSession(
                id=session_id,
                patient_info={},
                state_version=1,
                agent_runtime="langgraph",
                state_snapshot={
                    "langgraph_intake": {
                        "last_question_message_id": str(question_id),
                        "progress": {"no_new_facts_rounds": 0, "followup_rounds": 1},
                    }
                },
            )
        )
        db.add(
            ConsultMessage(
                id=question_id,
                session_id=session_id,
                role="agent",
                stage="inquiry",
                agent_name="question_composer",
                content="为补充用药安全信息，请问您目前是否有已知过敏？",
                structured_delta={
                    "selected_dimension": "safety.allergy_status",
                    "selection_kind": "required",
                },
                trace_id="bound-negative-question",
            )
        )

    async with db_factory() as db:
        response = await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            MessageCreateRequest(
                role="patient_proxy",
                content="没有",
                reply_to_message_id=question_id,
            ),
            doctor_id="doctor-a",
            trace_id="bound-negative-answer",
            x_state_version=1,
        )

    async with db_factory() as db:
        session = await db.get(ConsultSession, session_id)
        assertion = await db.scalar(
            select(SafetyFactAssertion).where(SafetyFactAssertion.session_id == session_id)
        )
        patient_message = await db.scalar(
            select(ConsultMessage).where(
                ConsultMessage.session_id == session_id,
                ConsultMessage.role == "patient_proxy",
            )
        )
        latest_question = await db.scalar(
            select(ConsultMessage)
            .where(
                ConsultMessage.session_id == session_id,
                ConsultMessage.agent_name == "question_composer",
                ConsultMessage.id != question_id,
            )
            .order_by(ConsultMessage.created_at.desc())
        )

    assert gateway.intake_calls == 0
    assert session is not None
    assert session.status == "active"
    assert session.state_snapshot["langgraph_intake"]["progress"]["no_new_facts_rounds"] == 0
    assert assertion is not None
    assert assertion.field_name == "allergy"
    assert assertion.status == "proposed"
    assert assertion.source_kind == "deterministic_reply_binding"
    assert patient_message is not None
    assert patient_message.structured_delta["reply_context"] == {
        "question_message_id": str(question_id),
        "selected_dimension": "safety.allergy_status",
        "selection_kind": "required",
    }
    assert response.agent_message is not None
    assert latest_question is not None
    assert latest_question.structured_delta["selected_dimension"] != "safety.allergy_status"


# R2-C: for "not sure" to keep asking allergy, every lower-priority required
# dimension must already be covered (gap selector picks the lowest missing).
# These seed the exact canonical facts for the RESPIRATORY dynamic ten-question
# set plus chief complaint, course, present-illness change and four diagnosis.
_R2C_SEEDED_FACT_KEYS: tuple[tuple[str, str], ...] = (
    ("chief_complaint.symptom", "headache"),
    ("chief_complaint.course", "three_days"),
    ("present_illness.change", "stable"),
    ("ten_questions.cold_heat", "none"),
    ("ten_questions.stool_urine", "normal"),
    ("ten_questions.diet", "normal"),
    ("ten_questions.sleep", "normal"),
    ("ten_questions.sweat", "none"),
    ("ten_questions.head_body", "none"),
    ("ten_questions.chest_abdomen", "none"),
    ("ten_questions.thirst", "none"),
    ("ten_questions.respiratory", "normal"),
    ("four_diagnosis.inspection", "red_tongue"),
)


@pytest.mark.asyncio
async def test_r2c_ambiguous_negation_keeps_allergy_required_then_explicit_none_slot_parity(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2-C reply-bound flow across the gray-scale intake slot path.

    "not sure" is an ambiguous negation bound to the current required allergy
    question: no model call, no fabricated fact, and the same question is asked
    again. An exact replay is idempotent. The later exact "no" deterministically
    records explicitly_none. Slot on/off must produce identical semantics.
    """

    async def run_case(slot_enabled: bool) -> dict[str, Any]:
        monkeypatch.setattr(get_settings(), "intake_slot_path_enabled", slot_enabled)
        session_id = uuid.uuid4()
        seed_question_id = uuid.uuid4()
        gateway = _E2EFakeGateway("incomplete")
        _install_fake_runtime(monkeypatch, gateway)
        async with db_factory() as db, db.begin():
            db.add(
                ConsultSession(
                    id=session_id,
                    patient_info={},
                    state_version=1,
                    agent_runtime="langgraph",
                    state_snapshot={
                        "langgraph_intake": {
                            "last_question_message_id": str(seed_question_id),
                            "progress": {"no_new_facts_rounds": 0, "followup_rounds": 1},
                        }
                    },
                )
            )
            db.add(
                ConsultMessage(
                    id=seed_question_id,
                    session_id=session_id,
                    role="agent",
                    stage="inquiry",
                    agent_name="question_composer",
                    content="您有没有药物或者食物过敏的情况？",
                    structured_delta={
                        "selected_dimension": "safety.allergy_status",
                        "selection_kind": "required",
                    },
                    trace_id=f"r2c-seed-{slot_enabled}",
                )
            )
            db.add_all(
                Observation(
                    id=uuid.uuid4(),
                    session_id=session_id,
                    fact_key=fact_key,
                    value=value,
                    normalized_value=value,
                    source_message_id=seed_question_id,
                    status="active",
                    confidence=0.95,
                )
                for fact_key, value in _R2C_SEEDED_FACT_KEYS
            )

        # Step 3: "not sure" bound to the seed allergy question.
        async with db_factory() as db:
            not_sure_response = await LangGraphIntakeMessageRunner(db).submit_message(
                str(session_id),
                MessageCreateRequest(
                    role="patient_proxy",
                    content="not sure",
                    reply_to_message_id=seed_question_id,
                ),
                doctor_id="doctor-a",
                trace_id=f"r2c-not-sure-{slot_enabled}",
                x_state_version=1,
                idempotency_key=f"r2c-not-sure-{slot_enabled}",
            )

        # Step 5: replay the exact same body/key/version.
        async with db_factory() as db:
            replay_response = await LangGraphIntakeMessageRunner(db).submit_message(
                str(session_id),
                MessageCreateRequest(
                    role="patient_proxy",
                    content="not sure",
                    reply_to_message_id=seed_question_id,
                ),
                doctor_id="doctor-a",
                trace_id=f"r2c-not-sure-{slot_enabled}",
                x_state_version=1,
                idempotency_key=f"r2c-not-sure-{slot_enabled}",
            )

        async with db_factory() as db:
            session = await db.get(ConsultSession, session_id)
            profile_before_no = await db.scalar(
                select(SafetyProfile).where(SafetyProfile.session_id == session_id)
            )
            assertions_before_no = list(
                await db.scalars(
                    select(SafetyFactAssertion).where(SafetyFactAssertion.session_id == session_id)
                )
            )
            patient_message_count = await db.scalar(
                select(func.count())
                .select_from(ConsultMessage)
                .where(
                    ConsultMessage.session_id == session_id,
                    ConsultMessage.role == "patient_proxy",
                )
            )
            claim_count = await db.scalar(
                select(func.count())
                .select_from(IntakeCommandClaim)
                .where(IntakeCommandClaim.session_id == session_id)
            )
            completion_outbox = await db.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.session_id == session_id,
                    OutboxEvent.event_type == "intake.command_completed.v1",
                )
            )
            allergy_question = await db.scalar(
                select(ConsultMessage)
                .where(
                    ConsultMessage.session_id == session_id,
                    ConsultMessage.agent_name == "question_composer",
                    ConsultMessage.id != seed_question_id,
                )
                .order_by(ConsultMessage.created_at.desc())
            )
            state_version_after_replay = session.state_version if session is not None else None
        allergy_question_id = allergy_question.id if allergy_question is not None else None

        # Step 6: exact "no" bound to the newly persisted allergy question.
        async with db_factory() as db:
            no_response = await LangGraphIntakeMessageRunner(db).submit_message(
                str(session_id),
                MessageCreateRequest(
                    role="patient_proxy",
                    content="no",
                    reply_to_message_id=str(allergy_question_id),
                ),
                doctor_id="doctor-a",
                trace_id=f"r2c-no-{slot_enabled}",
                x_state_version=state_version_after_replay,
                idempotency_key=f"r2c-no-{slot_enabled}",
            )

        async with db_factory() as db:
            assertions = list(
                await db.scalars(
                    select(SafetyFactAssertion).where(SafetyFactAssertion.session_id == session_id)
                )
            )
            profile = await db.scalar(select(SafetyProfile).where(SafetyProfile.session_id == session_id))
            next_question = await db.scalar(
                select(ConsultMessage)
                .where(
                    ConsultMessage.session_id == session_id,
                    ConsultMessage.agent_name == "question_composer",
                    ConsultMessage.id.notin_([seed_question_id, allergy_question_id]),
                )
                .order_by(ConsultMessage.created_at.desc())
            )
            claims = list(
                await db.scalars(
                    select(IntakeCommandClaim).where(IntakeCommandClaim.session_id == session_id)
                )
            )
            completion_outbox_total = await db.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.session_id == session_id,
                    OutboxEvent.event_type == "intake.command_completed.v1",
                )
            )
            claim_payload = await db.scalar(
                text(
                    "SELECT coalesce(string_agg(intermediate_payload::text, ' '), '') "
                    "FROM intake_command_claims WHERE session_id = :sid"
                ),
                {"sid": session_id},
            )
            outbox_payload = await db.scalar(
                text(
                    "SELECT coalesce(string_agg(payload::text, ' '), '') "
                    "FROM outbox_events WHERE session_id = :sid"
                ),
                {"sid": session_id},
            )
            checkpoint_payload = await db.scalar(
                text(
                    """
                    SELECT concat_ws(
                        ' ',
                        (SELECT coalesce(string_agg(to_jsonb(c)::text, ' '), '')
                         FROM checkpoints c WHERE thread_id LIKE :needle),
                        (SELECT coalesce(string_agg(to_jsonb(w)::text, ' '), '')
                         FROM checkpoint_writes w WHERE thread_id LIKE :needle),
                        (SELECT coalesce(string_agg(to_jsonb(b)::text, ' '), '')
                         FROM checkpoint_blobs b WHERE thread_id LIKE :needle)
                    )
                    """
                ),
                {"needle": f"%{session_id}%"},
            )
            session_final = await db.get(ConsultSession, session_id)

        return {
            "slot_enabled": slot_enabled,
            "gateway": gateway,
            "not_sure_response": not_sure_response,
            "replay_response": replay_response,
            "no_response": no_response,
            "profile_before_no": profile_before_no,
            "assertions_before_no": assertions_before_no,
            "allergy_question_delta": (
                allergy_question.structured_delta if allergy_question is not None else None
            ),
            "state_version_after_replay": state_version_after_replay,
            "patient_message_count": patient_message_count,
            "claim_count": claim_count,
            "completion_outbox": completion_outbox,
            "assertions": assertions,
            "profile": profile,
            "next_question_delta": next_question.structured_delta if next_question is not None else None,
            "claim_pattern": tuple(
                sorted(
                    (claim.status, claim.input_state_version, claim.output_state_version)
                    for claim in claims
                )
            ),
            "completion_outbox_total": completion_outbox_total,
            "raw_metadata": f"{claim_payload} {outbox_payload} {checkpoint_payload}",
            "final_state_version": session_final.state_version if session_final is not None else None,
        }

    slot_results: dict[bool, dict[str, Any]] = {}
    for slot_enabled in (False, True):
        slot_results[slot_enabled] = await run_case(slot_enabled)

    for _slot_enabled, result in slot_results.items():
        # Step 4: "not sure" fabricates nothing and re-asks the same required dimension.
        assert result["gateway"].intake_calls == 0
        assert result["profile_before_no"] is None
        assert result["assertions_before_no"] == []
        assert result["not_sure_response"].agent_message is not None
        assert result["allergy_question_delta"] is not None
        assert result["allergy_question_delta"]["selected_dimension"] == "safety.allergy_status"
        assert result["allergy_question_delta"]["selection_kind"] == "required"

        # Step 5: exact replay is stable and idempotent.
        assert result["replay_response"].agent_message is not None
        assert (
            result["replay_response"].agent_message.message_id
            == result["not_sure_response"].agent_message.message_id
        )
        assert result["patient_message_count"] == 1
        assert result["claim_count"] == 1
        assert result["completion_outbox"] == 1
        assert result["state_version_after_replay"] == 3

        # Step 7: exact "no" deterministically records explicitly_none.
        assert result["gateway"].intake_calls == 0
        assert len(result["assertions"]) == 1
        assertion = result["assertions"][0]
        assert assertion.field_name == "allergy"
        assert assertion.source_kind == "deterministic_reply_binding"
        assert assertion.status == "proposed"
        assert result["profile"] is not None
        assert result["profile"].allergy_collection_status == "explicitly_none"
        assert result["profile"].allergens is None
        assert result["next_question_delta"] is not None
        assert result["next_question_delta"]["selected_dimension"] != "safety.allergy_status"
        assert result["no_response"].agent_message is not None
        assert result["final_state_version"] == 5

        # Step 8: raw reply text never lands in claim/checkpoint metadata.
        assert "not sure" not in result["raw_metadata"]
        assert '"no"' not in result["raw_metadata"]

    # Step 9: slot off/on produce identical semantics.
    off = slot_results[False]
    on = slot_results[True]
    assert (off["profile"].allergy_collection_status, off["profile"].allergens) == (
        on["profile"].allergy_collection_status,
        on["profile"].allergens,
    )
    assert (
        off["assertions"][0].field_name,
        off["assertions"][0].source_kind,
        off["assertions"][0].status,
    ) == (
        on["assertions"][0].field_name,
        on["assertions"][0].source_kind,
        on["assertions"][0].status,
    )
    assert off["claim_pattern"] == on["claim_pattern"]
    assert off["completion_outbox_total"] == on["completion_outbox_total"]


@pytest.mark.asyncio
async def test_explicit_stale_reply_question_is_rejected_before_patient_message_or_claim(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    stale_question_id = uuid.uuid4()
    current_question_id = uuid.uuid4()
    gateway = _E2EFakeGateway("incomplete")
    _install_fake_runtime(monkeypatch, gateway)
    async with db_factory() as db, db.begin():
        db.add(
            ConsultSession(
                id=session_id,
                patient_info={},
                state_version=1,
                agent_runtime="langgraph",
                state_snapshot={
                    "langgraph_intake": {
                        "last_question_message_id": str(current_question_id),
                        "progress": {"no_new_facts_rounds": 0, "followup_rounds": 2},
                    }
                },
            )
        )
        db.add_all(
            [
                ConsultMessage(
                    id=stale_question_id,
                    session_id=session_id,
                    role="agent",
                    stage="inquiry",
                    agent_name="question_composer",
                    content="Do you have any known allergies?",
                    structured_delta={
                        "selected_dimension": "safety.allergy_status",
                        "selection_kind": "required",
                    },
                    trace_id="stale-reply-question",
                ),
                ConsultMessage(
                    id=current_question_id,
                    session_id=session_id,
                    role="agent",
                    stage="inquiry",
                    agent_name="question_composer",
                    content="Are you currently taking any medications?",
                    structured_delta={
                        "selected_dimension": "safety.medication_status",
                        "selection_kind": "required",
                    },
                    trace_id="current-reply-question",
                ),
            ]
        )

    async with db_factory() as db:
        with pytest.raises(XuanhuValidationError) as captured:
            await LangGraphIntakeMessageRunner(db).submit_message(
                str(session_id),
                MessageCreateRequest(
                    role="patient_proxy",
                    content="no",
                    reply_to_message_id=stale_question_id,
                ),
                doctor_id="doctor-a",
                trace_id="stale-reply-answer",
                x_state_version=1,
                idempotency_key="stale-reply-answer",
            )
        assert "current intake question" in (captured.value.detail or "")

    async with db_factory() as db:
        patient_message_count = await db.scalar(
            select(func.count())
            .select_from(ConsultMessage)
            .where(ConsultMessage.session_id == session_id, ConsultMessage.role == "patient_proxy")
        )
        claim_count = await db.scalar(
            select(func.count())
            .select_from(IntakeCommandClaim)
            .where(IntakeCommandClaim.session_id == session_id)
        )
        session = await db.get(ConsultSession, session_id)

    assert gateway.intake_calls == 0
    assert patient_message_count == 0
    assert claim_count == 0
    assert session is not None
    assert session.state_version == 1


@pytest.mark.asyncio
async def test_explicit_cross_session_reply_question_is_rejected_before_patient_message_or_claim(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    foreign_session_id = uuid.uuid4()
    foreign_question_id = uuid.uuid4()
    gateway = _E2EFakeGateway("incomplete")
    _install_fake_runtime(monkeypatch, gateway)
    async with db_factory() as db, db.begin():
        db.add_all(
            [
                ConsultSession(
                    id=session_id,
                    patient_info={},
                    state_version=1,
                    agent_runtime="langgraph",
                    # Exercise the ownership check even if a corrupt/stale
                    # snapshot points at another session's otherwise valid question.
                    state_snapshot={
                        "langgraph_intake": {
                            "last_question_message_id": str(foreign_question_id),
                            "progress": {"no_new_facts_rounds": 0, "followup_rounds": 1},
                        }
                    },
                ),
                ConsultSession(
                    id=foreign_session_id,
                    patient_info={},
                    state_version=1,
                    agent_runtime="langgraph",
                ),
                ConsultMessage(
                    id=foreign_question_id,
                    session_id=foreign_session_id,
                    role="agent",
                    stage="inquiry",
                    agent_name="question_composer",
                    content="Do you have any known allergies?",
                    structured_delta={
                        "selected_dimension": "safety.allergy_status",
                        "selection_kind": "required",
                    },
                    trace_id="foreign-reply-question",
                ),
            ]
        )

    async with db_factory() as db:
        with pytest.raises(XuanhuValidationError) as captured:
            await LangGraphIntakeMessageRunner(db).submit_message(
                str(session_id),
                MessageCreateRequest(
                    role="patient_proxy",
                    content="no",
                    reply_to_message_id=foreign_question_id,
                ),
                doctor_id="doctor-a",
                trace_id="cross-session-reply-answer",
                x_state_version=1,
                idempotency_key="cross-session-reply-answer",
            )
        assert "not a valid intake question" in (captured.value.detail or "")

    async with db_factory() as db:
        patient_message_count = await db.scalar(
            select(func.count())
            .select_from(ConsultMessage)
            .where(ConsultMessage.session_id == session_id, ConsultMessage.role == "patient_proxy")
        )
        claim_count = await db.scalar(
            select(func.count())
            .select_from(IntakeCommandClaim)
            .where(IntakeCommandClaim.session_id == session_id)
        )
        session = await db.get(ConsultSession, session_id)

    assert gateway.intake_calls == 0
    assert patient_message_count == 0
    assert claim_count == 0
    assert session is not None
    assert session.state_version == 1


@pytest.mark.asyncio
async def test_malformed_reply_question_uuid_is_rejected_by_schema_and_api_without_writes(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = uuid.uuid4()
    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    malformed_body = {
        "role": "patient_proxy",
        "content": "no",
        "reply_to_message_id": "definitely-not-a-uuid",
    }
    with pytest.raises(PydanticValidationError):
        MessageCreateRequest.model_validate(malformed_body)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            f"/api/v1/consult/sessions/{session_id}/messages",
            json=malformed_body,
            headers={
                "X-Doctor-Id": "doctor-a",
                "X-State-Version": "1",
                "X-Request-Id": "malformed-reply-question",
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    async with db_factory() as db:
        patient_message_count = await db.scalar(
            select(func.count())
            .select_from(ConsultMessage)
            .where(ConsultMessage.session_id == session_id, ConsultMessage.role == "patient_proxy")
        )
        claim_count = await db.scalar(
            select(func.count())
            .select_from(IntakeCommandClaim)
            .where(IntakeCommandClaim.session_id == session_id)
        )
    assert patient_message_count == 0
    assert claim_count == 0


@pytest.mark.asyncio
async def test_legacy_client_without_reply_id_binds_only_current_canonical_question(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    stale_question_id = uuid.uuid4()
    current_question_id = uuid.uuid4()
    gateway = _E2EFakeGateway("incomplete")
    _install_fake_runtime(monkeypatch, gateway)
    async with db_factory() as db, db.begin():
        db.add(
            ConsultSession(
                id=session_id,
                patient_info={},
                state_version=1,
                agent_runtime="langgraph",
                state_snapshot={
                    "langgraph_intake": {
                        "last_question_message_id": str(current_question_id),
                        "progress": {"no_new_facts_rounds": 0, "followup_rounds": 2},
                    }
                },
            )
        )
        db.add_all(
            [
                ConsultMessage(
                    id=stale_question_id,
                    session_id=session_id,
                    role="agent",
                    stage="inquiry",
                    agent_name="question_composer",
                    content="Are you currently taking any medications?",
                    structured_delta={
                        "selected_dimension": "safety.medication_status",
                        "selection_kind": "required",
                    },
                    trace_id="legacy-stale-question",
                ),
                ConsultMessage(
                    id=current_question_id,
                    session_id=session_id,
                    role="agent",
                    stage="inquiry",
                    agent_name="question_composer",
                    content="Do you have any known allergies?",
                    structured_delta={
                        "selected_dimension": "safety.allergy_status",
                        "selection_kind": "required",
                    },
                    trace_id="legacy-current-question",
                ),
            ]
        )

    async with db_factory() as db:
        await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            # Deliberately omit reply_to_message_id to emulate a legacy client.
            MessageCreateRequest(role="patient_proxy", content="no"),
            doctor_id="doctor-a",
            trace_id="legacy-bound-answer",
            x_state_version=1,
            idempotency_key="legacy-bound-answer",
        )

    async with db_factory() as db:
        patient_message = await db.scalar(
            select(ConsultMessage).where(
                ConsultMessage.session_id == session_id,
                ConsultMessage.role == "patient_proxy",
            )
        )
        assertions = (
            await db.scalars(
                select(SafetyFactAssertion).where(SafetyFactAssertion.session_id == session_id)
            )
        ).all()
        claim = await db.scalar(
            select(IntakeCommandClaim).where(IntakeCommandClaim.session_id == session_id)
        )

    assert gateway.intake_calls == 0
    assert patient_message is not None
    assert patient_message.structured_delta is not None
    assert patient_message.structured_delta["reply_context"] == {
        "question_message_id": str(current_question_id),
        "selected_dimension": "safety.allergy_status",
        "selection_kind": "required",
    }
    assert len(assertions) == 1
    assert assertions[0].field_name == "allergy"
    assert assertions[0].source_kind == "deterministic_reply_binding"
    assert claim is not None
    assert claim.patient_message_id == patient_message.id


@pytest.mark.asyncio
async def test_langgraph_messages_e2e_raw_text_red_flag_blocks_when_model_would_miss(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    gateway = _E2EFakeGateway("incomplete")
    _install_fake_runtime(monkeypatch, gateway)
    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    async with db_factory() as db:
        response = await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            MessageCreateRequest(role="patient_proxy", content="突然胸痛并且呼吸困难"),
            doctor_id=None,
            trace_id="messages-e2e-deterministic-red-flag",
            x_state_version=1,
        )

    async with db_factory() as db:
        claim = await db.scalar(select(IntakeCommandClaim).where(IntakeCommandClaim.session_id == session_id))
        session = await db.get(ConsultSession, session_id)

    assert response.agent_message is None
    assert response.current_stage == "blocked"
    assert session is not None
    assert session.recovery_status == "manual_required"
    assert gateway.intake_calls == 0
    assert gateway.question_model_calls == 0
    assert claim is not None
    assert claim.intermediate_payload is not None
    assert claim.intermediate_payload["triage_precheck"]["candidate_count"] == 2
    assert claim.intermediate_payload["triage_precheck"]["policy_version"] == "triage-raw-text-precheck.v1"
    assert claim.intermediate_payload["gates"]["route"] == "manual"
    assert claim.intermediate_payload["gates"]["completeness_disposition"] == "triage_blocked"


@pytest.mark.asyncio
async def test_langgraph_messages_e2e_model_red_flag_supplements_clear_precheck(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    gateway = _E2EFakeGateway("red_flag")
    _install_fake_runtime(monkeypatch, gateway)
    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    async with db_factory() as db:
        response = await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            MessageCreateRequest(role="patient_proxy", content="My temperature is 39.2°C"),
            doctor_id=None,
            trace_id="messages-e2e-model-red-flag",
            x_state_version=1,
        )

    assert response.agent_message is None
    assert response.current_stage == "blocked"
    assert gateway.intake_calls == 1
    assert gateway.question_model_calls == 0


@pytest.mark.parametrize(
    "content,expected_category",
    [
        ("突然胸痛", "severe_pain"),
        ("现在喘不过气", "breathing_difficulty"),
        ("患者意识不清，叫不醒", "altered_consciousness"),
        ("伤口大出血且血流不止", "severe_bleeding"),
        ("突然口角歪斜并且言语不清", "neurologic_deficit"),
        ("体温40.2℃", "high_fever"),
    ],
)
@pytest.mark.asyncio
async def test_messages_api_each_deterministic_red_flag_blocks_with_empty_model_candidates(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    expected_category: str,
) -> None:
    session_id = uuid.uuid4()
    gateway = _E2EFakeGateway("incomplete")
    _install_fake_runtime(monkeypatch, gateway)
    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            f"/api/v1/consult/sessions/{session_id}/messages",
            json={"role": "patient_proxy", "content": content},
            headers={
                "X-Doctor-Id": "doctor-precheck",
                "X-State-Version": "1",
                "X-Request-Id": f"precheck-api-{expected_category}",
            },
        )

    assert response.status_code == 200
    assert response.json()["data"]["current_stage"] == "blocked"
    assert response.json()["data"].get("agent_message") is None
    assert gateway.intake_calls == 0
    assert gateway.question_model_calls == 0

    async with db_factory() as db:
        session = await db.get(ConsultSession, session_id)
        gate = await db.scalar(
            select(GateResult)
            .where(GateResult.session_id == session_id, GateResult.gate_name == TRIAGE_GATE_NAME)
            .order_by(GateResult.created_at.desc())
        )
    assert session is not None
    assert session.current_stage == "blocked"
    assert session.recovery_status == "manual_required"
    assert gate is not None
    assert gate.policy_version == TRIAGE_POLICY_VERSION
    assert gate.decision == "blocked"
    assert expected_category in (gate.details or {}).get("category_counts", {})


@pytest.mark.asyncio
async def test_langgraph_messages_same_command_concurrent_replays_single_intake_call(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    gateway = _E2EFakeGateway("incomplete", delay=0.05)
    _install_fake_runtime(monkeypatch, gateway)
    body = MessageCreateRequest(role="patient_proxy", content="headache")
    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    async def submit_once(trace_id: str) -> MessageCreateResponse:
        async with db_factory() as db:
            return await LangGraphIntakeMessageRunner(db).submit_message(
                str(session_id),
                body,
                doctor_id=None,
                trace_id=trace_id,
                x_state_version=1,
                idempotency_key="messages-public-concurrent",
            )

    first, second = await asyncio.gather(
        submit_once("messages-e2e-concurrent-a"),
        submit_once("messages-e2e-concurrent-b"),
    )

    async with db_factory() as db:
        claim_count = await db.scalar(select(func.count()).select_from(IntakeCommandClaim))
        message_count = await db.scalar(
            text("SELECT count(*) FROM consult_messages WHERE session_id = :sid"), {"sid": session_id}
        )

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert gateway.intake_calls == 1
    assert claim_count == 1
    assert message_count == 2


@pytest.mark.asyncio
async def test_langgraph_messages_recovers_when_claim_completion_is_interrupted_after_domain_commit(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    gateway = _E2EFakeGateway("ready")
    _install_fake_runtime(monkeypatch, gateway)

    async def interrupted_complete_claim(
        self: LangGraphIntakeMessageRunner,
        claim_id: uuid.UUID,
        response: MessageCreateResponse,
        question_message_id: uuid.UUID | None,
        output_state_version: int,
    ) -> None:
        del self, claim_id, response, question_message_id, output_state_version
        return None

    monkeypatch.setattr(LangGraphIntakeMessageRunner, "_complete_claim", interrupted_complete_claim)
    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    async with db_factory() as db:
        response = await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            MessageCreateRequest(
                role="patient_proxy",
                content=(
                    "headache three days stable; no drug allergies; not pregnant; "
                    "not breastfeeding; no current medications; no major conditions"
                ),
            ),
            doctor_id=None,
            trace_id="messages-e2e-recover-after-commit",
            x_state_version=1,
        )

    async with db_factory() as db:
        claim = await db.scalar(select(IntakeCommandClaim).where(IntakeCommandClaim.session_id == session_id))
        commit = await db.scalar(select(DomainCommandCommit).where(DomainCommandCommit.session_id == session_id))
        session = await db.get(ConsultSession, session_id)
        agent_message_count = await db.scalar(
            text("SELECT count(*) FROM consult_messages WHERE session_id = :sid AND role = 'agent'"),
            {"sid": session_id},
        )

    # Mode "ready" omits the required four_diagnosis dimension → the graph asks a
    # four-diagnosis question and durably commits (question message + domain commit
    # + outbox) BEFORE _complete_claim runs. Monkeypatching _complete_claim to a
    # no-op simulates a crash in that window: the claim is left "running". Recovery
    # must reconstruct the exact response — the already-persisted four-diagnosis
    # question — without re-running intake (exactly-once), without re-persisting a
    # duplicate question, and must mark the claim completed.
    assert response.agent_message is not None
    assert response.agent_message.agent_name == "question_composer"
    assert response.current_stage == "inquiry"
    assert response.state_version == 3
    assert gateway.intake_calls == 1
    assert claim is not None
    assert claim.status == "completed"
    assert claim.response_payload is not None
    assert claim.response_payload == response.model_dump(mode="json")
    assert claim.question_message_id == uuid.UUID(response.agent_message.message_id)
    assert agent_message_count == 1
    assert session is not None
    assert session.state_snapshot["langgraph_intake"]["dialogue_status"] == "questioning"
    assert commit is not None


@pytest.mark.asyncio
async def test_messages_api_repairs_ambiguous_http_claim_from_completed_intake(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    gateway = _E2EFakeGateway("incomplete")
    _install_fake_runtime(monkeypatch, gateway)
    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    idempotency_key = f"message-http-repair-{uuid.uuid4()}"
    request_body = {"role": "patient_proxy", "content": "headache"}
    headers = {
        "X-Doctor-Id": "doctor-http-repair",
        "X-Idempotency-Key": idempotency_key,
        "X-State-Version": "1",
    }
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.post(
            f"/api/v1/consult/sessions/{session_id}/messages",
            json=request_body,
            headers=headers,
        )
        assert first.status_code == 200, first.text

        async with db_factory() as db, db.begin():
            http_claim = await db.scalar(
                select(HttpCommandClaim).where(
                    HttpCommandClaim.operation == "session.message.create.v1",
                    HttpCommandClaim.scope_key == f"session:{session_id}",
                )
            )
            assert http_claim is not None
            http_claim.status = "ambiguous"
            http_claim.http_status = None
            http_claim.response_payload = cast(Any, sql_null())
            http_claim.error_payload = cast(Any, sql_null())
            http_claim.lease_expires_at = None
            http_claim.completed_at = None

        replay = await client.post(
            f"/api/v1/consult/sessions/{session_id}/messages",
            json=request_body,
            headers=headers,
        )

    assert replay.status_code == 200, replay.text
    assert replay.json()["data"] == first.json()["data"]
    assert gateway.intake_calls == 1
    async with db_factory() as db:
        repaired = await db.scalar(
            select(HttpCommandClaim).where(
                HttpCommandClaim.operation == "session.message.create.v1",
                HttpCommandClaim.scope_key == f"session:{session_id}",
            )
        )
        intake_claim_count = await db.scalar(
            select(func.count())
            .select_from(IntakeCommandClaim)
            .where(IntakeCommandClaim.session_id == session_id)
        )
        patient_message_count = await db.scalar(
            select(func.count())
            .select_from(ConsultMessage)
            .where(
                ConsultMessage.session_id == session_id,
                ConsultMessage.role == "patient_proxy",
            )
        )
    assert repaired is not None and repaired.status == "completed"
    assert intake_claim_count == 1
    assert patient_message_count == 1


@pytest.mark.asyncio
async def test_messages_api_same_public_command_replays_degraded_intake_without_new_message(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    gateway = _UnavailableOnceGateway()
    _install_fake_runtime(monkeypatch, gateway)
    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    idempotency_key = f"message-gateway-resume-{uuid.uuid4()}"
    request_body = {"role": "patient_proxy", "content": "headache"}
    headers = {
        "X-Doctor-Id": "doctor-gateway-resume",
        "X-Idempotency-Key": idempotency_key,
        "X-State-Version": "1",
    }
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.post(
            f"/api/v1/consult/sessions/{session_id}/messages",
            json=request_body,
            headers=headers,
        )
        # 0d-2（1dc4c45）：网关瞬时不可用不再硬 503，而是降级为 ABSTAINED 继续追问，
        # 失败码写入 extraction 留痕；同幂等键重放直接回放完成响应，不新建消息。
        assert first.status_code == 200, first.text
        assert first.json()["data"]["agent_message"] is not None

        async with db_factory() as db:
            degraded_intake = await db.scalar(
                select(IntakeCommandClaim).where(IntakeCommandClaim.session_id == session_id)
            )
            degraded_http = await db.scalar(
                select(HttpCommandClaim).where(
                    HttpCommandClaim.operation == "session.message.create.v1",
                    HttpCommandClaim.scope_key == f"session:{session_id}",
                )
            )
            assert degraded_intake is not None
            degraded_run = await db.get(GraphRun, degraded_intake.run_id)
            message_count_after_degrade = await db.scalar(
                select(func.count())
                .select_from(ConsultMessage)
                .where(
                    ConsultMessage.session_id == session_id,
                    ConsultMessage.role == "patient_proxy",
                )
            )
        original_claim_id = degraded_intake.id
        original_message_id = degraded_intake.patient_message_id
        original_run_id = degraded_intake.run_id
        assert degraded_intake.status == "completed"
        assert degraded_intake.error_code is None
        assert degraded_intake.intermediate_payload is not None
        extraction_trace = degraded_intake.intermediate_payload["extraction"]
        assert extraction_trace["source"] == "degraded_fallback"
        assert extraction_trace["decision"] == "abstained"
        assert extraction_trace["last_failure_code"] == "MODEL_GATEWAY_UNAVAILABLE"
        assert degraded_http is not None and degraded_http.status == "completed"
        assert degraded_run is not None and degraded_run.status == "completed"
        assert message_count_after_degrade == 1

        retry = await client.post(
            f"/api/v1/consult/sessions/{session_id}/messages",
            json=request_body,
            headers=headers,
        )

    assert retry.status_code == 200, retry.text
    assert retry.json()["data"] == first.json()["data"]
    assert gateway.intake_calls == 1
    async with db_factory() as db:
        completed_intake = await db.scalar(
            select(IntakeCommandClaim).where(IntakeCommandClaim.session_id == session_id)
        )
        completed_http = await db.scalar(
            select(HttpCommandClaim).where(
                HttpCommandClaim.operation == "session.message.create.v1",
                HttpCommandClaim.scope_key == f"session:{session_id}",
            )
        )
        completed_run = await db.get(GraphRun, original_run_id)
        patient_messages = (
            await db.scalars(
                select(ConsultMessage).where(
                    ConsultMessage.session_id == session_id,
                    ConsultMessage.role == "patient_proxy",
                )
            )
        ).all()
    assert completed_intake is not None
    assert completed_intake.id == original_claim_id
    assert completed_intake.patient_message_id == original_message_id
    assert completed_intake.run_id == original_run_id
    assert completed_intake.status == "completed"
    assert completed_http is not None and completed_http.status == "completed"
    assert completed_run is not None and completed_run.status == "completed"
    assert [message.id for message in patient_messages] == [original_message_id]


@pytest.mark.asyncio
async def test_langgraph_messages_recovery_metadata_does_not_persist_clinical_payloads(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    gateway = _E2EFakeGateway("privacy")
    _install_fake_runtime(monkeypatch, gateway)
    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    async with db_factory() as db:
        await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            MessageCreateRequest(role="patient_proxy", content="privacy_patient_text_778899"),
            doctor_id=None,
            trace_id="messages-e2e-privacy",
            x_state_version=1,
        )

    async with db_factory() as db:
        claim_payload = await db.scalar(
            text("SELECT coalesce(string_agg(intermediate_payload::text, ' '), '') FROM intake_command_claims")
        )
        outbox_payload = await db.scalar(text("SELECT coalesce(string_agg(payload::text, ' '), '') FROM outbox_events"))
        checkpoint_payload = await db.scalar(
            text(
                """
                SELECT concat_ws(
                    ' ',
                    (SELECT coalesce(string_agg(to_jsonb(c)::text, ' '), '') FROM checkpoints c WHERE thread_id LIKE :needle),
                    (SELECT coalesce(string_agg(to_jsonb(w)::text, ' '), '') FROM checkpoint_writes w WHERE thread_id LIKE :needle),
                    (SELECT coalesce(string_agg(to_jsonb(b)::text, ' '), '') FROM checkpoint_blobs b WHERE thread_id LIKE :needle)
                )
                """
            ),
            {"needle": f"%{session_id}%"},
        )

    combined = f"{claim_payload} {outbox_payload} {checkpoint_payload}"
    assert "extraction_output" not in combined
    assert "privacy_patient_text_778899" not in combined
    assert "privacy_fact_value_778899" not in combined
    assert "chief_complaint.symptom" not in combined
    assert '"observations"' not in str(claim_payload)
    assert '"patient_safety_delta"' not in str(claim_payload)


@pytest.mark.asyncio
async def test_message_route_invokes_injected_intake_executor_without_patient_payload() -> None:
    state = _state()
    config = make_run_config(state["session_id"], graph_version=DEFAULT_GRAPH_VERSION)
    calls: list[dict[str, Any]] = []

    async def executor(input_state: XuanhuGraphState) -> dict[str, Any]:
        calls.append(dict(input_state))
        return {
            "route": NODE_INTAKE_SUBGRAPH_V1,
            "domain_state_version": 3,
            "gate_results": [
                {
                    "gate_name": "triage",
                    "decision": "passed",
                    "policy_version": "triage-red-flag.v1",
                }
            ],
            "artifact_refs": [
                {
                    "kind": "message",
                    "artifact_id": str(uuid.uuid4()),
                    "revision": 1,
                }
            ],
        }

    graph = build_main_graph(checkpointer=InMemorySaver(), intake_executor=executor)
    runner = GraphRunner(graph)
    result = await runner.ainvoke(dict(state), config=config)

    assert calls
    assert result["route"] == NODE_INTAKE_SUBGRAPH_V1
    assert result["domain_state_version"] == 3
    serialized = repr(result) + repr(calls)
    assert "头痛" not in serialized
    assert "patient" not in result
    validate_state_json_safe(result)


@pytest.mark.asyncio
async def test_message_route_invokes_injected_intake_executor_without_contextvar() -> None:
    state = _state()
    config = make_run_config(state["session_id"], graph_version=DEFAULT_GRAPH_VERSION)
    calls: list[dict[str, Any]] = []

    async def executor(input_state: XuanhuGraphState) -> dict[str, Any]:
        calls.append(dict(input_state))
        return {"route": NODE_INTAKE_SUBGRAPH_V1, "domain_state_version": 5}

    graph = build_main_graph(checkpointer=InMemorySaver(), intake_executor=executor)
    runner = GraphRunner(graph)
    result = await runner.ainvoke(dict(state), config=config)

    assert calls
    assert result["route"] == NODE_INTAKE_SUBGRAPH_V1
    assert result["domain_state_version"] == 5
    validate_state_json_safe(result)


@pytest.mark.asyncio
async def test_default_intake_subgraph_returns_sanitized_missing_claim_error(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    del db_factory
    state = _state()
    config = make_run_config(state["session_id"], graph_version=DEFAULT_GRAPH_VERSION)
    graph = build_main_graph(checkpointer=InMemorySaver())
    runner = GraphRunner(graph)

    result = await runner.ainvoke(dict(state), config=config)

    assert result["route"] == NODE_INTAKE_SUBGRAPH_V1
    assert result["last_error"] == {
        "code": "INTAKE_COMMAND_NOT_FOUND",
        "trace_id": state["run_id"],
        "detail": "intake command claim was not found",
    }
    validate_state_json_safe(result)


@pytest.mark.asyncio
async def test_intake_command_claim_replay_returns_stable_response_without_new_message(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = uuid.uuid4()
    body = MessageCreateRequest(role="patient_proxy", content="头痛三天")
    trace_id = "claim-replay"
    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    async with db_factory() as db:
        runner = LangGraphIntakeMessageRunner(db)
        claim = await runner._claim_or_replay(  # noqa: SLF001
            session_id,
            body,
            command_key="command:claim-replay",
            payload_digest=_payload_digest(body),
            doctor_id="doctor-a",
            trace_id=trace_id,
            x_state_version=1,
        )
        assert claim.message is not None
        response = MessageCreateResponse(
            message_id=str(claim.message.id),
            session_id=str(session_id),
            role="patient_proxy",
            stage="inquiry",
            content="头痛三天",
            current_stage="inquiry",
            state_version=2,
            created_at=claim.message.created_at,
            sufficiency_report=None,
        )
        await runner._complete_claim(claim.claim.id, response, None, 2)  # noqa: SLF001

    async with db_factory() as db:
        first_count = await db.scalar(select(func.count()).select_from(IntakeCommandClaim))
        message_count = await db.scalar(text("SELECT count(*) FROM consult_messages"))
        runner = LangGraphIntakeMessageRunner(db)
        replay = await runner._claim_or_replay(  # noqa: SLF001
            session_id,
            body,
            command_key="command:claim-replay",
            payload_digest=_payload_digest(body),
            doctor_id="doctor-a",
            trace_id=trace_id,
            x_state_version=1,
        )
        second_message_count = await db.scalar(text("SELECT count(*) FROM consult_messages"))

    assert first_count == 1
    assert replay.replay_response is not None
    assert replay.replay_response.model_dump(mode="json") == response.model_dump(mode="json")
    assert second_message_count == message_count


@pytest.mark.asyncio
async def test_intake_command_claim_rejects_same_key_different_payload(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = uuid.uuid4()
    first_body = MessageCreateRequest(role="patient_proxy", content="头痛三天")
    second_body = MessageCreateRequest(role="patient_proxy", content="胃痛一天")
    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    async with db_factory() as db:
        runner = LangGraphIntakeMessageRunner(db)
        await runner._claim_or_replay(  # noqa: SLF001
            session_id,
            first_body,
            command_key="command:same-key",
            payload_digest=_payload_digest(first_body),
            doctor_id=None,
            trace_id="same-key",
            x_state_version=1,
        )

    async with db_factory() as db:
        runner = LangGraphIntakeMessageRunner(db)
        with pytest.raises(Exception) as captured:
            await runner._claim_or_replay(  # noqa: SLF001
                session_id,
                second_body,
                command_key="command:same-key",
                payload_digest=_payload_digest(second_body),
                doctor_id=None,
                trace_id="same-key",
                x_state_version=1,
            )
        message_count = await db.scalar(text("SELECT count(*) FROM consult_messages"))

    assert type(captured.value).__name__ == "IdempotencyConflictError"
    assert message_count == 1


@pytest.mark.asyncio
async def test_intake_running_claim_recovers_from_domain_commit(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = uuid.uuid4()
    run_id = uuid.uuid4()
    message_id = uuid.uuid4()
    outbox_id = uuid.uuid4()
    claim_id = uuid.uuid4()
    command_key = "command:recover-claim"
    body = MessageCreateRequest(role="patient_proxy", content="å¤´ç—›ä¸‰å¤©")
    async with db_factory() as db, db.begin():
        db.add(
            ConsultSession(
                id=session_id,
                patient_info={},
                state_version=3,
                current_stage="inquiry",
                agent_runtime="langgraph",
                state_snapshot={
                    "sufficiency_report": {
                        "sufficient": True,
                        "covered": [],
                        "missing": [],
                        "suggestions": [],
                    }
                },
            )
        )
        db.add(
            ConsultMessage(
                id=message_id,
                session_id=session_id,
                role="patient_proxy",
                stage="inquiry",
                content=body.content,
                trace_id="recover-claim",
            )
        )
        db.add(
            GraphRun(
                id=run_id,
                session_id=session_id,
                graph_version=DEFAULT_GRAPH_VERSION,
                command_id=command_key,
                input_state_version=2,
                status="completed",
            )
        )
        db.add(
            OutboxEvent(
                id=outbox_id,
                event_type="intake.command_completed.v1",
                session_id=session_id,
                graph_run_id=run_id,
                state_version=3,
                trace_id="trace:recover-claim",
                payload={"session_id": str(session_id), "command_id": command_key},
            )
        )
        await db.flush()
        db.add(
            DomainCommandCommit(
                id=uuid.uuid4(),
                session_id=session_id,
                idempotency_key=command_key,
                input_state_version=2,
                agent_spec_version="intake-domain-delta.v1",
                delta_digest="0" * 64,
                output_state_version=3,
                changed=True,
                graph_run_id=run_id,
                outbox_event_id=outbox_id,
            )
        )
        db.add(
            IntakeCommandClaim(
                id=claim_id,
                session_id=session_id,
                idempotency_key=command_key,
                payload_digest=_payload_digest(body),
                input_state_version=2,
                status="running",
                run_id=run_id,
                patient_message_id=message_id,
            )
        )

    async with db_factory() as db:
        runner = LangGraphIntakeMessageRunner(db)
        response = await runner._wait_for_completed_claim(  # noqa: SLF001
            session_id,
            command_key,
            _payload_digest(body),
        )

    async with db_factory() as db:
        claim = await db.get(IntakeCommandClaim, claim_id)
        assert claim is not None

    assert response.message_id == str(message_id)
    assert response.state_version == 3
    assert claim.status == "completed"
    assert claim.response_payload is not None


@pytest.mark.asyncio
async def test_langgraph_advance_consumes_persisted_ready_gate(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_advance_graph(monkeypatch, db_factory)
    session_id = uuid.uuid4()
    async with db_factory() as db, db.begin():
        db.add(
            ConsultSession(
                id=session_id,
                patient_info={},
                state_version=2,
                current_stage="inquiry",
                agent_runtime="langgraph",
                state_snapshot={"agent_runtime": "langgraph", "current_stage": "inquiry", "state_version": 2},
            )
        )
        db.add(
            GateResult(
                id=uuid.uuid4(),
                session_id=session_id,
                gate_name=COMPLETENESS_GATE_NAME,
                policy_version=COMPLETENESS_POLICY_VERSION,
                input_state_version=2,
                decision="passed",
                details={"disposition": "ready"},
            )
        )

    async with db_factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        response = await _run_langgraph_advance(
            db,
            session,
            session_id=str(session_id),
            state_version=2,
            trace_id="advance-ready",
        )

    assert response["from_stage"] == "inquiry"
    assert response["current_stage"] == "syndrome"
    assert response["state_version"] == 3


@pytest.mark.asyncio
async def test_langgraph_advance_requires_current_ready_gate(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = uuid.uuid4()
    async with db_factory() as db, db.begin():
        db.add(
            ConsultSession(
                id=session_id,
                patient_info={},
                state_version=2,
                current_stage="inquiry",
                agent_runtime="langgraph",
            )
        )

    async with db_factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        with pytest.raises(InsufficientInquiryError):
            await _run_langgraph_advance(
                db,
                session,
                session_id=str(session_id),
                state_version=2,
                trace_id="advance-no-gate",
            )


@pytest.mark.asyncio
async def test_langgraph_advance_ready_gate_still_requires_proposed_safety_resolution(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = uuid.uuid4()
    source_message_id = uuid.uuid4()
    async with db_factory() as db, db.begin():
        db.add(
            ConsultSession(
                id=session_id,
                patient_info={},
                state_version=2,
                current_stage="inquiry",
                agent_runtime="langgraph",
            )
        )
        db.add(
            ConsultMessage(
                id=source_message_id,
                session_id=session_id,
                role="patient_proxy",
                stage="inquiry",
                content="No known allergies.",
                trace_id="advance-pending-safety-source",
            )
        )
        db.add(
            GateResult(
                id=uuid.uuid4(),
                session_id=session_id,
                gate_name=COMPLETENESS_GATE_NAME,
                policy_version=COMPLETENESS_POLICY_VERSION,
                input_state_version=2,
                decision="passed",
                details={"disposition": "ready"},
            )
        )
        await db.flush()
        db.add(
            SafetyFactAssertion(
                id=uuid.uuid4(),
                session_id=session_id,
                field_name="allergy",
                value={"status": "explicitly_none", "items": []},
                value_digest="a" * 64,
                assertion_fingerprint="b" * 64,
                status="proposed",
                source_kind="structured_form",
                source_message_id=source_message_id,
                extraction_run_id=None,
                template_version="advance-gate-test.v1",
                evidence_spans=[],
                evidence_digest="c" * 64,
                proposed_by_actor_type="doctor",
                proposed_by_actor_id="doctor-a",
            )
        )

    async with db_factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        with pytest.raises(InsufficientInquiryError) as captured:
            await _run_langgraph_advance(
                db,
                session,
                session_id=str(session_id),
                state_version=2,
                trace_id="advance-pending-safety",
            )
        assert "proposed safety facts" in (captured.value.detail or "")

    async with db_factory() as db:
        refreshed = await db.get(ConsultSession, session_id)
        claim_count = await db.scalar(
            select(func.count())
            .select_from(IntakeCommandClaim)
            .where(IntakeCommandClaim.session_id == session_id)
        )

    assert refreshed is not None
    assert refreshed.current_stage == "inquiry"
    assert refreshed.state_version == 2
    assert claim_count == 0


@pytest.mark.asyncio
async def test_langgraph_advance_rejects_stale_ready_gate_after_state_changes(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = uuid.uuid4()
    async with db_factory() as db, db.begin():
        db.add(
            ConsultSession(
                id=session_id,
                patient_info={},
                state_version=3,
                current_stage="inquiry",
                agent_runtime="langgraph",
            )
        )
        db.add(
            GateResult(
                id=uuid.uuid4(),
                session_id=session_id,
                gate_name=COMPLETENESS_GATE_NAME,
                policy_version=COMPLETENESS_POLICY_VERSION,
                input_state_version=2,
                decision="passed",
                details={"disposition": "ready"},
            )
        )

    async with db_factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        with pytest.raises(InsufficientInquiryError):
            await _run_langgraph_advance(
                db,
                session,
                session_id=str(session_id),
                state_version=None,
                trace_id="advance-stale-gate",
            )

    async with db_factory() as db:
        refreshed = await db.get(ConsultSession, session_id)
        assert refreshed is not None
        assert refreshed.current_stage == "inquiry"
        assert refreshed.state_version == 3


@pytest.mark.asyncio
async def test_langgraph_advance_replay_is_stable_and_writes_one_outbox(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_advance_graph(monkeypatch, db_factory)
    session_id = uuid.uuid4()
    async with db_factory() as db, db.begin():
        db.add(
            ConsultSession(
                id=session_id,
                patient_info={},
                state_version=2,
                current_stage="inquiry",
                agent_runtime="langgraph",
            )
        )
        db.add(
            GateResult(
                id=uuid.uuid4(),
                session_id=session_id,
                gate_name=COMPLETENESS_GATE_NAME,
                policy_version=COMPLETENESS_POLICY_VERSION,
                input_state_version=2,
                decision="passed",
                details={"disposition": "ready"},
            )
        )

    async with db_factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        first = await _run_langgraph_advance(
            db,
            session,
            session_id=str(session_id),
            state_version=2,
            trace_id="advance-replay-first-trace",
            idempotency_key="advance-public-replay",
        )

    async with db_factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        second = await _run_langgraph_advance(
            db,
            session,
            session_id=str(session_id),
            state_version=2,
            trace_id="advance-replay-retry-trace",
            idempotency_key="advance-public-replay",
        )
        outbox_count = await db.scalar(
            text("SELECT count(*) FROM outbox_events WHERE event_type = 'advance.command_completed.v1'")
        )

    assert second == first
    assert outbox_count == 1


# ---------------------------------------------------------------------------
# R1: 跨轮次 intake interrupt/resume 测试
# ---------------------------------------------------------------------------


def _collect_interrupt_values(snapshot: Any) -> list[dict[str, Any]]:
    """从 checkpoint snapshot 中收集所有挂起的 interrupt payload（L5 review 模式）。

    挂起 interrupt 可能出现在顶层 snapshot、每个 task 的 ``interrupts``、或嵌套
    state 的 ``__interrupt__`` 通道中。只收集结构化 payload（``kind``、
    ``interrupt_id``、``retry_error_code``…），不读取患者内容。
    """
    values: list[dict[str, Any]] = []

    def _append_from(container: Any) -> None:
        for item in getattr(container, "interrupts", ()) or ():
            value = getattr(item, "value", item)
            if isinstance(value, dict):
                values.append(value)
        channel = getattr(container, "values", None)
        if isinstance(channel, dict):
            for item in channel.get("__interrupt__", ()) or ():
                value = getattr(item, "value", item)
                if isinstance(value, dict):
                    values.append(value)

    _append_from(snapshot)
    for task in getattr(snapshot, "tasks", ()) or ():
        _append_from(task)
        nested = getattr(task, "state", None)
        if nested is not None:
            _append_from(nested)
            for sub in getattr(nested, "tasks", ()) or ():
                _append_from(sub)
    for sub in (getattr(snapshot, "subgraph_states", None) or {}).values():
        _append_from(sub)
    return values


def _checkpoint_intake_loop_count(snapshot: Any) -> int | None:
    """从 checkpoint snapshot 读取最内层子图的 ``intake_loop_count``。

    prepare_question 每轮 +1 落在 intake 子图 task state 里；顶层 main-graph state
    的 ``intake_loop_count`` 默认是 0（子图中断时未合并回父层），因此必须先看嵌套
    子图 state 再看顶层，避免读到父层默认值。
    """
    candidates: list[dict[str, Any]] = []
    for task in getattr(snapshot, "tasks", ()) or ():
        nested = getattr(task, "state", None)
        if nested is not None:
            if isinstance(getattr(nested, "values", None), dict):
                candidates.append(dict(nested.values))
            for sub in getattr(nested, "tasks", ()) or ():
                if isinstance(getattr(sub, "values", None), dict):
                    candidates.append(dict(sub.values))
    for sub in (getattr(snapshot, "subgraph_states", None) or {}).values():
        if isinstance(getattr(sub, "values", None), dict):
            candidates.append(dict(sub.values))
    if isinstance(snapshot.values, dict):
        candidates.append(dict(snapshot.values))
    for cand in candidates:
        value = cand.get("intake_loop_count")
        if isinstance(value, int):
            return value
    return None


@pytest.mark.asyncio
async def test_intake_r1_incomplete_interrupt_then_resume_continues_pipeline(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1 基本流程：首轮 incomplete → interrupt → 第二轮 resume → 继续管线。

    第一轮 POST 触发 interrupt（claim 在 prepare_question 中完成），
    第二轮 POST 检测挂起中断后通过 aresume 恢复，继续执行完整管线。
    """
    session_id = uuid.uuid4()
    gateway = _E2EFakeGateway("incomplete")
    _install_fake_runtime(monkeypatch, gateway)

    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    # Round 1: incomplete → interrupt
    async with db_factory() as db:
        response1 = await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            MessageCreateRequest(role="patient_proxy", content="headache"),
            doctor_id="doctor-a",
            trace_id="r1-interrupt-round1",
            x_state_version=1,
        )

    # 第一轮断言：问题已生成，管线所有步骤已完成
    assert response1.agent_message is not None
    assert response1.current_stage == "inquiry"
    assert gateway.intake_calls == 1
    assert gateway.question_model_calls == 1

    async with db_factory() as db:
        claim1 = await db.scalar(
            select(IntakeCommandClaim).where(
                IntakeCommandClaim.session_id == session_id,
                IntakeCommandClaim.status == "completed",
            )
        )
        assert claim1 is not None, "claim should be completed before interrupt"
        assert claim1.intermediate_payload is not None
        gates = claim1.intermediate_payload.get("gates", {})
        assert gates.get("route") == "incomplete"

    # Round 2: resume with answer
    async with db_factory() as db:
        response2 = await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            MessageCreateRequest(role="patient_proxy", content="头痛三天了"),
            doctor_id="doctor-a",
            trace_id="r1-interrupt-round2",
            x_state_version=None,
        )

    # 第二轮断言：管线再次完整执行
    assert response2.agent_message is not None
    assert response2.current_stage == "inquiry"
    assert gateway.intake_calls == 2
    assert gateway.question_model_calls == 2

    # 验证两个 claim 都已完成
    async with db_factory() as db:
        completed = await db.scalar(
            select(func.count())
            .select_from(IntakeCommandClaim)
            .where(
                IntakeCommandClaim.session_id == session_id,
                IntakeCommandClaim.status == "completed",
            )
        )
        assert completed == 2

    # 验证每条患者消息恰好持久化一次
    async with db_factory() as db:
        patient_msg_count = await db.scalar(
            select(func.count())
            .select_from(ConsultMessage)
            .where(
                ConsultMessage.session_id == session_id,
                ConsultMessage.role == "patient_proxy",
            )
        )
        assert patient_msg_count == 2, "exactly one patient message per round"


@pytest.mark.asyncio
async def test_intake_r1_multi_round_interrupt_resume_loop(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1 多轮循环：连续三轮 incomplete → interrupt → resume，验证 loop_count 递增。"""
    session_id = uuid.uuid4()
    gateway = _E2EFakeGateway("incomplete")
    _install_fake_runtime(monkeypatch, gateway)

    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    # Round 1
    async with db_factory() as db:
        r1 = await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            MessageCreateRequest(role="patient_proxy", content="头疼"),
            doctor_id="doctor-a",
            trace_id="r1-multi-round1",
            x_state_version=1,
        )
    assert r1.agent_message is not None

    # Round 2
    async with db_factory() as db:
        r2 = await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            MessageCreateRequest(role="patient_proxy", content="三天了"),
            doctor_id="doctor-a",
            trace_id="r1-multi-round2",
            x_state_version=None,
        )
    assert r2.agent_message is not None

    # Round 3
    async with db_factory() as db:
        r3 = await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            MessageCreateRequest(role="patient_proxy", content="没有发烧"),
            doctor_id="doctor-a",
            trace_id="r1-multi-round3",
            x_state_version=None,
        )
    assert r3.agent_message is not None

    # 验证所有三轮都完成
    assert gateway.intake_calls == 3
    assert gateway.question_model_calls == 3

    async with db_factory() as db:
        completed = await db.scalar(
            select(func.count())
            .select_from(IntakeCommandClaim)
            .where(
                IntakeCommandClaim.session_id == session_id,
                IntakeCommandClaim.status == "completed",
            )
        )
        assert completed == 3

    # 验证 outbox 事件恰好 3 个（每轮一个）
    async with db_factory() as db:
        outbox_count = await db.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(
                OutboxEvent.session_id == session_id,
                OutboxEvent.event_type == "intake.command_completed.v1",
            )
        )
        assert outbox_count == 3

    # 验证 loop_count 递增：每轮 interrupt 前 prepare_question 将 intake_loop_count +1。
    from app.agent_runtime.checkpoint import postgres_checkpointer
    from app.agent_runtime.graph import build_main_graph
    from app.core.config import get_settings

    config = make_run_config(str(session_id), graph_version=DEFAULT_GRAPH_VERSION)
    async with postgres_checkpointer(get_settings().database_url) as saver:
        graph = build_main_graph(checkpointer=saver)
        snapshot = await graph.aget_state(config, subgraphs=True)
    loop_count = _checkpoint_intake_loop_count(snapshot)
    assert loop_count == 3, f"三轮 interrupt 后 intake_loop_count 应为 3，实际 {loop_count}"


@pytest.mark.asyncio
async def test_intake_r1_claim_completed_before_interrupt_no_duplicate_persistence(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1 持久化：prepare_question 在 interrupt 前完成 claim，不重复持久化。

    interrupt() 触发的 GraphInterrupt 不应被当作执行失败——
    claim 已在 prepare_question 节点中 durable 完成。
    """
    session_id = uuid.uuid4()
    gateway = _E2EFakeGateway("incomplete")
    _install_fake_runtime(monkeypatch, gateway)

    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    async with db_factory() as db:
        response = await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            MessageCreateRequest(role="patient_proxy", content="headache and fever"),
            doctor_id="doctor-a",
            trace_id="r1-claim-durable",
            x_state_version=1,
        )

    assert response.agent_message is not None
    # prepare_question 应像 _execute_after_claim 一样从 DB 重载已持久化的追问消息，
    # 使响应携带真实 created_at（而非构建期遗留的 None）。
    assert response.agent_message.created_at is not None, "agent_message 应携带持久化后的真实 created_at"

    async with db_factory() as db:
        claim = await db.scalar(
            select(IntakeCommandClaim).where(IntakeCommandClaim.session_id == session_id)
        )
        assert claim is not None
        # 关键断言：claim 状态为 completed，不是 failed
        assert claim.status == "completed", (
            f"claim 应在 interrupt 前由 prepare_question 完成，"
            f"实际状态: {claim.status}, error_code: {claim.error_code}"
        )
        assert claim.response_payload is not None
        assert claim.error_code is None

        # 验证恰好一条医生提问消息
        agent_message_count = await db.scalar(
            select(func.count())
            .select_from(ConsultMessage)
            .where(
                ConsultMessage.session_id == session_id,
                ConsultMessage.role == "agent",
                ConsultMessage.agent_name == "question_composer",
            )
        )
        assert agent_message_count == 1, "应该恰好生成一条追问消息，不能重复"

        # 验证 domain commit 恰好一次
        commit_count = await db.scalar(
            select(func.count())
            .select_from(DomainCommandCommit)
            .where(DomainCommandCommit.session_id == session_id)
        )
        assert commit_count == 1

        # 验证 outbox 事件恰好一个
        outbox_count = await db.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(
                OutboxEvent.session_id == session_id,
                OutboxEvent.event_type == "intake.command_completed.v1",
            )
        )
        assert outbox_count == 1


@pytest.mark.asyncio
async def test_intake_r1_ready_exit_path_still_ends_without_interrupt(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1 出口路径：ready 仍直接结束，不进入 interrupt/resume 循环。

    验证 ready disposition 不走 prepare_question → interrupt_question，
    而是直接在 ready_route 节点完成 claim 并返回 sufficiency_report。
    """
    session_id = uuid.uuid4()
    # ready_full：共享 "ready" 模式缺 four_diagnosis（required_by_default），会判
    # INCOMPLETE 并追问四诊；只有采齐四诊才会产生真实 READY disposition。
    gateway = _E2EFakeGateway("ready_full")
    _install_fake_runtime(monkeypatch, gateway)

    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    async with db_factory() as db:
        response = await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            MessageCreateRequest(
                role="patient_proxy",
                content=(
                    "headache three days stable; no drug allergies; not pregnant; "
                    "not breastfeeding; no current medications; no major conditions"
                ),
            ),
            doctor_id="doctor-a",
            trace_id="r1-ready-exit",
            x_state_version=1,
        )

    assert response.current_stage == "inquiry"
    assert response.sufficiency_report is not None
    # ready 路径不生成 composer 追问：agent_message 是静态「问诊完成通知」
    # （问诊要素已采集完整），绝不是 composer 生成的追问问题。
    from app.services.intake_completion_notice import INTAKE_COMPLETE_NOTICE_TEXT

    assert response.agent_message is not None
    assert response.agent_message.content == INTAKE_COMPLETE_NOTICE_TEXT

    async with db_factory() as db:
        claim = await db.scalar(
            select(IntakeCommandClaim).where(IntakeCommandClaim.session_id == session_id)
        )
        assert claim is not None
        assert claim.status == "completed"
        gates = claim.intermediate_payload.get("gates", {}) if claim.intermediate_payload else {}
        assert gates.get("route") == "ready"

    # ready 路径只调用一次 intake extraction（不触发 question_composer）
    assert gateway.intake_calls == 1
    assert gateway.question_model_calls == 0

    # 图已正常结束：checkpoint 无挂起 task（不进入 prepare_question → interrupt 循环）
    from app.agent_runtime.checkpoint import postgres_checkpointer
    from app.agent_runtime.graph import build_main_graph
    from app.core.config import get_settings

    config = make_run_config(str(session_id), graph_version=DEFAULT_GRAPH_VERSION)
    async with postgres_checkpointer(get_settings().database_url) as saver:
        graph = build_main_graph(checkpointer=saver)
        snapshot = await graph.aget_state(config, subgraphs=True)
    assert not snapshot.tasks, "ready 出口后 checkpoint 不应有挂起的 interrupt task"


@pytest.mark.asyncio
async def test_intake_r1_resume_rejected_on_invalid_claim_ref(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1 resume 校验：无效的 answer claim ref 被拒绝且不消耗 interrupt。

    直接使用 GraphRunner.aresume 传入无效引用：interrupt_question_node 在
    apply_intake_resume 拒绝后于同一节点重入 interrupt()（携带
    retry_error_code），因此 aresume 不抛异常，retry_error_code 通过
    checkpoint 的 __interrupt__ 通道暴露；随后 submit_message 仍能正常恢复。
    """
    from app.agent_runtime.checkpoint import postgres_checkpointer
    from app.agent_runtime.runner import GraphRunner
    from app.core.config import get_settings

    session_id = uuid.uuid4()
    gateway = _E2EFakeGateway("incomplete")
    _install_fake_runtime(monkeypatch, gateway)

    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    # Round 1: 触发 interrupt
    async with db_factory() as db:
        await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            MessageCreateRequest(role="patient_proxy", content="头痛"),
            doctor_id="doctor-a",
            trace_id="r1-resume-reject",
            x_state_version=1,
        )

    config = make_run_config(str(session_id), graph_version=DEFAULT_GRAPH_VERSION)

    # 用无效的 resume ref 尝试恢复 —— 应被 reject 且不消耗 interrupt。
    # interrupt_question_node 的 reject 循环在 apply_intake_resume 抛
    # IntakeInterruptRejected 后在同一节点内再次 interrupt()（携带
    # retry_error_code）重新挂起；因此 aresume 不抛 GraphInterrupt，
    # retry_error_code 通过 checkpoint 的 __interrupt__ 通道暴露。
    # （与 L5 review 的 test_invalid_resume_can_be_followed_by_legal_resume
    # 模式一致。）
    async with postgres_checkpointer(get_settings().database_url) as saver:
        graph = build_main_graph(checkpointer=saver)
        runner = GraphRunner(graph, timeout_seconds=60)
        resume = {"intake_answer_claim_ref": str(uuid.uuid4())}  # 不存在的 claim
        await runner.aresume(
            session_id=str(session_id),
            graph_version=DEFAULT_GRAPH_VERSION,
            resume=resume,
            config=config,
        )
        snapshot = await graph.aget_state(config, subgraphs=True)
        interrupt_values = _collect_interrupt_values(snapshot)
        assert interrupt_values, "rejected resume 后仍应有挂起的 intake interrupt"
        assert interrupt_values[-1]["kind"] == "intake_question"
        assert interrupt_values[-1]["retry_error_code"] == "INTAKE_ANSWER_CLAIM_NOT_FOUND"

    # 验证 interrupt 未被消耗 —— 后续 submit_message 仍能正常 resume 继续管线
    async with db_factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        assert session.current_stage == "inquiry"

    async with db_factory() as db:
        response2 = await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            MessageCreateRequest(role="patient_proxy", content="头痛三天了"),
            doctor_id="doctor-a",
            trace_id="r1-resume-reject-recover",
            x_state_version=None,
        )
    assert response2.agent_message is not None
    assert gateway.intake_calls == 2


@pytest.mark.asyncio
async def test_intake_r1_graph_state_contains_only_refs_not_patient_content(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1 隐私投影：graph state 仅含 PHI-safe 引用，不含患者原始内容。

    验证 checkpoint state 中所有字段均为 JSON-safe 类型（str/int/bool/list/dict/None），
    不含 ORM 对象、SQLAlchemy session、模型客户端或患者原始文本。
    """
    session_id = uuid.uuid4()
    gateway = _E2EFakeGateway("incomplete")
    _install_fake_runtime(monkeypatch, gateway)

    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    async with db_factory() as db:
        await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            MessageCreateRequest(role="patient_proxy", content="头痛三天，有轻微发热"),
            doctor_id="doctor-a",
            trace_id="r1-state-refs",
            x_state_version=1,
        )

    # 从 PostgreSQL checkpoint 读取最新 state
    from app.agent_runtime.checkpoint import postgres_checkpointer
    from app.core.config import get_settings

    config = make_run_config(str(session_id), graph_version=DEFAULT_GRAPH_VERSION)

    async with postgres_checkpointer(get_settings().database_url) as saver:
        graph = build_main_graph(checkpointer=saver)
        snapshot = await graph.aget_state(config, subgraphs=True)

    # 获取最内层子图 state
    if snapshot.tasks:
        # 如果有 pending tasks，state 在 task 的 state 字段中
        task_state = snapshot.tasks[0].state
        if hasattr(task_state, 'values'):
            state_dict = dict(task_state.values)
        elif isinstance(task_state, dict):
            state_dict = dict(task_state)
        else:
            state_dict = {}
    elif snapshot.values:
        state_dict = dict(snapshot.values)
    else:
        state_dict = {}

    # 验证所有值均为 JSON-safe 类型
    patient_content_keywords = ["头痛", "头痛三天", "有轻微发热", "fever", "headache"]
    state_json = json.dumps(state_dict, ensure_ascii=False, default=str)

    for keyword in patient_content_keywords:
        assert keyword not in state_json, (
            f"graph state 不得包含患者原始内容 '{keyword}'，"
            f"state keys: {list(state_dict.keys())}"
        )

    # 验证 state 不包含 ORM 对象
    for key, value in state_dict.items():
        assert not hasattr(value, "_sa_instance_state"), (
            f"state key '{key}' 包含 SQLAlchemy ORM 对象，违反 ADR-002"
        )

    # 验证 state JSON 可序列化
    validate_state_json_safe(state_dict)

    # 验证 pending_interrupt 存在（来自子图）
    if not snapshot.tasks and state_dict.get("pending_interrupt") is None:
        # 检查子图 state
        for _ns, sub_snapshot in (snapshot.subgraph_states or {}).items():
            sub_values = dict(sub_snapshot.values) if sub_snapshot.values else {}
            if sub_values.get("pending_interrupt"):
                pending = sub_values["pending_interrupt"]
                assert pending.get("kind") == "intake_question"
                assert "interrupt_id" in pending
                assert "resume_token_ref" in pending
                break
    elif snapshot.tasks:
        # pending interrupt 在 task 中
        pass  # tasks 存在说明有挂起中断


@pytest.mark.asyncio
async def test_intake_r1_red_flag_still_blocks_without_interrupt(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1 红旗路径：red_flag 应被 triage_precheck 拦截，不进入 interrupt 循环。

    验证 red_flag disposition 仍然直接在 manual route 完成 claim，
    不经过 prepare_question / interrupt_question。
    """
    session_id = uuid.uuid4()
    gateway = _E2EFakeGateway("red_flag")
    _install_fake_runtime(monkeypatch, gateway)

    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    async with db_factory() as db:
        _resp = await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            MessageCreateRequest(
                role="patient_proxy",
                content="发烧 39.2°C 持续三天",
            ),
            doctor_id="doctor-a",
            trace_id="r1-red-flag",
            x_state_version=1,
        )

    # red_flag 路径：triage_precheck 拦截 → manual route → session 状态为 blocked
    # （与 L5 既有 red_flag 测试语义一致，见 test_messages_e2e_model_red_flag_block）。
    assert _resp.current_stage == "blocked"
    async with db_factory() as db:
        claim = await db.scalar(
            select(IntakeCommandClaim).where(IntakeCommandClaim.session_id == session_id)
        )
        assert claim is not None
        assert claim.status == "completed"
        assert claim.error_code is None
        gates = claim.intermediate_payload.get("gates", {}) if claim.intermediate_payload else {}
        # red_flag 的 triage gate 应标记为 blocked
        assert gates.get("triage_decision") in ("blocked", None) or gates.get("route") in ("manual", None)


@pytest.mark.asyncio
async def test_intake_r1_idempotency_replay_after_interrupt_returns_same_response(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1 幂等：interrupt 后同名 command_key 重放返回相同响应。

    interrupt 前 claim 已 completed，重放时 _claim_or_replay
    检测到 completed 状态直接返回缓存的 response。
    """
    session_id = uuid.uuid4()
    gateway = _E2EFakeGateway("incomplete")
    _install_fake_runtime(monkeypatch, gateway)

    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    body = MessageCreateRequest(role="patient_proxy", content="头痛")

    async with db_factory() as db:
        first = await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id), body,
            doctor_id="doctor-a",
            trace_id="r1-idempotent-first",
            x_state_version=1,
            idempotency_key="r1-idem-key-1",
        )

    async with db_factory() as db:
        second = await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id), body,
            doctor_id="doctor-a",
            trace_id="r1-idempotent-replay",
            x_state_version=1,
            idempotency_key="r1-idem-key-1",
        )

    assert second.message_id == first.message_id
    assert second.state_version == first.state_version
    assert second.agent_message is not None
    if first.agent_message and second.agent_message:
        assert second.agent_message.message_id == first.agent_message.message_id

    # 模型调用次数应不变（幂等重放不应重复执行管线）
    assert gateway.intake_calls == 1
    assert gateway.question_model_calls == 1


# ---------------------------------------------------------------------------
# R1: PostgreSQL checkpoint 恢复测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intake_r1_checkpoint_survives_and_resumes_across_fresh_graph(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1 checkpoint 恢复：interrupt 后 checkpoint 在 PostgreSQL 持久化，新 graph 实例可恢复。

    模拟跨请求/跨进程场景：
    1. 第一个 graph 实例运行并触发 interrupt（checkpoint 写入 PostgreSQL）
    2. 第二个全新 graph 实例通过 aget_state 发现挂起任务
    3. 使用 aresume 恢复执行
    """
    from app.agent_runtime.checkpoint import postgres_checkpointer
    from app.core.config import get_settings

    session_id = uuid.uuid4()
    gateway = _E2EFakeGateway("incomplete")
    _install_fake_runtime(monkeypatch, gateway)

    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    config = make_run_config(str(session_id), graph_version=DEFAULT_GRAPH_VERSION)

    # Step 1: 第一个 graph 实例 —— 触发 interrupt
    async with db_factory() as db:
        response1 = await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            MessageCreateRequest(role="patient_proxy", content="头痛"),
            doctor_id="doctor-a",
            trace_id="r1-checkpoint-step1",
            x_state_version=1,
        )
    assert response1.agent_message is not None

    # Step 2: 全新 graph 实例 —— 验证 checkpoint 可读取
    async with postgres_checkpointer(get_settings().database_url) as saver:
        graph = build_main_graph(checkpointer=saver)
        snapshot = await graph.aget_state(config, subgraphs=True)
        # 应有挂起任务（interrupt 未恢复）
        assert snapshot.tasks or snapshot.next, (
            "interrupt 后 checkpoint 应有挂起任务或 next 节点"
        )

    # Step 3: 新的 graph 实例 + 新 claim —— 恢复执行
    async with db_factory() as db:
        response2 = await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            MessageCreateRequest(role="patient_proxy", content="头痛三天了"),
            doctor_id="doctor-a",
            trace_id="r1-checkpoint-step3",
            x_state_version=None,
        )
    assert response2.agent_message is not None

    # 验证两轮都完成
    assert gateway.intake_calls == 2
    assert gateway.question_model_calls == 2


# ---------------------------------------------------------------------------
# R3-C: 生产形态 PostgreSQL/LangGraph trajectory bridge（对接离线轨迹评估）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r3c_checkpoint_resume_bridges_real_pg_interrupt_resume_to_manifest(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3-C：真实 PostgreSQL/LangGraph interrupt/resume 与离线轨迹评估的桥接。

    两轮同一会话：
    - 第 1 轮（state_version=1）→ durable completed claim + 真实挂起 checkpoint
      interrupt，记录 checkpoint 内 intake_loop_count == 1；
    - 第 2 轮（不带 state_version，生产路径恢复同一 pending checkpoint）→
      第二个 completed claim，gateway intake/question 调用均 == 2，
      记录 intake_loop_count == 2。

    通过官方 loader 加载内置 manifest 并选中 CHECKPOINT_RESUME 轨迹，用一个小型
    executor 闭包把真实 durable 观测投影成严格的两步 TrajectoryStep。executor
    fail-closed：只有"真实中断且恢复同一 checkpoint"的全部条件成立才映射
    checkpoint_ref，否则抛错 → evaluate_trajectory 产出 adapter_failure 报告。
    断言评估无失败码、六个不变式全满足、observed digest 与 golden digest 一致、
    重复评估字节级一致、两个唯一原始 sentinel 患者回复绝不进入报告。
    """
    import scripts.evaluate_agent_trajectories as trajectory_cli
    from app.agent_runtime.checkpoint import postgres_checkpointer
    from app.agent_runtime.trajectory_evaluation import (
        InvariantStatus,
        Route,
        Scenario,
        StepAction,
        Trajectory,
        TrajectoryStep,
        evaluate_trajectory,
        model_canonical_json,
        observed_steps_digest,
    )

    session_id = uuid.uuid4()
    gateway = _E2EFakeGateway("incomplete")
    _install_fake_runtime(monkeypatch, gateway)
    # 两个唯一原始 sentinel 患者回复：驱动 fake gateway 产出首轮 symptom 与次轮
    # course（均触发"三天"以外的默认/增量分支），且绝不进入任何 step 或报告。
    sentinel_round1 = "R3C_SENTINEL_HEADACHE_9f3d"
    sentinel_round2 = "R3C_SENTINEL_三天_9f3d"

    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    # Round 1：incomplete → interrupt
    async with db_factory() as db:
        response1 = await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            MessageCreateRequest(role="patient_proxy", content=sentinel_round1),
            doctor_id="doctor-a",
            trace_id="r3c-round1",
            x_state_version=1,
        )
    assert response1.agent_message is not None
    assert response1.current_stage == "inquiry"
    assert gateway.intake_calls == 1
    assert gateway.question_model_calls == 1

    # 第 1 轮后的 checkpoint：挂起 interrupt + intake_loop_count == 1
    config = make_run_config(str(session_id), graph_version=DEFAULT_GRAPH_VERSION)
    async with postgres_checkpointer(get_settings().database_url) as saver:
        graph = build_main_graph(checkpointer=saver)
        snapshot_after_round1 = await graph.aget_state(config, subgraphs=True)
    interrupt_payloads_after_round1 = _collect_interrupt_values(snapshot_after_round1)
    interrupt_kinds_after_round1 = [payload.get("kind") for payload in interrupt_payloads_after_round1]
    pending_interrupt_after_round1 = "intake_question" in interrupt_kinds_after_round1
    loop_after_round1 = _checkpoint_intake_loop_count(snapshot_after_round1)
    assert snapshot_after_round1.tasks, "round 1 后 checkpoint 应有挂起的 task（interrupt 未恢复）"
    assert pending_interrupt_after_round1, (
        f"round 1 后应有 kind=intake_question 的挂起 interrupt，实际 {interrupt_kinds_after_round1}"
    )
    assert loop_after_round1 == 1, f"round 1 后 checkpoint intake_loop_count 应为 1，实际 {loop_after_round1}"

    # durable completed claim
    async with db_factory() as db:
        claim1 = await db.scalar(select(IntakeCommandClaim).where(IntakeCommandClaim.session_id == session_id))
        assert claim1 is not None, "round 1 应产生 durable claim"
        assert claim1.status == "completed", (
            f"claim 应在 interrupt 前由 prepare_question 完成，实际状态 {claim1.status}"
        )
        assert claim1.response_payload is not None
        assert claim1.error_code is None
        outbox1 = await db.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(
                OutboxEvent.session_id == session_id,
                OutboxEvent.event_type == "intake.command_completed.v1",
            )
        )
        assert outbox1 == 1

    # Round 2：不带 state_version → 生产路径恢复同一 pending checkpoint
    async with db_factory() as db:
        response2 = await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            MessageCreateRequest(role="patient_proxy", content=sentinel_round2),
            doctor_id="doctor-a",
            trace_id="r3c-round2",
            x_state_version=None,
        )
    assert response2.agent_message is not None
    assert response2.current_stage == "inquiry"
    assert gateway.intake_calls == 2
    assert gateway.question_model_calls == 2

    # Round 2 后的 checkpoint：intake_loop_count == 2（证明恢复同一 checkpoint，
    # 而非重新开始 —— 若全新开始计数会退回 1）。
    async with postgres_checkpointer(get_settings().database_url) as saver:
        graph = build_main_graph(checkpointer=saver)
        snapshot_after_round2 = await graph.aget_state(config, subgraphs=True)
    loop_after_round2 = _checkpoint_intake_loop_count(snapshot_after_round2)
    assert loop_after_round2 == 2, f"round 2 后 checkpoint intake_loop_count 应为 2，实际 {loop_after_round2}"

    # durable：两个 completed claim、两条患者消息
    async with db_factory() as db:
        completed_claims = await db.scalar(
            select(func.count())
            .select_from(IntakeCommandClaim)
            .where(
                IntakeCommandClaim.session_id == session_id,
                IntakeCommandClaim.status == "completed",
            )
        )
        patient_messages = await db.scalar(
            select(func.count())
            .select_from(ConsultMessage)
            .where(
                ConsultMessage.session_id == session_id,
                ConsultMessage.role == "patient_proxy",
            )
        )
    assert completed_claims == 2
    assert patient_messages == 2

    # 通过官方 loader 加载内置 manifest，只选中 CHECKPOINT_RESUME 轨迹
    manifest = trajectory_cli.load_bundled_manifest()
    selected = next(item for item in manifest.trajectories if item.scenario == Scenario.CHECKPOINT_RESUME)

    # 把真实 durable 观测收集为纯 int/bool 事实，绝不携带消息内容、DB payload 或
    # 任何临床文本。executor 只从这些事实投影步骤，且仅当全部条件成立才映射
    # checkpoint_ref（fail-closed）。
    facts: dict[str, Any] = {
        "pending_interrupt_after_round1": pending_interrupt_after_round1,
        "loop_after_round1": loop_after_round1,
        "loop_after_round2": loop_after_round2,
        "intake_calls": gateway.intake_calls,
        "question_calls": gateway.question_model_calls,
        "completed_claims": int(completed_claims),
        "patient_messages": int(patient_messages),
    }

    def _r3c_executor(_trajectory: Trajectory) -> tuple[TrajectoryStep, ...]:
        # fail-closed：任一真实条件不成立 → 抛错 → evaluate_trajectory 产出
        # adapter_failure 报告（随后断言 adapter_failure is False 将失败）。
        if not facts["pending_interrupt_after_round1"]:
            raise AssertionError("round 1 未留下真实挂起的 checkpoint interrupt")
        if facts["loop_after_round1"] != 1 or facts["loop_after_round2"] != 2:
            raise AssertionError("checkpoint intake_loop_count 未按 1 然后 2 推进")
        if facts["intake_calls"] != 2 or facts["question_calls"] != 2:
            raise AssertionError("gateway intake/question 调用未在恢复后都达到 2")
        if facts["completed_claims"] != 2 or facts["patient_messages"] != 2:
            raise AssertionError("durable completed claims / patient messages 未达到 2")
        checkpoint_ref = "ckpt.ref.1"
        return (
            TrajectoryStep(
                step_id="step.checkpoint.save.1",
                action=StepAction.RESPOND,
                route=Route.INTAKE,
                state_version=0,
                question_count=facts["loop_after_round1"],
                safety_escalated=False,
                protocol_valid=True,
                replay_ref=None,
                checkpoint_ref=checkpoint_ref,
                projection_safe=True,
            ),
            TrajectoryStep(
                step_id="step.checkpoint.resume.1",
                action=StepAction.RESUME,
                route=Route.RESUME,
                state_version=1,
                question_count=facts["loop_after_round2"],
                safety_escalated=False,
                protocol_valid=True,
                replay_ref="replay.ref.1",
                checkpoint_ref=checkpoint_ref,
                projection_safe=True,
            ),
        )

    report = evaluate_trajectory(selected, _r3c_executor)
    assert report.adapter_failure is False, "真实中断/恢复条件应使 executor 正常产出步骤"
    assert report.failure_codes == ()
    assert all(outcome.status is InvariantStatus.SATISFIED for outcome in report.invariant_outcomes)
    assert report.observed_steps_digest == observed_steps_digest(selected.steps)
    assert report.trajectory_digest == selected.digest
    report.validate_digest()

    # 重复评估必须字节级一致
    again = evaluate_trajectory(selected, _r3c_executor)
    assert model_canonical_json(again) == model_canonical_json(report)

    # 两个唯一原始 sentinel 患者回复绝不进入 step 或报告
    canonical = model_canonical_json(report)
    assert sentinel_round1 not in canonical
    assert sentinel_round2 not in canonical
    assert "R3C_SENTINEL" not in canonical
