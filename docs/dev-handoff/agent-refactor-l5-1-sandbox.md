# L5-1 确定性 SafetyRuleEngine adapter（Sandbox）交付

## 1. 交付状态与执行起点

- 状态：**已交付，申请验收**；本文不作 `accepted`、`sandbox_scope_satisfied` 或任何临床/专业准入声明。
- 分支：`codex/l4-5-11-context-privacy-hardening`。
- 包含已发布任务书的 release / 执行起点 clean exact HEAD：`4a2e1392aea660d08c2440113059a94103c2df3d`。
- 开始时 `git status --short --branch` 仅显示分支行，工作区 clean；最近提交和任务书均与发布合同一致。
- 仓库内没有 `AGENTS.md`；任务书、L5 准入包和项目管理台账均为 tracked。执行者只承担本任务开发 writer 角色，未修改 PM 台账。

## 2. 真实 collection RED

在生产模块不存在、工作区唯一变更为新增专项测试时，使用第 7 节的完整 fake 环境运行：

```powershell
uv run pytest tests/test_l5_1_sandbox_safety_adapter.py -q -rs
```

真实结果为退出码 `1`，`collected 0 items / 1 error`，`1 error in 1.13s`。collection 在下列顶层导入处失败：

```text
ModuleNotFoundError: No module named 'app.agent_runtime.sandbox_safety'
```

当时 HEAD 仍为 `4a2e1392...`，`git status --short` 只列出未跟踪的 `tests/test_l5_1_sandbox_safety_adapter.py`；生产文件尚未创建。没有 skip、xfail、动态替身或预写生产代码。

## 3. 实现摘要

`app/agent_runtime/sandbox_safety.py` 是离线、无副作用模块，提供：

- frozen、strict、`extra="forbid"` 的 subject、规则包、规则参数、issue、evaluator output 和 result DTO；所有嵌套集合使用 tuple，builder 对输入复制并固定排序；
- subject 对 test session、Domain state version、formula/profile artifact ID、revision、content digest、graph/adapter/rule bundle 版本与 digest、synthetic dataset 版本与 digest 精确绑定；
- 只接受 `fixed_fictitious_manual`、`sandbox_only`、`not_clinically_adjudicated` 的固定测试标记；专项 fixture 全部 inline 且为纯技术虚构值；
- UTF-8、`ensure_ascii=False`、禁止 NaN、固定 key 顺序和紧凑分隔符的 canonical JSON，以及 SHA-256 digest；嵌套数组按稳定 ID / execution order 排序；
- `decision_subject_digest` 只绑定已验证 subject；`run_envelope_digest` 另行绑定 command/run/trace；`result_digest` 不含 run envelope，因此 run/trace 改变不影响裁决、issues 或结果 authority；
- 最小 `SandboxSafetyEvaluator` Protocol 注入；adapter 对同一 exact 输入执行两次并比较规范化 evaluator output，任何异常、坏 schema、未知 rule、重复 issue/order 或非确定输出均固定拒绝；
- evaluator 的 decision、issue ID、severity 和 execution order 经 strict 重解析后保留；adapter 只做固定排序和边界验证，不派生或覆盖 evaluator 裁决；
- 所有 adapter 失败只抛固定 `SandboxSafetyAdapterError` / `SandboxSafetyFailureCode`，异常消息不带 payload，且 evaluator 异常路径的 `__cause__`、`__context__` 均为 `None`；没有日志或原值输出；
- formula item `<=64`、issue `<=256`、rule `<=256`，subject/rule bundle/result canonical bytes 各 `<=256 KiB`；formula、bundle 和 subject 大小在 evaluator 前检查，issue 超限在首次 evaluator 返回后、任何第二次 evaluator 或下游动作前拒绝；
- 每次 adapter 入口都从 DTO dump 或 JSON 重新 strict 校验，不信任 `model_copy` / `model_construct` 形成的自报可信对象；formula/profile/dataset/rule bundle digest 均现场重算并精确比对。

模块只 import Python 标准库与 Pydantic；不 import 或调用 Settings、DB、Gateway、Legacy `SafetyRuleEngine`、review、record/export、network 或应用 Runtime。

## 4. 专项覆盖与零调用证据

专项保留任务书规定的 10 个测试名，并证明：

1. 同 subject/bundle 在重复运行和两个独立 `PYTHONHASHSEED` 子进程中产生相同 canonical result 与 digest；
2. command/run/trace 改变只改变 `run_envelope_digest`；
3. rule bundle 内容/digest 变化和 stale formula/profile digest 均在 evaluator 前拒绝，fake evaluator 调用数为 `0`；
4. missing、extra、坏 JSON、未知 schema version 全部固定 fail closed，调用数为 `0`；
5. result 和 nested issues 无法经公共赋值、list mutation 或输入 alias 改写；
6. evaluator payload 异常不进入 error string/repr，且无 cause/context；两次不同 evaluator output 固定报 nondeterministic；
7. 65 个 formula item 在 evaluator 前拒绝；257 个 issue 在首次返回后拒绝且不执行第二次 evaluator；
8. AST import probe 限定生产模块只能使用批准的纯本地依赖根；没有 settings/env/data/gateway/review/record/export/network import；
9. 1,000 次最大合法 64-item fixture 全部逐字节复现，并同时验证性能/RSS 门槛。

## 5. GREEN、静态与回归证据

除特别说明外，下列命令均使用第 7 节全部显式 fake 环境覆盖：

| 门禁 | 结果 |
|---|---|
| `uv run pytest tests/test_l5_1_sandbox_safety_adapter.py -q -rs` | 最终复跑 `10 passed in 5.22s`；首次完整 GREEN 为 `10 passed in 5.26s` |
| 资源单测 `...::test_l5_1_thousand_runs_are_reproducible_and_resource_bounded -q -s` | `1 passed in 3.27s`；指标见第 6 节 |
| `uv run pytest tests/test_safety_rule_engine.py -q -rs` | `71 passed, 3 deselected in 1.79s` |
| `uv run ruff check app/agent_runtime/sandbox_safety.py tests/test_l5_1_sandbox_safety_adapter.py` | `All checks passed!` |
| `uv run mypy app/agent_runtime/sandbox_safety.py` | `Success: no issues found in 1 source file`；仅有既有 `pymilvus.*` unused section note |
| `uv run pytest tests/test_l0_1_contract.py -q -rs` | 最终复跑 `131 passed in 2.12s`；前次为 `131 passed in 2.08s` |
| `uv lock --check` | 退出码 `0`，`Resolved 84 packages in 5ms`，lock 未改变 |
| `git diff --check` | 退出码 `0`，无输出 |

实现后的首次专项运行为 `9 passed, 1 failed`：Windows RSS helper 未声明 WinAPI handle/argument 类型，`GetProcessMemoryInfo` 返回 `0`。只在允许的专项测试内补齐 `restype` / `argtypes` 后获得上述最终 GREEN。首次 Ruff/mypy 还分别发现 PEP 695 generic / 测试 print 和 `NoReturn` narrowing；均在两个允许文件内修正，最终静态门禁如表中通过。失败历史未删除或改写。

### 5.1 精确全量命令的环境冲突

按任务书逐字执行 `uv run pytest -q -rs`，同时保持 `APP_ENV='sandbox-test'`，结果为：

```text
1 failed, 1634 passed, 362 deselected in 122.24s
```

唯一失败是既有 `tests/test_config.py::test_load_with_defaults`：该测试未清除当前进程的 `APP_ENV`，却断言未设置环境时的默认值为 `local`，因此实际 `sandbox-test != local`。L5-1 专项在同一次全量运行中全部通过；本交付没有修改 `tests/test_config.py`、`app/core/config.py` 或任何配置代码。要让该精确组合通过，必须修改允许范围外的既有测试，或不再保持任务书要求的 `APP_ENV='sandbox-test'`，二者均不能静默进行。

为区分环境合同冲突和代码回归，执行者保留所有外部连接 fake 值，仅在启动 pytest 前移除 `APP_ENV`，补充运行同一完整默认 suite：

```powershell
Remove-Item Env:APP_ENV -ErrorAction SilentlyContinue
uv run pytest -q -rs
```

补充结果为 `1635 passed, 362 deselected in 122.15s`。这项补充结果不是对精确失败命令的替代；独立 CI/PM 必须据实裁定或发布 bounded 的环境合同校准，开发者不自行把该项写成通过。

## 6. 性能与资源测量

- Python：`3.12.12`；CPU：`Intel64 Family 6 Model 142 Stepping 12, GenuineIntel`；machine：`AMD64`；Windows。
- fixture：最大合法 `64` 个 formula item、固定一个 issue、同一 subject/bundle/run envelope；预热 `20` 次，正式 `1,000` 次。
- 延迟方法：每次调用以 `time.perf_counter_ns()` 计时，升序样本索引 `949` / `989` 分别作为 p95 / p99。
- RSS 方法：Windows `GetProcessMemoryInfo` 的 process working set，在 `gc.collect()` 后比较循环前后，负变化按 `0` 计。
- 实测：p95 `1.933 ms`，p99 `2.681 ms`，RSS 增长 `315,392 bytes`。
- 合同门槛：p95 `<50 ms`、p99 `<100 ms`、RSS 增长 `<64 MiB`；三项均满足，未放宽阈值。

## 7. 受控环境与边界

正式命令前在同一 PowerShell 进程显式设置：

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

除第 5.1 节明确标注的补充全量外没有环境差异。实施与测试没有读取或显示本地 `.env`，没有读取 ignored `data/` 或 `.codex_tmp`，没有启动应用、FastAPI、HTTP/E2E、容器、数据库、Redis、Milvus、RAG、模型/embedding gateway 或其他网络/外部服务。没有真实/可关联个人数据、有效凭据、Prompt、nonce/signature 或临床内容进入 fixture、日志、异常或提交。

## 8. 实际范围、未决限制与回退

本交付只包含任务合同允许的三个文件：

1. `app/agent_runtime/sandbox_safety.py`：新增纯离线 deterministic adapter 与 DTO；
2. `tests/test_l5_1_sandbox_safety_adapter.py`：新增唯一 L5-1 专项；
3. `docs/dev-handoff/agent-refactor-l5-1-sandbox.md`：本交付/验收载体。

未修改 PM 台账、Legacy、RAG、UI、Domain 医疗事实、配置、依赖、migration、public flag、Runtime、Gateway、review、record/export 或部署材料。

未决限制：精确 full-suite + 强制 `APP_ENV=sandbox-test` 组合存在第 5.1 节的既有测试冲突，需独立 CI/PM 裁定；当前实现也按任务非目标不包含持久化、graph node、公开 API、L5-2 explanation、L5-3 review/challenge 或 L5-4 修改后重检。真实临床、患者服务、商业/公开生产和人体研究继续 NO-GO。

若独立验收失败，应以单一交付提交为单位执行 `git revert <delivery-commit>`，保留 RED、GREEN 和失败证据；不得 reset 或覆盖历史。

## 9. 交付提交约定

- 使用单一开发交付提交，提交消息：`feat: add L5-1 deterministic sandbox safety adapter`。
- exact parent 必须为 release HEAD `4a2e1392aea660d08c2440113059a94103c2df3d`。
- Git SHA 取决于包含本文的最终 tree，无法在同一提交正文中自引用尚未生成的 SHA；冻结后以 `git rev-parse HEAD`、交付消息和本文约定共同报告 delivery exact HEAD，由项目经理在独立验收章节锚定。
- 提交必须只含第 8 节三个文件；提交后全部 tracked，工作区 clean。

---

**已交付，申请验收。**

## 10. 项目经理第 1 轮独立验收（2026-07-22）

- 冻结交付：`d3eb7b90611b477eb837c395215473f80fe9726f`；exact parent `4a2e1392aea660d08c2440113059a94103c2df3d`；范围为本 handoff 第 8 节三个文件；Review/CI 前后 worktree clean。
- 独立 CI：专项、Safety、L4.5-11 隐私、Ruff、mypy、L0、lock、diff/scope/tracked 均通过；校准全量 `1635 passed, 362 deselected`。强制 `APP_ENV=sandbox-test` 的原命令保留唯一既有 defaults 冲突，不以合同外配置修改掩盖。
- 独立 Reviewer：P0=0、P1=1、P2=2、P3=0，结论 `rework required`。
- P1：连续两次相等采样不能证明 evaluator 纯度；pair-wise state drift 可令相同 exact 输入跨请求从 `block` 变成 `allow`。
- P2：fixture 缺准入包 §5.3/§8 完整 manifest；资源门禁只测 1 issue，未固化最大合法 256 issues。Reviewer 的 64 items + 256 issues 探针通过，因此性能本身未观察到失败。
- PM 结论：**未接受 / 发布 L5-1-R1 限定返工**。保留本提交和全部失败证据；L5-2 不得发布。

R1 合同见 [agent-refactor-l5-1-sandbox-rework-1-task.md](agent-refactor-l5-1-sandbox-rework-1-task.md)。

## 11. L5-1-R1 开发交付（2026-07-22）

### 11.1 状态、基线与范围

- 状态：**R1 已交付，申请验收**；执行者不声明 `accepted`、`sandbox_scope_satisfied`、临床批准或生产准入。
- R1 clean release / 执行起点 exact HEAD：`53cbb9cad9bbd4630f4259409708966df4369e4d`。
- R1 exact parent 中保留第 1 轮失败交付 `d3eb7b90611b477eb837c395215473f80fe9726f`、PM 验收失败历史、`ACC-20260722-019`、`DEC-20260722-014` 和 R1 任务书；没有 reset、覆盖或删除失败证据。
- 开始时分支仍为 `codex/l4-5-11-context-privacy-hardening`，worktree/index clean，无 `AGENTS.md`，R1 合同和原任务/准入包均 tracked。
- R1 只修改本 handoff 第 8 节相同的三个允许文件；不修改 R1 任务书、PM 台账、配置、依赖、公共开关、Legacy、Runtime、Gateway、review/record/export 或其他文件。

### 11.2 四项真实行为 RED

生产代码仍为 `d3eb7b9` 行为、工作区只修改专项测试时，在全部 loopback fake 环境下运行：

```powershell
uv run pytest tests/test_l5_1_sandbox_safety_adapter.py -q -rs
```

结果为退出码 `1`，`10 passed, 4 failed in 5.54s`。四个新测试均收集并按合同原因失败：

1. `test_l5_1_pairwise_state_drift_cannot_change_identical_request_result`：同一 adapter / exact subject / bundle / command / run / trace 的 calls 1～2 返回 `block`、3～4 返回 `allow`，两次 canonical result 不同；
2. `test_l5_1_arbitrary_stateful_callable_is_not_a_deterministic_authority`：旧 constructor 接受任意 stateful callable，没有拒绝；
3. `test_l5_1_inline_fixture_manifest_is_complete_strict_and_digest_bound`：完整 manifest / scan DTO 不存在；
4. `test_l5_1_thousand_true_maximum_results_are_resource_bounded`：digest-bound declarative authority case 不存在，因而没有 committed true-max authority/resource 覆盖。

RED 时 HEAD 为 `53cbb9c...`，生产模块未修改，没有 skip、xfail、条件跳过、增加黑盒采样次数或先写生产代码。

### 11.3 R1 单一确定性 authority

R1 删除 `SandboxSafetyEvaluator` Protocol、`_evaluator` slot、任意 callable constructor 参数、两次调用和输出相等采样。`SandboxSafetyRuleAdapter()` 不持有 evaluator、cache、clock、random 或进程状态；传入任意 callable 会在进入 adapter 前得到 Python `TypeError`，call count 保持 `0`。

唯一裁决 authority 现在是以下 immutable canonical 链：

```text
SandboxRuleBundleV1
  └─ SandboxEvaluatorAuthorityV1(authority_digest)
       └─ tuple[SandboxEvaluationCaseV1]
            ├─ exact formula/profile/dataset digests
            └─ SandboxSafetyEvaluationV1(decision + canonical issues)
```

- authority/case/evaluation/issue/rule/parameter 全部 frozen、strict、`extra="forbid"`，嵌套集合为 tuple，case ID、subject binding、issue ID 和 execution order 唯一且固定排序；
- `authority_digest` 覆盖全部 case input binding 和 decision/issues 输出映射；`rule_bundle_digest` 再覆盖全部规则/参数与完整 authority；subject 显式绑定两者，`decision_subject_digest` 最终覆盖 subject；
- adapter 只按 formula/profile/dataset 三个 exact digest 机械选择恰好一个 case，随后复制该唯一 authority 的 decision/issues；没有第二套裁决逻辑；
- rule reference、版本、authority/bundle/subject digest、唯一 binding 和大小均在输出前验证；无 case 或多个 case 固定 fail closed；
- calls 1～2 / 3～4 pair-wise drift 对手无法注入；同一 adapter、多次请求、新 adapter 和不同 `PYTHONHASHSEED` 均逐字节稳定；
- 没有用 3 次/N 次/随机采样、时间、无界 cache 或第二套 authority 关闭 P1。

### 11.4 完整 admitted synthetic manifest

subject 现内嵌 frozen/strict `SandboxSyntheticManifestV1` 和 `SandboxIdentifierScanV1`。所有字段均为显式 required，不能因省略而由默认值补齐：

- schema、dataset name/version、`personal_learning_synthetic_only`、`constructed_fixture`；
- 同时保留原任务固定 `fixed_fictitious_manual`、`sandbox_only`、`not_clinically_adjudicated`；
- 固定 source statement 明确不来自真实病历、个人记录、生产日志、聊天记录或外部数据集；
- generator path/version/digest/seed 全为 `not_applicable`，另有人工构造 fixed-fictitious 技术 fixture 证据；
- `case_count=1`，`content_sha256` 现场重算完整 canonical fixture case；
- 固定非临床 `created_at` 和 `sandbox_fixture_author` 测试角色；
- scan 绑定 tool/version/ruleset/time 和 `passed_no_prohibited_identifiers`，不保存任何命中原值；
- `synthetic_manifest_digest` 覆盖 manifest，`synthetic_dataset_digest` 同时覆盖 manifest 和 canonical fixture content，并进入 authority case 与 subject digest 链。

专项对 missing、extra、未知 provenance、case count、scan 非通过、content digest 和 manifest digest 篡改均固定拒绝；adapter 没有 callable、解释或下游 port，因此这些失败路径在 authority case 选择和任何副作用前结束。

### 11.5 R1 GREEN 与最大资源证据

全部命令继续使用第 7 节 fake 环境；除校准全量只移除 `APP_ENV` 外没有差异。

| 门禁 | R1 结果 |
|---|---|
| R1/L5-1 专项 | 最终复跑 `14 passed in 13.25s` |
| true-max 独立输出 | `1 passed in 9.23s`；指标如下 |
| Safety 回归 | 最终复跑 `71 passed, 3 deselected in 1.73s` |
| L4.5-11 两项 privacy 回归 | 最终复跑 `76 passed in 4.42s` |
| Ruff | `All checks passed!` |
| mypy | `Success: no issues found in 1 source file`；保留既有 `pymilvus.*` unused-section note |
| L0 | 最终复跑 `131 passed in 2.04s` |
| `uv lock --check` / `git diff --check` | lock `Resolved 84 packages in 4ms`；diff check 无错误 |
| 强制 `APP_ENV=sandbox-test` 全量 | `1 failed, 1638 passed, 362 deselected in 128.12s`；唯一为第 5.1 节既有 defaults 冲突 |
| 只移除 `APP_ENV`、保留全部 fake endpoint 的校准全量 | `1639 passed, 362 deselected in 127.83s` |

最终 true-max committed test 使用：

- Python `3.12.12`；CPU `Intel64 Family 6 Model 142 Stepping 12, GenuineIntel`；Windows/AMD64；
- 同一合法 fixture 同时为 `64` formula items 和 `256` unique issues；subject `10,345` bytes、bundle `28,297` bytes、result `27,676` bytes，均低于 `256 KiB`；
- 预热 `20` 次，正式 `1,000` 次；每次 `canonical_result_bytes` 与预期逐字节相同；
- `perf_counter_ns` 单次计时，排序索引 `949`/`989` 为 p95/p99；Windows process working-set RSS 在循环前后 `gc.collect()` 后比较；
- p95 `7.658 ms`、p99 `10.925 ms`、RSS 增长 `241,664 bytes`，分别满足 `<50 ms`、`<100 ms`、`<64 MiB`，未放宽门槛。

开发过程中首次 mypy 仅发现两个 Literal schema default/argument 注解不精确；在允许的生产文件中改为 required literal 与字面量 builder 后最终 mypy 通过。manifest required-field 收紧后的专项仍为 `14 passed`。失败历史保留。

### 11.6 边界、限制与提交约定

- 没有读取/显示本地 `.env`，没有读取 ignored `data/`/`.codex_tmp`，没有启动应用、网络、HTTP/E2E、容器、数据库、Redis、Milvus、RAG、模型/embedding Gateway 或外部服务；fixture 只含 inline fixed-fictitious 技术值。
- R1 仍不实现 persistence、graph node、L5-2 explanation、L5-3 challenge/review、L5-4 修改后重检、真实 record/export 或临床/生产语义；真实临床、患者服务、商业/公开生产和人体研究继续 NO-GO。
- 强制 `APP_ENV=sandbox-test` 与既有 `test_load_with_defaults` 的冲突按 R1 合同原样保留，不修改合同外 test/config 制造通过；校准全量单独记录，不能替代精确失败命令。
- 使用单一 R1 开发提交，消息为 `fix: make L5-1 sandbox authority structurally deterministic`；exact parent 必须为 `53cbb9cad9bbd4630f4259409708966df4369e4d`。
- Git SHA 不能在包含本文的同一提交中自引用；冻结后通过 `git rev-parse HEAD`、提交消息和本文约定报告 exact delivery commit，由独立 Reviewer/CI/PM 在后续验收章节锚定。
- 若 R1 独立验收失败，以单一 R1 提交执行 `git revert <r1-delivery-commit>`，保留全部历史；不得 reset 或覆盖。

---

**R1 已交付，申请验收。**

## 12. 项目经理 R1 最终验收（2026-07-22）

- 冻结交付：`461487e03d6529dfacbc7f3f1ff1fe919e8633d5`；exact parent/merge-base `53cbb9cad9bbd4630f4259409708966df4369e4d`；只修改本 handoff 第 8 节三个文件；Review/CI/PM 前后 clean。
- 独立 Reviewer：P0=0、P1=0、P2=0、P3=0。原 pair-wise determinism、manifest、true-max 三项 findings 全部关闭；authority/digest/stale/no-case/size/manifest fail-closed 和 zero-call 边界独立复现。
- 独立 CI：专项 `14 passed`，Safety `71 passed, 3 deselected`，L4.5-11 privacy `76 passed`，Ruff/mypy/L0/lock/diff/scope/tracked/clean 通过；校准全量 `1639 passed, 362 deselected`。
- PM 定向探针：同一 exact HEAD 六项 `6 passed`；true-max 为 64 items + 256 issues，p95 `7.042 ms`、p99 `8.794 ms`、RSS `+790,528 B`。
- 强制 `APP_ENV=sandbox-test` 的精确全量唯一 defaults 冲突原样保留；只移除 `APP_ENV`、保持全部 fake endpoints 的校准全量证明代码无回归，不修改合同外配置/测试。
- 结论：**L5-1 与 L5-1-R1 accepted**；关闭 `R-L5-DET-001`、`R-L5-EVID-001`；允许项目经理另行发布 L5-2，当前事务不实施 L5-2。
- 边界：这是个人学习、非临床、离线 synthetic sandbox 工程验收，不是临床、法律、隐私、伦理、监管或生产批准；G1～G6、EXT-001/EXT-002 和真实/公开用途 NO-GO 不变。

## 13. L5 最终组合第 5 轮 PM 关闭（2026-07-23）

- 最终组合冻结点为 `c052c5014d98e508fbfe861316ff5428574c197b`；全新独立 Reviewer P0/P1/P2/P3 全为 0，独立 CI 与根 PM 通过，工作区前后 clean。
- 本专项在 final R5 为 `14 passed`，四层组合 `176 passed`；同一 subject/bundle 同裁决、run envelope 不干扰、pair-wise state drift 和零外部 capability 均保持。
- `ACC-20260723-047` / `DEC-20260723-040` 确认 L5-1 继续 accepted，并将 L5 个人学习离线工程沙盒标记 accepted / engineering complete。
- 本结论只适用于 fixed-synthetic、offline unit/in-memory；不是 `clinical_approved`，不授权 Runtime、HTTP/E2E、容器、数据库、部署、真实临床/患者/公开生产。L6 未发布、未开始。
