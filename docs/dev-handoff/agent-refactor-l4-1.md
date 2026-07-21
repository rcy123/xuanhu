# L4-1 SyndromeDraftAgent 与 SyndromeVerifier 交接

## 变更文件

- `app/schemas/syndrome.py`
- `app/agent_runtime/repository.py`
- `app/agent_runtime/syndrome_verifier.py`
- `app/agents/syndrome_draft.py`
- `app/agents/prompts/syndrome_draft_v1.jinja2`
- `app/agents/prompts/manifest.yaml`
- `tests/test_l2_5_repository_outbox.py`
- `tests/test_l4_1_syndrome_draft.py`

## 版本

- Schema: `syndrome-draft.v1`, `syndrome-draft-input.v1`
- AgentSpec: `syndrome-draft-agent.v1`
- Prompt: `syndrome_draft_v1.jinja2`
- Policy: `syndrome-draft-policy.no-rag.v1`
- Verifier chain: `schema`, `run_provenance`, `preconditions`, `fact_links`, `decision_consistency`, `no_rag_contract`, `authority_boundary`

## 权威前置检查

`execute_syndrome_draft` 在调用 `AgentRuntime` 前执行 `validate_syndrome_preflight`，失败时模型调用次数为 0。检查项：

- `RunSpec.stage` 与输入 `current_stage` 必须为 `READY_FOR_REASONING`
- `RunSpec.session_id/state_version/AgentSpec/prompt_version/attempt_budget` 必须匹配
- AgentSpec 必须是固定只读 `syndrome_draft` v1，低温、短输出、短超时、单次尝试
- AR-B-026 第 4 轮修复后，公开执行入口不再把调用方传入的 `DomainState`、`context_observations`、`GateResultSchema` 或 `current_stage` 当作权威；`execute_syndrome_draft` 必须通过 `DomainRepository.get_reasoning_authority(run_spec.session_id, run_spec.state_version)` 在同一 Repository 读取边界中加载 authority bundle
- `/advance` 前后版本边界：推进前 session 为 `inquiry/V`，L3 gate 的 `input_state_version=V`；合法 `/advance` 后 session 为 `syndrome/V+1`，`state_snapshot.advance.source_gate_id` 精确绑定 V 版本 completeness gate，`source_gate_state_version=V`
- `ReasoningAuthoritySnapshot` 区分 `current_state_version` 与 `source_gate_state_version`：`domain_state.state_version == current_state_version == RunSpec.state_version`，而 `triage_gate.input_state_version == completeness_gate.input_state_version == source_gate_state_version`
- Repository authority bundle 只接受 `current_stage=syndrome`、`status=active`、`agent_runtime=langgraph`、`recovery_status != manual_required` 的当前会话；`inquiry/review/record/done/blocked`、`blocked/terminated/pending_review`、legacy runtime、state version mismatch 或 session missing 均在 gateway 前拒绝
- Repository 从 `state_snapshot.advance.source_gate_id/source_gate_state_version` 精确加载 source completeness gate，再用同一 `graph_run_id`、同一 source state version 加载唯一 triage gate；缺失、重复、跨 GraphRun、非 completed GraphRun、policy/decision/details 不符都会拒绝
- 模型上下文由 Repository 返回的权威 DomainState active observations 生成；调用方即使同时伪造 `domain_state` 与 `context_observations` 并使二者自洽，也不会把伪造事实送入 gateway
- triage gate 必须来自持久化 `triage-red-flag.v1`，且必须为 `passed/continue/candidate_count=0`；真实持久化 blocked gate 即使调用方伪造空候选 `continue` gate，也会在 gateway 前拒绝
- completeness gate 必须来自持久化 `completeness-policy.v1`，且必须为 `passed/ready`
- 明显不完整的 Domain snapshot 即使携带当前版本 `ready/PASSED` gate，也会在 gateway 前拒绝
- 不再通过重新解释 Observation 推断红旗；`vitals.temperature_c=40.5` 等未落成 `red_flag.*` observation 的红旗场景依赖 L3 持久化 blocked triage gate 作为权威
- context observation 必须精确对应当前 DomainState active observations 的 `observation_id/session_id/status/fact_key/value/normalized_value/state_version`
- `validate_syndrome_preflight(..., gate_authority=None)` 与 `verify_syndrome_artifact(..., gate_authority=None)` 固定失败，不再 fallback 信任 input gate
- context 不得包含身份 fact key、手机号、证件号等 PII
- active facts 存在阻断性同 fact_key 多值冲突时拒绝

## Verifier 规则

`verify_syndrome_artifact` 独立于模型执行，重新 canonicalize 输出并拒绝隐藏字段。规则包括：

- `syndrome_basis` 和 `differential` 的 fact IDs 必须存在于当前 active context
- unknown、inactive、superseded、stale、跨 session fact 不能作为有效依据
- `completed` 必须有 syndrome、basis、treatment_principle，且不得用“信息不足”“待补充”等伪 completed 文本
- `needs_more_info` 必须只携带 `missing_inputs`，不得携带 syndrome、basis、differential 或 treatment principle
- `abstained` 不得携带伪正常临床结论
- 禁止 route、stage、formula、prescription、safety_decision、doctor_decision 等越权字段
- 同 observation ID 下篡改 `fact_key`、`value` 或 `normalized_value` 会在 gateway 前拒绝

## 无 RAG 边界

- `evidence_mode` 仅允许 `model_knowledge_only`
- `claim_evidence_links` 必须为空
- citation/source/literature 等伪证据字段被拒绝
- confidence 上限为 `0.65`，超过即拒绝，不静默修正
- `review_required` 必须为 `true`
- 未提供可由调用方打开的 `rag_supported` 开关

## 持久化与 Graph State 边界

- 本轮不接入 Formula、Safety 或正式 ReasoningSubgraph
- 本轮不新增 DB migration，不创建旁路 commit
- 完整 `SyndromeDraft` 只作为 L2 runtime artifact 返回并由 verifier 验证，未写入 Graph State checkpoint
- Graph State 测试仅保存 `{kind: "syndrome_draft", artifact_id, revision}` 引用，不保存完整草案、临床事实或 PII
- 未在持锁数据库事务中调用模型
- `get_reasoning_authority()` 的数据库读取事务结束后，`execute_syndrome_draft()` 才构建模型 Context 并调用 gateway

## 测试结果

- L4-1 专项：`uv run pytest tests/test_l4_1_syndrome_draft.py -q -rs` -> `22 passed`
- Repository 专项：`uv run pytest tests/test_l2_5_repository_outbox.py -q -rs` -> `27 passed, 4 warnings`
- Advance API 专项：`uv run pytest tests/test_advance_api.py -q -rs` -> `6 passed`
- L3-1～L4-1 合并回归：`uv run pytest tests/test_l3_1_intake_extraction.py tests/test_l3_2_triage_policy.py tests/test_l3_3_completeness_policy.py tests/test_l3_4_gap_question.py tests/test_l3_5_intake_subgraph.py tests/test_l4_1_syndrome_draft.py -q -rs` -> `205 passed, 4 warnings`
- Repository/checkpoint/API 回归：`uv run pytest tests/test_l2_5_repository_outbox.py tests/test_l1_3_postgres_checkpoint.py tests/test_l1_4_graph_runner.py tests/test_messages_api.py tests/test_advance_api.py tests/test_agent_runtime_flag.py -q -rs` -> `130 passed, 4 warnings`
- 全量后端 pytest：`uv run pytest -q -rs` -> `1365 passed, 1 xfailed, 14 warnings`
- Ruff：`uv run ruff check .` -> passed
- mypy：`uv run mypy app` -> passed
- lock：`uv lock --check` -> passed
- diff whitespace：`git diff --check` -> exit 0, only LF/CRLF working-copy warnings for `app/agent_runtime/repository.py`, `app/agents/prompts/manifest.yaml`, `docs/01_agent部分优化/Agent优化任务进度表.md`, and `tests/test_l2_5_repository_outbox.py`

## 已知限制

- L4-1 只实现独立 SyndromeDraft 边界，不将草案落库为完整 clinical artifact payload
- 现有 `artifact_revisions` 表仅有 revision 元数据；若后续需要持久化完整 SyndromeDraft，应在 L4-4 或专门任务中通过 Repository/DomainCommandCommit/Outbox 模式扩展，不应写入 Graph State
- `READY_FOR_REASONING` 是 L4-1 独立契约；当前 `/advance` 仍保持既有行为，不正式运行 L4 推理链

## 明确未实现

- 未实现 L4-2 `FormulaDraftAgent`
- 未实现 L4-3 `FormulaConsistencyVerifier`
- 未实现 L4-4 `ReasoningSubgraph`、revision 或回问闭环
- 未实现 Safety、HITL、Record、RAG 或前端结果卡片
- 未切换默认 runtime，`AGENT_RUNTIME_VERSION=legacy` 保持不变
