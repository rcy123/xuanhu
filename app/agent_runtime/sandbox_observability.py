"""L8-1: Episode Package, Metrics, Business Events and Failure Attribution.

Frozen, versioned contracts for sandbox observability — an in-memory,
append-only episode store that enforces idempotency, canonical snapshots,
and tamper-evident replay.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ── Version constant ───────────────────────────────────────────────────────

OBSERVABILITY_SCHEMA_VERSION: Literal["sandbox-observability.v1"] = "sandbox-observability.v1"

_SENSITIVE_METADATA_MARKERS: tuple[str, ...] = (
    "raw_prompt",
    "prompt_text",
    "model_output",
    "patient_id",
    "diagnosis",
    "treatment",
    "prescription",
    "ssn",
    "phone",
    "email",
    "address",
)

# ── Event type closed set ──────────────────────────────────────────────────


class TrajectoryEventType(StrEnum):
    """Closed set of allowed trajectory event types (L8-1 §6)."""

    NODE_STARTED = "node.started"
    NODE_COMPLETED = "node.completed"
    GATE_FAILED = "gate.failed"
    INTERRUPT_REQUIRED = "interrupt.required"
    GRAPH_COMPLETED = "graph.completed"
    GRAPH_FAILED = "graph.failed"


# ── Strict frozen base ─────────────────────────────────────────────────────


class _StrictFrozenModel(BaseModel):
    """Base: frozen, strict, no extra fields, JSON-safe everywhere."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


# ── NodeTrajectoryEventV1 ──────────────────────────────────────────────────


class NodeTrajectoryEventV1(_StrictFrozenModel):
    """A single step-level event in the episode trajectory.

    Never contains raw prompt, model raw output, clinical text, identity
    fields, or exception stacks (enforced by validator on ``metadata``).
    """

    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    event_type: TrajectoryEventType
    node_name: str = Field(default="", max_length=200)
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def _no_exception_stacks(cls, v: dict[str, str]) -> dict[str, str]:
        for key, val in v.items():
            if "Traceback" in val or 'File "' in val or "line " in val:
                raise ValueError(f"metadata field '{key}' appears to contain an exception stack")
            lowered = f"{key} {val}".lower()
            if any(marker in lowered for marker in _SENSITIVE_METADATA_MARKERS):
                raise ValueError(f"metadata field '{key}' contains prohibited content")
        return v

    @field_validator("node_name")
    @classmethod
    def _no_sensitive_text(cls, v: str) -> str:
        # Reject if it looks like a prompt, clinical text, or identity
        lowered = v.lower()
        if any(keyword in lowered for keyword in ["diagnosis", "patient", "treatment", "prescription"]):
            raise ValueError("node_name must not contain clinical or identity text")
        return v


# ── ModelUsageV1 ───────────────────────────────────────────────────────────


class ModelUsageV1(_StrictFrozenModel):
    """Aggregated model usage counters for one episode.

    All token fields are non-negative integers.
    """

    model_name: str = Field(default="", max_length=200)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    call_count: int = Field(default=0, ge=0)


# ── FailureAttributionV1 ───────────────────────────────────────────────────


class FailureAttributionV1(_StrictFrozenModel):
    """Structured, safe failure attribution.

    Contains no raw exception stack, prompt, model output, or clinical data.
    ``error_code`` is a short safe string; ``details`` maps safe keys to safe
    values only.
    """

    failure_type: str = Field(min_length=1, max_length=100)
    component: str = Field(min_length=1, max_length=100)
    error_code: str = Field(min_length=1, max_length=100)
    details: dict[str, str] = Field(default_factory=dict)

    @field_validator("details")
    @classmethod
    def _details_are_safe(cls, v: dict[str, str]) -> dict[str, str]:
        for key, value in v.items():
            lowered = f"{key} {value}".lower()
            if any(marker in lowered for marker in _SENSITIVE_METADATA_MARKERS):
                raise ValueError("failure details contain prohibited content")
        return v


# ── BusinessEventV1 ────────────────────────────────────────────────────────


class BusinessEventV1(_StrictFrozenModel):
    """A business-significant event with typed external references.

    ``reference_type`` is limited to the closed set:
    ``evidence``, ``verification``, ``gate``, ``human-intervention``.
    """

    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    event_type: str = Field(min_length=1, max_length=100)
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    reference_type: str = Field(pattern=r"^(evidence|verification|gate|human-intervention)$")
    reference_id: str = Field(min_length=1, max_length=200)


# ── EpisodePackageV1 ───────────────────────────────────────────────────────


class EpisodePackageV1(_StrictFrozenModel):
    """A versioned, frozen episode package.

    Binds — via deterministic fields — the state hash, graph version,
    agent-spec version, prompt version, schema version, policy version,
    model-actual/usage, and references to evidence, verification, gate,
    and human-intervention records.

    All fields are JSON-safe.  No field contains raw prompt, model raw
    output, clinical text, identity data, or exception stacks.
    """

    episode_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    schema_version: str = OBSERVABILITY_SCHEMA_VERSION
    state_hash: str = Field(default="", max_length=128)
    graph_version: str = Field(default="", max_length=100)
    agent_spec_version: str = Field(default="", max_length=100)
    prompt_version: str = Field(default="", max_length=100)
    policy_version: str = Field(default="", max_length=100)
    model_actual: str = Field(default="", max_length=200)
    model_usage: ModelUsageV1 = Field(default_factory=ModelUsageV1)
    trajectory: tuple[NodeTrajectoryEventV1, ...] = Field(default_factory=tuple)
    failure: FailureAttributionV1 | None = None
    business_events: tuple[BusinessEventV1, ...] = Field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = Field(default_factory=tuple)
    verification_refs: tuple[str, ...] = Field(default_factory=tuple)
    gate_refs: tuple[str, ...] = Field(default_factory=tuple)
    intervention_refs: tuple[str, ...] = Field(default_factory=tuple)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


# ── Canonical serialization helpers ────────────────────────────────────────


def _canonical_bytes(obj: BaseModel) -> bytes:
    """Deterministic, sorted-key JSON representation.

    Used for idempotency comparison and snapshot chaining.
    """
    return json.dumps(
        obj.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_digest(obj: BaseModel) -> str:
    """SHA-256 hex digest of canonical bytes."""
    return hashlib.sha256(_canonical_bytes(obj)).hexdigest()


# ── Metrics — fixed names / fixed labels ───────────────────────────────────

METRIC_NAMES: frozenset[str] = frozenset(
    {
        "episode.total",
        "episode.trajectory_events",
        "episode.business_events",
        "episode.model_input_tokens",
        "episode.model_output_tokens",
        "episode.model_total_tokens",
        "episode.model_call_count",
        "episode.failure_count",
        "episode.evidence_refs",
        "episode.verification_refs",
        "episode.gate_refs",
        "episode.intervention_refs",
    }
)

METRIC_LABELS: frozenset[str] = frozenset(
    {
        "schema_version",
        "graph_version",
        "agent_spec_version",
        "prompt_version",
        "policy_version",
        "has_failure",
    }
)


def _metric_label_value(value: str) -> str:
    """Return a stable label token that cannot break metric keys."""

    if value == "":
        return value
    sanitized: list[str] = []
    for char in value:
        if char.isalnum() or char in "._-":
            sanitized.append(char)
        else:
            sanitized.append("_")
    return "".join(sanitized)


def extract_episode_metrics(
    package: EpisodePackageV1,
) -> dict[str, int | float]:
    """Extract fixed-name, fixed-label metrics from an episode package.

    Returns a flat dict:
    - Keys are metric names from *METRIC_NAMES* (numeric counters).
    - Additional keys carry the fixed label dimension using the convention
      ``<metric_name>:label_<label_name>=<value>``.

    No dynamic labels, raw content, or arbitrary keys appear in the output.
    """
    metrics: dict[str, int | float] = {}
    metrics["episode.total"] = 1
    metrics["episode.trajectory_events"] = len(package.trajectory)
    metrics["episode.business_events"] = len(package.business_events)
    metrics["episode.model_input_tokens"] = package.model_usage.input_tokens
    metrics["episode.model_output_tokens"] = package.model_usage.output_tokens
    metrics["episode.model_total_tokens"] = package.model_usage.total_tokens
    metrics["episode.model_call_count"] = package.model_usage.call_count
    metrics["episode.failure_count"] = 1 if package.failure is not None else 0
    metrics["episode.evidence_refs"] = len(package.evidence_refs)
    metrics["episode.verification_refs"] = len(package.verification_refs)
    metrics["episode.gate_refs"] = len(package.gate_refs)
    metrics["episode.intervention_refs"] = len(package.intervention_refs)

    # Fixed-label dimension: emit one key per label applied to a base metric.
    labels: dict[str, str] = {
        "schema_version": package.schema_version,
        "graph_version": package.graph_version,
        "agent_spec_version": package.agent_spec_version,
        "prompt_version": package.prompt_version,
        "policy_version": package.policy_version,
        "has_failure": "true" if package.failure is not None else "false",
    }
    for name, value in labels.items():
        metrics[f"episode.total:label_{name}={_metric_label_value(value)}"] = 1

    return metrics


# ── Append-only in-memory store ────────────────────────────────────────────

_STORE_NOT_FOUND = object()


class EpisodeStoreError(ValueError):
    """Base for store errors — fixed, payload-free, chainless."""

    __slots__ = ()
    _MESSAGE: str = "EPISODE_STORE_ERROR"

    def __init__(self) -> None:
        super().__init__(self._MESSAGE)


class IdempotencyConflict(EpisodeStoreError):
    """Raised when the same storage key maps to different content."""

    __slots__ = ()
    _MESSAGE: str = "IDEMPOTENCY_CONFLICT"


class EpisodeNotFound(EpisodeStoreError):
    """Raised when an episode ID or storage key is not found."""

    __slots__ = ()
    _MESSAGE: str = "EPISODE_NOT_FOUND"


class EpisodeStore:
    """Thread-safe, append-only in-memory episode store.

    Idempotency rule (L8-1 §6):
      - same storage key + same canonical bytes → accepted (no-op)
      - same storage key + different canonical bytes → **IdempotencyConflict**

    Snapshot/restore uses a SHA-256 chain for tamper evidence.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: dict[str, bytes] = {}  # record_key → canonical bytes
        self._key_index: dict[str, str] = {}  # storage_key → episode_id

    # ── write path ──────────────────────────────────────────────────────

    def put(
        self,
        package: EpisodePackageV1,
        *,
        storage_key: str = "",
    ) -> None:
        """Store an episode package.

        Args:
            package: The frozen episode package.
            storage_key: Optional external key for idempotency checks.
                         Must be non-empty to enable idempotency.

        Raises:
            IdempotencyConflict: Same storage key, different content.
        """
        canonical = _canonical_bytes(package)
        with self._lock:
            if storage_key:
                existing_id = self._key_index.get(storage_key)
                if existing_id is not None:
                    existing_bytes = self._store.get(existing_id, b"")
                    if existing_bytes != canonical:
                        raise IdempotencyConflict()
                    # Same key, same bytes → idempotent no-op.
                    return
            eid = package.episode_id
            record_key = eid
            if not storage_key and record_key in self._store:
                suffix = 2
                while f"{eid}#{suffix}" in self._store:
                    suffix += 1
                record_key = f"{eid}#{suffix}"
            self._store[record_key] = canonical
            if storage_key:
                self._key_index[storage_key] = eid

    # ── read path ───────────────────────────────────────────────────────

    def get(self, episode_id: str) -> EpisodePackageV1:
        """Retrieve a stored episode by its ID.

        Raises:
            EpisodeNotFound: No episode with this ID.
        """
        with self._lock:
            raw = self._store.get(episode_id, _STORE_NOT_FOUND)
            if raw is _STORE_NOT_FOUND:
                matching = sorted(
                    (key for key in self._store if key.startswith(f"{episode_id}#")),
                    key=lambda key: int(key.rsplit("#", 1)[1]),
                    reverse=True,
                )
                if matching:
                    raw = self._store[matching[0]]
        if raw is _STORE_NOT_FOUND:
            raise EpisodeNotFound()
        assert isinstance(raw, bytes)
        return EpisodePackageV1.model_validate_json(raw)

    def get_by_key(self, storage_key: str) -> EpisodePackageV1:
        """Retrieve an episode by its storage key.

        Raises:
            EpisodeNotFound: No episode registered under this key.
        """
        with self._lock:
            eid = self._key_index.get(storage_key)
        if eid is None:
            raise EpisodeNotFound()
        return self.get(eid)

    def list_ids(self) -> tuple[str, ...]:
        """Return all stored episode IDs (sorted for determinism)."""
        with self._lock:
            return tuple(sorted(self._store.keys()))

    def __contains__(self, episode_id: str) -> bool:
        with self._lock:
            return episode_id in self._store or any(key.startswith(f"{episode_id}#") for key in self._store)

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)

    # ── snapshot / restore ──────────────────────────────────────────────

    def snapshot(self) -> bytes:
        """Canonical, replayable snapshot of the entire store.

        The snapshot is a JSON object with:
        - ``schema``: version identifier.
        - ``entries``: list of ``{episode_id, canonical_hex}`` sorted by id.
        - ``chain_hash``: hex SHA-256 of the iterated hash chain over
          canonical bytes in entry order.

        Returns:
            UTF-8 encoded JSON bytes.
        """
        with self._lock:
            entries: list[dict[str, str]] = []
            chain = b""
            for eid in sorted(self._store.keys()):
                raw = self._store[eid]
                chain = hashlib.sha256(chain + raw).digest()
                entries.append(
                    {
                        "record_key": eid,
                        "episode_id": EpisodePackageV1.model_validate_json(raw).episode_id,
                        "canonical_hex": raw.hex(),
                    }
                )
            chain_hash = chain.hex() if entries else hashlib.sha256(b"").hexdigest()
            key_index = [
                {"storage_key": key, "episode_id": episode_id} for key, episode_id in sorted(self._key_index.items())
            ]
            key_index_digest = hashlib.sha256(
                json.dumps(key_index, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            snapshot = {
                "schema": OBSERVABILITY_SCHEMA_VERSION,
                "entries": entries,
                "chain_hash": chain_hash,
                "key_index": key_index,
                "key_index_digest": key_index_digest,
            }
            return json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def restore(self, snapshot: bytes) -> None:
        """Restore store from a canonical snapshot.

        The entire chain integrity is verified before any mutation.
        On success, previous contents are replaced entirely.
        On failure, the store is left untouched.

        Args:
            snapshot: UTF-8 JSON bytes previously produced by ``snapshot()``.

        Raises:
            ValueError: Schema mismatch, malformed content, or chain-hash
                        mismatch (tampered / corrupted).
        """
        try:
            data: Any = json.loads(snapshot.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"invalid snapshot encoding: {exc}") from None

        if not isinstance(data, dict):
            raise ValueError("snapshot root must be a JSON object")

        if data.get("schema") != OBSERVABILITY_SCHEMA_VERSION:
            raise ValueError(f"schema version mismatch: {data.get('schema')!r}")

        entries: Any = data.get("entries", [])
        if not isinstance(entries, list):
            raise ValueError("snapshot 'entries' must be a list")

        expected_chain: str = data.get("chain_hash", "")
        if not isinstance(expected_chain, str) or len(expected_chain) != 64:
            raise ValueError("snapshot 'chain_hash' must be a 64-char hex string")
        raw_key_index = data.get("key_index", [])
        if not isinstance(raw_key_index, list):
            raise ValueError("snapshot 'key_index' must be a list")
        key_index_digest = data.get("key_index_digest")
        if key_index_digest is None:
            raw_key_index = []
            key_index_digest = hashlib.sha256(b"[]").hexdigest()
        if not isinstance(key_index_digest, str) or len(key_index_digest) != 64:
            raise ValueError("snapshot 'key_index_digest' must be a 64-char hex string")
        actual_key_index_digest = hashlib.sha256(
            json.dumps(raw_key_index, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if actual_key_index_digest != key_index_digest:
            raise ValueError("snapshot key index digest mismatch")

        # Phase 1: verify chain integrity without mutation.
        chain = b""
        parsed: list[tuple[str, bytes]] = []
        for idx, entry in enumerate(entries):
            if not isinstance(entry, dict) or "canonical_hex" not in entry:
                raise ValueError(f"entry {idx} is malformed")
            try:
                raw = bytes.fromhex(entry["canonical_hex"])
            except ValueError as exc:
                raise ValueError(f"entry {idx} invalid hex: {exc}") from None
            chain = hashlib.sha256(chain + raw).digest()
            record_key = entry.get("record_key", entry.get("episode_id"))
            episode_id = entry.get("episode_id")
            if not isinstance(record_key, str) or not isinstance(episode_id, str):
                raise ValueError(f"entry {idx} identifiers are malformed")
            parsed.append((record_key, raw))

        actual_chain = chain.hex() if entries else hashlib.sha256(b"").hexdigest()
        if actual_chain != expected_chain:
            raise ValueError("snapshot chain hash mismatch — data tampered or corrupted")

        # Phase 2: build new store (validate every entry deserializes).
        new_store: dict[str, bytes] = {}
        for eid, raw in parsed:
            try:
                decoded = EpisodePackageV1.model_validate_json(raw)
            except Exception:
                raise ValueError("entry payload is invalid") from None
            expected_episode_id = eid.split("#", 1)[0]
            if decoded.episode_id != expected_episode_id:
                raise ValueError("entry episode id mismatch")
            new_store[eid] = raw

        new_key_index: dict[str, str] = {}
        for item in raw_key_index:
            if not isinstance(item, dict):
                raise ValueError("snapshot key index entry is malformed")
            storage_key = item.get("storage_key")
            episode_id = item.get("episode_id")
            if not isinstance(storage_key, str) or not isinstance(episode_id, str):
                raise ValueError("snapshot key index entry is malformed")
            if not any(
                EpisodePackageV1.model_validate_json(raw).episode_id == episode_id for raw in new_store.values()
            ):
                raise ValueError("snapshot key index references missing episode")
            new_key_index[storage_key] = episode_id

        # Phase 3: atomic replace.
        with self._lock:
            self._store = new_store
            self._key_index = new_key_index


__all__ = [
    "BusinessEventV1",
    "EpisodeNotFound",
    "EpisodePackageV1",
    "EpisodeStore",
    "EpisodeStoreError",
    "IdempotencyConflict",
    "METRIC_LABELS",
    "METRIC_NAMES",
    "ModelUsageV1",
    "FailureAttributionV1",
    "NodeTrajectoryEventV1",
    "OBSERVABILITY_SCHEMA_VERSION",
    "TrajectoryEventType",
    "extract_episode_metrics",
]
