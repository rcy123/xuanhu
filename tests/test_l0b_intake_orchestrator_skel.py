"""0b IntakeOrchestrator 骨架验证（纯单元，InMemorySaver，不依赖 DB）。

覆盖阶段 0b 的两项硬性验证：
1. 骨架接口：Phase 四出口 / 决策 / 快照 JSON-safe / Noop 行为零变更 / 确定性策略委托。
2. **有环子图 + langgraph checkpoint 兼容性**（问题清单 0b 的验证项）：
   - do-while 采集循环按条件边正确迭代、cap 强制退出防无限循环；
   - 循环中途 interrupt → checkpoint → resume 后状态一致、不重放已执行节点。
"""

from __future__ import annotations

import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict

from app.agent_runtime.intake_orchestrator import (
    NODE_INTAKE_ORCHESTRATOR_V1,
    CollectAction,
    CollectDecision,
    IntakeOrchestrator,
    IntakeOrchestratorPhase,
    IntakeOrchestratorSnapshot,
    NoopOrchestrationPolicy,
)

# ---------------------------------------------------------------------------
# 骨架接口
# ---------------------------------------------------------------------------


def test_noop_policy_keeps_collecting() -> None:
    """Noop 恒 collect_more——2c 接入前 orchestrator 行为零变更的显式锚点。"""
    policy = NoopOrchestrationPolicy()
    decision = policy.evaluate(
        IntakeOrchestratorSnapshot(
            collection_rounds=5,
            followup_rounds=2,
            covered_dimension_refs=["cold_heat"],
            missing_dimension_refs=["stool_urine"],
            phase=IntakeOrchestratorPhase.COLLECTING,
        )
    )
    assert decision["action"] is CollectAction.COLLECT_MORE
    assert decision["phase"] is IntakeOrchestratorPhase.COLLECTING
    assert "noop" in decision["reason"]


def test_phases_and_actions_coverage() -> None:
    """四出口 + 三动作齐全（决策 11/12/13 的接口形状）。"""
    assert set(IntakeOrchestratorPhase) == {
        IntakeOrchestratorPhase.COLLECTING,
        IntakeOrchestratorPhase.SATISFIED,
        IntakeOrchestratorPhase.CAP_REACHED,
        IntakeOrchestratorPhase.MANUAL_HANDOFF,
    }
    assert set(CollectAction) == {
        CollectAction.COLLECT_MORE,
        CollectAction.ADVANCE,
        CollectAction.MANUAL_HANDOFF,
    }


def test_snapshot_and_decision_json_safe() -> None:
    """快照/决策必须 JSON 可序列化（对齐 ADR-002：Graph State 只放计数/引用/阶段名）。"""
    snapshot = IntakeOrchestratorSnapshot(
        collection_rounds=1,
        followup_rounds=0,
        covered_dimension_refs=["cold_heat"],
        missing_dimension_refs=[],
        phase=IntakeOrchestratorPhase.COLLECTING,
    )
    decision: CollectDecision = {
        "action": CollectAction.COLLECT_MORE,
        "phase": IntakeOrchestratorPhase.COLLECTING,
        "reason": "ok",
    }
    # StrEnum 是 str 子类，json.dumps 直接可序列化
    assert json.loads(json.dumps(snapshot))["phase"] == "collecting"
    assert json.loads(json.dumps(decision))["action"] == "collect_more"


def test_orchestrator_delegates_to_deterministic_policy() -> None:
    """IntakeOrchestrator 委托确定性策略（铁律 10：成熟度判定不交给模型）。"""

    class _CapPolicy:
        """示例确定性策略：cap 到 → CAP_REACHED（决策 11 的确定性层形状）。"""

        def evaluate(self, snapshot: IntakeOrchestratorSnapshot) -> CollectDecision:
            if snapshot["collection_rounds"] >= 3:
                return CollectDecision(
                    action=CollectAction.ADVANCE,
                    phase=IntakeOrchestratorPhase.CAP_REACHED,
                    reason="followup cap reached",
                )
            return CollectDecision(
                action=CollectAction.COLLECT_MORE,
                phase=IntakeOrchestratorPhase.COLLECTING,
                reason="collect more",
            )

    orchestrator = IntakeOrchestrator(policy=_CapPolicy())
    early = orchestrator.evaluate(
        IntakeOrchestratorSnapshot(
            collection_rounds=1,
            followup_rounds=0,
            covered_dimension_refs=[],
            missing_dimension_refs=["cold_heat"],
            phase=IntakeOrchestratorPhase.COLLECTING,
        )
    )
    assert early["action"] is CollectAction.COLLECT_MORE

    capped = orchestrator.evaluate(
        IntakeOrchestratorSnapshot(
            collection_rounds=3,
            followup_rounds=2,
            covered_dimension_refs=[],
            missing_dimension_refs=["cold_heat"],
            phase=IntakeOrchestratorPhase.COLLECTING,
        )
    )
    assert capped["action"] is CollectAction.ADVANCE
    assert capped["phase"] is IntakeOrchestratorPhase.CAP_REACHED


def test_mount_constant_declared() -> None:
    """主图挂载点常量已声明（graph.py 引用它，阶段 2c 接线）。"""
    assert NODE_INTAKE_ORCHESTRATOR_V1 == "intake.orchestrator_v1"


# ---------------------------------------------------------------------------
# 有环子图 + checkpoint 兼容性（0b 硬性验证项）
# ---------------------------------------------------------------------------


class _LoopState(TypedDict, total=False):
    rounds: int
    done: bool


_CAP = 5


def _collect(state: _LoopState) -> dict[str, int]:
    return {"rounds": state.get("rounds", 0) + 1}


def _collect_with_interrupt(state: _LoopState) -> dict[str, int]:
    rounds = state.get("rounds", 0) + 1
    if rounds == 2:
        interrupt({"kind": "mid_collection", "rounds": rounds})
    return {"rounds": rounds}


def _decide(state: _LoopState) -> str:
    rounds = state.get("rounds", 0)
    if rounds >= 3:
        return "exit"
    return "collect"


def _decide_runaway(state: _LoopState) -> str:
    """坏条件边：永远想继续——只能靠 cap 保护强制退出。"""
    if state.get("rounds", 0) >= _CAP:
        return "exit"
    return "collect"


def _exit_node(state: _LoopState) -> dict[str, bool]:
    return {"done": True}


def _build_loop_graph(decide, collect=_collect) -> CompiledStateGraph:
    builder = StateGraph(_LoopState)
    builder.add_node("collect", collect)
    builder.add_node("exit", _exit_node)
    builder.add_edge(START, "collect")
    builder.add_conditional_edges("collect", decide, {"collect": "collect", "exit": "exit"})
    builder.add_edge("exit", END)
    return builder.compile(checkpointer=InMemorySaver())


def test_cyclic_subgraph_iterates_and_exits_within_cap() -> None:
    """do-while 采集循环：条件边驱动迭代，按 cap 内正常退出。"""
    compiled = _build_loop_graph(_decide)
    result = compiled.invoke({}, config={"configurable": {"thread_id": "loop-normal"}})
    assert result["rounds"] == 3
    assert result["done"] is True


def test_cyclic_subgraph_runaway_forced_exit_by_cap() -> None:
    """坏条件边（永远 collect）被 cap 保护强制退出——防无限循环的硬性要求。"""
    compiled = _build_loop_graph(_decide_runaway)
    result = compiled.invoke({}, config={"configurable": {"thread_id": "loop-runaway"}})
    assert result["rounds"] == _CAP
    assert result["done"] is True


@pytest.mark.asyncio
async def test_cyclic_subgraph_checkpoint_resume_consistent() -> None:
    """循环中途 interrupt → checkpoint → resume：状态一致、不重放已执行节点。

    第 2 轮在 collect 内 interrupt 暂停；resume 后从第 3 轮继续到退出。
    若 checkpoint 与循环不兼容（重跑），rounds 会从 1 重来或卡死。
    """
    compiled = _build_loop_graph(_decide, collect=_collect_with_interrupt)
    config = {"configurable": {"thread_id": "loop-resume"}}

    first = compiled.invoke({}, config)
    # interrupt() 在 collect 节点中途暂停：已提交 state 为第 1 轮（rounds=1），
    # 第 2 轮的局部值（rounds=2）随 __interrupt__ payload 返回，节点返回值未提交。
    assert first.get("rounds") == 1
    interrupts = first.get("__interrupt__", ())
    assert len(interrupts) == 1
    assert interrupts[0].value == {"kind": "mid_collection", "rounds": 2}

    # checkpoint 里保存了循环中间状态（第 1 轮已提交），不是初始态
    snapshot = compiled.get_state(config)
    assert snapshot.values["rounds"] == 1

    second = compiled.invoke(Command(resume="continue"), config)
    assert second["rounds"] == 3  # 从第 2 轮 interrupt 处继续到退出，不重放第 1 轮
    assert second["done"] is True
