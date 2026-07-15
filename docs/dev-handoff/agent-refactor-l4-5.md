# L4.5 Integration & Safety Hardening 交接

## 任务状态与事实口径

- 任务范围：重新打开 L0～L4 的工程验收，关闭中期审查列出的 2 个 P0 与 10 个 P1，不扩展 L5 Safety/HITL 业务语义。
- 实施分支：`codex/l4-5-integration-safety-hardening`。
- 初次技术提交：`3a92faa`；终审加固技术提交：`c9148c2`。
- 工程状态：L4.5-01～L4.5-10 已通过技术验收；L4.5-02 的自动化与工程验收通过，但临床红旗规则人工审定尚未完成。
- 阶段状态：L0～L4 可表述为“工程重新验收通过”，L4.5 不能表述为临床关闭、临床发布完成或真实患者试点获准。
- 临床门禁：`docs/01_agent部分优化/临床红旗规则人工审定签署单-2026-07-14.md` 当前明确为待具名临床专业人员审定；本交接不填写、不推定也不替代该签署。

## 发布范围总表

| 任务 | 发布范围 | 对应中期问题 |
|---|---|---|
| L4.5-01 | 测试数据库、Redis 和破坏性夹具安全隔离 | P0-02 |
| L4.5-02 | 模型调用前的原文确定性红旗预检 | P0-01 |
| L4.5-03 | 创建会话初始 Domain State、grounding 和高风险事实确认台账 | P1-01、P1-07 |
| L4.5-04 | 公共写命令耐久幂等和在途命令协调 | P1-02 |
| L4.5-05 | LangGraph Recovery 与 Legacy 的运行时隔离 | P1-03 |
| L4.5-06 | PostgreSQL 权威 Session Read Model 与版本化 DTO | P1-04 |
| L4.5-07 | Outbox publisher、Redis Stream、SSE、健康和可部署告警 | P1-05 |
| L4.5-08 | 模型运行审计与有界临床临时缓存 | P1-08、P1-09 |
| L4.5-09 | 默认关闭的 LangGraph WebUI 灰度闭环 | P1-10 |
| L4.5-10 | CI、供应链、全量回归和 L0～L4 重新验收门禁 | P1-06 与重新验收清单 |

## L4.5-01 测试数据库安全隔离与破坏性夹具治理

- 发布范围：统一真实服务测试入口；禁止从 `DB_URL`/`REDIS_URL` 推断破坏性测试目标；并行 worker 使用独立 PostgreSQL 数据库和 Redis logical DB。
- 执行证据：`tests/conftest.py` 在 integration 会话开始前创建唯一 run/worker 数据库，迁移到唯一 head，结束时恢复迁移并删除 worker 数据库；Redis worker 映射在 8～15 范围内清理。
- 直接文件：`tests/_database_safety.py`、`tests/conftest.py`、`.github/workflows/quality.yml`、`scripts/verify_l0_l4_reacceptance.ps1`。
- 直接测试：`tests/test_database_safety.py`、`tests/test_infrastructure_isolation.py`。
- 技术验收状态：通过。默认 unit 使用不可连接的占位地址；integration 缺少任一显式变量即失败，不允许 skip 后误报成功。
- 明确边界：只允许数据库名以 `_test` 结尾、Redis DB 8～15 且 `XUANHU_ALLOW_DESTRUCTIVE_TESTS=1`；脚本不会接受开发库作为替代。

## L4.5-02 原文确定性红旗预检与模型漏报阻断

- 发布范围：在模型抽取之前对患者原文执行版本化高召回预检；模型候选只能补充确定性候选，不能删除或降级候选。
- 执行证据：`triage-raw-text-precheck.v1` 处理高危短语、数值阈值、否定范围、既往/已缓解、假设、第三人称、不确定表达及无法可靠解析的 fail-closed 路径。
- 直接文件：`app/agent_runtime/triage_precheck.py`、`app/services/langgraph_intake.py`、`app/agent_runtime/triage_policy.py`。
- 直接测试：`tests/test_triage_precheck.py`、`tests/test_l3_5_intake_subgraph.py`、`tests/test_l3_2_triage_policy.py`。
- 技术验收状态：自动化和工程验收通过；模型返回空候选时，原文红旗仍会阻断。
- 临床验收状态：未完成。高危类别、同义词、否定/时态、阈值、处置文案及召回评估集仍须具名临床专业人员审定。
- 明确边界：临床签署完成前，LangGraph 必须默认关闭，不得进入真实临床、患者服务或临床试点；自动化测试不能代替临床审定。

## L4.5-03 初始 Domain State、grounding 与安全事实确认

- 发布范围：创建 LangGraph 会话时原子写入身份无关初始 Observation；对模型事实建立精确 quote/span grounding；高风险模型事实只进入提议台账。
- 执行证据：结构化表单输入按来源写入权威状态；模型抽取的过敏、妊娠、哺乳、当前用药等先生成 `proposed` assertion，只有 confirm/reject/retract 命令能改变权威投影。
- 直接文件：`app/services/initial_domain_seed.py`、`app/agent_runtime/intake_grounding.py`、`app/services/safety_confirmation.py`、`app/models/domain.py`、`app/db/migrations/versions/20260714_0009_safety_fact_assertions.py`。
- 直接测试：`tests/test_initial_domain_seed.py`、`tests/test_safety_confirmation_unit.py`、`tests/test_safety_confirmation_integration.py`、`tests/test_l3_1_intake_extraction.py`。
- 技术验收状态：通过。
- 明确边界：姓名、`patient_ref` 等身份信息不进入模型上下文；无 `X-Doctor-Id` 的结构化表单只可称为 system-recorded input，不能称为具名医师确认。

## L4.5-04 公共写命令耐久幂等

- 发布范围：为公共写 API 建立 operation/scope/key/request digest 的 PostgreSQL claim 和稳定 outcome replay；解决跨 trace、断线重试和真实多进程竞争。
- 执行证据：同一 key 与同一请求只允许一个 owner 执行业务副作用；相同 key 不同请求摘要固定冲突；已完成响应和固定错误均可耐久重放。
- 直接文件：`app/services/http_idempotency.py`、`app/models/http_command.py`、`app/api/request_context.py`、`app/db/migrations/versions/20260713_0008_http_command_claims.py` 及各公共写 API 路由。
- 直接测试：`tests/test_http_command_idempotency.py`、`tests/test_idempotency_protocol.py`、`tests/_http_command_idempotency_subprocess.py`。
- 技术验收状态：通过，包括真实双操作系统进程竞争的单执行/稳定重放路径。
- 明确边界：幂等只保证同一 operation/scope/key 与相同 request digest；调用方不能跨 operation 或跨 scope 复用 key 来合并不同命令。

## L4.5-05 LangGraph Recovery 运行时隔离

- 发布范围：Recovery dispatcher 必须先读取会话的持久化 runtime，再选择实现；禁止 LangGraph 会话进入 Legacy checkpoint/snapshot 修改路径。
- 执行证据：Legacy 会话保留 Legacy recovery；LangGraph 会话固定返回 `501 LANGGRAPH_RECOVERY_NOT_IMPLEMENTED`，且不读取、不写入 Legacy recovery 存储。
- 直接文件：`app/services/recovery_dispatcher.py`、`app/api/recovery.py`。
- 直接测试：`tests/test_recovery_dispatcher.py`、`tests/test_recovery_api.py`。
- 技术验收状态：以 fail-closed 隔离口径通过。
- 明确边界：本任务没有实现可用的 LangGraph Recovery Adapter，`501` 只能证明安全隔离，不能证明恢复能力已完成。

## L4.5-06 PostgreSQL 权威 Session Read Model

- 发布范围：GET session 从 PostgreSQL Domain State、Gate 和 current Artifact 生成版本化 L3/L4 DTO；前端刷新或应用进程重建后可重新取得结果。
- 执行证据：Read Model 以权威 revision 和 current 状态排序、过滤 stale/superseded 产物，并输出 unresolved source/kind/key；非权威 `state_snapshot` 不能覆盖结果。
- 直接文件：`app/services/session_read_model.py`、`app/schemas/session_read_model.py`、`app/api/sessions.py`、`frontend/src/utils/readModel.ts`。
- 直接测试：`tests/test_session_read_model.py`、`tests/test_sessions_api.py` 及相应前端组件测试。
- 技术验收状态：通过。
- 明确边界：Read Model 是读取投影，不是新的临床真源；它不得从 Redis、进程缓存或调用方 snapshot 恢复权威临床事实。

## L4.5-07 Outbox、Redis Stream、SSE 与可部署告警

- 发布范围：独立 publisher 实现 claim/publish/ack/retry/backoff/DLQ/lease takeover；Redis 使用原子去重和有界保留；SSE 支持独立客户端；运维面输出隐私安全聚合指标和 Prometheus 告警规则。
- 执行证据：publish 后 ack 前失败可由 lease 接管且不重复写 Stream；达到最大重试进入 durable DLQ；Outbox health 和 Prometheus endpoint 只输出固定名称聚合值。
- 直接文件：`app/services/outbox_publisher.py`、`app/services/events.py`、`app/core/redis.py`、`app/api/health.py`、`app/services/outbox_metrics.py`、`deploy/prometheus/rules/xuanhu-outbox-alerts.yml`、`app/db/migrations/versions/20260714_0008_outbox_dead_letter.py`。
- 直接测试：`tests/test_outbox_publisher.py`、`tests/test_outbox_publisher_integration.py`、`tests/test_sse_stream.py`、`tests/test_outbox_health.py`、`tests/test_outbox_metrics.py`。
- 技术验收状态：通过。规则覆盖 backlog age、DLQ、health unavailable、publisher disabled 和按 job/instance 判定的 metrics missing；固定版本 Prometheus `promtool check rules` 与 `promtool test rules` 均纳入一键复跑脚本和正式 CI。
- 明确边界：仓库交付可抓取端点和可部署规则，不负责机构 Prometheus/Alertmanager 的实际安装、通知接收人或短信/IM 路由；这些必须由部署方配置并演练。

## L4.5-08 模型运行审计与有界临时缓存

- 发布范围：生产模型调用记录 actual model、spec/prompt/policy 版本、attempt、latency、usage、固定错误码及输入/输出 digest；Intake/Syndrome/Formula 临时缓存增加容量、TTL 和 terminal 清理。
- 执行证据：input digest v2 绑定 canonical validated DTO 与实际有序 messages；required PostgreSQL recorder 的 started 写失败/超时会在 gateway 前 fail-closed，terminal 写失败不会返回 artifact；provenance、终态字段及 terminal→started 冲突固定拒绝，完全相同终态重放幂等。审计不保存原始临床文本。
- 直接文件：`app/services/model_run_audit.py`、`app/models/model_run_audit.py`、`app/db/migrations/versions/20260715_0010_model_run_audits.py`、`app/db/migrations/versions/20260715_0011_model_run_audit_input_provenance.py`、`app/agent_runtime/runtime.py`、`app/agent_runtime/specs.py`、`app/agent_runtime/ephemeral_cache.py`。
- 直接测试：`tests/test_model_run_audit.py`、`tests/test_model_run_audit_integration.py`、`tests/test_ephemeral_cache.py`、`tests/test_gateway.py`。
- 技术验收状态：通过。
- 明确边界：无密钥 SHA-256 digest 用于完整性和关联，不是保密或原始内容恢复机制；审计字段不能扩展为保存 prompt、患者消息或完整模型输出。若审计读权限扩大，应升级为带 key version 的 HMAC。

## L4.5-09 WebUI LangGraph 灰度闭环

- 发布范围：创建页可显式选择 LangGraph，推进栏调用新版 advance，页面显示 runtime、graph revision、current artifact 和 unresolved；请求使用稳定 idempotency key。
- 执行证据：前后端 feature flag 均默认关闭；显式启用后可创建/推进 LangGraph 会话并从 Session Read Model 在刷新后恢复显示。
- 直接文件：`frontend/src/components/CreateSessionModal.tsx`、`frontend/src/components/LangGraphAdvanceBar.tsx`、`frontend/src/api/index.ts`、`frontend/src/utils/readModel.ts`、`frontend/.env.example`、`app/core/config.py`、`app/api/sessions.py`、`app/api/advance.py`。
- 直接测试：`frontend/src/components/CreateSessionModal.test.tsx`、`frontend/src/components/LangGraphAdvanceBar.test.tsx`、`frontend/src/api/client.test.ts`、`frontend/src/App.test.tsx`。
- 技术验收状态：通过。
- 明确边界：`VITE_LANGGRAPH_UI_ENABLED` 和 `XUANHU_LANGGRAPH_PUBLIC_ENABLED` 默认均为 false；UI 可见不代表临床许可，也不实现 L5～L9。

## L4.5-10 CI、供应链与全量重新验收

- 发布范围：Python 3.11/3.12 unit+contract、真实 PG/Redis integration、串行生产形态 Legacy/LangGraph 性能基线与 worker collision、Ruff/mypy/lock、前端、依赖 audit、SBOM、Prometheus rule syntax/behavior、actionlint、Gitleaks history 和 detached clean tree 形成单一门禁。
- 执行证据：`.github/workflows/quality.yml` 固化 CI；`scripts/verify_l0_l4_reacceptance.ps1` 固化本地/验收环境的精确命令，并在 integration 前验证三项显式安全变量。
- 直接文件：`.github/workflows/quality.yml`、`pyproject.toml`、`frontend/package-lock.json`、`.gitleaksignore`、`scripts/verify_l0_l4_reacceptance.ps1`。
- 直接测试：`tests/test_reacceptance_gate_script.py`、`tests/test_database_safety.py`、`tests/test_infrastructure_isolation.py`，以及脚本调用的全量 suites。
- 技术验收状态：通过。当前证据为 Python 3.11/3.12 各 `1547 passed, 362 deselected`；真实服务 `359 passed, 1 xfailed`；碰撞 `2 passed`；双性能基线连续两轮 `2 passed`；前端 `23 files / 171 tests`；静态、安全、SBOM、promtool 和 Gitleaks 均通过。完整证据见重新验收报告。
- 明确边界：可复跑脚本要求 clean HEAD 和显式 `TEST_DATABASE_URL`、`TEST_REDIS_URL`、`XUANHU_ALLOW_DESTRUCTIVE_TESTS=1`；它不会自动启动、猜测或清空开发基础设施，也不会把临床签署变成自动化检查。

## 精确复跑入口

在 PostgreSQL 测试数据库、Redis 测试 logical DB 和 Docker 已就绪，且当前 Git worktree 干净时，在仓库根目录执行：

```powershell
$env:TEST_DATABASE_URL = "postgresql://127.0.0.1:5432/xuanhu_test"
$env:TEST_REDIS_URL = "redis://127.0.0.1:6379/8"
$env:XUANHU_ALLOW_DESTRUCTIVE_TESTS = "1"
powershell -NoProfile -File scripts/verify_l0_l4_reacceptance.ps1
```

上面的值仅为结构示例，执行人必须提供实际隔离测试资源；脚本会再次调用 `tests._database_safety` 拒绝不安全目标。脚本不接受跳过 integration/security 的开关，所有命令均为完整参数，不含省略项。

脚本会把环境证据 manifest、生产依赖导出、Python CycloneDX 1.6 SBOM 和 Node CycloneDX SBOM 写入 `.codex_tmp/l0-l4-reacceptance`，并对 Git 历史和从 exact HEAD 创建的 detached clean worktree 分别执行 Gitleaks。clean worktree 在 `finally` 中移除；移除前必须同时证明目标位于操作系统临时目录的批准子目录内，并且该目录确为预期 Git worktree root。

## `3a92faa` 后的终审加固

- L0：`runtime.switched` 持久台账、部署 CLI、advisory lock、配置/台账 readiness 与创建前 fail-closed；migration `20260715_0012` 保证 deployment 唯一。
- L1：FastAPI lifespan 每 worker 复用一个 checkpointer pool、saver 和 compiled graph；生产无 request-local fallback；Windows 使用受支持 Selector loop 的 `xuanhu-api` 入口。
- L2：required model-run recorder、policy/input provenance migration `20260715_0011`、canonical DTO + ordered messages digest 和重放冲突保护。
- 性能：Legacy 与 LangGraph 都使用生产 SQLAlchemy pool，20 个独立新会话各一条首轮消息；两轮 LangGraph P95 分别 `1.15s`、`1.82s`，阈值 `<5s`。确定性 gateway 只测编排，不代表 live-model SLA。
- 运维：Outbox 聚合 metrics、5 条 Prometheus 规则和 rule behavior tests。
- 临床准备：29 条合成工程种子及 manifest 可复跑，但报告状态固定为 `not_for_clinical_signoff`。

## 本交接的非声明事项

- 不声明具名临床人员已经审定红旗规则。
- 不声明 LangGraph Recovery 已实现；当前仅为 501 fail-closed 隔离。
- 不声明外部 Prometheus/Alertmanager 和通知渠道已部署。
- 不声明 runtime switch 的自由文本 operator/reason 具备不可抵赖身份；生产可进一步接 CI/OIDC 与最小权限发布角色。
- 不声明 L5～L9 已开始或完成。
- 不以本文档替代代码、测试输出、CI run 或正式机构审批。
