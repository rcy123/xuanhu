"""L9 staged-rollout and rollback new-session policy contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pytest

from app.core.exceptions import (
    LegacyRuntimeCreationDisabledError,
    RuntimeRolloutNotReadyError,
    RuntimeSwitchAuditMismatchError,
)
from app.schemas.session import SessionCreateRequest
from app.services.runtime_rollout import (
    RuntimeRolloutPhase,
    select_new_session_runtime,
)
from app.services.runtime_switch_audit import (
    RuntimeSwitchAuditMismatch,
    RuntimeSwitchAuditService,
)
from app.services.session import SessionService


@dataclass(frozen=True)
class _Settings:
    agent_runtime_version: Literal["legacy", "langgraph"] = "legacy"
    agent_runtime_rollout_phase: RuntimeRolloutPhase = "legacy"
    langgraph_public_enabled: bool = False
    langgraph_product_ready: bool = False


def test_non_terminal_phases_preserve_explicit_canary_selection() -> None:
    settings = _Settings(agent_runtime_rollout_phase="canary")

    assert select_new_session_runtime(settings, None) == "legacy"
    assert select_new_session_runtime(settings, "langgraph") == "langgraph"


def test_full_phase_requires_all_fail_closed_authorities() -> None:
    with pytest.raises(RuntimeRolloutNotReadyError) as raised:
        select_new_session_runtime(
            _Settings(
                agent_runtime_version="langgraph",
                agent_runtime_rollout_phase="full",
                langgraph_public_enabled=True,
                langgraph_product_ready=False,
            ),
            None,
        )
    assert raised.value.detail is not None
    assert "phase=full" in raised.value.detail


def test_full_phase_forces_all_new_sessions_to_langgraph() -> None:
    settings = _Settings(
        agent_runtime_version="langgraph",
        agent_runtime_rollout_phase="full",
        langgraph_public_enabled=True,
        langgraph_product_ready=True,
    )

    assert select_new_session_runtime(settings, None) == "langgraph"
    assert select_new_session_runtime(settings, "langgraph") == "langgraph"
    with pytest.raises(LegacyRuntimeCreationDisabledError):
        select_new_session_runtime(settings, "legacy")


def test_rollback_only_changes_new_sessions_and_rejects_new_v2() -> None:
    settings = _Settings(
        agent_runtime_version="legacy",
        agent_runtime_rollout_phase="rollback",
        langgraph_public_enabled=True,
        langgraph_product_ready=True,
    )

    assert select_new_session_runtime(settings, None) == "legacy"
    assert select_new_session_runtime(settings, "legacy") == "legacy"
    with pytest.raises(RuntimeRolloutNotReadyError, match="暂停新建"):
        select_new_session_runtime(settings, "langgraph")


def test_rollback_rejects_a_stale_langgraph_default() -> None:
    with pytest.raises(RuntimeRolloutNotReadyError) as raised:
        select_new_session_runtime(
            _Settings(
                agent_runtime_version="langgraph",
                agent_runtime_rollout_phase="rollback",
            ),
            None,
        )
    assert raised.value.detail == "phase=rollback 要求 AGENT_RUNTIME_VERSION=legacy"


@pytest.mark.asyncio
async def test_terminal_phase_explicit_runtime_still_requires_durable_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def reject_audit(
        _self: RuntimeSwitchAuditService,
        configured_runtime: str,
    ) -> None:
        calls.append(configured_runtime)
        raise RuntimeSwitchAuditMismatch("missing terminal-phase audit")

    monkeypatch.setattr(
        RuntimeSwitchAuditService,
        "ensure_configured_runtime",
        reject_audit,
    )

    with pytest.raises(RuntimeSwitchAuditMismatchError):
        await SessionService(object()).create_session(  # type: ignore[arg-type]
            SessionCreateRequest(agent_runtime="langgraph"),
            doctor_id=None,
            trace_id="terminal-audit-test",
            require_runtime_audit=True,
        )

    assert calls == ["langgraph"]
