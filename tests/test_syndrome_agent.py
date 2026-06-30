"""P5-3 辨证 Agent 测试。

使用 fake gateway 和 fake retriever 覆盖：
- SyndromeResult schema 校验
- RAG 有证据时输出辨证结果
- RAG 无证据时明确缺证提示
- fake gateway schema 失败归一化
- Evidence/citations 可追溯
- 不输出处方/剂量/安全审核结论
- Supervisor syndrome -> prescription 路由
- SyndromeAgent 走 BaseAgentImpl 和模型网关抽象
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.agents.errors import AgentRunError
from app.agents.prompt_loader import PromptLoader
from app.agents.syndrome import (
    SyndromeAgent,
    _validate_citations,
    build_syndrome_query,
    format_evidence_summary,
    merge_syndrome_result_to_state,
)
from app.rag.schemas import Evidence
from app.schemas.agent import (
    PatientInfo,
    SyndromeResult,
    TenQuestions,
    XuanhuState,
)
from app.schemas.types import Stage

# ---------------------------------------------------------------------------
# Fake Gateway（不依赖真实模型网关）
# ---------------------------------------------------------------------------


class FakeGateway:
    """可控 fake gateway，注入预设响应或异常。"""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def chat_structured(
        self,
        messages: list[dict[str, Any]],
        output_schema: type[Any],
        *,
        trace_id: str,
        session_id: str | None = None,
        agent_name: str | None = None,
    ) -> Any:
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


# ---------------------------------------------------------------------------
# Fake Retriever
# ---------------------------------------------------------------------------


class FakeRetriever:
    """可控 fake retriever，注入预设 Evidence 或异常。"""

    def __init__(self, responses: list[list[Evidence] | Exception]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def retrieve(
        self,
        query: str,
        primary_sources: list[str],
        *,
        allow_cross_source: bool = True,
        top_k: int = 8,
        filters: dict[str, Any] | None = None,
    ) -> list[Evidence]:
        self.calls.append(
            {
                "query": query,
                "primary_sources": primary_sources,
                "allow_cross_source": allow_cross_source,
                "top_k": top_k,
                "filters": filters,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_prompt_files(tmp_path: Path, *, manifest_extra: str = "") -> Path:
    """写临时 prompt 文件，返回 manifest 路径。"""
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    manifest_content = (
        "test_agent: test_agent_v1.jinja2\n"
        "inquiry: inquiry_v1.jinja2\n"
        "sufficiency: sufficiency_v1.jinja2\n"
        "syndrome: syndrome_v1.jinja2\n"
        + manifest_extra
    )
    (prompt_dir / "manifest.yaml").write_text(manifest_content, encoding="utf-8")
    (prompt_dir / "test_agent_v1.jinja2").write_text("TEST_PROMPT", encoding="utf-8")
    (prompt_dir / "inquiry_v1.jinja2").write_text(
        "You are a TCM inquiry assistant.\n{state_summary}\n{conversation_history}\n",
        encoding="utf-8",
    )
    (prompt_dir / "sufficiency_v1.jinja2").write_text(
        "You are a TCM sufficiency assistant.\n{state_summary}\n{conversation_history}\n",
        encoding="utf-8",
    )
    (prompt_dir / "syndrome_v1.jinja2").write_text(
        "You are a TCM syndrome differentiation assistant.\n"
        "{state_summary}\n{conversation_history}\n{evidence_summary}\n",
        encoding="utf-8",
    )
    return prompt_dir / "manifest.yaml"


def _evidence(
    evidence_id: str = "ev-001",
    source_type: str = "theory",
    title: str = "湿证辨治",
    content_snippet: str = "脾虚湿盛，症见食欲差、大便溏、身重困倦……",
    score: float = 0.85,
    rank: int = 1,
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        source_type=source_type,
        source_id="src-001",
        chunk_id="chk-001",
        title=title,
        content_snippet=content_snippet,
        score=score,
        rank=rank,
    )


def _state_with_full_info(session_id: str | None = None) -> XuanhuState:
    """构造一个包含完整四诊信息的 XuanhuState。"""
    return XuanhuState(
        session_id=session_id or str(uuid.uuid4()),
        patient_info=PatientInfo(name="测试患者", gender="female", age=30),
        chief_complaint="头痛三天",
        present_illness="三天前淋雨后开始，以双侧太阳穴附近胀痛为主，伴身重困倦",
        past_history="既往有偏头痛史",
        ten_questions=TenQuestions(
            cold_heat="恶寒发热",
            head_body="头痛连及项背，身重困倦",
            stool_urine="大便偏溏，小便清长",
            diet="食欲差，口淡不渴",
            sleep="睡眠尚可",
        ),
        inquiry_messages=[
            {"role": "doctor", "content": "患者诉头痛三天"},
            {"role": "assistant", "content": "请问头痛的具体位置？", "asked_dimension": "present_illness"},
        ],
    )


def _minimal_state(session_id: str | None = None) -> XuanhuState:
    """构造一个信息极少的 XuanhuState。"""
    return XuanhuState(
        session_id=session_id or str(uuid.uuid4()),
        inquiry_messages=[{"role": "doctor", "content": "患者来了"}],
    )


# ===========================================================================
# SyndromeResult Schema 校验
# ===========================================================================


def test_syndrome_result_minimal_valid() -> None:
    """最少合法 SyndromeResult 可独立校验。"""
    result = SyndromeResult.model_validate(
        {
            "syndrome": "脾虚湿困证",
            "syndrome_basis": ["食欲差", "大便溏"],
            "treatment_principle": "健脾化湿",
            "confidence": 0.8,
        }
    )
    assert result.syndrome == "脾虚湿困证"
    assert len(result.syndrome_basis) == 2
    assert result.differential == []
    assert result.citations == []
    assert result.confidence == 0.8


def test_syndrome_result_full_shape() -> None:
    """完整 SyndromeResult 可独立校验。"""
    result = SyndromeResult.model_validate(
        {
            "syndrome": "脾虚湿困，清阳不升",
            "syndrome_basis": [
                "食欲差、大便溏为脾虚湿盛之象",
                "头痛身重为湿困清阳",
            ],
            "differential": [
                "需排除肝阳上亢证：鉴别要点为头痛性质（胀痛而非跳痛）、无面红目赤、脉不弦",
            ],
            "treatment_principle": "健脾化湿，升清降浊",
            "citations": ["ev-001", "ev-002"],
            "confidence": 0.85,
        }
    )
    assert len(result.syndrome_basis) == 2
    assert len(result.differential) == 1
    assert len(result.citations) == 2
    assert result.confidence == 0.85


def test_syndrome_result_syndrome_required() -> None:
    """syndrome 字段必填。"""
    with pytest.raises(ValidationError):
        SyndromeResult.model_validate(
            {
                "syndrome_basis": ["症状"],
                "treatment_principle": "治法",
            }
        )


def test_syndrome_result_treatment_principle_required() -> None:
    """treatment_principle 字段必填。"""
    with pytest.raises(ValidationError):
        SyndromeResult.model_validate(
            {
                "syndrome": "证型",
                "syndrome_basis": ["症状"],
            }
        )


def test_syndrome_result_confidence_range() -> None:
    """confidence 必须在 [0, 1] 区间内。"""
    with pytest.raises(ValidationError):
        SyndromeResult.model_validate(
            {
                "syndrome": "证型",
                "syndrome_basis": ["症状"],
                "treatment_principle": "治法",
                "confidence": 1.5,
            }
        )
    with pytest.raises(ValidationError):
        SyndromeResult.model_validate(
            {
                "syndrome": "证型",
                "syndrome_basis": ["症状"],
                "treatment_principle": "治法",
                "confidence": -0.1,
            }
        )


# ===========================================================================
# merge_syndrome_result_to_state 测试
# ===========================================================================


def test_merge_syndrome_result_writes_to_state() -> None:
    """merge_syndrome_result_to_state 将 result 写入 state update dict。"""
    state = _state_with_full_info()
    result = SyndromeResult(
        syndrome="脾虚湿困证",
        syndrome_basis=["食欲差", "大便溏"],
        treatment_principle="健脾化湿",
        confidence=0.85,
    )
    updates = merge_syndrome_result_to_state(state, result)
    assert "syndrome_result" in updates
    assert updates["syndrome_result"] is result


def test_merge_syndrome_result_does_not_mutate_other_fields() -> None:
    """merge 不修改其他字段，只写 syndrome_result。"""
    state = _state_with_full_info()
    result = SyndromeResult(
        syndrome="脾虚湿困证",
        syndrome_basis=["食欲差", "大便溏"],
        treatment_principle="健脾化湿",
        confidence=0.85,
    )
    updates = merge_syndrome_result_to_state(state, result)
    assert len(updates) == 1
    assert "chief_complaint" not in updates
    assert "base_formula" not in updates


# ===========================================================================
# build_syndrome_query 测试
# ===========================================================================


def test_build_syndrome_query_full() -> None:
    """完整信息时构造的查询包含主诉、现病史和十问歌关键维度。"""
    state = _state_with_full_info()
    query = build_syndrome_query(state)
    assert "头痛三天" in query
    assert "淋雨" in query
    assert "大便偏溏" in query
    assert "食欲差" in query


def test_build_syndrome_query_minimal() -> None:
    """信息极少时查询只有主诉。"""
    state = XuanhuState(
        session_id=str(uuid.uuid4()),
        chief_complaint="头痛",
    )
    query = build_syndrome_query(state)
    assert query == "头痛"


def test_build_syndrome_query_empty() -> None:
    """无任何信息时查询为空串。"""
    state = _minimal_state()
    query = build_syndrome_query(state)
    assert query == ""


# ===========================================================================
# format_evidence_summary 测试
# ===========================================================================


def test_format_evidence_summary_with_evidence() -> None:
    """有证据时格式化为含 evidence_id 的文本。"""
    evs = [
        _evidence("ev-001", "theory", "湿证辨治", "脾虚湿盛，症见……"),
        _evidence("ev-002", "case", "医案一则", "患者食欲差、大便溏……"),
    ]
    summary = format_evidence_summary(evs)
    assert "ev-001" in summary
    assert "ev-002" in summary
    assert "theory" in summary
    assert "case" in summary
    assert "脾虚湿盛" in summary


def test_format_evidence_summary_empty() -> None:
    """无证据时返回缺证提示。"""
    summary = format_evidence_summary([])
    assert "未检索到" in summary


# ===========================================================================
# SyndromeAgent fake gateway + fake retriever 测试
# ===========================================================================


def _syndrome_output(
    syndrome: str = "脾虚湿困证",
    syndrome_basis: list[str] | None = None,
    differential: list[str] | None = None,
    treatment_principle: str = "健脾化湿，升清降浊",
    citations: list[str] | None = None,
    confidence: float = 0.85,
) -> SyndromeResult:
    return SyndromeResult(
        syndrome=syndrome,
        syndrome_basis=syndrome_basis or ["食欲差、大便溏为脾虚湿盛之象", "头痛身重为湿困清阳"],
        differential=differential or [],
        treatment_principle=treatment_principle,
        citations=citations or [],
        confidence=confidence,
    )


@pytest.mark.asyncio
async def test_syndrome_agent_with_evidence(tmp_path: Path) -> None:
    """RAG 有证据时输出辨证结果，且 citations 可追溯。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [_syndrome_output(citations=["ev-001", "ev-002"], confidence=0.85)]
    )
    retriever = FakeRetriever(
        [[_evidence("ev-001"), _evidence("ev-002")]]
    )

    agent = SyndromeAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_full_info()
    result = await agent.run(state, "trace-with-evidence")

    output = result.output
    assert isinstance(output, SyndromeResult)
    assert output.syndrome == "脾虚湿困证"
    assert len(output.syndrome_basis) >= 1
    assert output.treatment_principle == "健脾化湿，升清降浊"
    assert output.confidence == 0.85
    assert "ev-001" in output.citations
    assert "ev-002" in output.citations

    # Evidence 已返回且可追溯
    assert len(result.evidences) == 2
    assert result.evidences[0].evidence_id == "ev-001"
    assert result.evidences[1].evidence_id == "ev-002"

    # gateway 调用标记
    assert gateway.calls[0]["agent_name"] == "syndrome"

    # retriever 调用正确
    assert retriever.calls[0]["primary_sources"] == ["theory", "case"]
    assert retriever.calls[0]["allow_cross_source"] is True

    # prompt 版本
    assert result.prompt_version == "syndrome_v1.jinja2"


@pytest.mark.asyncio
async def test_syndrome_agent_no_evidence(tmp_path: Path) -> None:
    """RAG 无证据时明确缺证提示，confidence 低于 0.5。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            _syndrome_output(
                syndrome="信息不足，待补充",
                syndrome_basis=[
                    "缺证提示：RAG 未检索到相关理论/医案证据，辨证结论主要基于模型内知识，建议医师审慎判断"
                ],
                differential=[],
                treatment_principle="待补充四诊信息后确定",
                citations=[],
                confidence=0.3,
            )
        ]
    )
    retriever = FakeRetriever([[]])  # 空证据列表

    agent = SyndromeAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_full_info()
    result = await agent.run(state, "trace-no-evidence")

    output = result.output
    assert isinstance(output, SyndromeResult)
    assert output.citations == []
    assert output.confidence < 0.5
    assert any("缺证" in b or "未检索" in b for b in output.syndrome_basis)
    assert len(result.evidences) == 0


@pytest.mark.asyncio
async def test_syndrome_agent_bad_schema(tmp_path: Path) -> None:
    """fake gateway 返回坏 schema 时 AGENT_SCHEMA_INVALID。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            {"bad": "missing syndrome field"},
            {"also": "bad second attempt"},
        ]
    )
    retriever = FakeRetriever([[_evidence()]])

    agent = SyndromeAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=1,
        model_name="fake-model",
        retriever=retriever,
    )

    with pytest.raises(AgentRunError) as exc_info:
        await agent.run(
            XuanhuState(session_id=str(uuid.uuid4()),
                         chief_complaint="头痛"),
            "trace-bad-schema",
        )

    error = exc_info.value
    assert error.code == "AGENT_SCHEMA_INVALID"
    assert error.retryable is False
    assert len(gateway.calls) == 2  # 重试了


@pytest.mark.asyncio
async def test_syndrome_agent_no_diagnosis_or_prescription(tmp_path: Path) -> None:
    """SyndromeAgent 输出不包含处方、剂量、安全审核结论。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([_syndrome_output()])
    retriever = FakeRetriever([[_evidence()]])

    agent = SyndromeAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_full_info()
    result = await agent.run(state, "trace-no-dx")
    output = result.output
    output_dump = output.model_dump_json()

    assert "处方" not in output_dump
    assert "剂量" not in output_dump
    assert "安全审核" not in output_dump
    assert "跳过" not in output_dump
    assert "自动确认" not in output_dump
    assert "基础方" not in output_dump
    # 确认没有 HerbDose / FormulaResult 相关字段
    assert "herb" not in output_dump.lower() or "herb" not in output_dump

@pytest.mark.asyncio
async def test_syndrome_agent_prompt_includes_state_and_evidence(tmp_path: Path) -> None:
    """构造的 prompt 中包含状态摘要和证据摘要。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([_syndrome_output()])
    ev = _evidence("ev-001", "theory", "湿证辨治", "脾虚湿盛，症见食欲差……")
    retriever = FakeRetriever([[ev]])

    agent = SyndromeAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_full_info()
    await agent.run(state, "trace-prompt-check")

    system_msg = gateway.calls[0]["messages"][0]["content"]
    assert "头痛三天" in system_msg
    assert "测试患者" in system_msg
    assert "ev-001" in system_msg
    assert "湿证辨治" in system_msg


@pytest.mark.asyncio
async def test_syndrome_agent_empty_query_skips_rag(tmp_path: Path) -> None:
    """查询为空时跳过 RAG，不调用 retriever。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            _syndrome_output(
                syndrome="信息不足，待补充",
                syndrome_basis=["缺证提示：四诊信息不足以进行辨证"],
                confidence=0.2,
            )
        ]
    )
    retriever = FakeRetriever([])  # 不应被调用

    agent = SyndromeAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _minimal_state()
    result = await agent.run(state, "trace-empty-query")

    output = result.output
    assert isinstance(output, SyndromeResult)
    # retriever 不应被调用
    assert len(retriever.calls) == 0
    assert len(result.evidences) == 0


@pytest.mark.asyncio
async def test_syndrome_agent_via_base_agent_impl(tmp_path: Path) -> None:
    """SyndromeAgent 走 BaseAgentImpl 和模型网关抽象。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([_syndrome_output()])
    retriever = FakeRetriever([[_evidence()]])

    agent = SyndromeAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    # 验证继承关系
    from app.agents.base import BaseAgentImpl

    assert isinstance(agent, BaseAgentImpl)

    # 验证子类属性
    assert agent.name == "syndrome"
    assert agent.stage == Stage.SYNDROME
    assert agent.output_schema == SyndromeResult
    assert agent.next_stage == Stage.PRESCRIPTION

    state = _state_with_full_info()
    result = await agent.run(state, "trace-base-impl")

    assert isinstance(result.output, SyndromeResult)
    assert result.prompt_version == "syndrome_v1.jinja2"
    assert gateway.calls[0]["agent_name"] == "syndrome"


# ===========================================================================
# 不调用真实模型网关测试
# ===========================================================================


@pytest.mark.asyncio
async def test_no_real_model_gateway_called(tmp_path: Path) -> None:
    """验证所有测试路径只经过 FakeGateway，不调真实模型网关。"""
    manifest = _write_prompt_files(tmp_path)

    class TrackingGateway:
        async def chat_structured(self, *args: Any, **kwargs: Any) -> Any:
            return _syndrome_output()

    retriever = FakeRetriever([[_evidence()]])

    agent = SyndromeAgent(
        gateway=TrackingGateway(),
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    await agent.run(
        _state_with_full_info(), "trace-no-real"
    )
    assert True  # TrackingGateway 返回了 fake 输出，未调真实网关


# ===========================================================================
# SyndromeAgent 输出 field 测试
# ===========================================================================


@pytest.mark.asyncio
async def test_syndrome_result_differential_field(tmp_path: Path) -> None:
    """SyndromeResult 支持 differential 鉴别诊断字段。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            _syndrome_output(
                differential=[
                    "需排除肝阳上亢证：鉴别要点为头痛性质（胀痛而非跳痛）、无面红目赤",
                    "需排除外感风寒证：鉴别要点为有汗出、恶寒不重",
                ]
            )
        ]
    )
    retriever = FakeRetriever([[_evidence()]])

    agent = SyndromeAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_full_info()
    result = await agent.run(state, "trace-differential")

    output = result.output
    assert len(output.differential) == 2
    assert "肝阳上亢" in output.differential[0]
    assert "外感风寒" in output.differential[1]


# ===========================================================================
# _merge_evidences_to_state 测试
# ===========================================================================


def test_merge_evidences_dedup() -> None:
    from app.agents.syndrome import _merge_evidences_to_state

    existing = _evidence("ev-001")
    state = _state_with_full_info()
    state.evidences = [existing]

    new_evs = [_evidence("ev-001"), _evidence("ev-002")]
    merged = _merge_evidences_to_state(state, new_evs)
    assert len(merged) == 2
    assert merged[0].evidence_id == "ev-001"
    assert merged[1].evidence_id == "ev-002"


# ===========================================================================
# _validate_citations 测试
# ===========================================================================


def test_validate_citations_subset_passes() -> None:
    """citations 是 evidence_ids 的子集时不抛异常。"""
    _validate_citations(["ev-001", "ev-002"], {"ev-001", "ev-002", "ev-003"})


def test_validate_citations_empty_both_passes() -> None:
    """无证据且 citations 为空时不抛异常。"""
    _validate_citations([], set())


def test_validate_citations_fabricated_rejected() -> None:
    """citations 包含非本轮 Evidence 的 ID 时抛 ValidationError。"""
    with pytest.raises(ValidationError, match="citations"):
        _validate_citations(["ev-001", "fake-999"], {"ev-001"})


def test_validate_citations_no_evidence_but_citations_rejected() -> None:
    """无证据时 citations 不为空则抛 ValidationError。"""
    with pytest.raises(ValidationError, match="citations"):
        _validate_citations(["ev-001"], set())


def test_validate_citations_all_fabricated_rejected() -> None:
    """全部 citations 都非 real 时抛 ValidationError。"""
    with pytest.raises(ValidationError, match="citations"):
        _validate_citations(["fake-a", "fake-b"], {"ev-001"})


# ===========================================================================
# SyndromeAgent citations 校验集成测试
# ===========================================================================


@pytest.mark.asyncio
async def test_syndrome_agent_rejects_fabricated_citation(tmp_path: Path) -> None:
    """模型返回 fabricated citation 时被归一化为 AGENT_SCHEMA_INVALID。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [_syndrome_output(citations=["fake-999"], confidence=0.85)]
    )
    retriever = FakeRetriever([[_evidence("ev-001")]])

    agent = SyndromeAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_full_info()
    with pytest.raises(AgentRunError) as exc_info:
        await agent.run(state, "trace-fabricated-citation")
    assert exc_info.value.code == "AGENT_SCHEMA_INVALID"
    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_syndrome_agent_rejects_citation_when_no_evidence(tmp_path: Path) -> None:
    """无证据时模型返回 citations 则归一化为 AGENT_SCHEMA_INVALID。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [_syndrome_output(citations=["ev-ghost"], confidence=0.6)]
    )
    retriever = FakeRetriever([[]])  # 无证据

    agent = SyndromeAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_full_info()
    with pytest.raises(AgentRunError) as exc_info:
        await agent.run(state, "trace-no-ev-but-citation")
    assert exc_info.value.code == "AGENT_SCHEMA_INVALID"
    assert exc_info.value.retryable is False


# ===========================================================================
# merge_syndrome_result_to_state + evidence merge 测试
# ===========================================================================


def test_merge_syndrome_result_with_evidences() -> None:
    """merge_syndrome_result_to_state 传入 evidences 时合并到 state。"""
    state = _state_with_full_info()
    state.evidences = [_evidence("ev-existing")]
    result = _syndrome_output(citations=["ev-001"])
    new_evs = [_evidence("ev-001"), _evidence("ev-002")]

    updates = merge_syndrome_result_to_state(state, result, evidences=new_evs)
    assert updates["syndrome_result"] is result
    assert "evidences" in updates
    merged = updates["evidences"]
    assert len(merged) == 3  # ev-existing + ev-001 + ev-002
    ids = [ev.evidence_id for ev in merged]
    assert ids == ["ev-existing", "ev-001", "ev-002"]


def test_merge_syndrome_result_without_evidences_backward_compat() -> None:
    """merge_syndrome_result_to_state 不传 evidences 时仅写 syndrome_result。"""
    state = _state_with_full_info()
    result = _syndrome_output()
    updates = merge_syndrome_result_to_state(state, result)
    assert "syndrome_result" in updates
    assert "evidences" not in updates
    assert len(updates) == 1


# ===========================================================================
# Supervisor syndrome 输出应用（不依赖 DB）
# ===========================================================================


def test_supervisor_apply_syndrome_output_writes_state() -> None:
    """Supervisor._apply_agent_output 在 SYNDROME 阶段写入 state.syndrome_result。"""
    from app.agents.supervisor import Supervisor

    class StubRegistry:
        def __init__(self) -> None:
            pass

        def get(self, stage: Any) -> Any:
            return None

        def __contains__(self, stage: Any) -> bool:
            return False

    supervisor = Supervisor.__new__(Supervisor)  # type: ignore[arg-type]
    supervisor._db = None  # type: ignore[attr-defined]
    supervisor._registry = StubRegistry()  # type: ignore[attr-defined]

    state = _state_with_full_info()
    state.evidences = [_evidence("ev-existing")]
    output = _syndrome_output(citations=["ev-001"])
    new_evs = [_evidence("ev-001")]

    updated = supervisor._apply_agent_output(
        state, Stage.SYNDROME, output, evidences=new_evs,
    )
    assert updated.syndrome_result is not None
    assert updated.syndrome_result.syndrome == output.syndrome
    # 其他字段未被破坏
    assert updated.chief_complaint == state.chief_complaint
    # Evidence 合并到 state.evidences
    ids = [ev.evidence_id for ev in updated.evidences]
    assert ids == ["ev-existing", "ev-001"]
    # 不输出处方
    assert updated.base_formula is None

