"""Record an explicit deployment-time default Agent runtime switch.

Example::

    AGENT_RUNTIME_VERSION=langgraph uv run python -m scripts.audit_runtime_switch \
      --from-runtime legacy --to-runtime langgraph \
      --operator release-bot --reason "approved canary rollout" \
      --deployment-id release-2026-07-14-001

The command never changes existing sessions or the environment variable.  It
only records the already-authorized deployment transition in PostgreSQL.
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
from app.services.runtime_switch_audit import (
    PostgresRuntimeSwitchAuditRepository,
    RuntimeSwitchAuditError,
    RuntimeSwitchAuditService,
    RuntimeSwitchRecord,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record an audited AGENT_RUNTIME_VERSION deployment switch",
    )
    parser.add_argument("--from-runtime", choices=("legacy", "langgraph"), required=True)
    parser.add_argument("--to-runtime", choices=("legacy", "langgraph"), required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--deployment-id", required=True)
    return parser


async def _record(args: argparse.Namespace) -> dict[str, object]:
    settings = get_settings()
    record = RuntimeSwitchRecord(
        from_runtime=args.from_runtime,
        to_runtime=args.to_runtime,
        operator=args.operator,
        reason=args.reason,
        deployment_id=args.deployment_id,
        timestamp=datetime.now(UTC),
    )
    factory = get_session_factory()
    async with factory() as db, db.begin():
        stored, replayed = await RuntimeSwitchAuditService(
            PostgresRuntimeSwitchAuditRepository(db)
        ).record_switch(
            record,
            configured_runtime=settings.agent_runtime_version,
        )
    # Do not echo ``reason``: deployment logs need provenance, not arbitrary
    # operator text.
    return {
        "status": "replayed" if replayed else "recorded",
        "from_runtime": stored.from_runtime,
        "to_runtime": stored.to_runtime,
        "operator": stored.operator,
        "deployment_id": stored.deployment_id,
        "timestamp": stored.timestamp.isoformat(),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = asyncio.run(_record(args))
    except (RuntimeSwitchAuditError, ValidationError):
        print(
            json.dumps(
                {"status": "rejected", "error_code": "RUNTIME_SWITCH_REJECTED"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except Exception:
        # Deployment logs must never receive a connection string, SQL text,
        # operator reason, or arbitrary lower-layer exception message.
        print(
            json.dumps(
                {"status": "failed", "error_code": "RUNTIME_SWITCH_AUDIT_FAILED"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
