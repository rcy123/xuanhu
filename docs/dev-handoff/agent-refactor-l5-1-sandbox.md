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
