"""Read-only, privacy-safe L9 rollout and Legacy-drain readiness check.

Examples::

    uv run python -m scripts.check_runtime_rollout --require-phase canary
    uv run python -m scripts.check_runtime_rollout \
      --require-phase full --require-legacy-drained

Only aggregate counts and allowlisted configuration/audit values are printed.
No session identifiers, patient data, connection strings, or operator reasons
are emitted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.exceptions import XuanhuError
from app.db.session import get_session_factory
from app.models.consult import ConsultSession
from app.services.runtime_rollout import RuntimeRolloutPhase, select_new_session_runtime
from app.services.runtime_rollout_phase_audit import (
    PostgresRuntimeRolloutPhaseAuditRepository,
    RuntimeRolloutPhaseAuditError,
    RuntimeRolloutPhaseAuditService,
)
from app.services.runtime_switch_audit import (
    PostgresRuntimeSwitchAuditRepository,
    RuntimeSwitchAuditError,
    RuntimeSwitchAuditService,
)

_PHASES: tuple[RuntimeRolloutPhase, ...] = (
    "legacy",
    "development",
    "automated_test",
    "internal",
    "canary",
    "full",
    "rollback",
)
_TERMINAL_STATUSES = {"done", "terminated"}
_MAX_STABLE_WINDOW_MINUTES = 525_600
_DEPLOYMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,63}$")


def _stable_window_minutes(value: str) -> int:
    try:
        minutes = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("stable window must be an integer") from exc
    if not 1 <= minutes <= _MAX_STABLE_WINDOW_MINUTES:
        raise argparse.ArgumentTypeError(f"stable window must be between 1 and {_MAX_STABLE_WINDOW_MINUTES} minutes")
    return minutes


def _deployment_id(value: str) -> str:
    if _DEPLOYMENT_ID_RE.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("deployment id must be 8-64 allowlisted characters")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check audited L9 runtime rollout and Legacy drain readiness",
    )
    parser.add_argument("--require-phase", choices=_PHASES)
    parser.add_argument("--require-legacy-drained", action="store_true")
    parser.add_argument(
        "--require-stable-minutes",
        type=_stable_window_minutes,
        help="fail closed unless the current audited full cutover is at least this old",
    )
    parser.add_argument(
        "--deployment-id",
        type=_deployment_id,
        help="expected durable rollout-phase deployment identity",
    )
    return parser


async def _collect() -> dict[str, Any]:
    settings = get_settings()
    factory = get_session_factory()
    counts = {
        "legacy": {"open": 0, "terminal": 0},
        "langgraph": {"open": 0, "terminal": 0},
    }
    async with factory() as db:
        phase_repository = PostgresRuntimeRolloutPhaseAuditRepository(db)
        await phase_repository.lock_for_status()
        runtime_repository = PostgresRuntimeSwitchAuditRepository(db)
        audit = await RuntimeSwitchAuditService(runtime_repository).status(settings.agent_runtime_version)
        latest_runtime_switch = await runtime_repository.latest()
        phase_audit = await RuntimeRolloutPhaseAuditService(phase_repository).status(
            configured_phase=settings.agent_runtime_rollout_phase,
            configured_runtime=settings.agent_runtime_version,
            runtime_switch_deployment_id=(
                latest_runtime_switch.deployment_id if latest_runtime_switch is not None else None
            ),
            expected_phase_deployment_id=None,
        )
        rows = (
            await db.execute(
                select(
                    ConsultSession.agent_runtime,
                    ConsultSession.status,
                    func.count(),
                ).group_by(ConsultSession.agent_runtime, ConsultSession.status)
            )
        ).all()
    for runtime, status, count in rows:
        if runtime not in counts:
            continue
        bucket = "terminal" if status in _TERMINAL_STATUSES else "open"
        counts[runtime][bucket] += int(count)

    policy_ready = True
    policy_error_code: str | None = None
    try:
        select_new_session_runtime(settings, None)
    except XuanhuError as exc:
        policy_ready = False
        policy_error_code = exc.code

    legacy_removal_ready = bool(
        settings.agent_runtime_rollout_phase == "full"
        and settings.agent_runtime_version == "langgraph"
        and settings.langgraph_product_ready
        and audit.status == "ok"
        and phase_audit.status == "ok"
        and policy_ready
        and counts["legacy"]["open"] == 0
    )
    full_phase_age_seconds = (
        max(
            0,
            int((datetime.now(UTC) - phase_audit.full_entered_at).total_seconds()),
        )
        if phase_audit.full_entered_at is not None
        else None
    )
    ready = audit.status == "ok" and phase_audit.status == "ok" and policy_ready
    return {
        "status": "ready" if ready else "blocked",
        "phase": settings.agent_runtime_rollout_phase,
        "configured_runtime": settings.agent_runtime_version,
        "audited_runtime": audit.audited_runtime,
        "audit_present": audit.audit_present,
        "phase_audit_status": phase_audit.status,
        "phase_audit_present": phase_audit.audit_present,
        "phase_deployment_id": phase_audit.deployment_id,
        "runtime_switch_deployment_id": (
            latest_runtime_switch.deployment_id if latest_runtime_switch is not None else None
        ),
        "full_phase_age_seconds": full_phase_age_seconds,
        "public_langgraph_enabled": settings.langgraph_public_enabled,
        "product_ready_authorized": settings.langgraph_product_ready,
        "policy_error_code": policy_error_code,
        "sessions": counts,
        "legacy_removal_ready": legacy_removal_ready,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = asyncio.run(_collect())
    except (
        RuntimeRolloutPhaseAuditError,
        RuntimeSwitchAuditError,
        XuanhuError,
    ):
        print(
            json.dumps(
                {"status": "blocked", "error_code": "RUNTIME_ROLLOUT_CHECK_REJECTED"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except Exception:
        print(
            json.dumps(
                {"status": "failed", "error_code": "RUNTIME_ROLLOUT_CHECK_FAILED"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    exit_code = 0

    def block(requirement_error_code: str) -> None:
        nonlocal exit_code
        result["status"] = "blocked"
        result.setdefault("requirement_error_code", requirement_error_code)
        exit_code = 2

    if result["status"] != "ready":
        exit_code = 2
    if args.require_phase is not None and result["phase"] != args.require_phase:
        block("ROLLOUT_PHASE_MISMATCH")
    if args.require_legacy_drained and not result["legacy_removal_ready"]:
        block("LEGACY_NOT_DRAINED")
    if args.deployment_id is not None and result.get("phase_deployment_id") != args.deployment_id:
        block("ROLLOUT_DEPLOYMENT_MISMATCH")
    if args.require_stable_minutes is not None:
        required_seconds = args.require_stable_minutes * 60
        result["required_stable_seconds"] = required_seconds
        if args.deployment_id is None:
            block("STABLE_WINDOW_DEPLOYMENT_REQUIRED")
        observed_age = result.get("full_phase_age_seconds")
        stable_window_ready = bool(
            result.get("phase") == "full"
            and result.get("configured_runtime") == "langgraph"
            and result.get("audited_runtime") == "langgraph"
            and result.get("audit_present") is True
            and result.get("phase_audit_status") == "ok"
            and result.get("phase_audit_present") is True
            and args.deployment_id is not None
            and result.get("phase_deployment_id") == args.deployment_id
            and isinstance(observed_age, int)
            and observed_age >= required_seconds
        )
        result["stable_window_ready"] = stable_window_ready
        if not stable_window_ready:
            block("STABLE_WINDOW_NOT_MET")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
