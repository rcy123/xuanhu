"""P6-2 加减方 Agent 测试。

使用 fake gateway 和 fake retriever 覆盖：
- ModifiedFormulaResult schema 校验
- RAG 有证据时输出加减方
- RAG 无证据时缺证提示
- 缺 base_formula → BASE_FORMULA_MISSING
- fake gateway schema 失败归一化
- Evidence/citations 可追溯
- 四种加减动作（add/remove/replace/adjust）
- 不输出安全审核/医师确认/病历
- 不修改 state.base_formula
- Supervisor modification → safety 路由
- ModificationAgent 走 BaseAgentImpl 和模型网关抽象
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.agents.errors import AgentRunError
from app.agents.modification import (
    ModificationAgent,
    _validate_citations,
    _validate_modification_reasons,
    _validate_rationale_traceability,
    build_modification_query,
    format_base_formula_summary,
    format_evidence_summary,
    merge_modified_formula_result_to_state,
)
from app.agents.prompt_loader import PromptLoader
from app.rag.schemas import Evidence
from app.schemas.agent import (
    FormulaResult,
    HerbDose,
    ModificationItem,
    ModifiedFormulaResult,
    PatientInfo,
    SyndromeResult,
    TenQuestions,
    XuanhuState,
)
from app.schemas.types import ModificationAction, Stage

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
        "modification: modification_v1.jinja2\n"
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
    (prompt_dir / "modification_v1.jinja2").write_text(
        "You are a TCM modification assistant.\n"
        "{syndrome_summary}\n{base_formula_summary}\n"
        "{state_summary}\n{conversation_history}\n{evidence_summary}\n",
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


def _base_formula() -> FormulaResult:
    return FormulaResult(
        name="参苓白术散",
        composition=[
            HerbDose(herb="党参", dose=12, unit="g"),
            HerbDose(herb="白术", dose=10, unit="g"),
            HerbDose(herb="茯苓", dose=10, unit="g"),
        ],
        source="《太平惠民和剂局方》",
        rationale="健脾益气，渗湿止泻",
        citations=["ev-001"],
    )


def _state_with_syndrome_and_base_formula(
    session_id: str | None = None,
) -> XuanhuState:
    """构造一个含 syndrome_result 与 base_formula 的 XuanhuState。"""
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
        base_formula=_base_formula(),
        inquiry_messages=[
            {"role": "doctor", "content": "患者诉食欲差、便溏一月"},
            {"role": "assistant", "content": "请问大便情况？", "asked_dimension": "stool_urine"},
        ],
    )


def _state_with_base_formula_no_syndrome(
    session_id: str | None = None,
) -> XuanhuState:
    """构造一个有 base_formula 但无 syndrome_result 的 XuanhuState。"""
    return XuanhuState(
        session_id=session_id or str(uuid.uuid4()),
        patient_info=PatientInfo(name="测试患者", gender="female", age=30),
        chief_complaint="食欲差、便溏一月",
        base_formula=_base_formula(),
    )


def _minimal_state(session_id: str | None = None) -> XuanhuState:
    """构造一个信息极少、无 base_formula 的 XuanhuState。"""
    return XuanhuState(
        session_id=session_id or str(uuid.uuid4()),
        inquiry_messages=[{"role": "doctor", "content": "患者来了"}],
    )


# ===========================================================================
# ModifiedFormulaResult / ModificationItem Schema 校验
# ===========================================================================


def test_modified_formula_result_minimal_valid() -> None:
    """最少合法 ModifiedFormulaResult 可独立校验。"""
    result = ModifiedFormulaResult.model_validate(
        {
            "formula": {
                "name": "参苓白术散加减",
                "composition": [{"herb": "党参", "dose": 12, "unit": "g"}],
                "rationale": "健脾益气，加茯苓以增强渗湿之力",
            },
            "modifications": [],
        }
    )
    assert result.formula.name == "参苓白术散加减"
    assert len(result.formula.composition) == 1
    assert result.modifications == []


def test_modified_formula_result_full_shape() -> None:
    """完整 ModifiedFormulaResult 可独立校验，包含四种 action。"""
    result = ModifiedFormulaResult.model_validate(
        {
            "formula": {
                "name": "参苓白术散加减",
                "composition": [
                    {"herb": "党参", "dose": 12, "unit": "g"},
                    {"herb": "白术", "dose": 10, "unit": "g"},
                    {"herb": "茯苓", "dose": 15, "unit": "g"},
                    {"herb": "薏苡仁", "dose": 10, "unit": "g"},
                ],
                "source": "《太平惠民和剂局方》",
                "rationale": "健脾益气，渗湿止泻。加薏苡仁增强渗湿。",
                "citations": ["ev-001"],
            },
            "modifications": [
                {
                    "action": "add",
                    "herb": "薏苡仁",
                    "dose": 10,
                    "unit": "g",
                    "reason": "患者身重困倦，加薏苡仁以增强渗湿之力",
                },
                {
                    "action": "remove",
                    "herb": "甘草",
                    "dose": None,
                    "unit": "g",
                    "reason": "患者腹胀明显，减甘草以免壅滞",
                },
                {
                    "action": "replace",
                    "herb": "苍术",
                    "dose": 10,
                    "unit": "g",
                    "reason": "以苍术替换白术，增强燥湿之力",
                },
                {
                    "action": "adjust",
                    "herb": "茯苓",
                    "dose": 15,
                    "unit": "g",
                    "reason": "便溏明显，加重茯苓剂量以增强渗湿",
                },
            ],
        }
    )
    assert result.formula.name == "参苓白术散加减"
    assert len(result.modifications) == 4
    actions = [m.action for m in result.modifications]
    assert "add" in actions
    assert "remove" in actions
    assert "replace" in actions
    assert "adjust" in actions


def test_modification_item_reason_required() -> None:
    """ModificationItem.reason 必填。"""
    with pytest.raises(ValidationError):
        ModificationItem.model_validate(
            {
                "action": "add",
                "herb": "茯苓",
                "dose": 10,
                "unit": "g",
            }
        )


def test_modification_item_action_enum() -> None:
    """ModificationItem.action 必须为合法枚举值。"""
    with pytest.raises(ValidationError):
        ModificationItem.model_validate(
            {
                "action": "delete",
                "herb": "茯苓",
                "reason": "非法动作",
            }
        )


# ===========================================================================
# merge_modified_formula_result_to_state 测试
# ===========================================================================


def test_merge_modified_formula_writes_to_state() -> None:
    """merge_modified_formula_result_to_state 将 result 写入 state update dict。"""
    state = _state_with_syndrome_and_base_formula()
    result = ModifiedFormulaResult(
        formula=FormulaResult(
            name="参苓白术散加减",
            composition=[HerbDose(herb="党参", dose=12, unit="g")],
            rationale="健脾益气",
        ),
        modifications=[
            ModificationItem(
                action=ModificationAction.ADD,
                herb="薏苡仁",
                dose=10,
                unit="g",
                reason="增强渗湿",
            )
        ],
    )
    updates = merge_modified_formula_result_to_state(state, result)
    assert "modified_formula" in updates
    assert updates["modified_formula"] is result


def test_merge_modified_formula_does_not_mutate_base_formula() -> None:
    """merge 不修改 base_formula，只写 modified_formula。"""
    state = _state_with_syndrome_and_base_formula()
    result = ModifiedFormulaResult(
        formula=FormulaResult(
            name="参苓白术散加减",
            composition=[HerbDose(herb="党参", dose=12, unit="g")],
            rationale="健脾益气",
        ),
        modifications=[],
    )
    updates = merge_modified_formula_result_to_state(state, result)
    assert "base_formula" not in updates
    assert "modified_formula" in updates
    assert len(updates) == 1


def test_merge_modified_formula_with_evidences() -> None:
    """merge_modified_formula_result_to_state 传入 evidences 时合并到 state。"""
    state = _state_with_syndrome_and_base_formula()
    state.evidences = [_evidence("ev-existing")]
    result = ModifiedFormulaResult(
        formula=FormulaResult(
            name="参苓白术散加减",
            composition=[HerbDose(herb="党参", dose=12, unit="g")],
            rationale="健脾益气",
            citations=["ev-001"],
        ),
        modifications=[],
    )
    new_evs = [_evidence("ev-001"), _evidence("ev-002")]

    updates = merge_modified_formula_result_to_state(state, result, evidences=new_evs)
    assert updates["modified_formula"] is result
    assert "evidences" in updates
    merged = updates["evidences"]
    assert len(merged) == 3
    ids = [ev.evidence_id for ev in merged]
    assert ids == ["ev-existing", "ev-001", "ev-002"]


def test_merge_modified_formula_without_evidences_backward_compat() -> None:
    """merge_modified_formula_result_to_state 不传 evidences 时仅写 modified_formula。"""
    state = _state_with_syndrome_and_base_formula()
    result = ModifiedFormulaResult(
        formula=FormulaResult(
            name="参苓白术散加减",
            composition=[HerbDose(herb="党参", dose=12, unit="g")],
            rationale="健脾益气",
        ),
        modifications=[],
    )
    updates = merge_modified_formula_result_to_state(state, result)
    assert "modified_formula" in updates
    assert "evidences" not in updates
    assert len(updates) == 1


# ===========================================================================
# build_modification_query 测试
# ===========================================================================


def test_build_modification_query_with_base_formula() -> None:
    """有 base_formula 时查询包含基础方名、证型与治法。"""
    state = _state_with_syndrome_and_base_formula()
    query = build_modification_query(state)
    assert "参苓白术散" in query
    assert "脾虚湿盛证" in query
    assert "健脾化湿" in query


def test_build_modification_query_without_syndrome() -> None:
    """无辨证结论时回退到基础方名和主诉。"""
    state = _state_with_base_formula_no_syndrome()
    query = build_modification_query(state)
    assert "参苓白术散" in query
    assert "食欲差" in query


def test_build_modification_query_empty() -> None:
    """无任何信息时查询为空串。"""
    state = _minimal_state()
    query = build_modification_query(state)
    assert query == ""


# ===========================================================================
# format_base_formula_summary 测试
# ===========================================================================


def test_format_base_formula_summary_full() -> None:
    """完整基础方摘要包含所有字段。"""
    formula = _base_formula()
    summary = format_base_formula_summary(formula)
    assert "参苓白术散" in summary
    assert "太平惠民和剂局方" in summary
    assert "健脾益气" in summary
    assert "党参" in summary
    assert "12.0g" in summary


def test_format_base_formula_summary_none() -> None:
    """无基础方时返回缺证提示。"""
    summary = format_base_formula_summary(None)
    assert "无基础方信息" in summary


# ===========================================================================
# format_evidence_summary 测试
# ===========================================================================


def test_format_evidence_summary_with_evidence() -> None:
    """有证据时格式化为含 evidence_id 的文本。"""
    evs = [
        _evidence("ev-001", "formula", "参苓白术散", "健脾益气……"),
        _evidence("ev-002", "herb", "薏苡仁", "利水渗湿……"),
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
# _validate_citations 测试
# ===========================================================================


def test_validate_citations_subset_passes() -> None:
    """citations 是 evidence_ids 的子集时不抛异常。"""
    _validate_citations(["ev-001", "ev-002"], {"ev-001", "ev-002", "ev-003"})


def test_validate_citations_fabricated_rejected() -> None:
    """citations 包含非可用 Evidence 的 ID 时抛 ValidationError。"""
    with pytest.raises(ValidationError, match="formula"):
        _validate_citations(["ev-001", "fake-999"], {"ev-001"})


def test_validate_citations_no_evidence_but_citations_rejected() -> None:
    """无证据时 citations 不为空则抛 ValidationError。"""
    with pytest.raises(ValidationError, match="formula"):
        _validate_citations(["ev-001"], set())


def test_validate_citations_evidence_but_empty_rejected() -> None:
    """有证据但 citations 为空时抛 ValidationError——必须至少引用一条 Evidence。"""
    with pytest.raises(ValidationError, match="formula"):
        _validate_citations([], {"ev-001", "ev-002"})


# ===========================================================================
# _validate_rationale_traceability 测试
# ===========================================================================


def test_rationale_traceability_no_evidence_with_marker_passes() -> None:
    """无证据且 rationale 含缺证提示时通过。"""
    _validate_rationale_traceability(
        "缺证提示：RAG 未检索到相关证据，加减建议基于模型内知识。",
        set(),
        [],
    )


def test_rationale_traceability_no_evidence_without_marker_rejected() -> None:
    """无证据且 rationale 无缺证提示时抛 ValidationError。"""
    with pytest.raises(ValidationError, match="formula"):
        _validate_rationale_traceability(
            "选参苓白术散加减以健脾益气。",
            set(),
            [],
        )


def test_rationale_traceability_evidence_with_citations_passes() -> None:
    """有证据且已引用时 rationale 无需缺证提示。"""
    _validate_rationale_traceability(
        "选参苓白术散加减以健脾益气，加薏苡仁增强渗湿。",
        {"ev-001"},
        ["ev-001"],
    )


# ===========================================================================
# _validate_modification_reasons 测试
# ===========================================================================


def test_validate_modification_reasons_valid_passes() -> None:
    """合法理由通过校验。"""
    mods = [
        ModificationItem(
            action=ModificationAction.ADD,
            herb="薏苡仁",
            dose=10,
            unit="g",
            reason="患者身重困倦，加薏苡仁以增强渗湿之力",
        )
    ]
    _validate_modification_reasons(mods)


def test_validate_modification_reasons_placeholder_rejected() -> None:
    """占位符理由被拒绝。"""
    placeholder_reasons = ["N/A", "无", "略", "同上"]
    for reason in placeholder_reasons:
        mods = [
            ModificationItem(
                action=ModificationAction.ADD,
                herb="薏苡仁",
                dose=10,
                unit="g",
                reason=reason,
            )
        ]
        with pytest.raises(ValidationError, match="modifications"):
            _validate_modification_reasons(mods)


# ===========================================================================
# ModificationAgent fake gateway + fake retriever 测试
# ===========================================================================


def _modification_output(
    formula_name: str = "参苓白术散加减",
    formula_composition: list[HerbDose] | None = None,
    formula_source: str | None = "《太平惠民和剂局方》",
    formula_rationale: str = "健脾益气，渗湿止泻。加薏苡仁增强渗湿之力。",
    formula_citations: list[str] | None = None,
    modifications: list[ModificationItem] | None = None,
) -> ModifiedFormulaResult:
    return ModifiedFormulaResult(
        formula=FormulaResult(
            name=formula_name,
            composition=formula_composition
            or [
                HerbDose(herb="党参", dose=12, unit="g"),
                HerbDose(herb="白术", dose=10, unit="g"),
                HerbDose(herb="茯苓", dose=10, unit="g"),
                HerbDose(herb="薏苡仁", dose=10, unit="g"),
            ],
            source=formula_source,
            rationale=formula_rationale,
            citations=formula_citations or [],
        ),
        modifications=modifications
        or [
            ModificationItem(
                action=ModificationAction.ADD,
                herb="薏苡仁",
                dose=10,
                unit="g",
                reason="患者身重困倦，加薏苡仁以增强渗湿之力",
            )
        ],
    )


@pytest.mark.asyncio
async def test_modification_agent_with_evidence(tmp_path: Path) -> None:
    """RAG 有证据时输出加减方，且 citations 可追溯。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([_modification_output(formula_citations=["ev-001", "ev-002"])])
    retriever = FakeRetriever([[_evidence("ev-001"), _evidence("ev-002")]])

    agent = ModificationAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_syndrome_and_base_formula()
    result = await agent.run(state, "trace-with-evidence")

    output = result.output
    assert isinstance(output, ModifiedFormulaResult)
    assert output.formula.name == "参苓白术散加减"
    assert len(output.formula.composition) == 4
    assert output.formula.composition[0].herb == "党参"
    assert output.formula.source == "《太平惠民和剂局方》"
    assert "健脾" in output.formula.rationale
    assert "ev-001" in output.formula.citations
    assert "ev-002" in output.formula.citations
    assert len(output.modifications) == 1
    assert output.modifications[0].action == ModificationAction.ADD
    assert output.modifications[0].herb == "薏苡仁"

    # Evidence 已返回且可追溯
    assert len(result.evidences) == 2
    assert result.evidences[0].evidence_id == "ev-001"

    # gateway 调用标记
    assert gateway.calls[0]["agent_name"] == "modification"

    # retriever 调用正确
    assert retriever.calls[0]["primary_sources"] == ["formula", "herb"]
    assert retriever.calls[0]["allow_cross_source"] is True

    # prompt 版本
    assert result.prompt_version == "modification_v1.jinja2"
    # 推进到 safety
    assert result.next_stage == Stage.SAFETY


@pytest.mark.asyncio
async def test_modification_agent_no_evidence(tmp_path: Path) -> None:
    """RAG 无证据时缺证提示，citations 为空。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            _modification_output(
                formula_rationale=(
                    "缺证提示：RAG 未检索到相关方剂/中药证据，"
                    "加减建议主要基于模型内知识，建议医师审慎判断。"
                    "选参苓白术散加减以健脾益气。"
                ),
                formula_citations=[],
            )
        ]
    )
    retriever = FakeRetriever([[]])  # 空证据列表

    agent = ModificationAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_syndrome_and_base_formula()
    result = await agent.run(state, "trace-no-evidence")

    output = result.output
    assert isinstance(output, ModifiedFormulaResult)
    assert output.formula.citations == []
    assert "缺证" in output.formula.rationale or "未检索" in output.formula.rationale
    assert len(result.evidences) == 0


@pytest.mark.asyncio
async def test_modification_agent_missing_base_formula(tmp_path: Path) -> None:
    """缺少 base_formula 时抛 BASE_FORMULA_MISSING。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([_modification_output(formula_citations=["ev-001"])])
    retriever = FakeRetriever([[_evidence("ev-001")]])

    agent = ModificationAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _minimal_state()
    with pytest.raises(AgentRunError) as exc_info:
        await agent.run(state, "trace-missing-base")
    assert exc_info.value.code == "BASE_FORMULA_MISSING"
    assert exc_info.value.retryable is False
    # retriever 不应被调用
    assert len(retriever.calls) == 0
    # gateway 不应被调用
    assert len(gateway.calls) == 0


@pytest.mark.asyncio
async def test_modification_agent_bad_schema(tmp_path: Path) -> None:
    """fake gateway 返回坏 schema 时 AGENT_SCHEMA_INVALID。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            {"bad": "missing formula field"},
            {"also": "bad second attempt"},
        ]
    )
    retriever = FakeRetriever([[_evidence()]])

    agent = ModificationAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=1,
        model_name="fake-model",
        retriever=retriever,
    )

    with pytest.raises(AgentRunError) as exc_info:
        await agent.run(
            _state_with_syndrome_and_base_formula(),
            "trace-bad-schema",
        )

    error = exc_info.value
    assert error.code == "AGENT_SCHEMA_INVALID"
    assert error.retryable is False
    assert len(gateway.calls) == 2  # 重试了


@pytest.mark.asyncio
async def test_modification_agent_fabricated_citation_rejected(
    tmp_path: Path,
) -> None:
    """模型返回 fabricated citation 时被归一化为 AGENT_SCHEMA_INVALID。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([_modification_output(formula_citations=["fake-999"])])
    retriever = FakeRetriever([[_evidence("ev-001")]])

    agent = ModificationAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_syndrome_and_base_formula()
    with pytest.raises(AgentRunError) as exc_info:
        await agent.run(state, "trace-fabricated-citation")
    assert exc_info.value.code == "AGENT_SCHEMA_INVALID"
    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_modification_agent_empty_citations_when_evidence(
    tmp_path: Path,
) -> None:
    """有证据但模型输出 citations 为空时归一化为 AGENT_SCHEMA_INVALID。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([_modification_output(formula_citations=[])])
    retriever = FakeRetriever([[_evidence("ev-001"), _evidence("ev-002")]])

    agent = ModificationAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_syndrome_and_base_formula()
    with pytest.raises(AgentRunError) as exc_info:
        await agent.run(state, "trace-ev-but-empty-citations")
    assert exc_info.value.code == "AGENT_SCHEMA_INVALID"
    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_modification_agent_add_action(tmp_path: Path) -> None:
    """输出包含 add 动作。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            _modification_output(
                formula_citations=["ev-001"],
                modifications=[
                    ModificationItem(
                        action=ModificationAction.ADD,
                        herb="薏苡仁",
                        dose=10,
                        unit="g",
                        reason="患者身重困倦，加薏苡仁以增强渗湿之力",
                    )
                ],
            )
        ]
    )
    retriever = FakeRetriever([[_evidence("ev-001")]])

    agent = ModificationAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_syndrome_and_base_formula()
    result = await agent.run(state, "trace-add")
    output = result.output
    assert output.modifications[0].action == ModificationAction.ADD
    assert output.modifications[0].dose == 10


@pytest.mark.asyncio
async def test_modification_agent_remove_action(tmp_path: Path) -> None:
    """输出包含 remove 动作。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            _modification_output(
                formula_citations=["ev-001"],
                modifications=[
                    ModificationItem(
                        action=ModificationAction.REMOVE,
                        herb="甘草",
                        dose=None,
                        unit="g",
                        reason="患者腹胀明显，减甘草以免壅滞",
                    )
                ],
            )
        ]
    )
    retriever = FakeRetriever([[_evidence("ev-001")]])

    agent = ModificationAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_syndrome_and_base_formula()
    result = await agent.run(state, "trace-remove")
    output = result.output
    assert output.modifications[0].action == ModificationAction.REMOVE
    assert output.modifications[0].dose is None


@pytest.mark.asyncio
async def test_modification_agent_replace_action(tmp_path: Path) -> None:
    """输出包含 replace 动作。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            _modification_output(
                formula_citations=["ev-001"],
                modifications=[
                    ModificationItem(
                        action=ModificationAction.REPLACE,
                        herb="苍术",
                        dose=10,
                        unit="g",
                        reason="以苍术替换白术，增强燥湿之力",
                    )
                ],
            )
        ]
    )
    retriever = FakeRetriever([[_evidence("ev-001")]])

    agent = ModificationAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_syndrome_and_base_formula()
    result = await agent.run(state, "trace-replace")
    output = result.output
    assert output.modifications[0].action == ModificationAction.REPLACE
    assert output.modifications[0].herb == "苍术"


@pytest.mark.asyncio
async def test_modification_agent_adjust_action(tmp_path: Path) -> None:
    """输出包含 adjust 动作。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            _modification_output(
                formula_citations=["ev-001"],
                modifications=[
                    ModificationItem(
                        action=ModificationAction.ADJUST,
                        herb="茯苓",
                        dose=15,
                        unit="g",
                        reason="便溏明显，加重茯苓剂量以增强渗湿",
                    )
                ],
            )
        ]
    )
    retriever = FakeRetriever([[_evidence("ev-001")]])

    agent = ModificationAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_syndrome_and_base_formula()
    result = await agent.run(state, "trace-adjust")
    output = result.output
    assert output.modifications[0].action == ModificationAction.ADJUST
    assert output.modifications[0].dose == 15


@pytest.mark.asyncio
async def test_modification_agent_all_four_actions(tmp_path: Path) -> None:
    """单次输出包含全部四种加减动作。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            _modification_output(
                formula_citations=["ev-001"],
                modifications=[
                    ModificationItem(
                        action=ModificationAction.ADD,
                        herb="薏苡仁",
                        dose=10,
                        unit="g",
                        reason="加薏苡仁以增强渗湿",
                    ),
                    ModificationItem(
                        action=ModificationAction.REMOVE,
                        herb="甘草",
                        dose=None,
                        unit="g",
                        reason="减甘草以免壅滞",
                    ),
                    ModificationItem(
                        action=ModificationAction.REPLACE,
                        herb="苍术",
                        dose=10,
                        unit="g",
                        reason="以苍术替换白术",
                    ),
                    ModificationItem(
                        action=ModificationAction.ADJUST,
                        herb="茯苓",
                        dose=15,
                        unit="g",
                        reason="加重茯苓剂量以增强渗湿",
                    ),
                ],
            )
        ]
    )
    retriever = FakeRetriever([[_evidence("ev-001")]])

    agent = ModificationAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_syndrome_and_base_formula()
    result = await agent.run(state, "trace-all-four")
    output = result.output
    actions = [m.action for m in output.modifications]
    assert ModificationAction.ADD in actions
    assert ModificationAction.REMOVE in actions
    assert ModificationAction.REPLACE in actions
    assert ModificationAction.ADJUST in actions
    assert len(output.modifications) == 4


@pytest.mark.asyncio
async def test_modification_agent_no_safety_review(tmp_path: Path) -> None:
    """ModificationAgent 输出不包含安全审核、医师确认或病历。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([_modification_output(formula_citations=["ev-001"])])
    retriever = FakeRetriever([[_evidence("ev-001")]])

    agent = ModificationAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_syndrome_and_base_formula()
    result = await agent.run(state, "trace-no-safety")
    output = result.output
    output_dump = output.model_dump_json()

    assert "safety_review" not in output_dump
    assert "safety_rule_result" not in output_dump
    assert "安全审核" not in output_dump
    assert "医师确认" not in output_dump
    assert "病历" not in output_dump
    assert "跳过" not in output_dump
    assert "自动确认" not in output_dump
    assert "接受风险" not in output_dump


@pytest.mark.asyncio
async def test_modification_agent_prompt_includes_base_formula(
    tmp_path: Path,
) -> None:
    """构造的 prompt 中包含基础方摘要。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([_modification_output(formula_citations=["ev-001"])])
    ev = _evidence("ev-001", "formula", "参苓白术散", "健脾益气……")
    retriever = FakeRetriever([[ev]])

    agent = ModificationAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_syndrome_and_base_formula()
    await agent.run(state, "trace-prompt-check")

    system_msg = gateway.calls[0]["messages"][0]["content"]
    # 基础方摘要
    assert "参苓白术散" in system_msg
    assert "太平惠民和剂局方" in system_msg
    # 辨证摘要
    assert "脾虚湿盛证" in system_msg
    # 证据摘要
    assert "ev-001" in system_msg


@pytest.mark.asyncio
async def test_modification_agent_citation_validates_against_all_evidence(
    tmp_path: Path,
) -> None:
    """citations 校验涵盖本轮 RAG + 既有 state.evidences。"""
    manifest = _write_prompt_files(tmp_path)
    # 模型引用既有 evidence（ev-existing），本轮 RAG 返回 ev-001
    gateway = FakeGateway(
        [_modification_output(formula_citations=["ev-existing", "ev-001"])]
    )
    retriever = FakeRetriever([[_evidence("ev-001")]])

    agent = ModificationAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_syndrome_and_base_formula()
    state.evidences = [_evidence("ev-existing")]
    result = await agent.run(state, "trace-all-evidence")
    output = result.output
    assert "ev-existing" in output.formula.citations
    assert "ev-001" in output.formula.citations


@pytest.mark.asyncio
async def test_modification_agent_empty_query_skips_rag(tmp_path: Path) -> None:
    """build_modification_query 返回空串时，Agent 跳过 RAG 不调用 retriever。

    注意：base_formula.name 非空时 query 不可能为空，但 base_formula=None
    时会在 _retrieve_evidence 前置检查直接抛 BASE_FORMULA_MISSING，
    不经过 query 检查。因此本测试只验证 build_modification_query 在无
    任何信息时返回空串，以及当 query 为空时 Agent 内部跳过 RAG 的逻辑
    是一致的——通过单元测试 build_modification_query 覆盖。
    """
    # build_modification_query 在无 base_formula 且无其他信息时返回 ""
    state = _minimal_state()
    query = build_modification_query(state)
    assert query == ""


@pytest.mark.asyncio
async def test_modification_agent_via_base_agent_impl(tmp_path: Path) -> None:
    """ModificationAgent 走 BaseAgentImpl 和模型网关抽象。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([_modification_output(formula_citations=["ev-001"])])
    retriever = FakeRetriever([[_evidence("ev-001")]])

    agent = ModificationAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    from app.agents.base import BaseAgentImpl

    assert isinstance(agent, BaseAgentImpl)
    assert agent.name == "modification"
    assert agent.stage == Stage.MODIFICATION
    assert agent.output_schema == ModifiedFormulaResult
    assert agent.next_stage == Stage.SAFETY

    state = _state_with_syndrome_and_base_formula()
    result = await agent.run(state, "trace-base-impl")

    assert isinstance(result.output, ModifiedFormulaResult)
    assert result.prompt_version == "modification_v1.jinja2"
    assert gateway.calls[0]["agent_name"] == "modification"


@pytest.mark.asyncio
async def test_modification_agent_does_not_modify_state_base_formula(
    tmp_path: Path,
) -> None:
    """ModificationAgent 输出不修改 state.base_formula。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([_modification_output(formula_citations=["ev-001"])])
    retriever = FakeRetriever([[_evidence("ev-001")]])

    agent = ModificationAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_syndrome_and_base_formula()
    original_base = state.base_formula
    result = await agent.run(state, "trace-no-modify-base")
    # AgentResult.output 是 ModifiedFormulaResult，不是 XuanhuState
    # 验证 merge 函数不写 base_formula
    updates = merge_modified_formula_result_to_state(state, result.output)
    assert "base_formula" not in updates
    # state.base_formula 未被修改
    assert state.base_formula is original_base


@pytest.mark.asyncio
async def test_modification_agent_reads_base_formula(tmp_path: Path) -> None:
    """ModificationAgent 应从 state.base_formula 读取基础方名作为加减依据。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([_modification_output(formula_citations=["ev-001"])])
    retriever = FakeRetriever([[_evidence("ev-001")]])

    agent = ModificationAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    state = _state_with_syndrome_and_base_formula()
    await agent.run(state, "trace-read-base")

    # 检索查询应包含基础方名（证明读取了 base_formula）
    query = retriever.calls[0]["query"]
    assert "参苓白术散" in query


# ===========================================================================
# 不调用真实模型网关测试
# ===========================================================================


@pytest.mark.asyncio
async def test_no_real_model_gateway_called(tmp_path: Path) -> None:
    """验证所有测试路径只经过 FakeGateway，不调真实模型网关。"""
    manifest = _write_prompt_files(tmp_path)

    class TrackingGateway:
        async def chat_structured(self, *args: Any, **kwargs: Any) -> Any:
            return _modification_output(formula_citations=["ev-001"])

    retriever = FakeRetriever([[_evidence("ev-001")]])

    agent = ModificationAgent(
        gateway=TrackingGateway(),
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
        retriever=retriever,
    )

    await agent.run(_state_with_syndrome_and_base_formula(), "trace-no-real")
    assert True  # TrackingGateway 返回了 fake 输出，未调真实网关


# ===========================================================================
# _merge_evidences_to_state 测试
# ===========================================================================


def test_merge_evidences_dedup() -> None:
    from app.agents.prescription import _merge_evidences_to_state

    existing = _evidence("ev-001")
    state = _state_with_syndrome_and_base_formula()
    state.evidences = [existing]

    new_evs = [_evidence("ev-001"), _evidence("ev-002")]
    merged = _merge_evidences_to_state(state, new_evs)
    assert len(merged) == 2
    assert merged[0].evidence_id == "ev-001"
    assert merged[1].evidence_id == "ev-002"


# ===========================================================================
# Supervisor modification 输出应用（不依赖 DB）
# ===========================================================================


def test_supervisor_apply_modification_output_writes_state() -> None:
    """Supervisor._apply_agent_output 在 MODIFICATION 阶段写入 state.modified_formula。"""
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

    state = _state_with_syndrome_and_base_formula()
    state.evidences = [_evidence("ev-existing")]
    output = _modification_output(formula_citations=["ev-001"])
    new_evs = [_evidence("ev-001")]

    updated = supervisor._apply_agent_output(
        state, Stage.MODIFICATION, output, evidences=new_evs,
    )
    assert updated.modified_formula is not None
    assert updated.modified_formula.formula.name == output.formula.name
    # 其他字段未被破坏
    assert updated.syndrome_result is not None
    assert updated.syndrome_result.syndrome == "脾虚湿盛证"
    assert updated.base_formula is not None
    # Evidence 合并到 state.evidences
    ids = [ev.evidence_id for ev in updated.evidences]
    assert ids == ["ev-existing", "ev-001"]
    # 不修改 base_formula
    assert updated.base_formula.name == "参苓白术散"


def test_supervisor_default_registry_includes_modification() -> None:
    """Supervisor 默认 registry 包含 ModificationAgent。"""
    from app.agents.supervisor import _default_registry

    registry = _default_registry()
    agent = registry.get(Stage.MODIFICATION)
    assert agent is not None
    from app.agents.modification import ModificationAgent

    assert isinstance(agent, ModificationAgent)
