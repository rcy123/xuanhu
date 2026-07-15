# L4-4 ReasoningSubgraph、Revision 与回问闭环交接

## 任务状态

- 任务编号：L4-4
- 任务名称：ReasoningSubgraph、Revision 与回问闭环
- 状态：已完成；原 L4-4 交付证据保留为历史快照，2026-07-15 随 `c9148c2` 的 L0～L4 终审加固再次通过工程验收
- 日期：2026-07-12
- 范围：接入真实 LangGraph ReasoningSubgraph，串联 L4-1 SyndromeDraftAgent、L4-2 FormulaDraftAgent、L4-3 FormulaConsistencyVerifier，并把临床结构化产物从 Graph State 移入可恢复的 Domain artifact payload。

## 主要交付

- `app/agent_runtime/reasoning_subgraph.py`
  - 新增 `reasoning_subgraph_v1`。
  - 节点顺序：precheck -> build_syndrome_context -> draft_syndrome -> verify_syndrome -> build_formula_context -> draft_formula -> verify_formula_consistency -> ready_for_safety / invalidate_downstream / manual_required。
  - Graph State 只保留 `artifact_refs`、`gate_results`、`reasoning_route` 等引用，不保存 SyndromeDraft/FormulaDraft。

- `app/services/langgraph_reasoning.py`
  - 新增 Reasoning orchestration service。
  - 所有临床输入从 `PostgresDomainRepository.get_reasoning_authority()` 和 artifact payload 重建。
  - `completed` 路径只推进到 `current_stage=safety`，写 `ready_for_safety` gate，不执行 SafetyRuleAgent。
  - `needs_more_info` 路径 invalidates 当前 reasoning artifacts，写入单条 agent 问题并回到 `inquiry`。
  - `abstained`、agent/verifier/consistency failure、无法安全映射的 formula missing input 均进入 `blocked/manual_required`。

- `app/models/domain.py` + `app/db/migrations/versions/20260712_0007_l4_4_artifact_payloads.py`
  - 新增 `artifact_revision_payloads` 表。
  - payload 绑定 `artifact_revisions.id`，并带 `payload_schema_version`、`content_digest`、JSONB payload。
  - payload digest 由 schema version + payload canonical JSON 计算，防止 silent mismatch。

- `app/agent_runtime/repository.py`
  - 新增 `ArtifactPayloadSpec`、`ArtifactPayloadRecord`、`artifact_payload_digest()`、`get_artifact_payload()`。
  - `commit()` 支持 artifact revision 与 payload 同事务写入。
  - 修复 artifact revision 状态更新顺序：先 flush 旧 current -> superseded/stale，再插入新 current，避免 partial unique index 冲突。

- `app/api/advance.py`
  - LangGraph `/advance` 不再伪造 completed GraphRun。
  - API 只在会话锁内完成 ready gate 检查、stage 占用、GraphRun/claim running 创建。
  - 锁外调用 MainGraph + Postgres checkpointer；最终 response 由 reasoning 节点完成 claim 后返回。

## 关键边界

- `/advance` 不执行 Safety。L4-4 只完成 “ready_for_safety” handoff，`safety_rule_runs` 不应产生记录。
- `force=true` 仍不能绕过 persisted completeness ready gate。
- Graph State 不保存临床结构化模型输出。
- Formula 可以消费跨 state_version 的已封存 Syndrome artifact，但会验证：
  - 同一 session；
  - Syndrome run state_version 不晚于 Formula input state_version；
  - Syndrome input payload 与 sealed run/input/payload 自洽；
  - gate authority 与 Syndrome input 一致。
- 进程内 syndrome trusted cache 丢失后，ReasoningSubgraph 会从 `artifact_revision_payloads` 读取并重新验证 Syndrome artifact，再继续 Formula。

## AR-B-029 返工说明

- 已删除 `_restore_trusted_syndrome_execution`，不再存在“调用方手工 DTO -> trusted registry”的可导入恢复入口。
- 持久化恢复改为 `recover_trusted_syndrome_from_repository()`，入口只接受 session、artifact_id、exact revision 与 expected content digest；`PostgresDomainRepository` 由入口内部使用项目原始持久化工厂构造，当前 `ReasoningAuthoritySnapshot` 也由入口自行从 PostgreSQL 加载。
- 恢复流程会重新从 PostgreSQL artifact reference 加载并验证：
  - session、artifact type/id/revision/status；
  - `produced_by_run_id`、`input_state_version` 与 persisted `RunSpec`；
  - payload schema version、canonical payload、content digest；
  - 当前 gate authority 与当前 active facts；
  - stored verification 与重新执行的 Syndrome verifier report；
  - `RunArtifact` provenance 与 canonical output。
- 调用方传入 `Repository`、session factory、record、`RunSpec`、`RunArtifact`、`SyndromeDraftInput`、`SyndromeGateAuthority`、复制对象、伪 repository/record/authority 均不能获得 trusted capability。
- PostgreSQL checkpoint/restart 仍保留：cache 丢失后从 artifact payload 恢复 trusted Syndrome result，继续 Formula，且不重复调用 Syndrome 模型。

## AR-B-030 返工说明

- 已删除 Reasoning service 中 `fake-model/1/0` 的 RunArtifact 重建；Syndrome/Formula artifact payload 只能来自各自 closure trusted consumer 返回的 canonical `RunArtifact`。
- Syndrome commit 会先调用 `_consume_trusted_syndrome_execution(result)`；Formula commit 会先调用 `_consume_trusted_formula_execution(result)`。消费失败时固定转 `manual_required`，禁止写对应 clinical artifact payload。
- 持久化的 `run_artifact` 精确保留 `output`、`model_actual`、`attempts`、`latency_ms`、`trace_id`、`run_id`、`agent_spec_version`、`prompt_version`、`usage`、`evidence_ids`。
- `SyndromeVerificationReport` / `FormulaVerificationReport` 的 `subject_digest` 已绑定完整 RunArtifact provenance，恢复或 replay 时任意 provenance/output 篡改都会被拒绝。
- Formula current artifact replay 现在校验 payload schema、digest、run provenance、stored verification subject 和 canonical payload，不再只读取 `output.decision`。

## 回问闭环

- Syndrome `needs_more_info` 使用 schema 中的 `InquiryDimension`。
- Formula `missing_inputs` 是自由文本，当前只允许映射到已知 `InquiryDimension` 后走回问。
- 无法安全映射时固定进入 `manual_required`，避免伪造默认问题。
- 回问路径只写 1 条 agent message，并将当前 Syndrome/Formula artifact 标记 stale。

## 测试覆盖

新增：

- `tests/test_l4_4_reasoning_subgraph.py`
  - advance 路由进入 injected ReasoningSubgraph 且 Graph State JSON-safe。
  - artifact payload roundtrip 与 revision 精确绑定。
  - completed 路径推进到 safety，且不执行 safety rules。
  - formula needs_more_info 回 inquiry、单问题、artifact stale。
  - syndrome 后恢复：清空进程内 cache 后从 payload 重建，不二次调用 Syndrome 模型。
  - formula missing input 无法映射时进入 manual_required。
  - AR-B-029 回归：restore 符号不可用、raw/copy Syndrome result 无授权、恢复入口签名仅接受 artifact ref + digest、伪 repository 子类/实例方法替换/session factory 替换/伪 record/authority 无授权、跨 session/revision/digest/run/version 拒绝。
  - Syndrome completed / needs_more_info / abstained；Formula completed / needs_more_info / abstained。
  - Syndrome verifier failure、Formula verifier failure、Formula consistency failure。
  - completed command replay、并发同 command 单模型调用、stale state conflict、PostgreSQL checkpoint restart。
  - AR-B-030 回归：非 `fake-model` AgentSpec 持久化、Syndrome/Formula payload 与 trusted RunArtifact 精确一致、consumer 缺失不写 artifact、model/attempt/latency/trace/run/spec/prompt/output 篡改拒绝。

更新：

- L1-2/L1-3/L1-4：advance 断言从 `reasoning_placeholder` 更新为 `reasoning_subgraph_v1`，无业务 DB 的 checkpoint 测试注入轻量 reasoning executor。
- L3-5：保留 intake advance precheck 关注点，通过 fake advance graph 完成 claim。
- L4-2：Graph State reference-only 测试注入轻量 reasoning executor。

## L4-4 当日验证结果（历史证据）

```powershell
uv run ruff check app tests/test_l1_3_postgres_checkpoint.py tests/test_l4_4_reasoning_subgraph.py
# All checks passed

uv run mypy app
# Success: no issues found in 118 source files

uv run pytest tests/test_l1_2_graph_state_and_routing.py tests/test_l1_3_postgres_checkpoint.py tests/test_l1_4_graph_runner.py tests/test_l2_5_repository_outbox.py tests/test_l3_5_intake_subgraph.py tests/test_l4_1_syndrome_draft.py tests/test_l4_2_formula_draft.py tests/test_l4_3_formula_consistency.py tests/test_l4_4_reasoning_subgraph.py -q
# 310 passed, 11 warnings

uv run pytest tests/test_l4_4_reasoning_subgraph.py -q
# 26 passed, 3 warnings
```

以上数量对应 L4-4 当日提交附近的历史运行，不用于代替当前全量验收。当前 exact-HEAD 证据以 `Agent优化任务进度表.md` 和 `L0-L4中期优化重新验收报告-2026-07-14.md` 为准。

Warnings are existing Alembic `path_separator` deprecation warnings from test config.

## 已知后续

- LangGraph recovery 当前固定 501 fail-closed，并且不会回退到 Legacy；它仍不是可用的恢复实现。Review/Safety 属于 L5 以后范围。
- SafetySubgraph 尚未接入，本任务只生成 ready-for-safety handoff。
- 真实服务测试只接受受保护的 `TEST_DATABASE_URL`、`TEST_REDIS_URL` 和 `XUANHU_ALLOW_DESTRUCTIVE_TESTS=1`；`tests/conftest.py` 内部映射应用连接，不得把开发 `DB_URL` 当作测试入口。
- 本交接只保存 L4-4 范围与历史证据；当前阶段状态由中心进度表和重新验收报告维护。
