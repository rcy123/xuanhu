# L4.5-11-2 Intake Runtime 发送前隐私门禁交付

## 1. 执行起点与所有权

- 分支：`codex/l4-5-11-context-privacy-hardening`。
- 包含本任务合同的执行起点 clean exact HEAD：`2da64ae64ddbd22cd9631e72ea37eaf9f257b36e`。
- 原实施 writer 从上述 clean HEAD 开始，只创建了本任务唯一专项测试草稿；writer 切换时，项目经理核对工作区仅有未跟踪的 `tests/test_l4_5_11_2_runtime_privacy_guard.py`（425 行），中断原 writer，并将该同任务草稿的所有权明确转移给当前 writer。转移时两个允许的生产文件仍与 `2da64ae` 完全一致，无用户或无关改动。
- 当前 writer 接管、审查并修正该测试草稿，先在未修改生产代码的 `2da64ae` 行为上完成真实行为 RED，再修改生产代码。
- 实施期间只有当前 writer 写入本任务文件；未出现并发 writer。

## 2. 真实行为先红

生产代码仍为 `2da64ae` 时运行：

```powershell
uv run pytest --override-ini addopts= -q -m "not integration" tests/test_l4_5_11_2_runtime_privacy_guard.py
```

结果：退出码 `1`，`16 failed, 3 passed in 2.46s`。这是 Runtime 真实行为失败，不是 collection、import 或不存在 enum 成员导致的替代失败。

关键证据：

- `test_unsafe_intake_runtime_bypass_is_rejected_before_observed_gateway`：实际为 `error=None`、`observed_calls=1`、`plain_calls=0`、`actual_request_count=1`、privacy enum 不存在；unsafe Intake 未被拒绝并真实进入一次请求预算。
- `test_cross_final_message_identity_is_rejected_before_plain_gateway`：实际为 `error=None`、`observed_calls=0`、`plain_calls=1`、`actual_request_count=1`、privacy enum 不存在；跨 final messages 重组同样未被拒绝。
- 其余 RED 同时证明支持 grammar、输入/scanner 失败、recorder、不可重试和固定 error code 合同尚未实现；3 个既有行为测试通过。

## 3. 实现

### 3.1 唯一来源

`app/agent_runtime/runtime.py` 直接复用并实际 import：

- `.context.contains_model_input_identity_sequence`：L4.5-11-1 已 accepted 的唯一 scanner/matcher 来源；
- `.intake_verifier.INTAKE_AGENT_NAME`：Intake Agent 名称的唯一来源。

Runtime 未复制 Intake 名称、grammar、字符类、tokenizer 或 matcher，也未修改 `context.py`、`intake_verifier.py` 或入口 projector。

### 3.2 Runtime 门禁位置与输入

门禁是 `AgentRuntime._call_gateway()` 的第一段业务逻辑，位于 kwargs 构造、`getattr(self.gateway, "chat_structured_observed", None)`、普通 Gateway method 选择、签名检查和 method 调用之前。

仅当 `agent_spec.name == INTAKE_AGENT_NAME` 时：

1. 按 final `messages` 的原有顺序遍历；
2. 每个 message 必须为 `Mapping`，且 `message["content"]` 必须为 `str`；
3. 只将有序 content 字符串组成 `tuple[str, ...]` 传给 accepted scanner；
4. 不扫描或拼接 role、tools、schema、metadata、input DTO 或 message 序列化结果。

非 Intake 分支不执行新增的 message 读取、验证、转换或 scanner 调用，直接保持既有 Gateway 选择和调用路径。

### 3.3 固定、脱敏、不可重试失败

`app/agent_runtime/specs.py` 只新增一个 error code：

```text
MODEL_INPUT_PRIVACY_VIOLATION
```

scanner 命中、message 非 Mapping、content 缺失/非字符串/提取异常，或 scanner/tokenizer/matcher 内部异常，都统一在离开内部 `except` 后抛出：

```python
RuntimeErrorBase(
    RuntimeErrorCode.MODEL_INPUT_PRIVACY_VIOLATION,
    "model input privacy guard rejected request",
)
```

抛出使用 `from None`，且不在活动的内部异常处理块内，因此 `__cause__`、`__context__` 均为 `None`。异常不包含 message 原值、命中类型、片段、坐标或 scanner 原异常；实现没有新增日志、打印、recorder 字段、retry 或 fallback。

该 `RuntimeErrorBase` 从 `_call_gateway()` 直接穿过 `_one_attempt()` 回到 `run()` 的既有固定 failed-recorder 路径，不被转换成 retryable Gateway failure；即使 `max_attempts=3` 且 policy 将所有 enum code 配为 retryable，scanner 仍只调用一次，两个 Gateway method 与 request count 均为 0。

## 4. 专项合同证据

最终专项为 `19 passed`，覆盖：

- unsafe Intake 连续手机号和身份证号：observed/plain/request 均为 0；
- ASCII/全角 digit 与 `X`、空格/连字符/点三种精确手机号分隔、单 message 和跨 final message 命中；
- scanner 内部异常、缺少 content、非字符串 content 和 content 提取异常均 fail closed；
- 固定异常消息、非 retryable、无 cause/context，`caplog` 无固定虚构原值或内部异常文本；
- recorder 仅产生既有 `started`/`failed` 事件，含 digest/计数/固定 error code 等元数据，不含 input、messages 或固定虚构原值；
- `max_attempts > 1` 和允许所有 code 的 retry policy 下 scanner 仅调用一次，Gateway/request 仍为 0；
- 安全 Intake 成功，observed Gateway 调用一次、request count 为 1、`max_requests_seen == [1]`；
- 非 Intake 使用相同 unsafe messages 时保持既有成功行为；直接 `_call_gateway()` 的非 Intake 探针还证明新增分支不读取会抛异常的 content；
- observed Gateway 和普通 Gateway 两个 method 选择分支均在选择/调用之前拒绝；
- `RuntimeErrorCode` 集合只增加目标 privacy code。

所有专项数据均为固定虚构值；未调用真实患者数据、真实 Gateway 或真实数据库。

## 5. 修改范围

本交付只包含合同允许的 4 个文件：

1. `app/agent_runtime/runtime.py`：Intake-only pre-Gateway guard；
2. `app/agent_runtime/specs.py`：固定 error code；
3. `tests/test_l4_5_11_2_runtime_privacy_guard.py`：新增唯一专项测试；
4. `docs/dev-handoff/agent-refactor-l4-5-11-2.md`：本交付与验收载体。

两个允许的生产文件均需修改且只包含上述目标变更。未修改 `context.py`、`intake_extraction.py`、Gateway、recorder、既有测试、PM 台账、配置、依赖或其他禁区。

## 6. 最终门禁

所有结果均在最终生产代码与专项测试上运行：

| 门禁 | 结果 |
|---|---|
| L4.5-11-2 唯一专项 | `19 passed in 1.84s` |
| L4.5-11-1 accepted scanner 专项 | `53 passed in 1.93s` |
| 六文件 Runtime/Agent 相关回归 | `214 passed, 22 deselected in 3.98s` |
| Ruff（2 个生产文件 + 新专项） | `All checks passed!` |
| mypy（2 个生产文件） | `Success: no issues found in 2 source files` |
| L0 文档契约 | `131 passed in 2.18s` |
| 全量非 integration | `1621 passed, 362 deselected in 110.67s` |
| `git diff --check` / staged diff check | 通过，无输出 |
| scope / tracked / clean | 4 个允许文件；提交前全部 tracked；提交后 clean |

Ruff 首次运行只发现新测试 import block 后多一个空行；按 Ruff diff 机械修正后，Ruff 与所有最终门禁均如上通过。未运行真实外部 integration。

## 7. 不变量、残余风险与非目标

- 被拒绝请求在两种 Gateway 分支的 method call 和实际 request count 均为 0；安全 Intake 仍传 `max_requests=1`，没有额外 retry/fallback；非 Intake 行为不变。
- recorder 继续只持有不可逆 digest、计数、固定 error code 和既有运行元数据；实现未新增 raw log 或 recorder 原值。
- accepted scanner/projector、有限 grammar、字符类、坐标模型、入口投影、Gateway、HTTP payload、公共 API 和 retry/fallback 语义均未修改。
- 明确不覆盖自由文本姓名、15 位身份证、其他证件、任意编码、任意 Unicode 同形字、跨请求拼接、Legacy/其他 Agent 或完整隐私/法律/临床/伦理/机构合规。
- 本开发交付不自称 accepted，不执行组合关闭验收，也不接受或关闭 `AR-B-031`；该阻塞保持打开，等待独立 Review、CI、项目经理黑盒探针和后续组合验收。
- clean-start 唯一表面异常为 writer 切换时遗留的同任务未跟踪测试草稿；项目经理已核对来源、范围、唯一性并明确转移所有权，因此没有未归属改动。其余停止条件均未触发。

## 8. 交付提交约定

- 使用单一开发交付提交，提交消息：`feat: add L4.5-11-2 runtime privacy guard`。
- exact commit 在提交冻结后通过 `git rev-parse HEAD` 和交付消息报告；Git SHA 取决于包含本文的最终 tree，不能在同一个提交内自引用尚未生成的 SHA。
- exact parent 必须为 `2da64ae64ddbd22cd9631e72ea37eaf9f257b36e`；提交后工作区必须 clean。

---

**已交付，申请验收。**
