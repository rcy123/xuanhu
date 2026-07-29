"""Record an explicit deployment-time L9 rollout-phase transition.

The command records history only.  It never edits deployment configuration,
changes existing sessions, or toggles product gates.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime

from pydantic import ValidationError

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.services.runtime_rollout import RuntimeRolloutPhase
from app.services.runtime_rollout_phase_audit import (
    PostgresRuntimeRolloutPhaseAuditRepository,
    RuntimeRolloutPhaseAuditError,
    RuntimeRolloutPhaseAuditService,
    RuntimeRolloutPhaseRecord,
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record an audited AGENT_RUNTIME_ROLLOUT_PHASE transition",
    )
    parser.add_argument("--from-phase", choices=_PHASES, required=True)
    parser.add_argument("--to-phase", choices=_PHASES, required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--deployment-id", required=True)
    return parser


async def _record(args: argparse.Namespace) -> dict[str, object]:
    settings = get_settings()
    factory = get_session_factory()
    async with factory() as db, db.begin():
        phase_repository = PostgresRuntimeRolloutPhaseAuditRepository(db)
        # Freeze both durable ledgers before resolving the runtime deployment
        # reference. ``record_transition`` reacquires these transaction locks
        # safely and performs the write-side chain validation.
        await phase_repository.lock_chain()
        runtime_repository = PostgresRuntimeSwitchAuditRepository(db)
        runtime_service = RuntimeSwitchAuditService(runtime_repository)
        await runtime_service.ensure_configured_runtime(settings.agent_runtime_version)
        latest_runtime_switch = await runtime_repository.latest()
        runtime_deployment_id = latest_runtime_switch.deployment_id if latest_runtime_switch is not None else None
        record = RuntimeRolloutPhaseRecord(
            from_phase=args.from_phase,
            to_phase=args.to_phase,
            runtime=settings.agent_runtime_version,
            runtime_switch_deployment_id=runtime_deployment_id,
            operator=args.operator,
            reason=args.reason,
            deployment_id=args.deployment_id,
            timestamp=datetime.now(UTC),
        )
        stored, replayed = await RuntimeRolloutPhaseAuditService(phase_repository).record_transition(
            record,
            configured_phase=settings.agent_runtime_rollout_phase,
            configured_runtime=settings.agent_runtime_version,
            runtime_switch_deployment_id=runtime_deployment_id,
        )
    return {
        "status": "replayed" if replayed else "recorded",
        "from_phase": stored.from_phase,
        "to_phase": stored.to_phase,
        "runtime": stored.runtime,
        "runtime_switch_deployment_id": stored.runtime_switch_deployment_id,
        "operator": stored.operator,
        "deployment_id": stored.deployment_id,
        "timestamp": stored.timestamp.isoformat(),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = asyncio.run(_record(args))
    except (
        RuntimeRolloutPhaseAuditError,
        RuntimeSwitchAuditError,
        ValidationError,
    ):
        print(
            json.dumps(
                {
                    "status": "rejected",
                    "error_code": "ROLLOUT_PHASE_AUDIT_REJECTED",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except Exception:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": "ROLLOUT_PHASE_AUDIT_FAILED",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
