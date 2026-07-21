# L2-1 Observation/Safety/Artifact Schema 与数据库迁移

## 交付范围

新增领域事实的 Pydantic 契约、SQLAlchemy ORM 和独立 Alembic revision
`20260710_0002`。没有修改 Legacy 表或 `AGENT_RUNTIME_VERSION`，也没有修改
LangGraph Graph State；Graph State 仍只保存引用和执行游标。

## 契约

- `ObservationSchema` / `observations`：来源消息、原始和规范化值、置信度和
  `active` / `corrected` / `retracted` 生命周期。非 active 记录必须引用被修正/
  撤回的 Observation；active 记录不能带该引用。
- `SafetyProfileSchema` / `safety_profiles`：每 session 一条。每类采集字段使用
  `unknown`、`explicitly_none`、`collected` 三态；前两者值为 NULL，后者必须有
  非空列表或 pregnancy/lactation 的具体值。因此未知与“明确无”不会折叠。
- `ArtifactRevisionSchema` / `artifact_revisions`：逻辑 `artifact_id` + 唯一
  `revision`，记录 `input_state_version`、`current` / `superseded` / `stale`、
  生成它的 `graph_runs.id` 和父 revision。第一版无父项，后续版必须有父项。
- `gate_results` 只记录确定性 Gate 的名称、策略版本、输入版本和 decision；它不
  包含任何“模型已批准安全”的字段。`graph_runs` / `graph_run_steps` 只保留版本化
  执行元数据和 JSON object 元数据，不保存 prompt、原始模型输出、密钥或患者文本。

## 迁移

`upgrade()` 创建 `observations`、`safety_profiles`、`graph_runs`、
`graph_run_steps`、`artifact_revisions`、`gate_results` 和必要约束/索引。
`downgrade()` 以 FK 反向顺序删除新表，不会触碰 `20250624_0001` 创建的 Legacy
表；再次 upgrade 可从初始 head 重新建立这些表。

## AR-B-009 第 1 轮返工

- 所有 L2-1 migration 外键现在与 ORM 的 `ondelete` 一致：session 关系为
  `CASCADE`；Observation 来源和自引用为 `RESTRICT`；GraphRunStep 为 `CASCADE`；
  Artifact 产生 run 和父 revision 为 `RESTRICT`；GateResult 的 run 引用为
  `SET NULL`。
- `pregnancy_value` 与 `lactation_value` 已改为 Pydantic `StrEnum`，分别只接受
  `pregnant/not_pregnant/possible` 与 `lactating/not_lactating`；数据库 CHECK
  保持同一值域。
- `artifact_revisions` 新增 PostgreSQL partial unique index
  `uq_artifact_revisions_one_current`，限制每个 `artifact_id` 最多一条 current。
- 父链新增 `parent_revision` 及复合自引用 FK：
  `(parent_revision_id, artifact_id, session_id, parent_revision)` 必须指向
  `(id, artifact_id, session_id, revision)`；CHECK 同时要求后续 revision 的父项
  恰为 `revision - 1`。因此数据库拒绝自身、跨 artifact、跨 session 和非前序父项。
  Repository 仍将负责并发提交下的业务幂等与更高层状态转换，未在本任务实现。

真实 PostgreSQL（`postgresql://xuanhu@localhost:5432/xuanhu`）专项测试执行
`0001 → 0002 → 0001 → 0002`。测试查询 `pg_constraint` / `pg_get_constraintdef`
验证 FK 动作，并在事务内实际插入：非法 pregnancy/lactation、双 current、跨
artifact/session 父项均被拒绝；合法 revision 1 → 2 链成功写入。每个测试最终
rollback，不留下测试数据。

## 验证

已通过：

```text
uv run pytest tests/test_l2_1_domain_schemas_and_migration.py -q -rs  # 8 passed（真实 PostgreSQL）
uv run pytest tests/test_models.py tests/test_migrations.py -q -rs    # 59 passed
uv run ruff check .                                                   # passed
uv run mypy app --no-incremental                                      # passed
uv run mypy app --no-incremental --python-version 3.11                # passed
uv lock --check
git diff --check
```

`uv run pytest -q -rs` remains pending final rerun in this handoff environment.
The two mandated focused suites above completed normally. `alembic upgrade head
--sql` is also blocked before this revision by the existing initial migration's
JSONB seed literal-rendering failure; no initial migration was changed, per
L2-1 scope.

未实现 L2-2 至 L2-5：AgentSpec/RunSpec/AgentRuntime、ContextBuilder、Verifier/
Reducer、Repository/幂等/outbox。也未实现业务 Agent、API、SSE、RAG 或任何切流。
