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

## 11. L5-2-R1 开发交付（2026-07-22）

### 11.1 状态、基线与限定范围

- 状态：**R1 已交付，申请验收**；执行者不声明 accepted、clinical approved 或 production ready。
- R1 clean release / 执行起点 exact HEAD：`b09c94ce7da4edefea1e90b7c2f7963c02903c03`。
- exact parent 保留失败交付 `335f7ad1f8b07535edec3420f39dea5fcef02e4c`、`ACC-20260722-022`、`DEC-20260722-016`、原任务与 R1 合同；没有 reset、覆盖或删除第 1 轮失败证据。
- 开始时分支为 `codex/l4-5-11-context-privacy-hardening`，worktree/index clean；仓库及其父目录没有 `AGENTS.md`，全部任务/决策/handoff/accepted L5-1 authority 均 tracked。
- R1 只修改 `app/agent_runtime/sandbox_explanation.py`、`tests/test_l5_2_sandbox_safety_explanation.py` 和本文；没有修改 R1 任务书、L5-1、PM 台账、配置、依赖、公共开关、Runtime、HTTP、DB、Gateway、Legacy、review、record/export 或 L5-3。

### 11.2 三项真实 RED

生产代码仍为 `335f7ad` 行为、工作区唯一变更为专项测试时，在第 8 节完整 fake 环境运行：

```powershell
uv run pytest tests/test_l5_2_sandbox_safety_explanation.py -q -rs
```

结果为退出码 `1`，`3 failed, 15 passed in 6.29s`。三个合同指定测试全部收集并按共享引用根因失败：

1. `test_l5_2_port_request_nested_changes_cannot_change_allowlist_authority`：port 通过 `object.__setattr__` 改写 request nested allowlist text 后，旧 verifier 错误 attach 非 snapshot text；
2. `test_l5_2_port_request_entries_are_identity_isolated_from_verifier_snapshot`：port 只改 request-local entry、返回原 canonical candidate 时，旧 verifier 因共享 entry 被污染而错误 unavailable；
3. `test_l5_2_post_port_allowlist_and_source_snapshots_are_revalidated`：port 返回前改写原调用方 allowlist 时，旧实现没有 post-call allowlist revalidation，错误 attach。

RED 时 HEAD 仍为 `b09c94c...`，生产模块未修改；没有 skip、xfail、条件跳过、弱化断言或先改生产代码。

### 11.3 copy isolation、snapshot 与 revalidation

R1 在 port 调用前建立单一 verifier authority：

- source 保存完整 `canonical_result_bytes`，并把 schema/adapter version、decision、逐 issue 的 ID/rule/severity/order、decision subject digest、run envelope digest 和 result digest 冻结为 primitive invariant tuple；
- allowlist 保存完整 canonical bytes、原 allowlist digest 和逐 entry 的 `rule_id/text` primitive invariant tuple；独立构造 `rule_id→text` verifier mapping；
- source issue authority 独立投影为 pre-call `issue_id→rule_id` 和 source order mapping；port 返回后不从 request 或共享 nested model 重建任何 verifier authority；
- port request 的每个 issue ref 与每个 allowlist entry 都逐字段创建为新 DTO；request nested entries 不复用 verifier allowlist entry，request-local mutation 最多改变 port 自己的临时对象；
- port 返回后立即 strict 重解析原 source input 与原 allowlist input，并比较 canonical bytes、digest 和全部 invariants；candidate 验证和 final size gate 后、attach 前再次执行同一重验；任何差异只返回 fixed `explanation_unavailable`。

专项同时证明：request-local issue ID/allowlist text 被绕过 frozen 标志改写后，原 candidate 仍按 pre-call snapshot attach；把 request-local 非 snapshot text 回显为 candidate 时 fixed unavailable；caller source/allowlist canonical bytes 不因 request alias 改变。fixed unavailable 继续无 candidate/异常 payload、cause/context 或日志，L5-1 source bytes/digest 不变。原 exact reference、intervention、64 issues、8 KiB、immutability、zero import/call 和资源边界全部保留。

实现后的首次专项即为 `18 passed in 6.63s`，没有 first-GREEN 故障；增加 request-local issue-ref 断言后为 `18 passed in 6.75s`，最终交付树复跑为 `18 passed in 6.38s`。

### 11.4 R1 GREEN、回归与资源证据

除 calibrated full suite 只移除 `APP_ENV` 外，下列命令均使用第 8 节完整 fake 环境：

| 门禁 | R1 结果 |
|---|---|
| L5-2/R1 专项 | 最终交付树 `18 passed in 6.38s` |
| true-max 独立资源输出 | `1 passed in 6.48s`；指标如下 |
| accepted L5-1 回归 | `14 passed in 13.54s` |
| Safety 回归 | `71 passed, 3 deselected in 2.09s` |
| L4.5-11 privacy 回归 | `76 passed in 4.67s` |
| Ruff | `All checks passed!` |
| mypy | `Success: no issues found in 1 source file`；仅有既有 `pymilvus.*` unused-section note |
| L0 | 最终交付树 `131 passed in 2.09s`；前次 `131 passed in 1.97s` |
| `uv lock --check` / `git diff --check` | lock `Resolved 84 packages in 4ms`；diff check 无错误 |
| 强制 `APP_ENV=sandbox-test` 全量 | `1 failed, 1656 passed, 362 deselected in 131.32s`；唯一为既有 `test_load_with_defaults` 预期 `local`、实际 `sandbox-test` |
| 只移除 `APP_ENV`、保留全部 fake endpoints 的校准全量 | `1657 passed, 362 deselected in 138.70s` |

true-max committed test 继续使用 64 个 unique issues/rules 与 64 个 exact statements：candidate `7,926` bytes、final result `8,176` bytes；预热 20 次、正式 1,000 次，Python `3.12.12`，CPU `Intel64 Family 6 Model 142 Stepping 12, GenuineIntel`。`perf_counter_ns` p95/p99 为 `4.947 ms` / `6.397 ms`，Windows process working-set RSS 增长 `61,440 bytes`，满足 `<50 ms`、`<100 ms`、`<64 MiB`，没有放宽 64/8 KiB/资源门槛。

### 11.5 受控边界、提交与回退

- 全部执行仅使用 inline fixed-fictitious synthetic fixture 和第 8 节 loopback unavailable fake 值；没有读取/显示 `.env`，没有读取 ignored `data/`/`.codex_tmp`，没有启动或连接应用、网络、HTTP/E2E、容器、DB、Redis、Milvus、模型/embedding Gateway 或外部服务。
- 强制 `APP_ENV=sandbox-test` 与既有 defaults 测试的冲突原样保留；校准全量只移除 `APP_ENV`，不能替代精确命令事实，也没有修改合同外配置/测试制造通过。
- 本 R1 不改变 L5-1 decision authority，不实现真实 generator/LLM、wall-clock timeout runner、Runtime、review/challenge、record/export 或 L5-3；真实临床、患者服务、商业/公开生产和人体研究继续 NO-GO。
- 冻结前 `git diff --name-only` 精确列出三个允许文件，`git ls-files --error-unmatch` 对三者全部成功，`git diff --check` 无错误；冻结后以外部报告的 exact HEAD 和空 `git status --short` 完成 tracked/clean 证明，本文不伪造自引用 SHA。
- 单一 R1 开发提交消息为 `fix: isolate L5-2 explanation port authority`，exact parent 必须为 `b09c94ce7da4edefea1e90b7c2f7963c02903c03`，提交只含 11.1 节三个文件。
- Git SHA 不能在包含本文的同一提交中自引用；冻结后以 `git rev-parse HEAD`、exact parent、唯一提交消息、三文件 scope 和 clean worktree 共同报告 delivery exact HEAD，由独立 Reviewer/CI/PM 后续锚定。
- 若 R1 独立验收失败，使用 `git revert <r1-delivery-exact-head>` 保留全部历史，不 reset 或覆盖原失败交付。

---

**R1 已交付，申请验收。**

## 12. 项目经理 R1 最终验收（2026-07-22）

- 冻结交付：`1957ad311b3997499e4f9a0e3f2dd95aa652fa9e`；exact parent `b09c94ce7da4edefea1e90b7c2f7963c02903c03`；分支正确；提交只含原合同三个文件；Review、CI 与 PM 前后 worktree/index clean。
- 真实先红与最终 GREEN：原 `335f7ad` 行为下三个 R1 回归为 `3 failed, 15 passed`；最终专项 `18 passed`。第 1 次失败交付、P1 和 R1 修复历史均保留。
- 独立 Reviewer：P0=0、P1=0、P2=0、P3=0，结论 `no findings`；原共享 nested entry P1 **resolved**。Reviewer 独立确认 pre-call primitive/canonical snapshot、逐字段新 request DTO、只读 snapshot verifier、port 返回后及 attach 前重验成立。
- 独立 CI：L5-2 `18 passed`、true-max `1 passed`、L5-1 `14 passed`、Safety `71 passed, 3 deselected`、privacy `76 passed`、L0 `131 passed`；Ruff、mypy、lock、diff、scope、tracked、clean 全通过。
- 全量校准：强制 `APP_ENV=sandbox-test` 为 `1 failed, 1656 passed, 362 deselected`，唯一是既有 defaults 测试期望 `local`；只移除该变量且保持全部 fake endpoints 后为 `1657 passed, 362 deselected`。没有修改范围外 config/test 制造通过。
- 资源：Reviewer 对 64 issues/64 statements、7,926-byte candidate、8,176-byte result、1,000 samples 测得 p95 `8.891 ms`、p99 `9.964 ms`、RSS `+86,016 B`，满足原阈值。
- PM 定向探针：三个 R1 回归、valid attach、source byte identity、true-max 合计 `6 passed`；p95 `7.469 ms`、p99 `8.202 ms`、RSS `+303,104 B`。
- PM 结论：**通过 / accepted**（`ACC-20260722-023`）；关闭 `R-L5-EXPL-001`。允许项目经理从 clean accepted 基线另行发布 L5-3，但本验收事务不发布或实施 L5-3。
- 边界：仅个人学习、非临床、固定虚构 synthetic、离线单元级 sandbox；不构成临床、法律、隐私、伦理、监管或生产批准。应用 Runtime、HTTP/E2E、容器、DB、Gateway、Legacy、真实患者/机构/公开服务继续 NO-GO；G1～G6、EXT-001、EXT-002 保持原状态。

---

**L5-2 已完成 / accepted；下一动作仅为项目经理另行发布 L5-3。**
