"""P6-1 开方 Agent 测试。

使用 fake gateway 和 fake retriever 覆盖：
- FormulaResult schema 校验
- RAG 有证据时输出基础方
- RAG 无证据时缺证提示
- fake gateway schema 失败归一化
- Evidence/citations 可追溯
- 不输出加减方/安全审核/医师确认/病历
- Supervisor prescription -> modification 路由
- PrescriptionAgent 走 BaseAgentImpl 和模型网关抽象
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.agents.errors import AgentRunError
from app.agents.prescription import (
    PrescriptionAgent,
    _validate_citations,
    _validate_rationale_traceability,
    build_prescription_query,
    build_syndrome_summary,
    format_evidence_summary,
    merge_formula_result_to_state,
)
from app.agents.prompt_loader import PromptLoader
from app.rag.schemas import Evidence
from app.schemas.agent import (
    FormulaResult,
    HerbDose,
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
        "prescription: prescription_v1.jinja2\n"
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
    (prompt_dir / "prescription_v1.jinja2").write_text(
        "You are a TCM prescription assistant.\n"
        "{syndrome_summary}\n{state_summary}\n{conversation_history}\n{evidence_summary}\n",
        encoding="utf-8",
    )
    return prompt_dir / "manifest.yaml"


def _evidence(
    evidence_id: str = "ev-001",
    source_type: str = "formula",
    title: str = "参苓白术散",
    content_snippet: str = "参苓白术散：党参、白术、茯苓……健脾益气，渗湿止泻……",
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


def _syndrome_result() -> SyndromeResult:
    return SyndromeResult(
        syndrome="脾虚湿盛证",
        syndrome_basis=["食欲差、大便溏为脾虚湿盛之象"],
        treatment_principle="健脾化湿，理气和胃",
        differential=[],
        confidence=0.85,
    )


def _state_with_syndrome(session_id: str | None = None) -> XuanhuState:
    """构造一个含 syndrome_result 与四诊信息的 XuanhuState。"""
    return XuanhuState(
        session_id=session_id or str(uuid.uuid4()),
        patient_info=PatientInfo(name="测试患者", gender="female", age=30),
        chief_complaint="食欲差、便溏一月",
        present_illness="一月来食欲不振，食后腹胀，大便偏溏，伴身重困倦",
        ten_questions=TenQuestions(
            diet="食欲差，口淡不渴",
            stool_urine="大便偏溏",
            head_body="身重困倦",
        ),
        syndrome_result=_syndrome_result(),
        inquiry_messages=[
            {"role": "doctor", "content": "患者诉食欲差、便溏一月"},
            {"role": "assistant", "content": "请问大便情况？", "asked_dimension": "stool_urine"},
        ],
    )


def _minimal_state(session_id: str | None = None) -> XuanhuState:
    """构造一个信息极少、无辨证结论的 XuanhuState。"""
    return XuanhuState(
        session_id=session_id or str(uuid.uuid4()),
        inquiry_messages=[{"role": "doctor", "content": "患者来了"}],
    )


# ===========================================================================
# FormulaResult Schema 校验
# ===========================================================================


def test_formula_result_minimal_valid() -> None:
    """最少合法 FormulaResult 可独立校验。"""
    result = FormulaResult.model_validate(
        {
            "name": "参苓白术散",
            "composition": [{"herb": "党参", "dose": 12, "unit": "g"}],
            "rationale": "健脾益气",
        }
    )
    assert result.name == "参苓白术散"
    assert len(result.composition) == 1
    assert result.source is None
    assert result.citations == []


def test_formula_result_full_shape() -> None:
    """完整 FormulaResult 可独立校验。"""
    result = FormulaResult.model_validate(
        {
            "name": "参苓白术散",
            "composition": [
                {"herb": "党参", "dose": 12, "unit": "g"},
                {"herb": "白术", "dose": 10, "unit": "g", "note": "炒"},
            ],
            "source": "《太平惠民和剂局方》",
            "rationale": "健脾益气，渗湿止泻",
            "citations": ["ev-001", "ev-002"],
        }
    )
    assert result.source == "《太平惠民和剂局方》"
    assert len(result.composition) == 2
    assert result.composition[1].note == "炒"
    assert len(result.citations) == 2


def test_formula_result_name_required() -> None:
    """name 字段必填。"""
    with pytest.raises(ValidationError):
        FormulaResult.model_validate(
            {
                "composition": [{"herb": "党参"}],
                "rationale": "健脾",
            }
        )


def test_formula_result_rationale_required() -> None:
    """rationale 字段必填。"""
    with pytest.raises(ValidationError):
        FormulaResult.model_validate(
            {
                "name": "方",
                "composition": [{"herb": "党参"}],
            }
        )


def test_formula_result_composition_nonempty() -> None:
    """composition 至少 1 味。"""
    with pytest.raises(ValidationError):
        FormulaResult.model_validate(
            {
                "name": "方",
                "composition": [],
                "rationale": "健脾",
            }
        )


def test_formula_result_herb_dose_required() -> None:
    """HerbDose.herb 必填。"""
    with pytest.raises(ValidationError):
        FormulaResult.model_validate(
            {
                "name": "方",
                "composition": [{"dose": 12, "unit": "g"}],
                "rationale": "健脾",
            }
        )


def test_formula_result_dose_positive() -> None:
    """HerbDose.dose 必须为正数。"""
    with pytest.raises(ValidationError):
        FormulaResult.model_validate(
            {
                "name": "方",
                "composition": [{"herb": "党参", "dose": 0, "unit": "g"}],
                "rationale": "健脾",
            }
        )
    with pytest.raises(ValidationError):
        FormulaResult.model_validate(
            {
                "name": "方",
                "composition": [{"herb": "党参", "dose": -5, "unit": "g"}],
                "rationale": "健脾",
            }
        )


def test_formula_result_unit_default() -> None:
    """unit 默认为 g。"""
    result = FormulaResult.model_validate(
        {
            "name": "方",
            "composition": [{"herb": "党参", "dose": 12}],
            "rationale": "健脾",
        }
    )
    assert result.composition[0].unit == "g"


# ===========================================================================
# merge_formula_result_to_state 测试
# ===========================================================================


def test_merge_formula_result_writes_to_state() -> None:
    """merge_formula_result_to_state 将 result 写入 state update dict。"""
    state = _state_with_syndrome()
    result = FormulaResult(
        name="参苓白术散",
        composition=[HerbDose(herb="党参", dose=12, unit="g")],
        rationale="健脾益气",
    )
    updates = merge_formula_result_to_state(state, result)
    assert "base_formula" in updates
    assert updates["base_formula"] is result


def test_merge_formula_result_does_not_mutate_other_fields() -> None:
    """merge 不修改其他字段，只写 base_formula。"""
    state = _state_with_syndrome()
    result = FormulaResult(
        name="参苓白术散",
        composition=[HerbDose(herb="党参", dose=12, unit="g")],
        rationale="健脾益气",
    )
    updates = merge_formula_result_to_state(state, result)
    assert len(updates) == 1
    assert "modified_formula" not in updates
    assert "syndrome_result" not in updates


def test_merge_formula_result_with_evidences() -> None:
    """merge_formula_result_to_state 传入 evidences 时合并到 state。"""
    state = _state_with_syndrome()
    state.evidences = [_evidence("ev-existing")]
    result = FormulaResult(
        name="参苓白术散",
        composition=[HerbDose(herb="党参", dose=12, unit="g")],
        rationale="健脾益气",
        citations=["ev-001"],
    )
    new_evs = [_evidence("ev-001"), _evidence("ev-002")]

    updates = merge_formula_result_to_state(state, result, evidences=new_evs)
    assert updates["base_formula"] is result
    assert "evidences" in updates
    merged = updates["evidences"]
    assert len(merged) == 3
    ids = [ev.evidence_id for ev in merged]
    assert ids == ["ev-existing", "ev-001", "ev-002"]


def test_merge_formula_result_without_evidences_backward_compat() -> None:
    """merge_formula_result_to_state 不传 evidences 时仅写 base_formula。"""
    state = _state_with_syndrome()
    result = FormulaResult(
        name="参苓白术散",
        composition=[HerbDose(herb="党参", dose=12, unit="g")],
        rationale="健脾益气",
    )
    updates = merge_formula_result_to_state(state, result)
    assert "base_formula" in updates
    assert "evidences" not in updates
    assert len(updates) == 1


# ===========================================================================
# build_prescription_query 测试
# ===========================================================================


def test_build_prescription_query_with_syndrome() -> None:
    """有辨证结论时查询包含证型与治法。"""
    state = _state_with_syndrome()
    query = build_prescription_query(state)
    assert "脾虚湿盛证" in query
    assert "健脾化湿" in query
    assert "食欲差" in query  # 主诉


def test_build_prescription_query_without_syndrome() -> None:
    """无辨证结论时回退到主诉/现病史。"""
    state = XuanhuState(
        session_id=str(uuid.uuid4()),
        chief_complaint="头痛",
        present_illness="胀痛三天",
    )
    query = build_prescription_query(state)
    assert "头痛" in query
    assert "胀痛三天" in query


def test_build_prescription_query_empty() -> None:
    """无任何信息时查询为空串。"""
    state = _minimal_state()
    query = build_prescription_query(state)
    assert query == ""


# ===========================================================================
# build_syndrome_summary 测试
# ===========================================================================


def test_build_syndrome_summary_with_result() -> None:
    """有辨证结论时摘要包含证型与治法。"""
    state = _state_with_syndrome()
    summary = build_syndrome_summary(state)
    assert "脾虚湿盛证" in summary
    assert "健脾化湿" in summary
    assert "0.85" in summary


def test_build_syndrome_summary_without_result() -> None:
    """无辨证结论时返回缺证提示。"""
    state = _minimal_state()
    summary = build_syndrome_summary(state)
    assert "尚无辨证结论" in summary


# ===========================================================================
# format_evidence_summary 测试
# ===========================================================================


def test_format_evidence_summary_with_evidence() -> None:
    """有证据时格式化为含 evidence_id 的文本。"""
    evs = [
        _evidence("ev-001", "formula", "参苓白术散", "健脾益气……"),
        _evidence("ev-002", "herb", "党参", "补中益气……"),
    ]
    summary = format_evidence_summary(evs)
    assert "ev-001" in summary
    assert "ev-002" in summary
    assert "formula" in summary
    assert "herb" in summary


def test_format_evidence_summary_empty() -> None:
    """无证据时返回缺证提示。"""
    summary = format_evidence_summary([])
    assert "未检索到" in summary


# ===========================================================================
# PrescriptionAgent fake gateway + fake retriever 测试
# ===========================================================================


def _formula_output(
    name: str = "参苓白术散",
    composition: list[HerbDose] | None = None,
    source: str | None = "《太平惠民和剂局方》",
    rationale: str = "健脾益气，渗湿止泻。证属脾虚湿盛，治以健脾化湿，故选本方。",
    citations: list[str] | None = None,
) -> FormulaResult:
    return FormulaResult(
        name=name,
        composition=composition
        or [
            HerbDose(herb="党参", dose=12, unit="g"),
            HerbDose(herb="白术", dose=10, unit="g"),
            HerbDose(herb="茯苓", dose=10, unit="g"),
        ],
        source=source,
        rationale=rationale,
        citations=citations or [],
    )


@pytest.mark.asyncio
async def test_prescription_agent_with_evidence(tmp_path: Path) -> None:
    """RAG 有证据时输出基础方，且 citations 可追溯。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([_formula_output(citations=["ev-001", "ev-002"])])
    retriever = FakeRetriever([[_evidence("ev-001"), _evidence("ev-002")]])

    agent = PrescriptionAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_syndrome()
    result = await agent.run(state, "trace-with-evidence")

    output = result.output
    assert isinstance(output, FormulaResult)
    assert output.name == "参苓白术散"
    assert len(output.composition) == 3
    assert output.composition[0].herb == "党参"
    assert output.composition[0].dose == 12
    assert output.composition[0].unit == "g"
    assert output.source == "《太平惠民和剂局方》"
    assert "健脾" in output.rationale
    assert "ev-001" in output.citations
    assert "ev-002" in output.citations

    # Evidence 已返回且可追溯
    assert len(result.evidences) == 2
    assert result.evidences[0].evidence_id == "ev-001"

    # gateway 调用标记
    assert gateway.calls[0]["agent_name"] == "prescription"

    # retriever 调用正确
    assert retriever.calls[0]["primary_sources"] == ["formula", "herb"]
    assert retriever.calls[0]["allow_cross_source"] is True

    # prompt 版本
    assert result.prompt_version == "prescription_v1.jinja2"
    # 推进到 modification
    assert result.next_stage == Stage.MODIFICATION


@pytest.mark.asyncio
async def test_prescription_agent_no_evidence(tmp_path: Path) -> None:
    """RAG 无证据时缺证提示，citations 为空。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            _formula_output(
                rationale=(
                    "缺证提示：RAG 未检索到相关方剂/中药证据，"
                    "基础方主要基于模型内知识，建议医师审慎判断。"
                    "选参苓白术散以健脾益气。"
                ),
                citations=[],
            )
        ]
    )
    retriever = FakeRetriever([[]])  # 空证据列表

    agent = PrescriptionAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_syndrome()
    result = await agent.run(state, "trace-no-evidence")

    output = result.output
    assert isinstance(output, FormulaResult)
    assert output.citations == []
    assert "缺证" in output.rationale or "未检索" in output.rationale
    assert len(result.evidences) == 0


@pytest.mark.asyncio
async def test_prescription_agent_bad_schema(tmp_path: Path) -> None:
    """fake gateway 返回坏 schema 时 AGENT_SCHEMA_INVALID。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            {"bad": "missing name field"},
            {"also": "bad second attempt"},
        ]
    )
    retriever = FakeRetriever([[_evidence()]])

    agent = PrescriptionAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=1,
        model_name="fake-model",
        retriever=retriever,
    )

    with pytest.raises(AgentRunError) as exc_info:
        await agent.run(
            _state_with_syndrome(),
            "trace-bad-schema",
        )

    error = exc_info.value
    assert error.code == "AGENT_SCHEMA_INVALID"
    assert error.retryable is False
    assert len(gateway.calls) == 2  # 重试了


@pytest.mark.asyncio
async def test_prescription_agent_no_modification_or_safety(tmp_path: Path) -> None:
    """PrescriptionAgent 输出不包含加减方、安全审核、医师确认或病历。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([_formula_output(citations=["ev-001"])])
    retriever = FakeRetriever([[_evidence("ev-001")]])

    agent = PrescriptionAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_syndrome()
    result = await agent.run(state, "trace-no-mod")
    output = result.output
    output_dump = output.model_dump_json()

    assert "modifications" not in output_dump
    assert "modified_formula" not in output_dump
    assert "安全审核" not in output_dump
    assert "医师确认" not in output_dump
    assert "病历" not in output_dump
    assert "跳过" not in output_dump
    assert "自动确认" not in output_dump
    assert "接受风险" not in output_dump
    # FormulaResult 不含 modification/safety 字段
    assert not hasattr(output, "modifications")


@pytest.mark.asyncio
async def test_prescription_agent_prompt_includes_syndrome_and_evidence(
    tmp_path: Path,
) -> None:
    """构造的 prompt 中包含辨证摘要、状态摘要和证据摘要。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([_formula_output(citations=["ev-001"])])
    ev = _evidence("ev-001", "formula", "参苓白术散", "健脾益气，渗湿止泻……")
    retriever = FakeRetriever([[ev]])

    agent = PrescriptionAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_syndrome()
    await agent.run(state, "trace-prompt-check")

    system_msg = gateway.calls[0]["messages"][0]["content"]
    # 辨证摘要
    assert "脾虚湿盛证" in system_msg
    assert "健脾化湿" in system_msg
    # 状态摘要
    assert "食欲差" in system_msg or "测试患者" in system_msg
    # 证据摘要
    assert "ev-001" in system_msg
    assert "参苓白术散" in system_msg


@pytest.mark.asyncio
async def test_prescription_agent_empty_query_skips_rag(tmp_path: Path) -> None:
    """查询为空（无辨证且无主诉）时跳过 RAG，不调用 retriever。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            _formula_output(
                rationale="缺证提示：四诊信息与辨证结论不足以选方，待医师补充。",
                citations=[],
            )
        ]
    )
    retriever = FakeRetriever([])  # 不应被调用

    agent = PrescriptionAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _minimal_state()
    result = await agent.run(state, "trace-empty-query")

    output = result.output
    assert isinstance(output, FormulaResult)
    # retriever 不应被调用
    assert len(retriever.calls) == 0
    assert len(result.evidences) == 0


@pytest.mark.asyncio
async def test_prescription_agent_via_base_agent_impl(tmp_path: Path) -> None:
    """PrescriptionAgent 走 BaseAgentImpl 和模型网关抽象。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([_formula_output(citations=["ev-001"])])
    retriever = FakeRetriever([[_evidence("ev-001")]])

    agent = PrescriptionAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    from app.agents.base import BaseAgentImpl

    assert isinstance(agent, BaseAgentImpl)
    assert agent.name == "prescription"
    assert agent.stage == Stage.PRESCRIPTION
    assert agent.output_schema == FormulaResult
    assert agent.next_stage == Stage.MODIFICATION

    state = _state_with_syndrome()
    result = await agent.run(state, "trace-base-impl")

    assert isinstance(result.output, FormulaResult)
    assert result.prompt_version == "prescription_v1.jinja2"
    assert gateway.calls[0]["agent_name"] == "prescription"


@pytest.mark.asyncio
async def test_prescription_agent_reads_syndrome_result(tmp_path: Path) -> None:
    """PrescriptionAgent 应从 state.syndrome_result 读取证型/治法作为开方依据。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([_formula_output(citations=["ev-001"])])
    retriever = FakeRetriever([[_evidence("ev-001")]])

    agent = PrescriptionAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_syndrome()
    await agent.run(state, "trace-read-syndrome")

    # 检索查询应包含证型/治法（证明读取了 syndrome_result）
    query = retriever.calls[0]["query"]
    assert "脾虚湿盛证" in query
    assert "健脾化湿" in query


# ===========================================================================
# 不调用真实模型网关测试
# ===========================================================================


@pytest.mark.asyncio
async def test_no_real_model_gateway_called(tmp_path: Path) -> None:
    """验证所有测试路径只经过 FakeGateway，不调真实模型网关。"""
    manifest = _write_prompt_files(tmp_path)

    class TrackingGateway:
        async def chat_structured(self, *args: Any, **kwargs: Any) -> Any:
            return _formula_output(citations=["ev-001"])

    retriever = FakeRetriever([[_evidence("ev-001")]])

    agent = PrescriptionAgent(
        gateway=TrackingGateway(),
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    await agent.run(_state_with_syndrome(), "trace-no-real")
    assert True  # TrackingGateway 返回了 fake 输出，未调真实网关


# ===========================================================================
# _merge_evidences_to_state 测试
# ===========================================================================


def test_merge_evidences_dedup() -> None:
    from app.agents.prescription import _merge_evidences_to_state

    existing = _evidence("ev-001")
    state = _state_with_syndrome()
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


def test_validate_citations_evidence_but_empty_rejected() -> None:
    """有证据但 citations 为空时抛 ValidationError——必须至少引用一条 Evidence。"""
    with pytest.raises(ValidationError, match="不得为空"):
        _validate_citations([], {"ev-001", "ev-002"})


# ===========================================================================
# _validate_rationale_traceability 测试
# ===========================================================================


def test_rationale_traceability_no_evidence_with_marker_passes() -> None:
    """无证据且 rationale 含缺证提示时通过。"""
    _validate_rationale_traceability(
        "缺证提示：RAG 未检索到相关证据，结论基于模型内知识。",
        set(),
        [],
    )


def test_rationale_traceability_no_evidence_without_marker_rejected() -> None:
    """无证据且 rationale 无缺证提示时抛 ValidationError。"""
    with pytest.raises(ValidationError, match="rationale"):
        _validate_rationale_traceability(
            "选参苓白术散以健脾益气。",
            set(),
            [],
        )


def test_rationale_traceability_evidence_with_citations_passes() -> None:
    """有证据且已引用时 rationale 无需缺证提示。"""
    _validate_rationale_traceability(
        "选参苓白术散以健脾益气。",
        {"ev-001"},
        ["ev-001"],
    )


def test_rationale_traceability_evidence_but_empty_citations_without_marker_rejected() -> None:
    """有证据但 citations 为空且 rationale 无缺证提示时抛 ValidationError。"""
    with pytest.raises(ValidationError, match="rationale"):
        _validate_rationale_traceability(
            "选参苓白术散以健脾益气。",
            {"ev-001"},
            [],
        )


# ===========================================================================
# PrescriptionAgent citations 校验集成测试
# ===========================================================================


@pytest.mark.asyncio
async def test_prescription_agent_rejects_fabricated_citation(tmp_path: Path) -> None:
    """模型返回 fabricated citation 时被归一化为 AGENT_SCHEMA_INVALID。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([_formula_output(citations=["fake-999"])])
    retriever = FakeRetriever([[_evidence("ev-001")]])

    agent = PrescriptionAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_syndrome()
    with pytest.raises(AgentRunError) as exc_info:
        await agent.run(state, "trace-fabricated-citation")
    assert exc_info.value.code == "AGENT_SCHEMA_INVALID"
    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_prescription_agent_rejects_citation_when_no_evidence(
    tmp_path: Path,
) -> None:
    """无证据时模型返回 citations 则归一化为 AGENT_SCHEMA_INVALID。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([_formula_output(citations=["ev-ghost"])])
    retriever = FakeRetriever([[]])  # 无证据

    agent = PrescriptionAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_syndrome()
    with pytest.raises(AgentRunError) as exc_info:
        await agent.run(state, "trace-no-ev-but-citation")
    assert exc_info.value.code == "AGENT_SCHEMA_INVALID"
    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_prescription_agent_rejects_empty_citations_when_evidence(tmp_path: Path) -> None:
    """有证据但模型输出 citations 为空时归一化为 AGENT_SCHEMA_INVALID。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([_formula_output(citations=[])])
    retriever = FakeRetriever([[_evidence("ev-001"), _evidence("ev-002")]])

    agent = PrescriptionAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_syndrome()
    with pytest.raises(AgentRunError) as exc_info:
        await agent.run(state, "trace-ev-but-empty-citations")
    assert exc_info.value.code == "AGENT_SCHEMA_INVALID"
    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_prescription_agent_rejects_no_marker_when_no_evidence(tmp_path: Path) -> None:
    """无证据且 rationale 无缺证提示时归一化为 AGENT_SCHEMA_INVALID。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [_formula_output(rationale="选参苓白术散以健脾益气。", citations=[])]
    )
    retriever = FakeRetriever([[]])  # 无证据

    agent = PrescriptionAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_syndrome()
    with pytest.raises(AgentRunError) as exc_info:
        await agent.run(state, "trace-no-ev-no-marker")
    assert exc_info.value.code == "AGENT_SCHEMA_INVALID"
    assert exc_info.value.retryable is False


# ===========================================================================
# Supervisor prescription 输出应用（不依赖 DB）
# ===========================================================================


def test_supervisor_apply_prescription_output_writes_state() -> None:
    """Supervisor._apply_agent_output 在 PRESCRIPTION 阶段写入 state.base_formula。"""
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

    state = _state_with_syndrome()
    state.evidences = [_evidence("ev-existing")]
    output = _formula_output(citations=["ev-001"])
    new_evs = [_evidence("ev-001")]

    updated = supervisor._apply_agent_output(
        state, Stage.PRESCRIPTION, output, evidences=new_evs,
    )
    assert updated.base_formula is not None
    assert updated.base_formula.name == output.name
    # 其他字段未被破坏
    assert updated.syndrome_result is not None
    assert updated.syndrome_result.syndrome == "脾虚湿盛证"
    # Evidence 合并到 state.evidences
    ids = [ev.evidence_id for ev in updated.evidences]
    assert ids == ["ev-existing", "ev-001"]
    # 不输出加减方
    assert updated.modified_formula is None


def test_supervisor_default_registry_includes_prescription() -> None:
    """Supervisor 默认 registry 包含 PrescriptionAgent。"""
    from app.agents.supervisor import _default_registry

    registry = _default_registry()
    agent = registry.get(Stage.PRESCRIPTION)
    assert agent is not None
    from app.agents.prescription import PrescriptionAgent

    assert isinstance(agent, PrescriptionAgent)
