# L4.5-11-2 Intake Runtime 发送前最终隐私门禁任务

## 1. 发布信息

| 项目 | 内容 |
|---|---|
| 任务编号 | `L4.5-11-2` |
| 发布日期 | 2026-07-22 |
| 发布人 | Codex（工程项目经理） |
| 状态 | **已发布 / 待交付** |
| 发布输入 exact HEAD | `2f008b9ae1bf231abe4fd3fdea965ec665922eff` |
| 已接受生产基线 | `140262d944e16cc6043124d7537794bb46cd7960`（L4.5-11-1） |
| 方案基线 | `22feb390425c3b4fc4349980566a5c80314e60d0` 的 v2.2 方案 |
| 前置验收 | `ACC-20260722-008`：L4.5-11-1 accepted |
| 关联阻塞 | `AR-B-031`、`R-GATE-001` |
| 交付载体 | `docs/dev-handoff/agent-refactor-l4-5-11-2.md`（执行者新增） |

执行者必须从“包含本任务合同的 clean exact HEAD”开始，并在 handoff 记录该执行起点。若实际 Runtime/Gateway 调用链、accepted scanner 或允许文件已经变化，立即按停止条件报告，不得静默套用合同。

## 2. 权威关系与单一结果

- [当前状态](../01_agent部分优化/项目管理/00-当前状态.md) 是当前动作的唯一事实源；
- [任务台账](../01_agent部分优化/项目管理/01-任务台账.md) 是任务状态的唯一事实源；
- [v2.2 方案](../01_agent部分优化/L4.5-11模型输入隐私收敛方案-v2-2026-07-21.md) §6、§7.1 定义 Runtime/Gateway 责任边界；
- [L4.5-11-1 handoff](agent-refactor-l4-5-11-1.md) §13 证明 scanner/projector 已独立验收；本任务只能复用，不得修改。

本任务只有一个可验收结果：

> 当且仅当 `agent_spec.name == INTAKE_AGENT_NAME` 时，`AgentRuntime._call_gateway()` 必须在选择或调用任何 Gateway 方法之前扫描最终有序 messages 的字符串 `content`；命中已验收有限身份 grammar，或扫描输入/内部处理失败时，以固定脱敏 `MODEL_INPUT_PRIVACY_VIOLATION` fail closed，Gateway 方法调用数和实际请求数均为 0；安全 Intake 与所有非 Intake Agent 保持现有行为。

本任务通过也不单独关闭 `AR-B-031`。只有两个实现任务、组合验收、最终独立复审和 clean exact-HEAD 记录全部通过后，项目经理才能关闭该阻塞。

## 3. 已核对的真实调用链与先红

### 3.1 当前代码事实

在发布输入 `2f008b9` 上：

- `AgentRuntime.run()` 先验证输入、计算不可逆 `input_digest`，再进入 attempt；recorder 只接收 digest、计数、固定错误码和运行元数据，不记录 messages 或 input 原值；
- `_one_attempt()` 通过 `asyncio.wait_for()` 调用 `_call_gateway()`；`RuntimeErrorBase` 不被转换成 Gateway 错误，会回到 `run()` 的固定 failed recorder 路径；
- `_call_gateway()` 在选择 `chat_structured_observed` / `chat_structured` 前同时持有 `agent_spec.name` 和完整最终 `messages`；
- 对支持参数的真实 Gateway 方法传 `max_requests=1`，所以目标路径最多一次 HTTP 请求，且 Gateway 的 JSON fallback 不扩张该预算；
- 当前 `RuntimeErrorCode` 不含 `MODEL_INPUT_PRIVACY_VIOLATION`，`_call_gateway()` 不执行隐私扫描；
- `execute_intake_extraction()` 已捕获 `RuntimeErrorBase` 并只返回固定 `failure_code`；本任务不得修改该边界。

真实责任边界：Runtime 决定“此 Agent 是否允许发送这些最终 messages”；Gateway 只执行既有传输，不拥有 Intake 身份或 Runtime 失败码。不得把本门禁下沉到 `app/core/gateway.py`。

### 3.2 已复现先红

项目经理在 `2f008b9` 直接向 `AgentRuntime.run()` 传入未经过入口投影的固定虚构 Intake messages，当前结果为：

```text
current_intake_result=ok
current_gateway_method_calls=1
current_actual_request_count=1
privacy_error_code_present=False
```

执行者必须先在唯一专项测试中把该绕过入口的 Runtime 缺口写成失败测试，保存测试名、退出码和失败原因；生产修改前不得引用不存在的 enum 成员造成 collection error 来代替行为先红。至少证明“不安全 Intake 没有被拒绝、Gateway 被调用 1 次”。

## 4. 目标

1. 在 `RuntimeErrorCode` 新增且仅新增 `MODEL_INPUT_PRIVACY_VIOLATION = "MODEL_INPUT_PRIVACY_VIOLATION"`；
2. 在 `_call_gateway()` 的任何 Gateway 方法选择/调用之前，以 `agent_spec.name == INTAKE_AGENT_NAME` 精确启用门禁；
3. 只按最终 `messages` 的既有顺序读取每个 message 的字符串 `content`，将其作为一个有序 sequence 传给 L4.5-11-1 已验收 scanner；
4. scanner 命中、message 缺少字符串 `content`、tokenization/matcher 内部失败都以同一固定 `RuntimeErrorBase` fail closed；
5. 被拒绝请求不得调用 `chat_structured_observed`、`chat_structured` 或产生任何实际 Gateway 请求；
6. 安全 Intake 仍只调用一次 Gateway，并继续传 `max_requests=1`；
7. 非 Intake Agent 即使包含相同固定虚构序列也完全保持现有 Runtime 行为；
8. 异常、日志和 recorder 不得包含 message 原值、命中片段、坐标或 scanner 原异常。

## 5. 非目标

- 不修改 L4.5-11-1 scanner/projector、grammar、字符类、坐标模型或入口投影；
- 不修改 Gateway、HTTP payload 构造、retry、fallback 或公共 Gateway API；
- 不扫描 input DTO、schema、tools、trace metadata、role 或 message dict 的序列化形式；只扫描有序字符串 `content`；
- 不扩大到其他 Runtime Agent、Legacy/BaseAgent、直接 Gateway 调用或跨请求拼接；
- 不覆盖自由文本姓名、15 位身份证、其他证件、任意编码或任意 Unicode 同形字；
- 不修改 recorder、数据库、审计 schema、持久化、Domain、verifier、RAG、前端、临床规则、公共开关或 L5～L9；
- 不声称完整隐私、法律、临床、伦理或机构合规。

## 6. 允许修改范围

执行者只允许修改或新增：

1. `app/agent_runtime/runtime.py`
2. `app/agent_runtime/specs.py`
3. `tests/test_l4_5_11_2_runtime_privacy_guard.py`（新增，唯一专项测试所有者）
4. `docs/dev-handoff/agent-refactor-l4-5-11-2.md`（新增，交付与验收载体）

若某个允许生产文件实际无需修改，必须在 handoff 说明。项目管理台账由项目经理维护，执行者不得修改。

## 7. 禁止修改范围

除第 6 节外全部禁止，特别包括：

- `app/agent_runtime/context.py`、`app/agents/intake_extraction.py`；
- `app/agent_runtime/intake_verifier.py`、其他 verifier/schema；
- `app/core/gateway.py`、`app/core/exceptions.py`；
- `app/services/**`、其他 `app/agents/**`、Legacy、RAG、前端、迁移；
- `tests/test_l4_5_11_1_intake_privacy_projection.py` 和所有既有测试；
- `pyproject.toml`、`uv.lock`、依赖、配置、CI、部署和环境文件；
- `docs/01_agent部分优化/项目管理/**`、本任务书、v2.2 方案和历史证据。

需要修改禁区才能完成时立即停止，不得顺手扩张。

## 8. Runtime 门禁强制合同

### 8.1 Intake-only 启用条件

- 必须复用现有 `INTAKE_AGENT_NAME`，不得复制字符串作为第二事实源；
- 只在名称精确相等时启用；不得用前缀、后缀、stage、schema 类型或 prompt 名推断；
- 非 Intake 分支不得读取、验证或转换 message content，不得引入新的异常或延迟。

### 8.2 扫描输入

- 对 Intake final messages 保持现有列表顺序；
- 每个元素必须是 Mapping-like message 且 `content` 必须为 `str`；缺失、非字符串或提取异常按同一隐私失败码拒绝；
- 只把 content 字符串组成 sequence 交给 `contains_model_input_identity_sequence()`；不得拼 JSON、role、tools、schema 或 metadata；
- 相邻 final messages 由 scanner 现有 `B` 语义覆盖，因此单 message、跨 message、全角和精确分隔 grammar 均必须拒绝；
- 不得在 Runtime 重新实现、复制或分叉 matcher。

### 8.3 固定失败

命中或扫描失败时抛出：

```python
RuntimeErrorBase(
    RuntimeErrorCode.MODEL_INPUT_PRIVACY_VIOLATION,
    "model input privacy guard rejected request",
)
```

- 消息必须固定，不包含原文、命中类型、片段、坐标或内部异常；
- 必须以不会显示原异常链的方式抛出，`__cause__`、`__context__` 为 `None`；
- 不记录、打印或附加 message 原值；
- 该错误不可重试；即使 AgentSpec `max_attempts > 1`，被拒绝请求 Gateway 调用仍为 0；
- 若 recorder 存在，允许既有 `started` 和 `failed` 元数据事件；事件只能包含既有 digest/计数/固定错误码，不得含 messages 或原值。

### 8.4 Gateway 零调用

门禁必须位于获取/选择可调用 Gateway 方法并执行 `method(...)` 之前。测试 double 必须分别计数：

- `chat_structured_observed` 调用数；
- `chat_structured` 调用数；
- 代表实际请求预算的 request count。

拒绝时三者均为 0。安全 Intake 仍为一次调用、一次请求并接收 `max_requests=1`。

## 9. 专项测试所有权与矩阵

所有新增测试只位于 `tests/test_l4_5_11_2_runtime_privacy_guard.py`，至少覆盖：

1. 绕过入口直接传入连续手机号和身份证号，固定错误码，Gateway 两种方法与实际请求均为 0；
2. ASCII/全角 digit/X、三种精确分隔手机号、单 message 与跨 final message 命中；
3. scanner 内部异常、缺少 content、非字符串 content 均 fail closed；
4. 固定异常消息、无 cause/context、`caplog` 不含固定虚构原值；
5. recorder `started`/`failed` 事件不含原文，只含既有 digest/元数据和固定错误码；
6. `max_attempts > 1` 和 retryable policy 不得导致隐私拒绝重试；
7. 安全 Intake 正常成功，Gateway 调用 1 次、实际请求 1 次、`max_requests=1`；
8. 非 Intake Agent 使用同一不安全 messages 时不触发本门禁，保持现有调用和结果；
9. observed Gateway 与普通 Gateway 两个选择分支都证明拒绝发生在调用前；
10. L4.5-11-1 accepted scanner 专项保持全绿，证明未修改 scanner/grammar。

测试只使用固定虚构值，不得调用真实患者数据、真实 Gateway 或真实数据库。

## 10. 先红后绿要求

1. 先新增唯一专项测试，至少让“不安全 Intake Runtime 绕过入口”和“跨 final message 重组”在当前生产代码上失败；
2. 记录失败测试名、退出码、Gateway 当前调用 1 次和缺少固定错误码；
3. 再修改 `specs.py` 与 `runtime.py`；
4. 先跑专项转绿，再跑 accepted scanner 专项和相关 Runtime Agent 回归；
5. 最终代码上重跑静态、L0、全量非集成、diff 和范围门禁；
6. 创建单一开发交付提交，工作区 clean 后申请验收。

若生产修改前无法复现 Gateway 调用 1 次或专项意外全绿，立即停止并报告基线差异。

## 11. 验收命令与基线

### 11.1 专项与纵深防御回归

```powershell
uv run pytest --override-ini addopts= -q -m "not integration" tests/test_l4_5_11_2_runtime_privacy_guard.py

uv run pytest --override-ini addopts= -q -m "not integration" `
  tests/test_l4_5_11_1_intake_privacy_projection.py `
  tests/test_l2_2_agent_runtime.py `
  tests/test_l3_1_intake_extraction.py `
  tests/test_l3_4_gap_question.py `
  tests/test_l3_5_intake_subgraph.py `
  tests/test_l4_1_syndrome_draft.py `
  tests/test_l4_2_formula_draft.py
```

发布输入上，L4.5-11-1 专项为 `53 passed`；六个 Runtime/Agent 相关文件合计 `214 passed, 22 deselected`。交付不得减少既有通过数；新增专项按实际数量记录。

### 11.2 静态、L0 与全量非集成

```powershell
uv run ruff check `
  app/agent_runtime/runtime.py `
  app/agent_runtime/specs.py `
  tests/test_l4_5_11_2_runtime_privacy_guard.py

uv run mypy app/agent_runtime/runtime.py app/agent_runtime/specs.py
uv run pytest --override-ini addopts= -q -m "not integration" tests/test_l0_1_contract.py
uv run pytest --override-ini addopts= -q -m "not integration" tests
git diff --check
git status --short --branch
```

发布输入的 L0 为 `131 passed`；最近同一代码基线的独立 CI 全量非集成为 `1602 passed, 362 deselected`。新增专项后通过数应按实际增加，既有通过数不得减少。不得为本任务调用真实外部 integration。

## 12. 交付记录要求

新建 `docs/dev-handoff/agent-refactor-l4-5-11-2.md`，至少记录：

1. 包含本合同的 clean exact HEAD、分支和开始状态；
2. 行为先红的测试名、退出码、Gateway 调用 1 次和原因；
3. 实际 import 的 accepted scanner 与 `INTAKE_AGENT_NAME` 单一来源；
4. 门禁在 `_call_gateway()` 中相对 Gateway 方法选择/调用的准确位置；
5. 固定错误、零调用、不可重试、recorder/log 不含原值证据；
6. 安全 Intake 与非 Intake 不变量；
7. 修改文件与禁区核对；
8. 专项、L4.5-11-1 专项、相关回归、Ruff、mypy、L0、全量、diff 结果；
9. tracked 文件、提交消息和 clean worktree；
10. 残余风险、明确非目标与停止条件触发情况；
11. 明确写“已交付，申请验收”，不得自称 accepted 或关闭 AR-B-031。

提交 SHA 无法在同一提交内自引用；执行者须在冻结提交后通过 handoff 约定、交付消息和 `git rev-parse HEAD` 报告 exact commit，项目经理在验收章节锚定。

## 13. 停止条件

出现任一情况立即停止：

- 执行起点不是包含本合同的 clean exact HEAD，或真实调用链/accepted scanner 已变化；
- 需要修改 `context.py`、Intake 入口、Gateway、recorder、既有测试或其他禁区；
- 生产修改前不能复现 unsafe Intake Gateway 调用 1 次；
- 被拒绝请求任一 Gateway 方法或 request count 非 0；
- 安全 Intake 不再传 `max_requests=1`，或目标路径会进入额外 retry/fallback；
- 非 Intake Agent 行为变化；
- scanner/guard 异常、日志或 recorder 泄露原始 message；
- 需要扩大 grammar、扫描 input DTO/metadata，或覆盖 Legacy/其他 Agent；
- 相关/全量回归失败且不能在允许范围内确定性修复；
- 工作区含无法归属改动，或正式测试/handoff 未被 Git 跟踪；
- 发现新的范围外 P0/P1。

## 14. 回退方式

- 完整回退本任务单一开发提交，移除 Runtime Intake 条件门禁、错误码、专项测试和 handoff；
- 不回退已独立验收的 L4.5-11-1 入口投影和 scanner；
- 不通过修改 Gateway、Legacy、原始消息、verifier 或 Domain 来补偿；
- 回退后 `AR-B-031` 保持 P1 打开，重新进入项目经理裁决。

## 15. 项目经理验收标准

项目经理只在以下全部满足时接受：

- exact delivery commit 的父提交是包含本合同的独立 clean 发布提交；
- diff 只含 4 个允许文件，正式测试和 handoff 全部 tracked；
- 行为先红真实，且失败原因是 unsafe Intake 调用 Gateway 1 次；
- 所有支持集合命中和 scanner/输入失败均固定拒绝，Gateway 两种方法与实际请求为 0；
- 安全 Intake 一次请求、`max_requests=1`；非 Intake 行为不变；
- 固定失败没有 cause/context/log/recorder 原值；
- L4.5-11-1 专项、任务 2 专项、相关回归、Ruff、mypy、L0、全量和 diff 全部通过；
- 未参与实现的 Reviewer no findings，独立 CI 在同一 exact commit 通过；
- 项目经理黑盒探针独立复现入口绕过、零调用、固定错误和作用域不变量。

验收通过后仍需 L4.5-11 组合关闭验收与最终独立复审，才能评估关闭 `AR-B-031`。

## 16. 发布后的唯一下一动作

开发测试执行者从包含本合同的 clean exact HEAD 领取 `L4.5-11-2`，按第 10 节先红后绿实施并提交 handoff。其他任务保持冻结。
