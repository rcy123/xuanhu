# Durable Async Commands (R6-A / R6-B / R7)

A separate substrate for durable, worker-dispatched asynchronous commands. It is
independent of the synchronous `http_command_claims` write path (which keeps its
exact R1-R5 semantics). R6-A delivered the substrate; R6-B wired the three POST
endpoints into it behind an opt-in gate; **R7 made the async 202 path the
default** (see [R7: default async rollout](#r7-default-async-rollout-shipped)).

## Scope of this document

- Table / migration
- Repository semantics and invariants
- Worker lifecycle
- Status API
- Outbox → SSE mapping
- Privacy contract
- Configuration & runbook

## Table

`async_commands` (new Alembic revision `20260729_0016`, after head
`20260729_0015`). `id` is the public stable command UUID.

| column | notes |
| --- | --- |
| `session_id` | FK → `consult_sessions.id` CASCADE |
| `operation` | bounded `[A-Za-z0-9][A-Za-z0-9._:-]{0,63}` |
| `idempotency_key_digest` | SHA-256 of the raw key (raw key never persisted) |
| `request_digest` | canonical SHA-256 of the request JSON |
| `request_payload` | **PRIVATE** JSONB — the raw request, may contain PHI |
| `status` | `queued` \| `running` \| `succeeded` \| `failed` |
| `available_at` / `attempt_count` | claim eligibility + attempt counter |
| `lease_owner` / `lease_token` / `lease_expires_at` | owner-token fencing |
| `result_http_status` / `result_payload` | public-safe success output |
| `error_code` / `error_payload` | sanitized, bounded failure output |
| `created_at` / `started_at` / `completed_at` / `updated_at` | timestamps |

Database invariants (checks/indexes):

- `uq_async_commands_logical_command` — unique `(session_id, idempotency_key_digest)`.
- `uq_async_commands_active_session` — partial unique on `session_id` where
  `status IN ('queued','running')`; at most one active command per session.
- `idx_async_commands_claim` — `(status, available_at, lease_expires_at)` for the
  claim query; `idx_async_commands_session_created` for session listing.
- JSON-object checks, digest/operation/error-code regexes, terminal payload
  invariants (succeeded ⇒ result, no error; failed ⇒ error, no result),
  lease-relation and completed-at relations.

## Repository

`PostgresAsyncCommandRepository` (in `app/agent_runtime/async_command.py`) is the
only writer. Its typed DTOs (`AsyncCommandRef`, `ClaimedCommand`,
`AsyncCommandStatus`) are frozen with `extra="forbid"`. The private request
payload is returned only inside `ClaimedCommand`, for worker dispatch.

Semantics:

- **enqueue** — same key + same canonical request digest → returns the same
  command (`replayed=True`); same key + different digest → `IdempotencyConflict`;
  any other active command for the session → `SessionBusy`. A bounded retry loop
  re-reads the logical key after a unique-race rollback so replays resolve
  deterministically.
- **claim** — `FOR UPDATE SKIP LOCKED`, atomically transitions queued or
  expired-running → running, increments `attempt_count`, assigns an unpredictable
  `lease_token`, extends the lease.
- **renew_lease / complete / fail / retry** — every settle is fenced by
  `status='running' AND lease_owner AND lease_token`. A stale owner can never
  settle after a lease takeover; `False` is returned deterministically.
- **retry** — returns a transiently failed command to `queued` with backoff; no
  client-visible Outbox row (the running→queued→running lifecycle is already
  described by queued/running events).
- **get_status** — session-scoped; a command owned by another session is
  indistinguishable from a missing one.

## Worker

`AsyncCommandWorker` (in `app/agent_runtime/async_command_worker.py`) is generic:
an explicit allowlist `operation → AsyncCommandHandler` registry. Unknown
operations fail closed (`UNKNOWN_OPERATION`). Long handlers are heartbeated;
cancellation is never swallowed (the lease expires and another worker reclaims).
A claimed item is always finished before graceful stop returns. Settle failures
roll back atomically with their Outbox row, leaving the command leased for a
later reclaim — observation/publishing failures never corrupt command state.

`max_attempts` bounds transient retries with deterministic exponential backoff;
exhaustion persists a sanitized terminal failure.

## Status API

`GET /api/v1/consult/sessions/{session_id}/commands/{command_id}` returns a
privacy-safe envelope at HTTP 200 (status query, not 202). Queued/running return
200. Cross-session and missing commands both return `COMMAND_NOT_FOUND` (404).

## Outbox → SSE

Every externally meaningful transition writes a versioned Outbox row in the same
transaction:

| internal version | client event |
| --- | --- |
| `async_command.queued.v1` | `command.queued` |
| `async_command.running.v1` | `command.running` |
| `async_command.succeeded.v1` | `command.succeeded` |
| `async_command.failed.v1` | `command.failed` |

Because commands are not graph runs, `outbox_events.graph_run_id` was relaxed to
nullable, and `chk_outbox_events_graph_run_boundary` keeps `async_command.*` rows
(NOT NULL run id) disjoint from everything else. The existing Redis append-once
dedupe, Last-Event-ID/resync and SSE schemas are preserved. Mapping builds only
from allowlists and fails closed on malformed/unknown versions.

## Privacy contract

The private `request_payload` (may contain PHI) is protected as PostgreSQL domain
data. It must never be returned to clients, logged, copied to Outbox/SSE, error
details, metrics, `repr`, or the status API. Only the digest `request_digest` and
the worker `ClaimedCommand` projection are safe. Owner/lease internals and
digests never appear in Outbox/SSE or status either.

## Configuration

Settings (env prefix `XUANHU_`). Since R7 the async worker is **on by default**:

- `async_command_enabled` (default `true`) — start the worker in lifespan. This
  is the operator kill switch: set `XUANHU_ASYNC_COMMAND_ENABLED=false` to roll
  back / circuit-break to the synchronous R1-R5 path (worker not started ⇒
  admission never ready ⇒ sync fallback).
- `async_command_batch_size` (10), `lease_seconds` (60), `heartbeat_seconds`
  (20, must be < lease), `max_attempts` (8), `retry_base_seconds` (1),
  `retry_max_seconds` (300), `poll_interval_seconds` (0.5),
  `shutdown_grace_seconds` (10).

The worker is started/stopped only inside lifespan and only when enabled.

## R6-B: opt-in HTTP 202 admission for the three POST operations (superseded by R7)

This section documents the **R6-B phase**: the three production operations
(`intake.message`, `session.advance`, `prescription.review`) were wired into
this substrate behind an **opt-in gate**, with synchronous POST behavior the
default. R7 flipped that default (see [R7: default async
rollout](#r7-default-async-rollout-shipped)); this phase is retained as history.

- **Admission** (`app/agent_runtime/async_command_admission.py`): a client opts
  in with the HTTP `Prefer: respond-async` header. Admission honours the
  preference ONLY when `app.state.async_command_state` reports the feature
  enabled, the worker ready, and every allowlisted operation has a registered
  handler (`async_admission_ready`). Without the header, or when the feature is
  disabled / the runtime failed to start / a handler is missing, admission fails
  closed: the preference is ignored and the existing synchronous path runs.
  **Never enqueue without a worker.** Admission does only bounded work in the
  request task — validate/auth/load session/enqueue the durable row + its
  `async_command.queued.v1` Outbox row. No model/graph/review/safety execution
  happens inline. The same public `Idempotency-Key` contract applies: same key +
  same digest → same command (`replayed=true`, 202); same key + different digest
  → `IDEMPOTENCY_KEY_REUSED`; another active command on the session →
  `SESSION_BUSY`.
- **Acceptance** returns HTTP 202 with the existing success envelope, a typed
  body (`command_id`, `operation`, `status=queued`, `replayed`, `attempt_count`,
  and `links.self|session|stream`), `Location`, `Preference-Applied:
  respond-async` (present because the opt-in client sent the preference), and a
  bounded `Retry-After`. Disconnecting after 202 cannot cancel execution.
- **Handlers** (`app/agent_runtime/async_handlers.py`) are registered ONLY in
  lifespan, and only when the shared LangGraph runtime started. Each handler
  reuses the exact synchronous business function (no clinical logic copy) with a
  fresh, job-local session and the already-started shared runtime. The worker
  derives a deterministic downstream idempotency key from the stable
  `command_id` (`derive_downstream_key`), so a lease takeover replays the same
  business claim and cannot duplicate messages / transitions / reviews / safety /
  outbox. Runtime unavailable ⇒ retryable `HANDLER_UNAVAILABLE`. Known
  deterministic business outcomes map onto the finite PHI-safe allowlist in
  `ASYNC_COMMAND_ERROR_CODES`; unexpected failures ⇒ `HANDLER_UNEXPECTED`.
  Exception text and arbitrary error payloads are never persisted or logged.
  Successful endpoint data stays in the private `result_payload`; public status
  exposes only the HTTP status.
- **Frontend** (`frontend/src/types/api.ts`, `api/sse.ts`, `api/client.ts`,
  `api/index.ts`, `hooks/useSessionStream.ts`): `command.*` event types are added
  to the client unions without breaking existing events, plus `getCommandStatus`
  and an optional `respondAsync` flag that injects the `Prefer` header. The UI
  default was NOT switched in R6-B; R7 switched it on (see below).
- **Readiness** is carried by `app.state.async_command_state`; a worker that
  failed or lacks handlers is never marked ready, so admission never honours the
  preference. The existing `/health` readiness endpoint is unchanged.

## R7: default async rollout (shipped)

R7 flipped the default: the three POST endpoints (`intake.message`,
`session.advance`, `prescription.review`) **prefer the durable async 202 path**
and fall back to synchronous only when the async feature is unavailable. The
synchronous path keeps its exact R1-R5 semantics.

- **Default admission returns 202 without a `Prefer` header.** When the
  substrate is enabled, the worker ready, and every allowlisted operation has a
  registered handler, admission returns HTTP 202 by default. `Location` and a
  bounded `Retry-After` are always present; `Preference-Applied: respond-async`
  is emitted **only** when the incoming request actually carried the
  `respond-async` preference — R7 never claims a preference the client did not
  send.
- **Fail-closed synchronous fallback.** When the feature is disabled
  (`XUANHU_ASYNC_COMMAND_ENABLED=false`, the operator kill switch), the worker is
  not ready (crash / startup failure), or the handler registry is partial,
  admission fails closed: no command is enqueued and the existing synchronous
  R1-R5 path runs with byte/field/error semantics unchanged. **Never enqueue
  without a ready worker.**
- **No client sync override.** There is deliberately no synchronous-override
  header: RFC 7240 defines no standards-compatible sync preference, and the sync
  path is kept strictly as the fail-closed fallback. The client-facing
  `respondAsync=false` flag only **omits** the optional `Prefer: respond-async`
  header — it does **not** force the synchronous path. The R7 backend returns 202
  for a ready substrate regardless, so callers must distinguish the actual
  outcome (202 vs synchronous result), never assume the header dictates it.
- **Terminal reconciliation is frontend responsibility.** A 202 is an
  *accepted* command, never completed clinical work. The frontend
  (`useCommandReconciliation.ts`) drives an accepted command to a terminal state:
  - **SSE `command.*` events are a wake signal only** — the authoritative state
    is always `GET /commands/{id}`.
  - **Bounded polling** (a fixed attempt/elapsed budget) reconciles while a
    command is queued/running; when the budget is exhausted the entry moves to an
    **attention** state (uncertain / needs manual handling) — never treated as a
    failure and never re-sent as a new logical command.
  - **Idempotency retained**: the accepted command's idempotency key stays on the
    entry until a terminal state (or explicit attention) so the UI does not
    re-issue a new POST for an already-accepted command; on terminal
    **success** the read model is refetched from the authoritative GET.
  - `respondAsync=false` and a sync fallback result are handled through the same
    outcome discrimination (`isAsyncCommandAccepted`).

## Tests

- Unit, deterministic, DB-free: `tests/test_async_command_unit.py`,
  `test_async_command_worker_unit.py`, `test_async_command_outbox_mapping.py`,
  `test_async_command_migration.py`, `test_async_command_status_api.py`,
  `test_async_admission.py` (admission parsing / readiness / 202 envelope and
  `Preference-Applied` semantics), and `test_async_command_lifecycle.py`
  (supervisor readiness state machine + lifespan wiring).
- Integration (`integration` marker, real PostgreSQL/Redis):
  `test_async_command_integration.py` (concurrency, lease fencing, retry,
  publisher, privacy), `test_async_command_admission_integration.py` (202
  admission per operation, R7 default-without-header, sync fallback,
  worker settle / status privacy), and `test_async_command_handlers_integration.py`.
  Requires the guarded `TEST_DATABASE_URL` / `TEST_REDIS_URL` services and the
  destructive-tests sentinel.
