"""1a 主诉大类归集：把 complaint_classifier 决策产出的 category 并入终端单 commit。

旧实现（已在第二 bug 修复中废弃）的问题：classify 节点排在 extract 之前，调
``classify_and_persist_category`` 时 ``repository.get_state`` 拿到的 domain_state
里还没有本轮新落的 ``chief_complaint.symptom``（intake 子图内部节点只在内存
``reduce_domain_state`` 上算，从不 flush 到 PG；只有路由终端节点才
``repository.commit`` 把 observation 真正落库）→ 永远走 ``skip_no_symptom`` →
不调模型 → ``chief_complaint.category`` 永不出现 → 下游 ``_complaint_category()``
拿不到 category → 一律退回 GENERAL 档 4 维十问，激活不了 respiratory 维度。

修法（用户已确认方向"并入终端单 commit"）：classify 决策逻辑内联进
``_compute_intake_from_claim``，在 ``reduce_domain_state`` 算出 next_state 之后、
``evaluate_completeness_policy`` 之前，读 next_state.observations 里的 active
``chief_complaint.symptom`` 决策归大类，把 category **作为一条 ADD observation
追加进同一个 delta**，重算一次 ``reduce_domain_state``，路由终端
``repository.commit`` 一次同时落 symptom + category。

本模块现在只保留纯辅助：读 next_state（而非 PG ``get_state``）、调
``execute_complaint_classification``、产出一条 category ``ObservationSchema``
候选。调模型/构造/落库/commit 全部由 ``langgraph_intake._classify_and_merge_category``
编排，本模块不再做 ``repository.commit``。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from app.schemas.completeness import ComplaintCategory
from app.schemas.domain import ObservationSchema, ObservationStatus
from app.schemas.intake import ComplaintClassificationInput
from app.agent_runtime.reducer import DomainState


COMPLAINT_CLASSIFY_DELTA_RUN_SPEC_STAGE = "intake_classify_complaint"
# agent_spec_version/prompt_version 直接取 app.agents.complaint_classifier 的
# COMPLAINT_CLASSIFIER_AGENT_VERSION / COMPLAINT_CLASSIFIER_PROMPT_VERSION（与
# AgentRuntime.run 的强校验一致，避免 AGENT_SPEC_VERSION_MISMATCH）。
COMPLAINT_CLASSIFY_POLICY_VERSION = "complaint-classify-policy.v1"


def _classification_run_id(claim_session_id: uuid.UUID, claim_idempotency_key: str) -> uuid.UUID:
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"xuanhu:intake-classify:{claim_session_id}:{claim_idempotency_key}",
    )


def _chief_complaint_symptom_fact(
    state: DomainState,  # noqa: ARG001  # kept for symmetry with old signature
) -> tuple[ObservationSchema, ...]:
    """active 的 chief_complaint.symptom observation（取第一个作为归集输入）。"""
    active = tuple(
        item
        for item in state.observations
        if item.fact_key == "chief_complaint.symptom" and item.status is ObservationStatus.ACTIVE
    )
    return active


def _existing_category_observations(state: DomainState) -> tuple[ObservationSchema, ...]:
    return tuple(
        item
        for item in state.observations
        if item.fact_key == "chief_complaint.category" and item.status is ObservationStatus.ACTIVE
    )


class _ClassificationTrace:
    """分类节点的中间留痕载荷（写进 intermediate_payload["classify_complaint"]）。"""

    __slots__ = (
        "category",
        "source",
        "degraded",
        "last_failure_code",
        "agent_run_id",
        "confidence",
        "skipped",
    )

    def __init__(  # noqa: PLR0913 - 7 fields mirror the trace schema; no kwargs needed
        self,
        category: str,
        source: str,
        degraded: bool,
        last_failure_code: str | None,
        agent_run_id: str | None,
        confidence: float | None,
        skipped: bool,
    ) -> None:
        self.category = category
        self.source = source
        self.degraded = degraded
        self.last_failure_code = last_failure_code
        self.agent_run_id = agent_run_id
        self.confidence = confidence
        self.skipped = skipped

    def to_payload(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "source": self.source,
            "source_kind": "complaint_classifier",
            "degraded": self.degraded,
            "last_failure_code": self.last_failure_code,
            "agent_run_id": self.agent_run_id,
            "confidence": self.confidence,
            "skipped": self.skipped,
        }


def _build_classification_input(
    symptom_observation: ObservationSchema,
    state: DomainState,
) -> ComplaintClassificationInput | None:
    raw = (
        symptom_observation.normalized_value
        if symptom_observation.normalized_value is not None
        else symptom_observation.value
    )
    if not isinstance(raw, str) or not raw.strip():
        return None
    return ComplaintClassificationInput(
        chief_complaint_text=raw.strip()[:4_000],
        patient_sex=_population_value(state, "patient.sex"),
        patient_age=_population_value(state, "patient.age"),
    )


def _population_value(state: DomainState, fact_key: str) -> str | int | None:
    """取 state 中 active 的人口学观察值（patient.sex/patient.age），供归集输入人口学校正。

    sex 透传原始字符串（prompt 侧做妇科适用性校正）；age 接受 int 直接值或可转 int 的
    字符串（seed/抽取两种形态），并在 [0,150] 范围内守卫——越界/非法返回 None，
    避免 ComplaintClassificationInput 的 ge=0/le=150 校验失败导致整条归集降级 GENERAL。
    """
    for item in state.observations:
        if item.fact_key != fact_key or item.status is not ObservationStatus.ACTIVE:
            continue
        raw = item.normalized_value if item.normalized_value is not None else item.value
        if fact_key == "patient.sex":
            if not isinstance(raw, str) or not raw.strip():
                continue
            return raw.strip()[:16]
        if isinstance(raw, int):
            candidate = raw
        elif isinstance(raw, str) and raw.strip():
            try:
                candidate = int(raw.strip()[:16])
            except ValueError:
                continue
        else:
            continue
        return candidate if 0 <= candidate <= 150 else None
    return None


def _build_category_observation(
    *,
    run_id: uuid.UUID,
    session_id: uuid.UUID,
    category: ComplaintCategory,
    source_message_id: uuid.UUID,
    confidence: float,
) -> ObservationSchema:
    """构造一条 ADD ``chief_complaint.category`` observation（仅值，不外裹 DomainDelta）。

    不走 ``filter_legal_observations``——``chief_complaint.category`` 在
    ``LEGAL_FACT_KEYS`` 白名单内（已校验），且 category 已是 ``ComplaintCategory``
    枚举，归一并归一表已是 canonical 值，无需再过 E1。
    """
    return ObservationSchema(
        observation_id=uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"xuanhu:observation:{run_id}:0:chief_complaint.category:add",
        ),
        session_id=session_id,
        fact_key="chief_complaint.category",
        value=category.value,
        normalized_value=category.value,
        source_message_id=source_message_id,
        status=ObservationStatus.ACTIVE,
        confidence=confidence,
        supersedes_observation_id=None,
        created_at=datetime.now(UTC),
    )
