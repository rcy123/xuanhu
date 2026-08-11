"""Pure unit tests for the R2-B1 current-semantic observation projection.

The projection decides which observations are current semantic truth (chain
heads) so the reducer and the R2-A slot projection agree.  These tests pin the
chain-walking rules, the session filter, permutation stability, and the
no-mutation guarantee — all with deterministic ids and timestamps.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

from app.agent_runtime.observation_projection import project_current_observations
from app.schemas.domain import ObservationSchema, ObservationStatus

BASE_TS = datetime(2026, 8, 10, 8, 0, 0, tzinfo=UTC)
SESSION = UUID("00000000-0000-0000-0000-000000000001")
OTHER_SESSION = UUID("00000000-0000-0000-0000-000000000002")
SOURCE = UUID("00000000-0000-0000-0000-000000000099")


def _uuid(suffix: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{suffix:012d}")


def _obs(
    *,
    key: str,
    value: object,
    obs_id: UUID,
    session_id: UUID = SESSION,
    status: ObservationStatus = ObservationStatus.ACTIVE,
    target_id: UUID | None = None,
    normalized_value: object = None,
) -> ObservationSchema:
    return ObservationSchema(
        observation_id=obs_id,
        session_id=session_id,
        fact_key=key,
        value=value,
        normalized_value=normalized_value,
        source_message_id=SOURCE,
        status=status,
        supersedes_observation_id=target_id,
        created_at=BASE_TS,
    )


def test_plain_active_root_is_current() -> None:
    root = _obs(key="a.fact", value="v1", obs_id=_uuid(10))
    assert project_current_observations([root]) == (root,)


def test_corrected_successor_is_head_and_supersedes_root() -> None:
    root = _obs(key="a.fact", value="v1", obs_id=_uuid(10))
    successor = _obs(
        key="a.fact",
        value="v2",
        obs_id=_uuid(11),
        status=ObservationStatus.CORRECTED,
        target_id=_uuid(10),
    )
    projected = project_current_observations([root, successor])
    assert projected == (successor,)


def test_retracted_successor_removes_whole_chain() -> None:
    root = _obs(key="a.fact", value="v1", obs_id=_uuid(10))
    tombstone = _obs(
        key="a.fact",
        value=None,
        obs_id=_uuid(12),
        status=ObservationStatus.RETRACTED,
        target_id=_uuid(10),
    )
    assert project_current_observations([root, tombstone]) == ()


def test_corrected_then_retracted_chain_has_no_head() -> None:
    root = _obs(key="a.fact", value="v1", obs_id=_uuid(10))
    corrected = _obs(
        key="a.fact",
        value="v2",
        obs_id=_uuid(11),
        status=ObservationStatus.CORRECTED,
        target_id=_uuid(10),
    )
    tombstone = _obs(
        key="a.fact",
        value=None,
        obs_id=_uuid(12),
        status=ObservationStatus.RETRACTED,
        target_id=_uuid(11),
    )
    assert project_current_observations([root, corrected, tombstone]) == ()


def test_multi_link_corrected_chain_exposes_only_tip() -> None:
    root = _obs(key="a.fact", value="v1", obs_id=_uuid(10))
    middle = _obs(
        key="a.fact",
        value="v2",
        obs_id=_uuid(11),
        status=ObservationStatus.CORRECTED,
        target_id=_uuid(10),
    )
    tip = _obs(
        key="a.fact",
        value="v3",
        obs_id=_uuid(12),
        status=ObservationStatus.CORRECTED,
        target_id=_uuid(11),
    )
    assert project_current_observations([root, middle, tip]) == (tip,)


def test_independent_chains_are_not_collapsed() -> None:
    a_root = _obs(key="a.fact", value="a1", obs_id=_uuid(10))
    a_tip = _obs(
        key="a.fact",
        value="a2",
        obs_id=_uuid(11),
        status=ObservationStatus.CORRECTED,
        target_id=_uuid(10),
    )
    b_root = _obs(key="b.fact", value="b1", obs_id=_uuid(20))
    b_tip = _obs(
        key="b.fact",
        value="b2",
        obs_id=_uuid(21),
        status=ObservationStatus.CORRECTED,
        target_id=_uuid(20),
    )
    projected = project_current_observations([a_root, b_root, a_tip, b_tip])
    # Distinct canonical fact keys stay distinct heads, ordered by fact_key.
    assert [item.fact_key for item in projected] == ["a.fact", "b.fact"]
    assert projected == (a_tip, b_tip)


def test_dangling_corrected_head_is_current() -> None:
    # A CORRECTED event whose target is not present has nothing to supersede,
    # so it is itself a current head (broken input handled deterministically).
    dangling = _obs(
        key="a.fact",
        value="v2",
        obs_id=_uuid(11),
        status=ObservationStatus.CORRECTED,
        target_id=_uuid(999),
    )
    assert project_current_observations([dangling]) == (dangling,)


def test_session_filter_isolates_and_same_key_across_sessions_stays_separate() -> None:
    a = _obs(key="a.fact", value="s1", obs_id=_uuid(10), session_id=SESSION)
    b = _obs(key="a.fact", value="s2", obs_id=_uuid(11), session_id=OTHER_SESSION)
    # Same canonical fact_key across two sessions is two independent heads,
    # never merged into one current truth.
    assert project_current_observations([b, a]) == (a, b)
    # Filtering by one session returns only that session's chain head.
    assert project_current_observations([a, b], session_id=SESSION) == (a,)
    assert project_current_observations([a, b], session_id=OTHER_SESSION) == (b,)
    # A filter of a session with no facts is empty, not silently mixed.
    assert project_current_observations([a, b], session_id=uuid4()) == ()


def test_cross_session_corrected_never_suppresses_other_session_root() -> None:
    # Session B reuses session A's observation_id and then CORRECTs it.  The
    # supersession must be scoped to (session_id, target): only session B's row
    # is retired, session A's root stays a current head.
    a_root = _obs(key="a.fact", value="a1", obs_id=_uuid(10), session_id=SESSION)
    b_root = _obs(key="a.fact", value="b1", obs_id=_uuid(10), session_id=OTHER_SESSION)
    b_corrected = _obs(
        key="a.fact",
        value="b2",
        obs_id=_uuid(11),
        session_id=OTHER_SESSION,
        status=ObservationStatus.CORRECTED,
        target_id=_uuid(10),
    )
    # Unfiltered: session A root plus session B's own CORRECTED head, in order.
    assert project_current_observations([a_root, b_root, b_corrected]) == (a_root, b_corrected)
    # Filtered views stay isolated: session B's correction never touches A.
    assert project_current_observations([a_root, b_root, b_corrected], session_id=SESSION) == (a_root,)
    assert project_current_observations([a_root, b_root, b_corrected], session_id=OTHER_SESSION) == (b_corrected,)


def test_cross_session_retracted_never_suppresses_other_session_root() -> None:
    # Same shared-id scenario but the superseding event is a RETRACTED tombstone.
    a_root = _obs(key="a.fact", value="a1", obs_id=_uuid(10), session_id=SESSION)
    b_root = _obs(key="a.fact", value="b1", obs_id=_uuid(10), session_id=OTHER_SESSION)
    b_tombstone = _obs(
        key="a.fact",
        value=None,
        obs_id=_uuid(12),
        session_id=OTHER_SESSION,
        status=ObservationStatus.RETRACTED,
        target_id=_uuid(10),
    )
    # Session A's root survives; session B's chain is fully gone.
    assert project_current_observations([a_root, b_root, b_tombstone]) == (a_root,)
    assert project_current_observations([a_root, b_root, b_tombstone], session_id=SESSION) == (a_root,)
    assert project_current_observations([a_root, b_root, b_tombstone], session_id=OTHER_SESSION) == ()


def test_dangling_cross_session_corrected_keeps_other_session_root() -> None:
    # Session B issues a CORRECTED event referencing an id that only exists in
    # session A.  The pair key keeps session A's head current while session B's
    # dangling CORRECTED is itself a head.
    a_root = _obs(key="a.fact", value="a1", obs_id=_uuid(10), session_id=SESSION)
    b_dangling = _obs(
        key="b.fact",
        value="b1",
        obs_id=_uuid(21),
        session_id=OTHER_SESSION,
        status=ObservationStatus.CORRECTED,
        target_id=_uuid(10),
    )
    projected = project_current_observations([a_root, b_dangling])
    assert projected == (a_root, b_dangling)


def test_result_is_stable_across_input_permutations() -> None:
    items = [
        _obs(key="b.fact", value="b1", obs_id=_uuid(20)),
        _obs(key="a.fact", value="a1", obs_id=_uuid(10)),
        _obs(key="a.fact", value="a2", obs_id=_uuid(11), status=ObservationStatus.CORRECTED, target_id=_uuid(10)),
        _obs(key="d.fact", value="d1", obs_id=_uuid(40)),
        _obs(key="d.fact", value=None, obs_id=_uuid(41), status=ObservationStatus.RETRACTED, target_id=_uuid(40)),
        _obs(key="c.fact", value="c1", obs_id=_uuid(30)),
        _obs(key="b.fact", value="b2", obs_id=_uuid(21), status=ObservationStatus.CORRECTED, target_id=_uuid(20)),
    ]
    expected = project_current_observations(items)
    assert [item.observation_id for item in expected] == [_uuid(11), _uuid(21), _uuid(30)]
    permutations = (
        list(items),
        list(reversed(items)),
        items[::2] + items[1::2],
        items[3:] + items[:3],
        items[-2:] + items[:-2],
        sorted(items, key=lambda item: str(item.observation_id)),
    )
    for permuted in permutations:
        assert project_current_observations(permuted) == expected


def test_input_list_is_not_mutated() -> None:
    items = [
        _obs(key="a.fact", value="a1", obs_id=_uuid(10)),
        _obs(key="a.fact", value="a2", obs_id=_uuid(11), status=ObservationStatus.CORRECTED, target_id=_uuid(10)),
        _obs(key="b.fact", value=None, obs_id=_uuid(12), status=ObservationStatus.RETRACTED, target_id=_uuid(11)),
    ]
    snapshot = list(items)
    project_current_observations(items)
    assert items == snapshot


def test_string_statuses_and_simple_namespaces_are_accepted() -> None:
    root = SimpleNamespace(
        observation_id=_uuid(10),
        session_id=SESSION,
        fact_key="a.fact",
        value="a1",
        normalized_value=None,
        source_message_id=SOURCE,
        status="active",
        created_at=BASE_TS,
    )
    successor = SimpleNamespace(
        observation_id=_uuid(11),
        session_id=SESSION,
        fact_key="a.fact",
        value="a2",
        normalized_value="a2",
        source_message_id=SOURCE,
        status="corrected",
        supersedes_observation_id=_uuid(10),
        created_at=BASE_TS,
    )
    tombstone = SimpleNamespace(
        observation_id=_uuid(12),
        session_id=SESSION,
        fact_key="b.fact",
        value=None,
        normalized_value=None,
        source_message_id=SOURCE,
        status="retracted",
        supersedes_observation_id=_uuid(99),
        created_at=BASE_TS,
    )
    projected = project_current_observations([root, successor, tombstone])
    assert len(projected) == 1
    assert projected[0].observation_id == _uuid(11)
    # Effective value comes from normalized_value when present.
    assert projected[0].normalized_value == "a2"


def test_langgraph_intake_wrapper_delegates_to_shared_projection() -> None:
    """R2-B1：服务层 langgraph_intake._current_observations 是共享投影的薄包装。

    旧实现只排除被取代根与 RETRACTED，不认 CORRECTED 链头、也不按
    (session_id, target) 作用域判定 → 排列/语义与共享投影不一致。包装后必须
    与 project_current_observations 逐项等价（CORRECTED 头保留、RETRACTED 链剔除、
    排列稳定）。
    """
    from app.services.langgraph_intake import _current_observations

    root = _obs(key="a.fact", value="v1", obs_id=_uuid(10))
    corrected = _obs(
        key="a.fact",
        value="v2",
        obs_id=_uuid(11),
        status=ObservationStatus.CORRECTED,
        target_id=_uuid(10),
    )
    retracted = _obs(
        key="b.fact",
        value=None,
        obs_id=_uuid(12),
        status=ObservationStatus.RETRACTED,
        target_id=_uuid(20),
    )
    chain = (retracted, corrected, root)

    projected = _current_observations(chain)
    assert projected == (corrected,), "CORRECTED 链头保留、RETRACTED 链剔除"
    assert projected == project_current_observations(chain)
    assert _current_observations((root, corrected, retracted)) == projected, "排列必须稳定"
