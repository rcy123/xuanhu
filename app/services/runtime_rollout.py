"""Fail-closed L9 policy for selecting the runtime of a new session.

The policy never mutates an existing session.  During ordinary development and
canary phases, existing explicit selection remains available behind the public
LangGraph flag.  The two terminal operational phases are stricter:

* ``full`` requires an explicitly authorized LangGraph product gate and
  rejects every request for a new Legacy session.
* ``rollback`` requires a Legacy deployment default and rejects every request
  for a new LangGraph session while existing v2 sessions retain their runtime.
"""

from __future__ import annotations

from typing import Literal, Protocol

from app.core.exceptions import (
    LegacyRuntimeCreationDisabledError,
    RuntimeRolloutNotReadyError,
)

RuntimeName = Literal["legacy", "langgraph"]
RuntimeRolloutPhase = Literal[
    "legacy",
    "development",
    "automated_test",
    "internal",
    "canary",
    "full",
    "rollback",
]


class RuntimeRolloutSettings(Protocol):
    agent_runtime_version: RuntimeName
    agent_runtime_rollout_phase: RuntimeRolloutPhase
    langgraph_public_enabled: bool
    langgraph_product_ready: bool


def select_new_session_runtime(
    settings: RuntimeRolloutSettings,
    requested_runtime: RuntimeName | None,
) -> RuntimeName:
    """Return the only runtime permitted for this new-session request."""

    phase = settings.agent_runtime_rollout_phase
    configured = settings.agent_runtime_version

    if phase == "full":
        if (
            configured != "langgraph"
            or not settings.langgraph_public_enabled
            or not settings.langgraph_product_ready
        ):
            raise RuntimeRolloutNotReadyError(
                detail=(
                    "phase=full 要求 AGENT_RUNTIME_VERSION=langgraph、"
                    "XUANHU_LANGGRAPH_PUBLIC_ENABLED=true 且 "
                    "XUANHU_LANGGRAPH_PRODUCT_READY=true"
                )
            )
        if requested_runtime == "legacy":
            raise LegacyRuntimeCreationDisabledError(
                detail="phase=full 仅允许新建 agent_runtime=langgraph 的会话"
            )
        return "langgraph"

    if phase == "rollback":
        if configured != "legacy":
            raise RuntimeRolloutNotReadyError(
                detail="phase=rollback 要求 AGENT_RUNTIME_VERSION=legacy"
            )
        if requested_runtime == "langgraph":
            raise RuntimeRolloutNotReadyError(
                message="回滚阶段暂停新建 LangGraph 会话",
                detail=(
                    "phase=rollback 只影响新会话；既有 LangGraph 会话"
                    "继续按持久化 agent_runtime 恢复"
                ),
                retryable=False,
            )
        return "legacy"

    return requested_runtime or configured


__all__ = [
    "RuntimeName",
    "RuntimeRolloutPhase",
    "RuntimeRolloutSettings",
    "select_new_session_runtime",
]
