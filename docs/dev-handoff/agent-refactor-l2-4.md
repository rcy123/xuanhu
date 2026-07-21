# L2-4 Verifier Chain 与 Domain Reducer

## 修改文件

- `app/agent_runtime/verifiers.py`：不可变 Verifier/Check/VerificationReport 合约、五段确定性验证链和失败策略。
- `app/agent_runtime/reducer.py`：受限 DomainState/DomainDelta、固定错误码、Delta 指纹和纯函数 Reducer。
- `app/agent_runtime/__init__.py`：公开 L2-4 合约。
- `tests/test_l2_4_verifier_reducer.py`：纯本地专项测试；不调用模型、数据库、API 或 Graph。
- `docs/dev-handoff/agent-refactor-l2-4.md`：本交接文件。

工作区原有 `.gitignore` 与 `docs/01_agent部分优化/Agent优化任务进度表.md` 改动未覆盖、未回退，也不属于本次实现。

## 合约与执行顺序

`Verifier`、`CheckResult`、`VerificationReport`、`VerificationContext` 和 `VerifierChain` 均为 `frozen=True, extra="forbid"` 的 Pydantic 合约。默认链固定按下列顺序执行，报告保留同一顺序：

1. `SchemaVerifier`：用 `AgentSpec.output_schema` 重新校验结构；
2. `OutputTypeVerifier`：要求输出精确匹配声明类型，且该类型是 `DomainDelta`；
3. `ProvenanceVersionVerifier`：校验 run/spec/prompt/trace、session、source message 白名单、artifact 产生 run 和输入版本；
4. `PrerequisiteVerifier`：校验允许阶段与调用方提供的确定性前置条件集合；
5. `DeltaLegalityVerifier`：使用与 Reducer 相同的规则预演 Delta。

`VerificationReport` 只包含 `passed`、有序 checks、固定 failure class/code、`retry_allowed`、`requires_human` 和 64 位十六进制 `subject_digest`。类别、重试和人工策略由首个失败检查的固定映射产生。Report 是无敏感文本的审计结果，不是 Reducer 的提交授权；即使调用方手工构造全通过 checks、passed 和正确 Delta digest，也不能用它提交 Delta。

`DomainState` 是一次纯内存权威快照，只含 L2-1 的 `ObservationSchema`、`SafetyProfileSchema`、`ArtifactRevisionSchema` 及 `session_id/state_version`。`DomainDelta` 只允许：

- 追加 Observation ledger 事件；
- 替换一份 SafetyProfile；
- 追加 Artifact revision；
- 将指定 artifact 的 current revision 标记为 stale。

Delta 还必须声明 `delta_id`、`run_id`、`session_id`、`expected_state_version` 和受信 source message IDs。禁止在同一 Delta 中同时更新事实/Safety 与新增 Artifact revision，避免基于旧输入版本的新工件在同一步变成 current。

新签名是 `reduce_domain_state(state, delta, context)`，第三个参数必须是 `VerificationContext`。Reducer 在自身边界确认 context 中的 Domain State 与待更新 state 相同、context artifact output 与待应用 Delta 相同，然后重新执行完整 `DEFAULT_VERIFIER_CHAIN`。只有内部重验得到 `report.passed=true` 才继续执行 Reducer legality 和状态更新；传入 `VerificationReport` 会以 `VERIFICATION_CONTEXT_REQUIRED` 拒绝。无实际变化时返回等价深拷贝且不增加版本，有实际变化时版本只增加 1。

### AR-B-014 / AR-B-015 授权边界回归

- 真实链因 `SOURCE_NOT_ALLOWED` 拒绝时，手工构造的全通过 Report 不能提交；同一未授权 Context 在 Reducer 内重验后以 `VERIFICATION_REJECTED` 拒绝。
- stage 不在 allowlist 或 prerequisite 缺失时，伪造 Report 不能提交，真实 Context 也会在 Reducer 内部重验后拒绝。
- 合法 VerificationContext 可正常提交；为另一 Delta 创建的 Context 以 `VERIFICATION_CONTEXT_MISMATCH` 拒绝。
- stale Delta 在内部重验阶段以 `STATE_VERSION_CONFLICT` 拒绝。
- Observation 去重、更正、撤回、Safety/Artifact 失效、Artifact revision/supersede 和重复失效测试全部继续使用合法 VerificationContext 并通过。
- AR-B-015 已清理 artifact revision/失效测试中的旧 API；除两处专门验证“伪造 Report 被拒绝”的负向调用外，所有生产和测试调用的第三参数均为 VerificationContext。

## 去重、更正、失效和冲突规则

- Observation ID 已存在且内容完全相同：重放 no-op；同 ID 不同内容：确定性冲突。
- 同 source message、fact key、动作、目标和值的 Observation，即使新生成了另一个 ID，也视为语义重复并 no-op。
- 同一 fact key 的当前叶节点出现不同值时拒绝隐式覆盖；必须提交显式 `corrected` 事件。该冲突报告 `requires_human=true`。
- `corrected/retracted` 必须引用同 session、同 fact key 且尚未被后续事件取代的当前叶节点。更正必须有值；撤回必须无值。ledger 只追加，不原地改写历史。
- Observation 或 SafetyProfile 有实质变化时，所有 current Artifact 自动标记 `stale`；纯重复 no-op 不使工件失效。
- 新 Artifact 必须是 `current`。首版 revision 必须为 1；后续版必须紧邻当前历史最大 revision、声明前一 revision 和非空父引用、保持同 artifact type。新增版会把旧 current 标记 `superseded`。
- 显式 artifact invalidation 把 current 标记 `stale`；已 stale/superseded 的重复失效是 no-op；不存在的 artifact ID 被拒绝。
- `expected_state_version` 必须精确等于当前 Domain State version。旧 Delta 会在 Reducer 内部默认链重验时以 `STATE_VERSION_CONFLICT` 拒绝，调用方只能基于最新状态重建上下文并重跑。

这些规则均为纯函数规则：相同输入得到相同输出；用当前版本重放语义重复或已完成失效不会再次增加版本。原始旧版本 Delta 不作为幂等捷径，始终按乐观并发规则拒绝。

## 隐私与安全边界

- Report 不保存 Prompt、原始/结构化模型输出、异常文本、API key、患者姓名、session ID 或 source message ID。
- Check 只有枚举 verifier/status/failure code，没有自由文本 details。
- `subject_digest` 绑定规范化完整 Delta，但只保留 SHA-256；哈希输入包含随机 Delta/run/object IDs，不把明文复制进报告。
- Verifier 与 Reducer 不导入 DB、Repository、LangGraph、API、模型客户端或日志模块，不读写 Graph State，不改变阶段，不决定 Safety。
- Reducer 会基于完整 VerificationContext 重新执行 `DEFAULT_VERIFIER_CHAIN`；Report、passed、check 顺序和 Delta digest 均不具备独立授权能力。

## 精确验证结果

执行环境：Windows，Python 3.12.12，pytest 8.4.2。

```text
uv run pytest tests/test_l2_4_verifier_reducer.py -q -rs
16 passed in 0.21s

uv run pytest tests/test_l2_1_domain_schemas_and_migration.py tests/test_l2_2_agent_runtime.py tests/test_l2_3_context_builder.py -q -rs
40 passed, 4 warnings in 1.78s

uv run pytest -q -rs
1133 passed, 1 xfailed, 6 warnings in 208.71s

uv run ruff check .
All checks passed!

uv run mypy app
Success: no issues found in 91 source files

uv lock --check
Resolved 83 packages in 14ms

git diff --check
exit 0；仅报告 app/agent_runtime/__init__.py 与任务进度表未来 LF→CRLF 的工作区提示
```

L2-1 的真实 PostgreSQL 用例本次实际执行且全部通过。四个 warning 来自既有 Alembic `path_separator` 弃用提示；全量另含既有 Pydantic 字段名提示和一次 SQLAlchemy/psycopg coroutine runtime warning。新增 L2-4 专项无 warning。

## 风险与未完成项

- 本任务没有 Repository、数据库事务、Outbox、持久化或 command 幂等存储；这些属于 L2-5。当前 State/Delta 是事务边界上游可复用的纯内存合约。
- L2-1 `ArtifactRevisionSchema` 暴露逻辑 `artifact_id` 和 `parent_revision_id`，但没有暴露数据库 revision row 主键。因此纯 Reducer 能校验父引用非空、紧邻 revision、同逻辑 artifact/type，不能在内存中证明 `parent_revision_id` 正好等于前一数据库行 ID；L2-1 数据库复合外键仍是最终持久化约束，L2-5 Repository 应在提交前解析并复核该引用。
- Verifier 的 `allowed_stages`、source 白名单与前置条件集合由未来确定性 Harness 节点提供；本任务不实现 State Machine、Triage、Completeness 或 Safety Gate。
- 未接入 AgentRuntime、MainGraph、API/SSE/前端或 Legacy 切流，也未调用真实模型。
- 未提交 commit。
