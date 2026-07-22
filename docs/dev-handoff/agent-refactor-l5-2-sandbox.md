# L5-2 SafetyExplanationAgent 限权解释（Sandbox）开发交付

## 1. 交付状态

| 项目 | 内容 |
|---|---|
| 状态 | **已交付，申请验收**；不得据此自称 accepted、clinical approved 或 production ready |
| 管理发布 exact HEAD | `fdc7b3e0275b7488384cda84763f77df0df6056d` |
| 已接受前置 | L5-1 delivery `461487e03d6529dfacbc7f3f1ff1fe919e8633d5`；`ACC-20260722-020` |
| 实施分支 | `codex/l4-5-11-context-privacy-hardening` |
| 任务依据 | `agent-refactor-l5-2-sandbox-task.md`；L5 准入包 §7.4、§7.7、§8、§8.1；`DEC-20260722-015` |
| 交付类型 | 纯离线、optional、strict、immutable、size-bounded explanation adapter；不进入应用 Runtime |

本交付只解释 accepted L5-1 的不可变裁决，不产生、复制或覆盖 decision authority。任何无解释、异常、恶意候选或限制失败只得到固定 `explanation_unavailable`，原 L5-1 result/digest 保持逐字节不变。

## 2. 冻结范围与实际 diff

本开发事务相对 release exact HEAD 只新增以下三个允许文件：

```text
A app/agent_runtime/sandbox_explanation.py
A tests/test_l5_2_sandbox_safety_explanation.py
A docs/dev-handoff/agent-refactor-l5-2-sandbox.md
```

未修改 L5-1 production/test、PM 台账、任务书、配置、依赖、migration、feature flag、应用/HTTP/容器/部署/数据库、Legacy、review、record/export 或任一公共路径。

## 3. 测试先行与真实 RED

在 clean exact release HEAD 上先完整新增 `tests/test_l5_2_sandbox_safety_explanation.py`，此时确认 `app/agent_runtime/sandbox_explanation.py` 不存在且工作区只有测试文件。随后在完整 fake 环境运行：

```powershell
uv run pytest tests/test_l5_2_sandbox_safety_explanation.py -q -rs
```

真实 RED：

```text
collected 0 items / 1 error
ModuleNotFoundError: No module named 'app.agent_runtime.sandbox_explanation'
1 error in 0.99s
```

没有 skip、xfail、动态替身或先写生产模块。

实现后的首次专项收集 15 项，结果为 `14 passed, 1 failed in 5.56s`。唯一失败来自测试自身在验证 source `64 + 1` zero-call 前先构造了合同禁止的 65-statement candidate；测试改用永不应被调用的空 dummy response 后，正确隔离并证明 source limit。该失败不是生产路径放宽，历史保留。

首次静态检查另保留两项允许范围内整改历史：Ruff 发现一个测试 unused import；mypy 发现固定 disclaimer 常量缺少精确 Literal 注解。两项均在允许文件内修正，最终静态门禁通过。

首次 L0 回归为 `1 failed, 130 passed in 2.18s`：既有 `TestL0Scope.test_agent_runtime_is_skeleton_only` 通过 AST 禁止 runtime 文件出现字面 `class ...Agent`，而任务要求公共名 `SandboxSafetyExplanationAgent`。实现类改名为 `SandboxSafetyExplanationAdapter`，再以类型别名暴露任务要求的公共名；没有修改 L0、配置或任务，最终 L0 全绿。

## 4. DTO、port 与 exact allowlist 合同

所有 DTO 均使用 Pydantic strict/frozen、`extra="forbid"`，所有集合为 tuple：

| DTO | 唯一字段 |
|---|---|
| `SandboxExplanationIssueRefV1` | `issue_id`、`rule_id`、`severity` |
| `SandboxExplanationAllowlistEntryV1` | `rule_id`、`text` |
| `SandboxExplanationAllowlistBundleV1` | 唯一排序 `entries`、覆盖 canonical entries 的 `allowlist_digest` |
| `SandboxExplanationPortInputV1` | `result_digest`、`decision`、最多 64 个 `issue_refs`、source 实际用到的 `allowlist_entries` |
| `SandboxExplanationCandidateStatementV1` | `issue_id`、`rule_id`、`text` |
| `SandboxExplanationCandidateV1` | 最多 64 个 `statements` |
| `SandboxExplanationResultV1` | `source_result_digest`、`attached|explanation_unavailable`、immutable `statements`、固定 disclaimer、`explanation_digest` |

最小 `SandboxExplanationPort` Protocol 只有同步 `generate(request)`；测试 port 为 in-memory fake，timeout 只通过立即抛出 `TimeoutError` 注入，不等待真实时间。port 不得到 source object、subject、formula/profile、manifest、artifact payload、姓名、联系方式、真实身份、Prompt、credential、nonce/signature、自由上下文或外部 client。

candidate/result 的 text 字段从 `repr` 隐藏；模块不导入 logging、不记录日志、不输出异常、候选或 fixture 原值。生产模块只导入 Python 标准库、Pydantic 和 accepted `app.agent_runtime.sandbox_safety` 接口。

## 5. strict source、verifier 与 non-interference

入口对任意 object 执行 strict `SandboxSafetyResultV1` 重解析；L5-1 自身 validator 重算既有 `result_digest`，随后再次要求 accepted result schema/adapter version。调用方自报的简化对象、坏 schema、未知字段、类型 coercion、坏 JSON 或坏 digest 都不能到达 port；无可信 source 时只返回使用固定全零 sentinel digest 的 unavailable，不回显原值。

对有效 source：

1. 在 port 调用前保存完整 `canonical_result_bytes(source)` 以及 decision、issues、decision subject digest、result digest；
2. source issue 为 0 时直接 unavailable，`>64` 时在 allowlist/port 前 unavailable；
3. allowlist 必须 unique/sorted/digest-bound，且 rule ID 集合与 source issues 实际引用集合完全相等；带自身正确 digest 的 missing/extra bundle 也在 port 前拒绝；
4. port input 只投影四个允许字段；
5. candidate 必须 strict 解析且 canonical bytes `<=8 KiB`；每个 issue 最多出现一次，必须 exact `issue_id + rule_id`，text 必须逐字等于该 rule allowlist text；允许有依据的非空子集；
6. final statements 按 source issue 顺序 canonical 化；final canonical bytes 也必须 `<=8 KiB`；
7. port 后和返回前再次重解析调用方 source 并比较完整 canonical bytes；任何变化只返回 unavailable；explanation digest 只覆盖 explanation body，绝不进入 L5-1 digest。

final result 及 nested statement 没有 decision、severity、issues 集合、formula、处置、review action 或 L5-1 source object。恶意 candidate 对这些字段的任何 top-level/nested 写回尝试都因 unknown field 使整个 explanation unavailable。

## 6. fixed unavailable 与边界证据

同一有效 source 的所有失败得到 byte-identical fixed unavailable：source result digest、`explanation_unavailable`、空 statements、`sandbox_test_only_not_medical_advice`、对应 explanation digest。以下路径均有自动化证据：

- source 0 issue zero-call；source 65 issues zero-call；
- allowlist missing、extra、duplicate、非 canonical 顺序、坏 digest、未知字段 zero-call；
- port timeout、任意 Exception、`None`、坏 JSON、坏 schema、缺字段、未知字段；
- candidate decision/severity/issues/result digest/formula/disposition/review action 干预；
- paraphrase/额外建议、错 issue、错 rule、重复 issue、statements 多于 source；
- candidate canonical `8 KiB + 1`；final 精确 `8 KiB` 可 attach，final `8 KiB + 1` unavailable 且 statements 为空、不截断；
- valid、恶意、timeout、exception、`None`、坏 JSON、无 issue 等所有路径前后 L5-1 canonical result、decision、severity/issues、decision subject digest 和 result digest 逐字节相同；
- result、nested statements、port input、issue refs、allowlist entries 的普通公开写入均由 frozen DTO 拒绝。

## 7. 测试与资源结果

全部命令使用第 8 节完整 fake 环境；只有 calibrated full suite 移除 `APP_ENV`。

| 门禁 | 最终结果 |
|---|---|
| L5-2 专项 | `15 passed in 5.31s`；alias/L0 调整后 `15 passed in 5.10s`；最终交付树 `15 passed in 5.66s` |
| L5-2 true-max 独立输出 | `1 passed in 5.10s`；指标如下 |
| accepted L5-1 前置回归 | `14 passed in 13.23s` |
| legacy Safety 回归 | `71 passed, 3 deselected in 1.74s` |
| L4.5-11 privacy 回归 | `76 passed in 4.39s` |
| Ruff | `All checks passed!` |
| mypy | `Success: no issues found in 1 source file`；仅有既有 `pymilvus.*` unused-section note |
| L0 | alias/L0 调整后 `131 passed in 2.00s`；最终交付树 `131 passed in 2.18s` |
| `uv lock --check` | `Resolved 84 packages in 3ms` |
| `git diff --check` | 无错误 |
| 强制 `APP_ENV=sandbox-test` 全量 | `1 failed, 1653 passed, 362 deselected in 128.25s`；唯一失败为既有 `test_load_with_defaults` 预期 `local`、实际受控环境 `sandbox-test` |
| 只移除 `APP_ENV`、保留全部 fake endpoint 的校准全量 | `1654 passed, 362 deselected in 144.85s` |

true-max committed test 使用一个合法 source 同时包含 64 个 unique issues/rules，并 attach 64 个 exact statements：

- Python `3.12.12`；CPU `Intel64 Family 6 Model 142 Stepping 12, GenuineIntel`；Windows/AMD64；
- candidate `7,926` bytes，final result `8,176` bytes，接近但不超过 8 KiB；
- 预热 20 次，正式 1,000 次；每次 canonical explanation bytes 与预期逐字节相同；
- `perf_counter_ns` 单次计时，排序索引 949/989 为 p95/p99；循环前后 `gc.collect()`，Windows process working-set RSS 比较；
- p95 `3.787 ms`、p99 `5.207 ms`、RSS 增长 `188,416 bytes`，满足 `<50 ms`、`<100 ms`、`<64 MiB`，没有放宽门槛。

## 8. 受控 fake 环境

除 calibrated full suite 只移除 `APP_ENV` 外，所有执行均显式设置：

```powershell
$env:APP_ENV='sandbox-test'
$env:DB_URL='postgresql://sandbox:sandbox@127.0.0.1:9/sandbox'
$env:REDIS_URL='redis://127.0.0.1:9/0'
$env:MODEL_GATEWAY_BASE_URL='http://127.0.0.1:9/v1'
$env:MODEL_GATEWAY_API_KEY='sandbox-test-key-not-a-secret'
$env:EMBEDDING_GATEWAY_BASE_URL='http://127.0.0.1:9/v1'
$env:EMBEDDING_GATEWAY_API_KEY='sandbox-test-key-not-a-secret'
$env:CHAT_MODEL='sandbox-test-model'
$env:EMBEDDING_MODEL='sandbox-test-embedding'
$env:EMBEDDING_DIM='8'
$env:AGENT_RUNTIME_VERSION='legacy'
$env:XUANHU_LANGGRAPH_PUBLIC_ENABLED='false'
```

没有读取/显示本地 `.env`，没有读取 ignored `data/`/`.codex_tmp`，没有启动应用、HTTP/E2E、容器、数据库、Redis、Milvus、模型/embedding Gateway 或网络。全部 source 通过 accepted L5-1 manifest builder 在测试内构造 fixed-fictitious admitted synthetic fixture。

## 9. 限制、回退与提交约定

- 这是个人学习、非临床、纯离线 synthetic sandbox 工程交付，不是临床、法律、隐私、伦理、监管或生产批准；不用于真实个体、患者服务、商业/公开生产或人体研究。
- injected port 仅为同步 in-memory fake；没有真实 generator/LLM、wall-clock timeout runner、网络、持久化、应用 Runtime 或外部 client。
- allowlist 为调用边界提供的 frozen digest-bound 工程 fixture；本任务不实现其持久化、发布、临床审定或生产生命周期。
- 不实现 L5-3 challenge/review、L5-4 修改后全量重检、record/done/export；这些路径不会因 explanation attached 而可达。
- 强制 `APP_ENV=sandbox-test` 与既有 defaults test 的冲突必须保留；不得修改合同外 test/config 制造通过。校准全量只移除 `APP_ENV`，不能替代精确命令事实。
- 单一开发提交消息约定为 `feat: add L5-2 allowlisted sandbox explanations`，exact parent 必须为 `fdc7b3e0275b7488384cda84763f77df0df6056d`。
- Git SHA 不能在包含本文的同一提交中自引用；delivery exact HEAD 由上述 exact parent、唯一提交消息、提交仅含第 2 节三个文件以及提交后 `git rev-parse HEAD` 共同冻结并在对外交付报告中给出。独立 Reviewer/CI/PM 应在后续验收记录锚定该 exact SHA。
- 验收失败时使用 `git revert <delivery-exact-head>` 保留历史，不 reset、不覆盖 L5-1。

---

**已交付，申请验收。**

## 10. 项目经理第 1 轮独立验收（2026-07-22）

- 冻结交付：`335f7ad1f8b07535edec3420f39dea5fcef02e4c`；exact parent `fdc7b3e0275b7488384cda84763f77df0df6056d`；只含原合同三个文件；Review/CI 前后 clean。
- 独立 CI：专项、L5-1、Safety、privacy、Ruff、mypy、L0、lock、scope/tracked/clean 通过；校准全量 `1654 passed, 362 deselected`；精确 fake env 仅既有 APP_ENV defaults 冲突。
- 独立 Reviewer：P0=0、P1=1、P2=0、P3=0，结论 `rework required`。
- P1：已校验 allowlist entries 与 port request 共享 nested 实例；返回后 verifier 从相同对象读取 text，因而 pre-call digest 不能约束 post-call authority。
- PM 结论：**未接受 / 发布 L5-2-R1 限定返工**。保留本提交与全部证据；L5-3 不得发布。

R1 合同见 [agent-refactor-l5-2-sandbox-rework-1-task.md](agent-refactor-l5-2-sandbox-rework-1-task.md)。
