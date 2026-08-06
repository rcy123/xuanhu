"""验证 ``_REASONING_AUTHORITY_CACHE`` 的 cache-hit 行为与提交后失效契约。

背景：推理子图 T1.2 引入 ``_REASONING_AUTHORITY_CACHE`` 减少 ``get_state`` 重复调用。
但若缓存命中跨越了 ``_commit_syndrome_artifact`` 的版本跃迁（N → N+1），下游
``build_formula_context`` 会读到 stale N 版本 → formula input state_version=N →
提交时撞 STATE_VERSION_CONFLICT。本文件保护该不变式：
1. cache hit：同 claim_id 重复读不再走 DB
2. 失效后再读：cache 被 pop 后必须重拉，拿到 post-commit 版本
3. 源码契约：``_commit_syndrome_artifact`` / ``_commit_formula_artifact`` 必须
   显式调用 ``_REASONING_AUTHORITY_CACHE.pop(claim.id, None)``，防止后续重构误删
"""

from __future__ import annotations

import inspect
import uuid

import app.services.langgraph_reasoning as reasoning_module
from app.agent_runtime.repository import ReasoningAuthoritySnapshot
from app.agent_runtime.reducer import DomainState


class _CountingRepository:
    """记录 get_state / get_reasoning_authority 调用次数 + 版本切换的最小 stub。"""

    def __init__(
        self,
        *,
        session_id: uuid.UUID,
        states: list[DomainState],
        authorities: list[ReasoningAuthoritySnapshot],
    ) -> None:
        assert len(states) == len(authorities)
        self._session_id = session_id
        self._states = states
        self._authorities = authorities
        self._step = 0
        self.get_state_calls = 0
        self.get_authority_calls = 0

    async def get_state(self, session_id: uuid.UUID) -> DomainState:
        self.get_state_calls += 1
        return self._states[self._step]

    async def get_reasoning_authority(self, session_id: uuid.UUID, state_version: int) -> ReasoningAuthoritySnapshot:
        self.get_authority_calls += 1
        return self._authorities[self._step]

    def advance(self) -> None:
        """模拟一次 commit 导致的 state_version 推进。"""
        self._step += 1


def _make_state(version: int, session_id: uuid.UUID) -> DomainState:
    return DomainState(
        session_id=session_id,
        state_version=version,
        observations=(),
        safety_profile=None,
        artifacts=(),
    )


def _make_authority(version: int, session_id: uuid.UUID) -> ReasoningAuthoritySnapshot:
    from app.schemas.domain import GateDecision, GateResultSchema

    dummy_state = _make_state(version, session_id)
    return ReasoningAuthoritySnapshot(
        session_id=session_id,
        current_state_version=version,
        current_stage="syndrome",
        session_status="active",
        agent_runtime="langgraph",
        domain_state=dummy_state,
        source_gate_id=uuid.uuid4(),
        source_gate_state_version=1,
        triage_gate=GateResultSchema(
            gate_name="triage",
            policy_version="triage.v1",
            input_state_version=1,
            decision=GateDecision.PASSED,
            details={},
        ),
        completeness_gate=GateResultSchema(
            gate_name="completeness",
            policy_version="completeness.v1",
            input_state_version=1,
            decision=GateDecision.PASSED,
            details={},
        ),
        intake_graph_run_id=uuid.uuid4(),
        advance_run_id=None,
    )


async def test_current_authority_cache_hit_avoids_repeated_db_round_trips() -> None:
    """同 claim 重复读取 authority，DB 调用不增长。"""
    reasoning_module._REASONING_AUTHORITY_CACHE.clear()
    session_id = uuid.uuid4()
    claim_id = uuid.uuid4()
    state = _make_state(3, session_id)
    authority = _make_authority(3, session_id)
    repo = _CountingRepository(session_id=session_id, states=[state], authorities=[authority])

    first = await reasoning_module._current_authority(repo, session_id, claim_id=claim_id)
    second = await reasoning_module._current_authority(repo, session_id, claim_id=claim_id)
    third = await reasoning_module._current_authority(repo, session_id, claim_id=claim_id)

    assert first is authority and second is authority and third is authority
    # 首次 miss：1 get_state + 1 get_reasoning_authority；后续 hit 不触发
    assert repo.get_state_calls == 1
    assert repo.get_authority_calls == 1


async def test_authority_cache_must_be_invalidated_after_state_version_bump() -> None:
    """复现 T1.2 缓存版本错位风险：simulate syndrome commit 后未失效缓存，
    下游读到 stale N 版本，让 formula 提交撞 STATE_VERSION_CONFLICT。

    本测试直接验证正确行为——pop 后下次读取返回 post-commit 版本。
    """
    reasoning_module._REASONING_AUTHORITY_CACHE.clear()
    session_id = uuid.uuid4()
    claim_id = uuid.uuid4()
    pre_state = _make_state(5, session_id)
    post_state = _make_state(6, session_id)
    pre_authority = _make_authority(5, session_id)
    post_authority = _make_authority(6, session_id)
    repo = _CountingRepository(
        session_id=session_id,
        states=[pre_state, post_state],
        authorities=[pre_authority, post_authority],
    )

    # 1. precheck 路径：灌缓存（N=5）
    precommit = await reasoning_module._current_authority(repo, session_id, claim_id=claim_id)
    assert precommit is pre_authority
    assert repo.get_state_calls == 1

    # 2. simulate _commit_syndrome_artifact 提交成功：state_version N→N+1
    repo.advance()
    # 3. 关键失效契约：commit 必须 pop 缓存，否则 build_formula_context 仍命中 stale
    reasoning_module._REASONING_AUTHORITY_CACHE.pop(claim_id, None)

    # 4. 下游 build_formula_context 重读 authority → 必须拿到 N+1 版本
    postcommit = await reasoning_module._current_authority(repo, session_id, claim_id=claim_id)
    assert postcommit is post_authority
    assert postcommit.current_state_version == 6
    assert repo.get_state_calls == 2


def test_commit_helpers_invalidate_authority_cache_source_contract() -> None:
    """源码契约：防止后续重构误删 _commit_*_artifact 体内的 cache pop 行。

    这两个 pop 是 build_formula_context 拿到 post-commit version 的唯一唯一
    防线；删掉会导致 STATE_VERSION_CONFLICT 复现（见 test_authority_cache_must_be_invalidated_after_state_version_bump）。
    """
    syndrome_src = inspect.getsource(reasoning_module._commit_syndrome_artifact)
    formula_src = inspect.getsource(reasoning_module._commit_formula_artifact)
    assert "_REASONING_AUTHORITY_CACHE.pop(claim.id, None)" in syndrome_src, (
        "_commit_syndrome_artifact must pop _REASONING_AUTHORITY_CACHE after commit "
        "to invalidate stale pre-commit snapshots for downstream build_formula_context"
    )
    assert "_REASONING_AUTHORITY_CACHE.pop(claim.id, None)" in formula_src, (
        "_commit_formula_artifact must pop _REASONING_AUTHORITY_CACHE after commit "
        "(defensive: 当前图拓扑无下游 reader，但保留避免拓扑变更漏失效)"
    )