"""Independent-process worker for HTTP command idempotency acceptance tests.

The database URL is accepted only through the guarded ``TEST_DATABASE_URL``
environment variable.  Command-line arguments intentionally carry no
credentials; the non-sensitive worker configuration is supplied as JSON in a
dedicated environment variable.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from _database_safety import require_destructive_test_database

_CONFIG_ENV = "XUANHU_IDEMPOTENCY_PROCESS_CONFIG"
_WAIT_SECONDS = 30.0


def _configure_process() -> None:
    """Fail closed and route this fresh process only to the isolated test DB."""

    database_url = require_destructive_test_database()
    os.environ["DB_URL"] = database_url
    os.environ["OUTBOX_PUBLISHER_ENABLED"] = "false"
    repository_root = str(Path(__file__).resolve().parents[1])
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)


def _load_config() -> dict[str, Any]:
    raw = os.environ.get(_CONFIG_ENV, "")
    if not raw:
        raise RuntimeError("worker configuration is missing")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise TypeError("worker configuration must be an object")
    required_strings = (
        "label",
        "operation",
        "scope",
        "idempotency_key",
        "session_id",
        "patient_ref",
        "effect_token",
        "ready_file",
        "start_file",
        "entered_file",
    )
    for name in required_strings:
        if not isinstance(payload.get(name), str) or not payload[name]:
            raise TypeError(f"worker configuration field {name!r} must be a non-empty string")
    peer_entered_files = payload.get("peer_entered_files")
    if not isinstance(peer_entered_files, list) or not peer_entered_files:
        raise TypeError("worker configuration field 'peer_entered_files' must be a non-empty list")
    if not all(isinstance(value, str) and value for value in peer_entered_files):
        raise TypeError("peer entered paths must be non-empty strings")
    return payload


async def _wait_for_files(paths: list[Path], *, timeout: float = _WAIT_SECONDS) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not all(path.is_file() for path in paths):
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("process barrier timed out")
        await asyncio.sleep(0.01)


async def _run(config: dict[str, Any]) -> dict[str, Any]:
    from app.db.session import get_session_factory, reset_session_factory
    from app.models.consult import ConsultSession
    from app.services.http_idempotency import HttpCommandExecutor

    factory = get_session_factory()
    ready_file = Path(config["ready_file"])
    start_file = Path(config["start_file"])
    entered_file = Path(config["entered_file"])
    peer_entered_files = [Path(value) for value in config["peer_entered_files"]]
    ready_file.touch()
    await _wait_for_files([start_file])

    async with factory() as db:

        async def handler() -> dict[str, Any]:
            # The owner does not complete until both OS processes have entered
            # execute().  This makes the test exercise an in-flight contender,
            # rather than a merely sequential restart/replay.
            await _wait_for_files(peer_entered_files)
            await asyncio.sleep(0.25)
            db.add(
                ConsultSession(
                    id=uuid.UUID(config["session_id"]),
                    patient_ref=config["patient_ref"],
                    patient_info={},
                    state_version=1,
                )
            )
            return {
                "session_id": config["session_id"],
                "effect_token": config["effect_token"],
                "executed_by_pid": os.getpid(),
            }

        entered_file.touch()
        result = await HttpCommandExecutor(db, session_factory=factory).execute(
            operation=config["operation"],
            scope_key=config["scope"],
            concurrency_scope=config["scope"],
            idempotency_key=config["idempotency_key"],
            is_idempotent=True,
            request_payload={
                "body": {
                    "session_id": config["session_id"],
                    "effect_token": config["effect_token"],
                }
            },
            success_status=201,
            success_message="created",
            handler=handler,
        )

    await reset_session_factory()
    return {
        "status": "ok",
        "label": config["label"],
        "worker_pid": os.getpid(),
        "data": result.data,
        "http_status": result.status_code,
        "message": result.message,
        "replayed": result.replayed,
    }


def _safe_error(exc: Exception) -> dict[str, Any]:
    return {
        "status": "error",
        "code": "IDEMPOTENCY_PROCESS_WORKER_FAILED",
        "error_type": type(exc).__name__,
    }


def main() -> int:
    try:
        _configure_process()
        result = asyncio.run(_run(_load_config()))
    except Exception as exc:
        result = _safe_error(exc)
        return_code = 1
    else:
        return_code = 0
    sys.stdout.write(json.dumps(result, ensure_ascii=True, sort_keys=True) + "\n")
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
