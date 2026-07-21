# L3-5 Handoff: IntakeSubgraph, Messages API, Intake E2E

## Scope

- Replaced the MainGraph `message` route with versioned `intake_subgraph_v1`.
- Kept reasoning/review/recovery/manual placeholders unchanged.
- Added per-session `agent_runtime` persistence so runtime identity is fixed at session creation.
- Added LangGraph-only `/messages` intake flow while preserving Legacy `/messages` behavior for legacy sessions.
- Added LangGraph `/advance` branch that consumes a persisted current completeness ready gate and does not invoke L4 reasoning/model flow.
- Round-1 AR-B-025 rework restored L2 verifier/reducer/repository authority for clinical facts, added durable intake command claims, and closed the follow-up P1 findings around stale `/advance` gates, recoverability, claim replay, and event privacy.
- Round-2 AR-B-025 rework replaced the pseudo-split intake service node with a real checkpointable IntakeSubgraph, added conditional route branches, and added true LangGraph `/messages` E2E coverage with fake gateway call counts and post-commit recovery.

## Implementation Notes

- `app/agent_runtime/intake_subgraph.py` now builds a compiled versioned IntakeSubgraph with named nodes:
  - `intake.persist_message`
  - `intake.triage_precheck`
  - `intake.build_intake_context`
  - `intake.extract_intake`
  - `intake.verify_intake`
  - `intake.reduce_observations`
  - `intake.gates_and_route`
- `intake.gates_and_route` uses `add_conditional_edges` to branch to `intake.route.ready`, `intake.route.incomplete`, `intake.route.conflict`, or `intake.route.manual`.
- Production MainGraph embeds that compiled subgraph. Each production node rebuilds execution from durable `session_id` / `command_id` / `run_id` refs and DB claim rows; it no longer depends on request closures or `ContextVar`.
- `app/services/langgraph_intake.py` owns request orchestration only:
  - durable command claim/replay
  - patient message persistence
  - `build_intake_context` / `extract_intake` outside database transactions
  - verifier -> reducer -> repository commit for observations and safety facts
  - triage and completeness gate persistence
  - ready/incomplete/conflict/triage-blocked/stagnated routing
- Intake command replay can recover a `running` claim after repository commit by reading `DomainCommandCommit`, persisted messages, and session snapshot, then completing the claim with a stable response payload.
- Intake node replay persists checkpointable non-clinical command progress in `intake_command_claims.intermediate_payload`: step status, stable agent run/idempotency refs, output digest, counts, state versions, route, and gate refs only. It does not persist `IntakeExtractionOutput`, gate details, patient text, fact keys, or fact values.
- Before repository commit, downstream nodes use a process-local short-lived extraction cache. If that cache is gone after restart, the node retries extraction with a stable run/idempotency key; after repository commit, recovery reads `DomainCommandCommit` and Domain State rather than a model-output copy.
- Concurrent duplicate `/messages` commands with the same idempotency key and payload reuse the existing running claim after a short lock-busy probe, so only one intake extraction model call is made.
- LangGraph intake no longer publishes Redis/SSE message events directly with message `content`; LangGraph business events are persisted through Outbox with privacy-minimal payloads.
- Graph State stores only route metadata, domain state version, gate refs, message artifact refs, and sanitized error refs.
- Domain State remains authoritative for clinical facts via `observations` and `safety_profiles`.
- `NODE_INTAKE_PLACEHOLDER` remains as a deprecated alias for import compatibility, but its value now resolves to `intake_subgraph_v1`.
- `consult_sessions.agent_runtime` defaults to `legacy`; `SessionCreateRequest.agent_runtime` can explicitly set `legacy` or `langgraph`. If omitted, new sessions use `Settings.agent_runtime_version`; existing sessions keep their persisted runtime.

## DB Migration

- Added `20260711_0004_session_agent_runtime.py`.
- Added `20260711_0005_l3_5_intake_command_claims.py`.
- Added `20260712_0006_l3_5_intake_intermediate_payload.py`.
- 0004 backfills existing sessions to `legacy` and adds `chk_consult_sessions_agent_runtime` / `idx_consult_sessions_agent_runtime`.
- 0005 adds `intake_command_claims` for durable idempotency, digest mismatch rejection, replay, and command result storage.
- 0006 adds nullable `intake_command_claims.intermediate_payload` with a JSON object check constraint for node-level recovery metadata.

## Compatibility

- Existing legacy sessions continue through `InquiryAgent + SufficiencyAgent`.
- Existing sessions do not change path when `AGENT_RUNTIME_VERSION` changes.
- LangGraph sessions do not silently fall back to Legacy.
- LangGraph `/advance` performs session lock + DB transaction before reading the completeness gate. It requires a persisted `completeness-policy.v1` gate with `decision=passed`, `details.disposition=ready`, and `input_state_version == locked consult_sessions.state_version`. `force=true` does not bypass this requirement.
- LangGraph `/advance` records durable command replay state in `intake_command_claims`, writes a `GraphRun`, writes `advance.command_completed.v1` to Outbox, and writes an audit event in the same transaction. Same command key + same payload returns the stored response without a second Outbox event.
- Ready intake does not generate a question; incomplete/conflict generates one question; red flag and stagnated outcomes block/manual-hold.

## Verification

Executed on 2026-07-11 with `DB_URL=postgresql://xuanhu:xuanhu_dev@localhost:5432/xuanhu`:

- `uv run ruff check .`
- `uv run mypy app`
- `uv lock --check`
- `uv run pytest -q -rs` -> `1323 passed, 1 xfailed, 14 warnings`
- `uv run pytest tests/test_l3_5_intake_subgraph.py -q -rs` -> `10 passed`
- `uv run pytest tests/test_l3_1_intake_extraction.py tests/test_l3_2_triage_policy.py tests/test_l3_3_completeness_policy.py tests/test_l3_4_gap_question.py tests/test_l3_5_intake_subgraph.py -q -rs` -> `178 passed`
- `uv run pytest tests/test_l1_2_graph_state_and_routing.py tests/test_l1_4_graph_runner.py tests/test_messages_api.py tests/test_advance_api.py tests/test_l3_5_intake_subgraph.py -q -rs` -> `116 passed`
- `uv run alembic downgrade 20260711_0004; uv run alembic upgrade 20260711_0005; uv run alembic upgrade head`
- `git diff --check`

Additional round-2 AR-B-025 verification:

- `.venv\Scripts\python.exe -m pytest tests\test_l3_5_intake_subgraph.py -q` -> `15 passed, 4 warnings`
- `.venv\Scripts\python.exe -m pytest tests\test_l3_1_intake_extraction.py tests\test_l3_2_triage_policy.py tests\test_l3_3_completeness_policy.py tests\test_l3_4_gap_question.py tests\test_l3_5_intake_subgraph.py -q` -> `182 passed, 4 warnings`
- `.venv\Scripts\python.exe -m pytest tests\test_l2_5_repository_outbox.py -q` -> `12 passed, 4 warnings`
- `.venv\Scripts\python.exe -m pytest tests\test_l1_3_postgres_checkpoint.py -q` -> `33 passed`
- `.venv\Scripts\python.exe -m pytest tests\test_messages_api.py tests\test_advance_api.py -q` -> `28 passed`
- `.venv\Scripts\python.exe -m ruff check .` -> passed
- `.venv\Scripts\python.exe -m mypy app` -> `Success: no issues found in 108 source files`

New E2E evidence in `tests/test_l3_5_intake_subgraph.py`:

- fake gateway LangGraph `/messages` incomplete route: one intake call, zero question model calls, template question, completed claim, Outbox event, and all intake node progress persisted.
- fake gateway LangGraph `/messages` red-flag route: one intake call, zero question model calls, blocked/manual-required session, no ordinary follow-up question.
- concurrent same-command `/messages`: one durable claim, stable replayed response, one intake extraction call.
- post-Repository-commit interruption: monkeypatched `_complete_claim` no-op after real commit; `_wait_for_completed_claim` recovers from `DomainCommandCommit` and completes the claim.
- privacy boundary negative test: `intermediate_payload`, Outbox payloads, and Postgres checkpoint rows do not contain patient message text, fact values, fact keys, `extraction_output`, or structured output fields.

## Open Risks

- The LangGraph intake path still depends on real model gateway availability for `IntakeExtractionAgent` and model fallback in `QuestionComposer`.
- Reasoning/review/recovery subgraphs remain placeholders by design and are not implemented in L3-5.
