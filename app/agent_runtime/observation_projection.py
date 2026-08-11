"""Deterministic pure projection of current semantic observations (R2-B1).

The append-only fact model keeps every observation event, but only the **chain
heads** are current semantic truth:

- an observation is current when its ``(session_id, observation_id)`` pair is
  not superseded by a CORRECTED or RETRACTED event;
- its own status is not RETRACTED;
- both ACTIVE roots and CORRECTED successors can be current, depending on chain
  position.

This module is the single pure projection shared by the reducer and the R2-A
slot projection so the whole runtime agrees on "what the patient currently
said" without re-implementing chain walking in each consumer.
"""

from __future__ import annotations

from typing import Any

#: Statuses whose ``supersedes_observation_id`` target is removed from current truth.
_SUPERSEDING_STATUSES = frozenset({"corrected", "retracted"})
#: Statuses that are never themselves current truth.
_NON_CURRENT_STATUSES = frozenset({"retracted"})


def _status_of(fact: Any) -> str | None:
    status = getattr(fact, "status", None)
    if status is None:
        return None
    return str(status).lower()


def _observation_id_of(fact: Any) -> str:
    value = getattr(fact, "observation_id", None)
    return "" if value is None else str(value)


def _supersedes_of(fact: Any) -> str | None:
    value = getattr(fact, "supersedes_observation_id", None)
    return None if value is None else str(value)


def _session_id_of(fact: Any) -> str:
    value = getattr(fact, "session_id", None)
    return "" if value is None else str(value)


def _fact_key_of(fact: Any) -> str:
    value = getattr(fact, "fact_key", None)
    return "" if value is None else str(value)


def project_current_observations(
    observations: Any,
    *,
    session_id: Any = None,
) -> tuple[Any, ...]:
    """Return current semantic chain heads as a stable-order tuple.

    - ``observations``: an iterable of observation-like objects accepted in any
      input order.  The inputs are never mutated.
    - ``session_id``: when given, only observations of that session are
      projected; other sessions are never mixed in silently.  When omitted the
      full history is projected (each session's chains are still walked
      independently and never merged).
    - Supersession is scoped to the session that issued the superseding event:
      a CORRECTED/RETRACTED event removes the ``(session_id, target)`` pair, so
      a cross-session reference never suppresses another session's row.
    - CORRECTED chain heads are included as current truths; their superseded
      targets and any RETRACTED heads are removed.
    - The result is ordered by ``(session_id, fact_key, observation_id)`` so it
      is independent of input order and never collapses distinct canonical
      fact keys.
    """
    items = list(observations)
    if session_id is not None:
        expected = str(session_id)
        items = [item for item in items if _session_id_of(item) == expected]

    # Supersession identity is the (session_id, target) pair of the superseding
    # event: a CORRECTED/RETRACTED event only retires rows of its own session,
    # never a row of another session that happens to reuse the observation_id.
    superseded: set[tuple[str, str]] = set()
    for item in items:
        if _status_of(item) in _SUPERSEDING_STATUSES:
            target = _supersedes_of(item)
            if target is not None and target:
                superseded.add((_session_id_of(item), target))

    current = [
        item
        for item in items
        if (_session_id_of(item), _observation_id_of(item)) not in superseded
        and _status_of(item) not in _NON_CURRENT_STATUSES
    ]
    current.sort(
        key=lambda item: (_session_id_of(item), _fact_key_of(item), _observation_id_of(item))
    )
    return tuple(current)
