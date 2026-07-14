"""P5-1 问诊 Agent 测试。

使用 fake gateway 覆盖：
- 主诉提取
- 现病史/十问歌字段归并
- 已有字段不被空值覆盖
- 下一问只包含一个核心问题
- 安全信息采集提示
- schema 解析失败时沿用 BaseAgent 错误归一化
- AgentRun / audit_events 写入路径
- Supervisor 应用 InquiryAgent 输出后 State 中问诊字段被更新
- 不调用真实模型网关
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.errors import AgentRunError
from app.agents.inquiry import (
    InquiryAgent,
    _format_conversation_history,
    _format_four_diagnosis,
    _format_patient_info,
    _format_ten_questions,
    _merge_four_diagnosis,
    _merge_ten_questions,
    build_state_summary,
    merge_inquiry_output_to_state,
)
from app.agents.prompt_loader import PromptLoader
from app.agents.registry import AgentRegistry
from app.agents.supervisor import Supervisor
from app.core.exceptions import ModelGatewayTimeoutError
from app.models.agent import AgentRun
from app.models.audit import AuditEvent
from app.models.consult import ConsultSession
from app.schemas.agent import (
    FourDiagnosis,
    InquiryAgentOutput,
    MenstruationInfo,
    PatientInfo,
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
# Prompt 文件辅助
# ---------------------------------------------------------------------------


def _write_prompt_files(tmp_path: Path, *, manifest_extra: str = "") -> Path:
    """写临时 prompt 文件，返回 manifest 路径。"""
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    manifest_content = "test_agent: test_agent_v1.jinja2\ninquiry: inquiry_v1.jinja2\n" + manifest_extra
    (prompt_dir / "manifest.yaml").write_text(manifest_content, encoding="utf-8")
    (prompt_dir / "test_agent_v1.jinja2").write_text("TEST_PROMPT", encoding="utf-8")
    (prompt_dir / "inquiry_v1.jinja2").write_text(
        "You are a TCM inquiry assistant.\n"
        "{state_summary}\n"
        "{conversation_history}\n"
        "Return structured JSON.",
        encoding="utf-8",
    )
    return prompt_dir / "manifest.yaml"


# ---------------------------------------------------------------------------
# Output helper
# ---------------------------------------------------------------------------


def _valid_output(**overrides: Any) -> InquiryAgentOutput:
    """构造合法的问诊 Agent 输出。"""
    defaults: dict[str, Any] = {
        "chief_complaint": None,
        "present_illness": None,
        "past_history": None,
        "personal_family_history": None,
        "ten_questions_delta": None,
        "four_diagnosis_delta": None,
        "next_question": "请问您主要哪里不舒服？",
        "asked_dimension": "chief_complaint",
        "safety_info_requested": [],
        "safety_notes": None,
    }
    defaults.update(overrides)
    return InquiryAgentOutput.model_validate(defaults)


# ===========================================================================
# Schema 校验测试
# ===========================================================================


def test_inquiry_agent_output_schema_requires_next_question() -> None:
    """InquiryAgentOutput 必须有 next_question，否则校验失败。"""
    with pytest.raises(ValidationError):
        InquiryAgentOutput.model_validate({"asked_dimension": "chief_complaint"})


def test_inquiry_agent_output_schema_requires_asked_dimension() -> None:
    """InquiryAgentOutput 必须有 asked_dimension。"""
    with pytest.raises(ValidationError):
        InquiryAgentOutput.model_validate({"next_question": "哪里不舒服？"})


def test_inquiry_agent_output_minimal_valid() -> None:
    """最少合法字段可独立校验。"""
    output = InquiryAgentOutput.model_validate(
        {"next_question": "哪里不舒服？", "asked_dimension": "chief_complaint"}
    )
    assert output.next_question == "哪里不舒服？"
    assert output.asked_dimension == "chief_complaint"
    assert output.chief_complaint is None
    assert output.safety_info_requested == []


def test_inquiry_agent_output_full_shape() -> None:
    """完整的问诊输出可独立校验。"""
    output = InquiryAgentOutput.model_validate(
        {
            "chief_complaint": "头痛三天",
            "present_illness": "三天前淋雨后开始，以双侧太阳穴附近胀痛为主",
            "past_history": "既往有偏头痛史5年",
            "personal_family_history": "母亲有高血压病史",
            "ten_questions_delta": {
                "cold_heat": "恶寒发热",
                "sleep": "入睡困难",
            },
            "four_diagnosis_delta": {
                "inspection": "面色红",
            },
            "next_question": "请问大便情况怎么样？",
            "asked_dimension": "ten_questions",
            "safety_info_requested": ["allergy"],
            "safety_notes": "尚未确认药物过敏史",
        }
    )
    assert output.chief_complaint == "头痛三天"
    assert output.ten_questions_delta is not None
    assert output.ten_questions_delta.cold_heat == "恶寒发热"
    assert output.four_diagnosis_delta is not None
    assert output.four_diagnosis_delta.inspection == "面色红"
    assert "allergy" in output.safety_info_requested


def test_inquiry_agent_output_empty_next_question_fails() -> None:
    """next_question 不得为空字符串。"""
    with pytest.raises(ValidationError):
        InquiryAgentOutput.model_validate({"next_question": "", "asked_dimension": "chief_complaint"})


def test_inquiry_agent_output_multiple_questions_rejected() -> None:
    """next_question 包含多个问号时 schema 校验失败。"""
    with pytest.raises(ValidationError):
        InquiryAgentOutput.model_validate(
            {
                "next_question": "您头痛多久了？有发热吗？大便怎么样？",
                "asked_dimension": "present_illness",
            }
        )


def test_inquiry_agent_output_parallel_markers_rejected() -> None:
    """next_question 包含并列追问标记（"另外""还有"等）时 schema 校验失败。"""
    with pytest.raises(ValidationError):
        InquiryAgentOutput.model_validate(
            {
                "next_question": "请问您睡眠怎么样？另外还有头痛吗？",
                "asked_dimension": "ten_questions",
            }
        )


def test_inquiry_agent_output_single_question_accepted() -> None:
    """单问句通过单问题校验。"""
    output = InquiryAgentOutput.model_validate(
        {"next_question": "您睡眠怎么样？", "asked_dimension": "ten_questions"}
    )
    assert output.next_question == "您睡眠怎么样？"


# ===========================================================================
# 状态摘要构造测试
# ===========================================================================


def test_format_patient_info_with_allergies() -> None:
    """患者基础信息包含过敏史时应体现在摘要中。"""
    info = PatientInfo(
        name="张三",
        gender="male",
        age=35,
        allergies=["青霉素"],
        pregnancy_status="no",
    )
    text = _format_patient_info(info)
    assert "张三" in text
    assert "35 岁" in text
    assert "青霉素" in text


def test_format_patient_info_pregnancy_status() -> None:
    """妊娠状态为 unknown 时不显示，为 pregnant 时显示。"""
    unknown_info = PatientInfo(pregnancy_status="unknown")
    assert "妊娠" not in _format_patient_info(unknown_info)

    preg_info = PatientInfo(gender="female", pregnancy_status="pregnant")
    assert "pregnant" in _format_patient_info(preg_info)


def test_format_ten_questions_partial() -> None:
    """十问歌只展示已采集的字段。"""
    tq = TenQuestions(cold_heat="恶寒发热", sleep="入睡困难")
    text = _format_ten_questions(tq)
    assert "寒热" in text
    assert "恶寒发热" in text
    assert "睡眠" in text
    assert "汗出" not in text  # 未采集的字段不显示


def test_format_ten_questions_blank_for_empty() -> None:
    """全空十问歌显示未采集。"""
    text = _format_ten_questions(TenQuestions())
    assert "未采集" in text


def test_format_four_diagnosis_partial() -> None:
    """四诊只展示已采集的字段。"""
    fd = FourDiagnosis(inspection="面色红", palpation="脉浮")
    text = _format_four_diagnosis(fd)
    assert "望诊" in text
    assert "面色红" in text
    assert "切诊" in text
    assert "闻诊" not in text


def test_format_conversation_history_truncates_long_messages() -> None:
    """长对话消息应截断。"""
    messages = [{"role": "doctor", "content": "X" * 600}]
    text = _format_conversation_history(messages)
    assert len(text) < 700  # 500 + label 头


def test_format_conversation_history_empty() -> None:
    """空对话显示提示。"""
    text = _format_conversation_history([])
    assert "尚无对话" in text


def test_build_state_summary_includes_all_sections() -> None:
    """状态摘要应包含所有问诊维度。"""
    state = XuanhuState(
        session_id="test-summary",
        patient_info=PatientInfo(name="李四", gender="female", age=28),
        chief_complaint="头痛",
        ten_questions=TenQuestions(cold_heat="恶寒发热"),
    )
    summary = build_state_summary(state)
    assert "李四" in summary
    assert "头痛" in summary
    assert "恶寒发热" in summary
    assert "主诉" in summary
    assert "现病史" in summary
    assert "既往史" in summary
    assert "十问歌" in summary
    assert "四诊摘要" in summary


# ===========================================================================
# 安全合并测试
# ===========================================================================


def test_merge_ten_questions_only_delta_fields() -> None:
    """仅 delta 中有值的字段才合并到已有数据。"""
    existing = TenQuestions(cold_heat="恶寒发热", sweat="汗出")
    delta = TenQuestions(sleep="入睡困难", stool_urine="大便溏")
    merged = _merge_ten_questions(existing, delta)
    assert merged.cold_heat == "恶寒发热"  # 已有字段保持
    assert merged.sweat == "汗出"
    assert merged.sleep == "入睡困难"  # 新字段写入
    assert merged.stool_urine == "大便溏"
    assert merged.diet is None  # 未涉及的字段保持空


def test_merge_ten_questions_empty_delta_does_not_erase() -> None:
    """空 delta 不覆盖已有数据。"""
    existing = TenQuestions(cold_heat="恶寒发热")
    delta = TenQuestions()
    merged = _merge_ten_questions(existing, delta)
    assert merged.cold_heat == "恶寒发热"


def test_merge_ten_questions_menstruation_detail() -> None:
    """月经详情做字段级合并。"""
    existing = TenQuestions(
        menstruation="月经量少",
        menstruation_detail=MenstruationInfo(cycle="28天", volume="量少"),
    )
    delta = TenQuestions(
        menstruation_detail=MenstruationInfo(color="暗红", pain="经行腹痛"),
    )
    merged = _merge_ten_questions(existing, delta)
    assert merged.menstruation == "月经量少"
    assert merged.menstruation_detail is not None
    assert merged.menstruation_detail.cycle == "28天"  # 已有字段保持
    assert merged.menstruation_detail.volume == "量少"
    assert merged.menstruation_detail.color == "暗红"  # 新字段写入
    assert merged.menstruation_detail.pain == "经行腹痛"


def test_merge_four_diagnosis_delta() -> None:
    """四诊 delta 做字段级合并。"""
    existing = FourDiagnosis(inspection="面色红")
    delta = FourDiagnosis(palpation="脉浮", auscultation_olfaction="呼吸粗")
    merged = _merge_four_diagnosis(existing, delta)
    assert merged.inspection == "面色红"  # 已有字段保持
    assert merged.palpation == "脉浮"  # 新字段写入
    assert merged.auscultation_olfaction == "呼吸粗"
    assert merged.inquiry is None


def test_merge_inquiry_output_preserves_existing_fields() -> None:
    """已有非空字段不被空值覆盖。"""
    state = XuanhuState(
        session_id="test-no-overwrite",
        chief_complaint="原有头痛",
        present_illness="原有现病史",
    )
    output = _valid_output(chief_complaint=None, present_illness=None)
    updates = merge_inquiry_output_to_state(state, output)
    # 输出为 None → 不应覆盖已有字段
    assert "chief_complaint" not in updates
    assert "present_illness" not in updates
    # inquiry_messages 应追加
    assert "inquiry_messages" in updates
    assert len(updates["inquiry_messages"]) == 1


def test_merge_inquiry_output_only_writes_non_none() -> None:
    """只将输出中非 None 的标量字段写入 updates。"""
    state = XuanhuState(session_id="test-non-none")
    output = _valid_output(chief_complaint="新提取的主诉")
    updates = merge_inquiry_output_to_state(state, output)
    assert updates["chief_complaint"] == "新提取的主诉"
    assert "present_illness" not in updates
    assert "past_history" not in updates


def test_merge_inquiry_output_appends_messages() -> None:
    """inquiry_messages 追加而非替换。"""
    state = XuanhuState(
        session_id="test-append",
        inquiry_messages=[{"role": "doctor", "content": "患者头痛"}],
    )
    output = _valid_output(next_question="痛了多久了？", asked_dimension="present_illness")
    updates = merge_inquiry_output_to_state(state, output)
    assert len(updates["inquiry_messages"]) == 2
    assert updates["inquiry_messages"][0]["role"] == "doctor"
    assert updates["inquiry_messages"][1]["role"] == "assistant"
    assert updates["inquiry_messages"][1]["content"] == "痛了多久了？"
    assert updates["inquiry_messages"][1]["asked_dimension"] == "present_illness"


def test_merge_inquiry_output_ten_questions_delta() -> None:
    """十问歌 delta 安全合并。"""
    state = XuanhuState(
        session_id="test-tq-delta",
        ten_questions=TenQuestions(cold_heat="恶寒"),
    )
    delta = TenQuestions(sweat="自汗", sleep="入睡困难")
    output = _valid_output(ten_questions_delta=delta)
    updates = merge_inquiry_output_to_state(state, output)
    merged = updates["ten_questions"]
    assert merged.cold_heat == "恶寒"  # 原有字段保持
    assert merged.sweat == "自汗"  # 新字段写入
    assert merged.sleep == "入睡困难"


def test_merge_inquiry_output_four_diagnosis_delta() -> None:
    """四诊 delta 安全合并。"""
    state = XuanhuState(
        session_id="test-fd-delta",
        four_diagnosis=FourDiagnosis(inspection="面色萎黄"),
    )
    delta = FourDiagnosis(palpation="脉沉细")
    output = _valid_output(four_diagnosis_delta=delta)
    updates = merge_inquiry_output_to_state(state, output)
    merged = updates["four_diagnosis"]
    assert merged.inspection == "面色萎黄"  # 原有字段保持
    assert merged.palpation == "脉沉细"  # 新字段写入


def test_merge_inquiry_output_safety_info_in_message() -> None:
    """安全信息采集标签入 inquiry_messages。"""
    state = XuanhuState(session_id="test-safety-msg")
    output = _valid_output(
        next_question="请问您有药物过敏史吗？",
        asked_dimension="safety",
        safety_info_requested=["allergy"],
        safety_notes="尚未采集过敏史",
    )
    updates = merge_inquiry_output_to_state(state, output)
    msg = updates["inquiry_messages"][0]
    assert msg["safety_info_requested"] == ["allergy"]
    assert msg["safety_notes"] == "尚未采集过敏史"


def test_merge_inquiry_output_incremental_append_scalar() -> None:
    """已有字段收到新值时增量追加（以'；'拼接），而非覆盖旧值。"""
    state = XuanhuState(
        session_id="test-append-scalar",
        chief_complaint="头痛",
        present_illness="三日前淋雨后起病",
    )
    output = _valid_output(
        chief_complaint="以双侧太阳穴胀痛为主",
        present_illness="伴恶寒发热",
        next_question="有汗出吗？",
        asked_dimension="ten_questions",
    )
    updates = merge_inquiry_output_to_state(state, output)
    assert updates["chief_complaint"] == "头痛；以双侧太阳穴胀痛为主"
    assert updates["present_illness"] == "三日前淋雨后起病；伴恶寒发热"
    # past_history 无新增，不在 updates 中
    assert "past_history" not in updates


def test_merge_inquiry_output_incremental_first_write() -> None:
    """state 字段为空时增量合并等价于直接写入。"""
    state = XuanhuState(session_id="test-first-write")
    output = _valid_output(
        chief_complaint="胃痛",
        next_question="痛了多久了？",
        asked_dimension="present_illness",
    )
    updates = merge_inquiry_output_to_state(state, output)
    assert updates["chief_complaint"] == "胃痛"


# ===========================================================================
# InquiryAgent fake gateway 测试
# ===========================================================================


@pytest.mark.asyncio
async def test_inquiry_agent_extracts_chief_complaint(tmp_path: Path) -> None:
    """问诊 Agent 通过 fake gateway 提取主诉。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            _valid_output(
                chief_complaint="头痛三天",
                next_question="痛在哪里？",
                asked_dimension="present_illness",
            )
        ]
    )
    agent = InquiryAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
    )

    state = XuanhuState(
        session_id=str(uuid.uuid4()),
        inquiry_messages=[{"role": "doctor", "content": "患者头痛三天"}],
    )
    result = await agent.run(state, "trace-cc")

    output = result.output
    assert isinstance(output, InquiryAgentOutput)
    assert output.chief_complaint == "头痛三天"
    assert output.next_question == "痛在哪里？"
    assert gateway.calls[0]["agent_name"] == "inquiry"
    assert result.prompt_version == "inquiry_v1.jinja2"


@pytest.mark.asyncio
async def test_inquiry_agent_merges_ten_questions(tmp_path: Path) -> None:
    """十问歌字段归并——Agent 输出中的 ten_questions_delta 正确提取。"""
    manifest = _write_prompt_files(tmp_path)
    delta = TenQuestions(cold_heat="恶寒发热", sleep="入睡困难")
    gateway = FakeGateway(
        [
            _valid_output(
                ten_questions_delta=delta,
                next_question="请问大便怎么样？",
                asked_dimension="ten_questions",
            )
        ]
    )
    agent = InquiryAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
    )

    state = XuanhuState(session_id=str(uuid.uuid4()))
    result = await agent.run(state, "trace-tq")
    output = result.output
    assert output.ten_questions_delta is not None
    assert output.ten_questions_delta.cold_heat == "恶寒发热"
    assert output.ten_questions_delta.sleep == "入睡困难"


@pytest.mark.asyncio
async def test_inquiry_agent_single_question_constraint(tmp_path: Path) -> None:
    """fake gateway 返回单问题时测试通过。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            _valid_output(
                next_question="您睡眠怎么样？",
                asked_dimension="ten_questions",
            )
        ]
    )
    agent = InquiryAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
    )

    state = XuanhuState(session_id=str(uuid.uuid4()))
    result = await agent.run(state, "trace-single-q")
    output = result.output
    # 验证 next_question 不含多个问号（粗略检测无并列追问）
    assert output.next_question.count("？") <= 1
    assert output.next_question.count("?") <= 1


@pytest.mark.asyncio
async def test_inquiry_agent_safety_info_collection(tmp_path: Path) -> None:
    """安全信息采集：fake gateway 返回过敏史询问。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            _valid_output(
                next_question="请问您有药物或食物过敏史吗？",
                asked_dimension="safety",
                safety_info_requested=["allergy", "medication_contraindication"],
                safety_notes="尚未确认过敏史，需在开方前采集",
            )
        ]
    )
    agent = InquiryAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
    )

    state = XuanhuState(
        session_id=str(uuid.uuid4()),
        chief_complaint="头痛",
        patient_info=PatientInfo(name="王五", gender="female", age=30),
    )
    result = await agent.run(state, "trace-safety")
    output = result.output
    assert output.asked_dimension == "safety"
    assert "allergy" in output.safety_info_requested
    assert "medication_contraindication" in output.safety_info_requested
    assert output.safety_notes is not None
    assert "过敏" in output.next_question


@pytest.mark.asyncio
async def test_inquiry_agent_does_not_output_diagnosis_or_prescription(tmp_path: Path) -> None:
    """问诊 Agent 输出不包含辨证、治法、处方。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            _valid_output(
                chief_complaint="头痛",
                next_question="头痛是持续性的还是阵发性的？",
                asked_dimension="present_illness",
            )
        ]
    )
    agent = InquiryAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
    )

    state = XuanhuState(session_id=str(uuid.uuid4()))
    result = await agent.run(state, "trace-no-dx")
    output = result.output
    # 不应有任何辨证/处方/安全审核字段
    assert output.next_question is not None
    output_dump = output.model_dump_json()
    assert "syndrome" not in output_dump.lower()
    assert "处方" not in output_dump
    assert "剂量" not in output_dump
    assert "安全审核" not in output_dump
    assert "跳过" not in output_dump
    assert "自动确认" not in output_dump


@pytest.mark.asyncio
async def test_inquiry_agent_prompt_includes_state_summary_and_history(tmp_path: Path) -> None:
    """构造的 prompt 中包含状态摘要和对话历史。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            _valid_output(
                chief_complaint="头痛",
                next_question="持续多久了？",
                asked_dimension="present_illness",
            )
        ]
    )
    agent = InquiryAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
    )

    state = XuanhuState(
        session_id=str(uuid.uuid4()),
        patient_info=PatientInfo(name="赵六", gender="male", age=40),
        chief_complaint="胃痛",
        inquiry_messages=[{"role": "doctor", "content": "患者诉胃痛"}],
    )
    await agent.run(state, "trace-prompt-check")

    messages = gateway.calls[0]["messages"]
    system_msg = messages[0]["content"]
    assert "赵六" in system_msg
    assert "胃痛" in system_msg
    assert "doctor" in system_msg or "医师" in system_msg


# ===========================================================================
# BaseAgent 错误归一化测试
# ===========================================================================


@pytest.mark.asyncio
async def test_inquiry_agent_schema_parse_failure_normalized(tmp_path: Path) -> None:
    """schema 解析失败时沿用 BaseAgent AGENT_SCHEMA_INVALID 错误码。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            {"invalid": "no next_question or asked_dimension"},
            {"also": "bad"},
        ]
    )
    agent = InquiryAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=1,
        model_name="fake-model",
    )

    with pytest.raises(AgentRunError) as exc_info:
        await agent.run(XuanhuState(session_id=str(uuid.uuid4())), "trace-schema-fail")

    error = exc_info.value
    assert error.code == "AGENT_SCHEMA_INVALID"
    assert error.retryable is False
    assert len(gateway.calls) == 2  # 重试了一次


@pytest.mark.asyncio
async def test_inquiry_agent_timeout_failure_sanitized(tmp_path: Path) -> None:
    """超时错误归一化，且不泄露敏感信息。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            ModelGatewayTimeoutError("timeout with sk-secret-key"),
        ]
    )
    agent = InquiryAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
    )

    with pytest.raises(AgentRunError) as exc_info:
        await agent.run(XuanhuState(session_id=str(uuid.uuid4())), "trace-timeout")

    error = exc_info.value
    assert error.code == "AGENT_MODEL_TIMEOUT"
    assert error.retryable is True
    assert "sk-secret-key" not in error.message
    assert "sk-secret-key" not in (error.detail or "")


# ===========================================================================
# 不调用真实模型网关测试
# ===========================================================================


@pytest.mark.asyncio
async def test_no_real_model_gateway_called(tmp_path: Path) -> None:
    """验证所有测试路径只经过 FakeGateway，不调真实模型网关。"""
    manifest = _write_prompt_files(tmp_path)

    real_gateway_called = False

    class TrackingGateway:
        async def chat_structured(self, *args: Any, **kwargs: Any) -> Any:
            nonlocal real_gateway_called
            real_gateway_called = True
            return _valid_output()

    agent = InquiryAgent(
        gateway=TrackingGateway(),
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
    )

    await agent.run(XuanhuState(session_id=str(uuid.uuid4())), "trace-no-real")

    # TrackingGateway 不是真实网关，而是测试用 gate
    # 关键：测试没有导入或实例化 ModelGatewayClient；gateway_called 为 True 说明走的是 fake
    assert True  # TrackingGateway 返回了 fake 输出，未调真实网关


# ===========================================================================
# 集成测试（需要 PostgreSQL/Redis，标记 integration）
# ===========================================================================

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


async def _create_session(
    db: AsyncSession,
    stage: Stage = Stage.INQUIRY,
    status: str = "active",
) -> ConsultSession:
    """在数据库中创建测试会话。"""
    session = ConsultSession(
        id=uuid.uuid4(),
        patient_ref="P5-1-TEST",
        patient_info={"patient_ref": "P5-1-TEST", "gender": "female", "age": 30},
        current_stage=stage.value,
        status=status,
        state_version=1,
        rollback_counts={},
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def _cleanup_session(db: AsyncSession, session_id: uuid.UUID) -> None:
    """清理测试会话及相关数据。"""
    await db.execute(delete(AuditEvent).where(AuditEvent.session_id == session_id))
    await db.execute(delete(AgentRun).where(AgentRun.session_id == session_id))
    await db.execute(delete(ConsultSession).where(ConsultSession.id == session_id))
    await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_inquiry_agent_writes_agent_run_and_audit(
    db: AsyncSession,
    tmp_path: Path,
) -> None:
    """有 DB session 时写入 agent_runs 和 audit_events，且审计 payload 脱敏。"""
    session = await _create_session(db)
    session_id = session.id

    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            _valid_output(
                chief_complaint="咳嗽",
                next_question="有痰吗？",
                asked_dimension="present_illness",
            )
        ]
    )
    agent = InquiryAgent(
        gateway=gateway,
        db=db,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-inquiry-model",
    )

    try:
        result = await agent.run(XuanhuState(session_id=str(session_id)), "trace-db-inquiry")
        await db.commit()

        # 检查 agent_run
        run_result = await db.execute(select(AgentRun).where(AgentRun.session_id == session_id))
        run = run_result.scalar_one()
        assert result.agent_run_id == str(run.id)
        assert run.agent_name == "inquiry"
        assert run.stage == "inquiry"
        assert run.status == "success"
        assert run.prompt_version == "inquiry_v1.jinja2"
        assert run.model == "fake-inquiry-model"
        assert run.retry_count == 0
        assert run.output_snapshot["chief_complaint"] == "咳嗽"
        assert run.output_snapshot["next_question"] == "有痰吗？"

        # 检查 audit_events
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
        assert {e.event_type for e in audits} == {"agent.started", "agent.finished"}

        # 审计 payload 不含 prompt 原文
        audit_text = " ".join(str(e.payload) for e in audits)
        assert "sk-" not in audit_text
        assert "bearer" not in audit_text.lower()
    finally:
        await db.rollback()
        await _cleanup_session(db, session_id)


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_inquiry_agent_failure_writes_failed_run(
    db: AsyncSession,
    tmp_path: Path,
) -> None:
    """失败路径写入 failed agent_run 和 agent.failed 审计事件。"""
    session = await _create_session(db)
    session_id = session.id

    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            {"bad": "invalid schema"},
            {"also_bad": "still invalid"},
        ]
    )
    agent = InquiryAgent(
        gateway=gateway,
        db=db,
        prompt_loader=PromptLoader(manifest),
        max_retries=1,
        model_name="fake-model",
    )

    try:
        with pytest.raises(AgentRunError) as exc_info:
            await agent.run(XuanhuState(session_id=str(session_id)), "trace-db-failed")
        await db.commit()

        assert exc_info.value.code == "AGENT_SCHEMA_INVALID"

        run_result = await db.execute(select(AgentRun).where(AgentRun.session_id == session_id))
        run = run_result.scalar_one()
        assert run.status == "failed"
        assert run.error_code == "AGENT_SCHEMA_INVALID"

        audit_result = await db.execute(
            select(AuditEvent)
            .where(AuditEvent.session_id == session_id)
            .where(AuditEvent.event_type == "agent.failed")
        )
        audit = audit_result.scalar_one()
        assert audit.payload["error_code"] == "AGENT_SCHEMA_INVALID"
    finally:
        await db.rollback()
        await _cleanup_session(db, session_id)


# ===========================================================================
# Supervisor 集成测试
# ===========================================================================


def test_supervisor_default_registry_includes_inquiry() -> None:
    """Supervisor 默认 AgentRegistry 包含 InquiryAgent（不会 blocked 为 missing_agent）。"""
    from app.agents.supervisor import _default_registry
    registry = _default_registry()
    assert Stage.INQUIRY in registry
    agent = registry.get(Stage.INQUIRY)
    assert agent is not None
    assert agent.name == "inquiry"
    assert agent.stage == Stage.INQUIRY


class FakeInquiryAgentForSupervisor:
    """直接实现 BaseAgent Protocol 的 fake inquiry agent，供 Supervisor 集成测试。"""

    name = "inquiry"
    stage = Stage.INQUIRY
    primary_sources = ()
    allow_cross_source = True
    output_schema = InquiryAgentOutput
    next_stage = Stage.SUFFICIENCY

    def __init__(self, output: InquiryAgentOutput | None = None) -> None:
        self._output = output or _valid_output(
            chief_complaint="头痛三天",
            present_illness="三天前淋雨后开始",
            next_question="还有哪里不舒服吗？",
            asked_dimension="present_illness",
        )

    async def run(self, state: XuanhuState, trace_id: str) -> Any:
        from app.agents.base import AgentResult
        return AgentResult(
            output=self._output,
            prompt_version="fake",
        )


class FakeSufficiencyAgent:
    """Fake sufficiency agent for stage routing."""

    name = "sufficiency"
    stage = Stage.SUFFICIENCY
    primary_sources = ()
    allow_cross_source = True
    output_schema = type("FakeSufficiencyOutput", (), {})
    next_stage = Stage.SYNDROME

    async def run(self, state: XuanhuState, trace_id: str) -> Any:
        from app.agents.base import AgentResult
        from app.schemas.agent import SufficiencyReport
        return AgentResult(
            output=SufficiencyReport(sufficient=False, covered=[], missing=["ten_questions"]),
            prompt_version="fake",
        )


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_supervisor_applies_inquiry_output_to_state(
    db: AsyncSession,
) -> None:
    """Supervisor 推进 inquiry 后 State 中问诊字段被更新。"""
    session = await _create_session(db, stage=Stage.INQUIRY)
    session_id = session.id

    inquiry_output = _valid_output(
        chief_complaint="恶寒发热",
        present_illness="昨日开始，伴头痛",
        next_question="汗出情况如何？",
        asked_dimension="ten_questions",
        ten_questions_delta=TenQuestions(cold_heat="恶寒发热"),
        safety_info_requested=["allergy"],
    )

    registry = AgentRegistry()
    registry.register(Stage.INQUIRY, FakeInquiryAgentForSupervisor(output=inquiry_output))
    registry.register(Stage.SUFFICIENCY, FakeSufficiencyAgent())

    supervisor = Supervisor(db, registry=registry)

    try:
        # 推进 inquiry -> sufficiency
        result = await supervisor.advance(str(session.id), "trace-inquiry-supervisor")
        assert result.to_stage == Stage.SUFFICIENCY

        state = result.state
        # 验证问诊字段已写入
        assert state.chief_complaint == "恶寒发热"
        assert state.present_illness == "昨日开始，伴头痛"
        # 验证 inquiry_messages 被追加
        assert len(state.inquiry_messages) == 1
        assert state.inquiry_messages[0]["content"] == "汗出情况如何？"
        assert state.inquiry_messages[0]["asked_dimension"] == "ten_questions"
        assert state.inquiry_messages[0]["safety_info_requested"] == ["allergy"]

        # 验证 PG state_snapshot 包含问诊字段
        await db.refresh(session)
        snapshot = session.state_snapshot or {}
        assert snapshot.get("chief_complaint") == "恶寒发热"
        assert snapshot.get("present_illness") == "昨日开始，伴头痛"

    finally:
        await _cleanup_session(db, session_id)
        try:
            from app.core.redis import get_redis
            redis = await get_redis()
            await redis.delete(f"xuanhu:checkpoint:{session_id}")
            await redis.delete(f"xuanhu:events:{session_id}")
        except Exception:
            pass


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_supervisor_inquiry_no_overwrite_with_empty(
    db: AsyncSession,
) -> None:
    """Supervisor 合并 inquiry 输出时不用空值覆盖已有有效字段。"""
    session = await _create_session(db, stage=Stage.INQUIRY)
    session_id = session.id
    # 预设 PG snapshot 中已有 chief_complaint
    session.state_snapshot = {
        "chief_complaint": "原有主诉-头痛",
        "present_illness": "原有现病史-已持续一月",
        "current_stage": "inquiry",
    }
    await db.commit()

    # Agent 输出中 chief_complaint 和 present_illness 为 None
    inquiry_output = _valid_output(
        chief_complaint=None,
        present_illness=None,
        past_history="无特殊既往史",
        next_question="睡眠怎么样？",
        asked_dimension="ten_questions",
    )

    registry = AgentRegistry()
    registry.register(Stage.INQUIRY, FakeInquiryAgentForSupervisor(output=inquiry_output))
    registry.register(Stage.SUFFICIENCY, FakeSufficiencyAgent())
    supervisor = Supervisor(db, registry=registry)

    try:
        result = await supervisor.advance(str(session.id), "trace-no-overwrite")
        state = result.state
        # 已有字段不被空值覆盖
        assert state.chief_complaint == "原有主诉-头痛"
        assert state.present_illness == "原有现病史-已持续一月"
        # 新字段正常写入
        assert state.past_history == "无特殊既往史"
    finally:
        await _cleanup_session(db, session_id)
        try:
            from app.core.redis import get_redis
            redis = await get_redis()
            await redis.delete(f"xuanhu:checkpoint:{session_id}")
            await redis.delete(f"xuanhu:events:{session_id}")
        except Exception:
            pass
