"""R3-B: deterministic offline gate for the agent-trajectory evaluation suite.

This CLI is a regression gate, not a general-purpose tool.  Run from the
repository root with no arguments::

    uv run python -m scripts.evaluate_agent_trajectories

It may also be run directly as a script from any working directory::

    python scripts/evaluate_agent_trajectories.py

It loads the versioned bundled manifest at
``evals/agent_trajectories/manifest.v1.json`` (resolved from this file's own
location, never from user input), validates it, and evaluates every scenario
with the default deterministic executor.

Output contract:

- success: exactly one canonical JSON object (the ``SuiteEvaluationReport``)
  on stdout, nothing on stderr, exit code 0;
- any malformed, tampered, or unsafe manifest, or any evaluator failure: only
  a fixed PHI-safe failure object ``{"status": "failed", "error_code": ...}``
  on stderr, nothing on stdout, exit code nonzero.

The failure object never echoes input bytes, exception text, or any raw step
body.  No logs and no stack traces are ever emitted.

The loader is layered and fail-closed: a hard byte ceiling, strict UTF-8
decoding, strict JSON parsing, a top-level object check, strict Pydantic
schema validation (``extra="forbid"``, no coercion), and — still inside the
loader, before any executor can run — verification of the manifest digest,
every trajectory digest, and the ``SAFE_EXPECTED_INVARIANTS`` contract.
:func:`evaluate_suite` re-verifies the same invariants defensively.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make ``app`` importable whether this file runs via ``python -m
# scripts.evaluate_agent_trajectories`` or directly as ``python
# scripts/evaluate_agent_trajectories.py`` from an arbitrary working directory.
# The repository root is derived from this file's own location; there is no
# user-controlled path.  This must run before the ``app`` import below, or a
# direct run from a clean cwd would fail at import time.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pydantic import ValidationError  # noqa: E402

from app.agent_runtime.trajectory_evaluation import (  # noqa: E402
    SAFE_EXPECTED_INVARIANTS,
    SuiteManifest,
    TrajectoryEvaluationError,
    evaluate_suite,
    model_canonical_json,
    recorded_steps_executor,
    verify_manifest_digest,
    verify_trajectory_digest,
)

#: Hard ceiling on the number of manifest bytes accepted by the loader.
MAX_MANIFEST_BYTES = 1 << 16  # 64 KiB

# Fixed PHI-safe failure codes.  These are closed constants, never derived from
# input, so a failure object can never carry a payload fragment.
ERROR_MANIFEST_NOT_BYTES = "MANIFEST_NOT_BYTES"
ERROR_MANIFEST_OVERSIZE = "MANIFEST_OVERSIZE"
ERROR_MANIFEST_INVALID_UTF8 = "MANIFEST_INVALID_UTF8"
ERROR_MANIFEST_NOT_JSON = "MANIFEST_NOT_JSON"
ERROR_MANIFEST_NOT_OBJECT = "MANIFEST_NOT_OBJECT"
ERROR_MANIFEST_SCHEMA_INVALID = "MANIFEST_SCHEMA_INVALID"
ERROR_MANIFEST_DIGEST_MISMATCH = "MANIFEST_DIGEST_MISMATCH"
ERROR_TRAJECTORY_DIGEST_MISMATCH = "TRAJECTORY_DIGEST_MISMATCH"
ERROR_UNSAFE_EXPECTED_INVARIANTS = "UNSAFE_EXPECTED_INVARIANTS"
ERROR_MANIFEST_READ_FAILED = "MANIFEST_READ_FAILED"
ERROR_UNEXPECTED_ARGUMENT = "UNEXPECTED_ARGUMENT"
ERROR_INVARIANT_FAILURE = "INVARIANT_FAILURE"
ERROR_EVALUATION_FAILED = "EVALUATION_FAILED"

#: The bundled manifest is a fixed repository-root-relative path.
_MANIFEST_RELPATH = Path("evals") / "agent_trajectories" / "manifest.v1.json"


class LoaderError(ValueError):
    """A fixed-code rejection that never carries a manifest fragment."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def load_manifest_bytes(data: bytes | bytearray, *, max_bytes: int = MAX_MANIFEST_BYTES) -> SuiteManifest:
    """Load and strictly validate a manifest from raw bytes.

    Validates in layers: hard byte ceiling, strict UTF-8, strict JSON, a
    top-level JSON object, the frozen contract schema, and — before any
    executor can run — the manifest digest, every trajectory digest, and the
    ``SAFE_EXPECTED_INVARIANTS`` contract.  Every failure raises
    :class:`LoaderError` with a fixed, payload-free code.
    """
    if not isinstance(data, bytes | bytearray):
        raise LoaderError(ERROR_MANIFEST_NOT_BYTES)
    raw = bytes(data)
    if len(raw) > max_bytes:
        raise LoaderError(ERROR_MANIFEST_OVERSIZE)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LoaderError(ERROR_MANIFEST_INVALID_UTF8) from exc
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LoaderError(ERROR_MANIFEST_NOT_JSON) from exc
    if not isinstance(parsed, dict):
        raise LoaderError(ERROR_MANIFEST_NOT_OBJECT)
    try:
        # ``model_validate_json`` keeps the JSON-originated validation path so
        # the strict contract still accepts JSON enums and JSON arrays for the
        # ``steps`` tuple.  Python-dict ``model_validate`` would reject both.
        manifest = SuiteManifest.model_validate_json(text)
    except ValidationError as exc:
        raise LoaderError(ERROR_MANIFEST_SCHEMA_INVALID) from exc

    # Integrity and safety are verified here, inside the loader, so a tampered
    # or unsafe manifest is rejected before any executor can run.  The core
    # verifiers raise ``TrajectoryEvaluationError``; each failure is mapped to
    # a fixed loader code with the exception chain suppressed so no payload or
    # exception text can ever leak.
    try:
        verify_manifest_digest(manifest)
    except TrajectoryEvaluationError:
        raise LoaderError(ERROR_MANIFEST_DIGEST_MISMATCH) from None
    for trajectory in manifest.trajectories:
        try:
            verify_trajectory_digest(trajectory)
        except TrajectoryEvaluationError:
            raise LoaderError(ERROR_TRAJECTORY_DIGEST_MISMATCH) from None
        if trajectory.expected_invariants != SAFE_EXPECTED_INVARIANTS:
            raise LoaderError(ERROR_UNSAFE_EXPECTED_INVARIANTS)
    return manifest


def bundled_manifest_path() -> Path:
    """The fixed repository-root-relative bundled manifest path."""
    return _REPO_ROOT / _MANIFEST_RELPATH


def load_bundled_manifest() -> SuiteManifest:
    """Load the bundled manifest from the fixed repository-relative path."""
    return load_manifest_bytes(bundled_manifest_path().read_bytes())


def _fail(code: str) -> int:
    print(json.dumps({"status": "failed", "error_code": code}, sort_keys=True), file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    """Run the deterministic trajectory gate; see the module docstring."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        # No arguments are supported: in particular there is no manifest path.
        return _fail(ERROR_UNEXPECTED_ARGUMENT)
    try:
        manifest = load_bundled_manifest()
        suite_report = evaluate_suite(manifest, recorded_steps_executor)
    except LoaderError as exc:
        return _fail(exc.code)
    except TrajectoryEvaluationError as exc:
        return _fail(exc.code.value)
    except OSError:
        return _fail(ERROR_MANIFEST_READ_FAILED)
    except Exception:
        return _fail(ERROR_EVALUATION_FAILED)
    if suite_report.failed_count != 0:
        return _fail(ERROR_INVARIANT_FAILURE)
    print(model_canonical_json(suite_report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
