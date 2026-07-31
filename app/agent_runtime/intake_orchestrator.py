"""IntakeOrchestrator 编排骨架（阶段 0b，问题清单 0b/21）。

把"采够没、要不要继续采、何时转向"作为主图第一类能力挂回。
**本阶段只搭骨架，不改变现有采集语义**：默认 ``NoopOrchestrationPolicy``
返回 ``collect_more``，与现状"每轮进图采集、子图内条件边判 incomplete → END"完全一致。

阶段 2c 接入路径（决策已定案 2025-07，见 ``docs/02_agent逻辑优化/问题清单.md``）：
- 决策 12 粗槽位：由 ``MATURITY_KEY_THRESHOLDS`` / ``DIMENSION_KEYSETS`` 派生的
  ``DimensionSlotSnapshot`` 判 SATISFIED（槽位齐才推进）。
- 决策 13 混合·确定性层：以 ``GAP_PRIORITY_RULES_AUTHORITY`` 为底，cap 内允许在
  确定性候选中切换维度；LLM 只发"该维度无法继续"信号，不直接选维度。
- 决策 11 partial：追问 cap 到 → CAP_REACHED → 强行落 partial（带缺口列表）；
  **安全三项维度 cap 到仍转 MANUAL_HANDOFF**（铁律 9）。
- 铁律 10：槽位齐没齐是确定性闸门判，agent 不能输出"放过"。

数据边界（ADR-002）：快照/决策用 **JSON-safe TypedDict**（与 ``XuanhuGraphState``
同一风格），只放计数/阶段/引用，不放临床事实；完整槽位内容存 DomainState，
快照仅存 domain_state_version 指针 + 维度引用。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Protocol

from typing_extensions import TypedDict

# 主图挂载点节点名（graph.py 引用；阶段 2c 在此把 intake 子图从"跑完即 END"
# 升级为 orchestrator 驱动的有环采集循环）。
NODE_INTAKE_ORCHESTRATOR_V1: str = "intake.orchestrator_v1"


class IntakeOrchestratorPhase(StrEnum):
    """采集循环的四个出口阶段（骨架常量，阶段 2c 实际使用）。"""

    COLLECTING = "collecting"  # 继续采集（Noop 骨架的默认语义 = 现状每轮进图）
    SATISFIED = "satisfied"  # 槽位齐 → 推进下一大阶段（阶段 2c 生效）
    CAP_REACHED = "cap_reached"  # 追问 cap 到 → 强行落 partial（决策 11，阶段 2d 生效）
    MANUAL_HANDOFF = "manual_handoff"  # 安全项 cap 到 → 转人工（铁律 9）


class CollectAction(StrEnum):
    """与 phase 对应的动作，供上层（阶段 2c 的 intake 循环/主图条件边）分派。"""

    COLLECT_MORE = "collect_more"
    ADVANCE = "advance"
    MANUAL_HANDOFF = "manual_handoff"


class IntakeOrchestratorSnapshot(TypedDict, total=False):
    """采集态快照（仅计数/引用，JSON-safe，对齐 ADR-002）。

    骨架阶段只定义形状；阶段 2c 由 completeness/槽位判定填充。
    """

    collection_rounds: int  # 本轮 intake 采集轮次
    followup_rounds: int  # 同维度追问轮次
    no_new_facts_rounds: int  # 连续无新事实轮次（stagnation 输入）
    covered_dimension_refs: list[str]  # 已齐维度引用（2c：槽位齐才计入）
    missing_dimension_refs: list[str]  # 缺口维度引用（2c：槽位缺口）
    phase: IntakeOrchestratorPhase  # 当前循环阶段
    domain_state_version: int  # DomainState 版本指针


class CollectDecision(TypedDict, total=False):
    """编排判定结果（JSON-safe dict，可直接写入 Graph State / 驱动条件边）。

    阶段 2c 扩展：next_dimension（确定性候选）、missing_slots（缺口列表）、
    partial 标记——保持本形状的 JSON-safe 语义。
    """

    action: CollectAction
    phase: IntakeOrchestratorPhase
    reason: str


class OrchestrationPolicy(Protocol):
    """采集编排策略协议：确定性闸门（铁律 10），不是 agent。

    实现必须纯确定性：同 snapshot → 同 decision。阶段 2c 的真实策略
    （槽位判定 + 优先级表 + cap 计数）必须满足本协议。
    """

    def evaluate(self, snapshot: IntakeOrchestratorSnapshot) -> CollectDecision: ...


class NoopOrchestrationPolicy:
    """骨架默认策略：永远 collect_more —— 保持现状采集语义（0b 红线）。

    阶段 2c 用真实策略替换本类；替换时以本类为回归基准
    （行为不得弱于"每轮至少进一次采集"）。
    """

    def evaluate(self, snapshot: IntakeOrchestratorSnapshot) -> CollectDecision:
        return {
            "action": CollectAction.COLLECT_MORE,
            "phase": IntakeOrchestratorPhase.COLLECTING,
            "reason": "noop policy keeps current per-round intake semantics",
        }


class IntakeOrchestrator:
    """IntakeOrchestrator 骨架：主图采集层编排的第一类能力挂载点。

    阶段 0b：只承载策略接口与快照形状，不接线、不改行为、不碰 intake 子图。
    阶段 2c：在 intake 子图出口（gates_and_route 处）接入——
    ``evaluate(snapshot)`` 驱动"采满→推进 / 没采满→追问 / cap→强落"，
    封装形态（有环条件边 vs 节点内调用）是实现细节，由 2c 定。
    cap 保护：上层必须保证 rounds 上限（有环子图 + langgraph checkpoint
    兼容性由 tests/test_l0b_intake_orchestrator_skel.py 验证）。
    """

    def __init__(self, policy: OrchestrationPolicy | None = None) -> None:
        self._policy = policy or NoopOrchestrationPolicy()

    @property
    def policy(self) -> OrchestrationPolicy:
        return self._policy

    def evaluate(self, snapshot: IntakeOrchestratorSnapshot) -> CollectDecision:
        """确定性编排判定（协议保证：同快照同决策，不依赖模型）。"""
        return self._policy.evaluate(snapshot)

    def snapshot_from_graph_state(self, state: Mapping[str, Any]) -> IntakeOrchestratorSnapshot:
        """从 XuanhuGraphState 提取 JSON-safe 采集快照（骨架：仅读现有字段）。

        当前仅 ``domain_state_version`` 真实存在；``intake_*_rounds`` 为阶段 2c
        预留的计数键（尚未加入 XuanhuGraphState），缺失时按 0 兜底——骨架阶段
        不修改 Graph State 形状。阶段 2c 扩展：读槽位计数 / covered / missing。
        """
        return IntakeOrchestratorSnapshot(
            collection_rounds=_as_non_negative_int(state.get("intake_collection_rounds")),
            followup_rounds=_as_non_negative_int(state.get("intake_followup_rounds")),
            no_new_facts_rounds=_as_non_negative_int(state.get("intake_no_new_facts_rounds")),
            phase=IntakeOrchestratorPhase.COLLECTING,
            domain_state_version=_as_non_negative_int(state.get("domain_state_version")),
        )


def _as_non_negative_int(value: object) -> int:
    """仅接受 int（排除 bool/float 静默截断），其余按 0 兜底——确定性意图。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if value >= 0 else 0
