# L2-5 Repository、幂等事务与 Outbox

## 修改文件

- `app/agent_runtime/repository.py`：Repository/Outbox 协议、PostgreSQL 实现、固定错误码和稳定结果 DTO。
- `app/agent_runtime/__init__.py`：公开 L2-5 合约。
- `app/models/domain.py`：`DomainCommandCommit`、`OutboxEvent` ORM；补齐 L2 时间戳 timezone 与 nullable JSONB 的 SQL NULL 映射。
- `app/models/__init__.py`：注册新 ORM。
- `app/db/migrations/versions/20260711_0003_l2_repository_outbox.py`：幂等提交和 Outbox migration。
- `tests/test_l2_5_repository_outbox.py`：真实 PostgreSQL 事务、并发、恢复和隐私专项。
- `docs/01_agent部分优化/Agent优化任务进度表.md`：仅在原有用户改动上将 L2-5 标为已交付待验收。
- `docs/dev-handoff/agent-refactor-l2-5.md`：本交接文件。

## Repository 与事务边界

`PostgresDomainRepository` 使用调用方注入的 `async_sessionmaker[AsyncSession]`。`commit()` 的一个 `session.begin()` 覆盖完整提交：

1. `SELECT consult_sessions ... FOR UPDATE` 锁定 session 行；
2. 在锁内查询 `DomainCommandCommit` 幂等记录；
3. 非重放请求校验锁定行的 `state_version`；
4. 从 `observations`、`safety_profiles`、`artifact_revisions` 重建权威 `DomainState`；
5. 将该快照和调用方完整 `VerificationContext` 交给 L2-4 `reduce_domain_state()`，由 Reducer 在自身边界重跑 canonical `DEFAULT_VERIFIER_CHAIN`；
6. 持久化 Observation ledger、SafetyProfile 和 Artifact revision/status；
7. 更新 `consult_sessions.state_version`；
8. 写入最小 `GraphRun`、`GraphRunStep(domain_commit)` 和 `GateResult(canonical_verifier_chain)`；
9. 写入 `OutboxEvent`；
10. 写入稳定结果 `DomainCommandCommit` 并提交。

显式 flush 只用于保证 FK 插入次序；所有 flush 都在同一个数据库事务中。任何 SQLAlchemy/DBAPI 失败都会回滚，并仅对外返回 `TRANSACTION_FAILED`，不保留底层异常文本或异常链。Repository 不读写 Graph checkpoint，`consult_sessions.state_snapshot` 继续由 Legacy 路径拥有；L2 权威状态完全从 L2 业务表重建。

Artifact revision > 1 时，Repository 在提交前按 `(session_id, artifact_id, revision - 1)` 取得现有数据库行，并要求 Delta 的 `parent_revision_id` 精确等于该行主键。L2-1 复合外键继续作为最终数据库约束。

## 数据库级幂等

唯一约束为：

```text
(session_id, idempotency_key_ref, input_state_version, agent_spec_version)
```

其中调用方 `RunSpec.idempotency_key` 在入库前转换为 `command:<sha256>` 稳定引用，保持等值查询和唯一性，同时避免自由文本进入 run metadata。`DomainCommandCommit` 还保存 Delta digest、input/output version、changed、GraphRun ID 和 Outbox ID。

- 首次请求：锁定 session、重验 Reducer、执行全部事务写入。
- 完全重复：在版本检查和 Reducer 之前命中提交记录，返回与首次相等的 `CommitResult`；不新增 Domain、run、gate、step 或 Outbox 行。
- 相同复合键但不同 Delta digest：固定拒绝 `IDEMPOTENCY_KEY_REUSED`。
- 不同幂等键：绝不复用已有结果；若输入版本已过期，固定拒绝 `STATE_VERSION_CONFLICT` 且零写入。
- 同一版本并发不同命令：session 行锁串行化临界区，至多一个更新版本，后到者看到 stale version 并零写入。

不使用进程内缓存、mutex 或单进程假设。

## Outbox 状态机

```text
pending --claim--> leased --ack--> published
   ^                 |
   |--release_failed-|

leased(expired) --claim--> leased(new worker)
```

- `claim(worker_id, limit, lease_seconds)`：在一个事务中查询可用 pending 或 lease 已过期行，使用 `FOR UPDATE SKIP LOCKED`，写入 worker/lease 并增加 `attempt_count`。
- 多 worker：被一个事务锁定的行会被其他 worker 跳过，不能同时领取。
- `acknowledge()`：仅当前 lease owner 可把 leased 改为 published；清除 lease/error 并写 `published_at`。published 永不再被 claim。
- `release_failed()`：仅接受 `OutboxErrorCode` 枚举，不接受异常文本；清除 lease、保存固定码、设置下一 `available_at` 并回到 pending。
- 恢复：pending 是数据库持久状态；进程退出后不丢失。leased 行到期后可由新 worker 重新领取。

本任务只提供查询、claim/lease、ack/retry 边界，不调用 Redis、SSE 或任何外部 publisher。

## 并发、回滚与恢复证据

真实 PostgreSQL 专项覆盖：

- `0002 → 0003 → 0002 → 0003` migration 循环；
- Observation、SafetyProfile、Artifact revision 的落库和权威状态重建；
- 单事务 Domain/version/run/step/gate/outbox/commit 原子提交；
- Outbox `BEFORE INSERT` 故障触发器导致所有 Domain 与 metadata 写入、版本更新全部回滚；
- stale version 零写入；
- 两个异步连接并发提交同一版本，恰好一成一败；
- 重复请求在 monkeypatch 为“禁止调用”的 reducer 下仍返回相同结果，证明重放不重跑 Reducer；
- 错误父 row ID 被 Repository 拒绝，精确父 row ID 成功；
- 两 worker 并发 claim 得到不同事件；失败释放、固定错误码、重试计数、ack 后不再领取；
- 模拟进程重启后 pending 和 leased-expired 均可重新领取。

## 隐私边界

Outbox payload 的固定键只有：

```text
session_id
input_state_version
output_state_version
observation_ids
artifact_ids
```

事件类型固定为 `domain.state_committed.v1`。payload 不复制 fact key/value、Safety 内容、Artifact 临床内容、Prompt、raw model output、异常或身份字段。Trace 和 idempotency key 均转换为稳定 SHA-256 引用再持久化；数据库错误统一为固定码且断开异常链。负向测试把身份/Prompt/API key 风格文本放入 Domain value 和 trace，确认 run/step/gate/commit/outbox metadata 不含原文。

## 精确验证结果

执行环境：Windows，Python 3.12.12，pytest 8.4.2，真实 PostgreSQL。

```text
$env:DB_URL='postgresql://xuanhu:xuanhu_dev@localhost:5432/xuanhu'
uv run pytest tests/test_l2_5_repository_outbox.py -q -rs
12 passed, 4 warnings in 6.68s

uv run pytest tests/test_l2_1_domain_schemas_and_migration.py tests/test_l2_4_verifier_reducer.py -q -rs
24 passed, 4 warnings in 1.74s

uv run pytest -q -rs
1145 passed, 1 xfailed, 10 warnings in 214.23s

uv run ruff check .
All checks passed!

uv run mypy app
Success: no issues found in 93 source files

uv lock --check
Resolved 83 packages

git diff --check
exit 0；仅有既有 LF→CRLF 工作区提示
```

四个专项 warning 和全量中的八个相关 warning 均为既有 Alembic `path_separator` 弃用提示；全量另有既有 Pydantic 字段名 warning 和 SQLAlchemy/asyncpg cancellation runtime warning。

## 风险与未完成项

- 没有接入 publisher、Redis Stream、SSE、EventService、API、MainGraph 或业务 Agent；这是明确范围边界。
- Repository 当前只重建“当前权威状态”，不提供任意历史 `state_version` 快照读取；审计历史来自 append-only Observation/Artifact ledger 与 commit metadata。
- Trace/idempotency 在数据库中是 SHA-256 稳定引用；排障工具若要按原始引用查询，需要使用同一规范化函数计算引用。
- Outbox 没有实现最大重试次数或 dead-letter 状态；本任务只定义可恢复 retry 边界，具体发布策略留给后续 publisher 任务。
- `AGENT_RUNTIME_VERSION` 默认值、Legacy 生产链路和 EventService 行为均未修改。
- 未提交 commit。

交接文件：`docs/dev-handoff/agent-refactor-l2-5.md`。
