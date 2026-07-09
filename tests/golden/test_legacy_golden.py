"""L0-2 Legacy Golden 行为基线。

这些测试固定迁移前可观察行为。API 链路使用 fake agents，绝不访问真实模型；
SafetyRuleEngine 的纯规则仍使用真实实现。已知 Legacy 红旗缺口以严格 xfail
记录，它不是 LangGraph 目标行为，也不得在迁移时复制。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.safety.engine import _check_allergy, _check_pregnancy
from app.safety.normalizer import HerbNormalizer
from app.schemas.agent import PatientInfo
from app.schemas.types import SafetyIssueType, Severity
from tests.e2e import test_backend_flow as legacy_flow
from tests.e2e.conftest import (
    E2E_DOCTOR_ID,
    cleanup_session_lock,
    cleanup_stream,
    create_session,
    fetch_session_fresh,
    post_advance,
    submit_message,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


@dataclass
class _GoldenHerb:
    name: str
    aliases: list[str]
    pregnancy_contraindication: str = "none"


async def test_golden_normal_consultation_and_record(
    client: AsyncClient, db: AsyncSession
) -> None:
    """正常问诊、医师确认和病历生成保持 Legacy 端到端契约。"""
    await legacy_flow.test_e2e_happy_path_full_pipeline(client, db)


async def test_golden_insufficient_information_stays_in_inquiry(
    client: AsyncClient, db: AsyncSession
) -> None:
    """信息不足不得进入辨证。"""
    await legacy_flow.test_e2e_insufficient_inquiry_rollback(client, db)


async def test_golden_allergy_is_a_deterministic_blocker() -> None:
    """处方命中已知过敏原时产生 BLOCKER，模型不能覆盖。"""
    normalizer = HerbNormalizer()
    records: dict[str, Any] = {
        "党参": _GoldenHerb(name="党参", aliases=["潞党参"]),
    }
    issues = _check_allergy(["党参"], ["潞党参"], normalizer, records)
    assert len(issues) == 1
    assert issues[0].type == SafetyIssueType.ALLERGY
    assert issues[0].severity == Severity.BLOCKER


@pytest.mark.parametrize("status", ["pregnant", "possible"])
async def test_golden_pregnancy_and_possible_are_equally_strict(status: str) -> None:
    """妊娠和可能妊娠对禁用药执行相同硬规则。"""
    normalizer = HerbNormalizer()
    records: dict[str, Any] = {
        "莪术": _GoldenHerb(
            name="莪术",
            aliases=[],
            pregnancy_contraindication="forbidden",
        ),
    }
    patient = PatientInfo(gender="female", age=30, pregnancy_status=status)
    issues = _check_pregnancy(["莪术"], patient, records, normalizer)
    assert len(issues) == 1
    assert issues[0].type == SafetyIssueType.PREGNANCY
    assert issues[0].severity == Severity.BLOCKER


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Legacy 没有确定性 red-flag gate；L3 必须修复，"
        "该行为禁止作为 LangGraph 兼容目标"
    ),
)
async def test_golden_red_flag_must_not_advance(client: AsyncClient) -> None:
    """刻画 Legacy 已知缺口：红旗文本目前可能被 fake sufficiency 放行。"""
    session_data = await create_session(client, chief_complaint="突发胸痛伴大汗和呼吸困难")
    session_id = session_data["session_id"]
    try:
        await submit_message(
            client,
            session_id,
            content="患者突发压榨性胸痛，伴大汗、呼吸困难和濒死感。",
        )
        response = await post_advance(client, session_id)
        assert response["data"]["current_stage"] == "inquiry"
        session = await fetch_session_fresh(session_id)
        assert session.current_stage == "inquiry"
    finally:
        await cleanup_stream(session_id)
        await cleanup_session_lock(session_id)


async def test_golden_safety_failure_and_doctor_modification(
    client: AsyncClient, db: AsyncSession
) -> None:
    """安全失败回退，以及医师修改后二次安全审核，均保持现有契约。"""
    await legacy_flow.test_e2e_safety_block_rollback(client, db)
    await legacy_flow.test_e2e_modify_formula_path(client, db)


async def test_golden_doctor_review_confirm_modify_reject(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Doctor Review 是硬门禁，并覆盖修改和拒绝动作。"""
    await legacy_flow.test_e2e_review_cannot_bypass_doctor(client, db)
    await legacy_flow.test_e2e_modify_formula_path(client, db)
    await legacy_flow.test_e2e_reject_formula_rollback(client, db)


async def test_golden_record_requires_valid_doctor_review(
    client: AsyncClient, db: AsyncSession
) -> None:
    """无有效医师复核不得生成最终病历。"""
    await legacy_flow.test_e2e_record_without_doctor_review_blocked(client, db)


async def test_golden_api_uses_doctor_identity_header(client: AsyncClient) -> None:
    """Golden 请求使用测试医师身份，不依赖患者身份或真实凭据。"""
    session_data = await create_session(client)
    session_id = session_data["session_id"]
    try:
        response = await client.get(
            f"/api/v1/consult/sessions/{session_id}",
            headers={"X-Doctor-Id": E2E_DOCTOR_ID},
        )
        assert response.status_code == 200
    finally:
        await cleanup_stream(session_id)
        await cleanup_session_lock(session_id)
