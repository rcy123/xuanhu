# L4.5-11-0 开发交付与验收申请

> 任务编号：L4.5-11-0
> 合同版本：v2.0
> 发布日期：2026-07-21
> 执行起点：`a1b80effdce481452f1c0f65c9c1f3dc8d4246b3`
> 分支：`codex/l4-5-11-context-privacy-hardening`
> 事实基线：`a0790c6`
> 生产代码基线：`b97c9f9`
> 状态：**已交付，申请验收**

---

## 1. 执行起点

| 项目 | 值 |
|---|---|
| 执行起点 exact HEAD | `a1b80effdce481452f1c0f65c9c1f3dc8d4246b3` |
| 分支 | `codex/l4-5-11-context-privacy-hardening` |
| 开始工作树状态 | 干净（`git status --short` 无输出） |
| 结束工作树状态 | 新增 2 个文档文件，工作区干净 |

---

## 2. 事实核对记录

### 2.1 代码路径与结果

| 检查项 | 命令 | 退出码 | 结果 |
|---|---|---|---|
| HEAD | `git rev-parse HEAD` | 0 | `a1b80effdce481452f1c0f65c9c1f3dc8d4246b3` |
| 分支状态 | `git status --short --branch` | 0 | `## codex/l4-5-11-context-privacy-hardening` |
| context.py 行数 | `uv run python -c "from pathlib import Path; print(len(Path('app/agent_runtime/context.py').read_text(encoding='utf-8').splitlines()))"` | 0 | 210 |
| UserMessageProjection | `git grep -n "UserMessageProjection" -- app tests` | 1（预期） | 不存在 |
| Context Builder 测试 | `uv run pytest --override-ini addopts= --collect-only -q tests/test_l2_3_context_builder.py` | 0 | 9 项 |
| 关键函数位置 | `rg -n "def _build_intake_input|def build_intake_context|def execute_intake_extraction|async def run\(|def _call_gateway|chat_structured" app` | 0 | 见下方 |
| L0 文档契约 | `uv run pytest --override-ini addopts= -q tests/test_l0_1_contract.py` | 0 | 131 passed |
| diff 检查 | `git diff --check` | 0 | 通过 |
| 工作区状态 | `git status --short` | 0 | 干净 |

### 2.2 关键函数代码路径

```
_build_intake_input      -> app\services\langgraph_intake.py:1621
build_intake_context     -> app\agents\intake_extraction.py:91
execute_intake_extraction -> app\agents\intake_extraction.py:124
run                      -> app\agent_runtime\runtime.py:172
_call_gateway            -> app\agent_runtime\runtime.py:356
chat_structured          -> app\core\gateway.py:278
chat_structured_observed -> app\core\gateway.py:308
_chat_structured_impl    -> app\core\gateway.py:338
```

### 2.3 当前调用链验证

```text
ConsultMessage.content
  -> app.services.langgraph_intake._build_intake_input (line 1621)
  -> IntakeExtractionInput.current_messages
  -> app.agents.intake_extraction.build_intake_context (line 91)
  -> ContextPacket.messages（USER 为 current_messages JSON）
  -> app.agents.intake_extraction.execute_intake_extraction (line 124)
  -> AgentRuntime.run (line 172)
  -> AgentRuntime._call_gateway (line 356)
  -> ModelGatewayClient.chat_structured_observed / chat_structured
```

验证方式：通过 `rg` 命令定位函数定义位置，通过 `Read` 工具读取函数体确认调用关系。

---

## 3. 新增/修改文件清单

| 文件 | 操作 | 说明 |
|---|---|---|
| `docs/01_agent部分优化/L4.5-11模型输入隐私收敛方案-v2-2026-07-21.md` | 新增 | v2 方案文档 |
| `docs/dev-handoff/agent-refactor-l4-5-11-0.md` | 新增 | 本交付文档 |

**未修改任何禁区文件。**

---

## 4. 与原 v1 方案比较

| 项目 | v1（B133 回退前） | v2（本方案） | 决策 |
|---|---|---|---|
| `context.py` 规模 | 10,064 行 | 210 行（当前基线） | **保留**当前基线 |
| `UserMessageProjection` | 存在 | 不存在 | **保留**当前基线 |
| 测试数量 | ~180 项 | 9 项（当前基线） | **保留**当前基线 |
| 脱敏点 | 投影边界（层 2） | 入口投影（层 1）+ Gateway 兜底（层 3） | **拒绝**v1 单点方案，采用分层 |
| 临床豁免 | 黑名单（`_CLINICAL_TRUST_CONFLICT_TOKENS`） | 白名单（字段名 + 数值范围 + 单位） | **拒绝**v1 黑名单，采用白名单 |
| Domain State 改存假名 | 是 | **否** | **拒绝**v1，本任务不修改持久化 |
| 文件拆分 | 拆分为 7 个模块 | **否** | **拒绝**v1，当前 210 行按需拆分 |
| EvidenceSpan 策略 | 未明确 | 等长替换（推荐） | **新决策** |
| Gateway 门禁 | 未实现 | 明确设计 | **新决策** |
| 实施授权 | 已回退 | 待验收后发布 | **保留**当前状态 |

---

## 5. v2 方案不变量与测试维度

| 不变量 | 测试维度 | 当前状态 |
|---|---|---|
| I1：USER 层不含裸手机号 | T1-T3：ASCII、NFKC 全角、分隔形式 | ❌ 缺口（当前 USER 为原文 JSON） |
| I2：USER 层不含裸身份证号 | T4-T6：ASCII、NFKC 全角、分隔形式 | ❌ 缺口 |
| I3：临床数字不被误脱敏 | T7-T11：体温、血压、心率、血糖、剂量 | ❌ 缺口（当前无临床白名单） |
| I4：最终 Gateway 零请求 | T12：门禁命中时 Gateway 调用次数为 0 | ❌ 缺失（当前无门禁） |
| I5：失败码固定且脱敏 | T13：失败码不包含原始 PII | ❌ 缺失 |

---

## 6. 候选任务及依赖图

### 6.1 候选任务

| 编号 | 名称 | 结果 | 依赖 | 先红证据 |
|---|---|---|---|---|
| L4.5-11-1 | Intake 入口投影层 | USER 层不含裸 PII | L4.5-11-0 | 手机号在 USER 层未被脱敏 |
| L4.5-11-2 | Gateway 最终隐私门禁 | 命中时零 Gateway 请求 | L4.5-11-1（推荐）或独立 | `_call_gateway()` 直接调用 Gateway |
| L4.5-11-3 | 临床数字白名单 | 临床数字不被误脱敏 | L4.5-11-1 | 体温"38.5℃"被误脱敏 |

### 6.2 依赖图

```
L4.5-11-0（本任务：方案设计）
   │
   ▼
L4.5-11-1（Intake 入口投影层）
   │
   ├──→ L4.5-11-3（临床数字白名单）
   │
   └──→ L4.5-11-2（Gateway 最终门禁）
            │
            ▼
      AR-B-031 关闭评估
```

---

## 7. 风险矩阵

| 风险 | 级别 | Owner | 缓解 | 触发条件 |
|---|---|---|---|---|
| R1：等长替换破坏 EvidenceSpan offset | P0 | 开发 | 严格等长；单元测试 | grounding 测试失败 |
| R2：临床数字被误脱敏 | P1 | 开发/临床 | 白名单三重匹配 | 临床数字测试失败 |
| R3：Gateway 门禁误报 | P1 | 开发 | 极简扫描；仅裸串 | 正常请求被误拦截 |
| R4：性能退化 | P2 | 开发 | O(n) 线性扫描 | 投影耗时 > 10ms/消息 |
| R5：与现有 Agent 冲突 | P2 | 开发 | 仅修改 Intake 路径 | 非 Intake Agent 测试失败 |

---

## 8. 修改范围声明

| 范围 | 是否修改 | 说明 |
|---|---|---|
| 生产代码（`app/**`） | ❌ 否 | 本任务仅设计，不实施 |
| 测试代码（`tests/**`） | ❌ 否 | 本任务仅设计，不实施 |
| 前端代码（`frontend/**`） | ❌ 否 | 不在范围内 |
| 数据库迁移 | ❌ 否 | 不在范围内 |
| 依赖文件 | ❌ 否 | 不在范围内 |
| CI/部署配置 | ❌ 否 | 不在范围内 |
| 项目经理台账 | ❌ 否 | 本任务未修改 |
| 方案文档 | ✅ 是 | 新增 v2 方案文档 |
| Handoff 文档 | ✅ 是 | 新增交付文档 |

---

## 9. 推荐下一任务

**推荐：L4.5-11-1 Intake 入口投影层**

**理由：**
1. 最直接影响当前 USER 层原文缺口（最大隐私风险）
2. 可独立验收，不依赖 Gateway 门禁或白名单
3. 先红后绿边界清晰：当前测试会红（USER 层含原文手机号），实施后变绿
4. 为后续任务奠定基础

**先红边界：**
- 测试用例：在 `current_messages` 中放入手机号 `13800138000`
- 断言：USER 层 JSON 中包含原文 `13800138000`（当前行为，应为红）
- 断言：USER 层 JSON 中包含 `[REDACTED:11]`（目标行为，实施后变绿）

---

## 10. 未决问题与停止条件

### 10.1 未决问题

| 问题 | 状态 | 说明 |
|---|---|---|
| 临床白名单具体范围 | 待决 | 需要临床专家确认体温、血压、心率等字段的完整列表 |
| 等长替换占位符格式 | 已决策 | 采用 `[REDACTED:N]`，N 为原始长度 |
| 其他 Agent 是否复用门禁 | 待决 | 当前任务仅覆盖 Intake；其他 Agent 在后续任务中评估 |
| NFKC 规范化时机 | 已决策 | 投影前对原文做 NFKC，验证时也做 NFKC |

### 10.2 停止条件触发情况

| 条件 | 是否触发 | 证据 |
|---|---|---|
| 执行起点的生产代码、调用链、测试数量与本合同基线不一致 | ❌ 否 | 所有核对命令结果与合同基线一致 |
| 需要修改禁区才能证明设计成立 | ❌ 否 | 未修改任何禁区 |
| 方案要求改变原始消息、Domain State、Legacy、UI、临床规则 | ❌ 否 | 方案明确不修改这些 |
| EvidenceSpan 无法在所选投影方案下确定性映射和校验 | ❌ 否 | 等长替换保持 offset 不变 |
| 威胁模型再次依赖无限 Unicode/转义/跨分片样例追加 | ❌ 否 | 支持集合明确限定 |
| 候选任务超过 4 个、范围重叠 | ❌ 否 | 3 个候选任务，范围互斥 |
| 发现新的 P0/P1，AR-B-031 无法承载 | ❌ 否 | 未发现新 P0/P1 |

---

## 11. 交付声明

本任务状态：**已交付，申请验收**。

交付内容：
1. `docs/01_agent部分优化/L4.5-11模型输入隐私收敛方案-v2-2026-07-21.md`
2. `docs/dev-handoff/agent-refactor-l4-5-11-0.md`

未提前实施任何候选任务。未修改生产代码、测试、依赖、配置或项目经理台账。

---

*交付日期：2026-07-21*
*交付人：开发测试执行者*

---

## 12. 项目经理验收结论（第 1 轮）

> 验收日期：2026-07-21
> 验收角色：Codex（工程项目经理；未参与本轮两份交付文档的编写）
> 交付提交：`5280d3a963d3e6addedd3f22ca279d55876f730c`
> 父基线：`a1b80effdce481452f1c0f65c9c1f3dc8d4246b3`
> 结论：**验收未通过 / 限定返工**

### 12.1 已通过项目

| 检查项 | 项目经理复验结果 |
|---|---|
| 提交关系 | `5280d3a^` 精确等于任务发布提交 `a1b80ef` |
| 交付范围 | 只新增合同允许的 v2 方案和 handoff 两份文档 |
| Git 跟踪 | 两份文档均已进入 `5280d3a` |
| `context.py` 行数 | 210 |
| `UserMessageProjection` | 不存在；`git grep` 退出码 1，符合预期 |
| Context Builder 收集 | 9 项 |
| L0 文档契约 | `131 passed in 2.01s` |
| `git diff --check 5280d3a^` | 通过 |
| 验收开始工作区 | clean |
| 禁区变更 | 无生产代码、测试、依赖、配置或项目经理台账变更 |

上述结果证明交付范围和基础事实核对合格，但不能替代方案内容验收。

### 12.2 阻断验收的问题

#### P1-1：等长替换与 EvidenceSpan 结论不成立

方案第 5.2 节把 `[REDACTED:11]`、`[REDACTED:18]`、`[REDACTED:2]` 分别声明为 11、18、2 字符。项目经理在 Python 3.12 复验得到：

```text
[('[REDACTED:11]', 13), ('[REDACTED:18]', 13), ('[REDACTED:2]', 12)]
```

因此这些占位符不保持原文坐标，方案据此得出的 “`EvidenceSpan` offset 不变” 结论无效。方案同时声称投影前和验证时对全文执行 NFKC 即可保持坐标，但 NFKC 可能扩展字符；复验 `体温㍑后疼痛` 从 6 个字符变为 `体温リットル后疼痛` 的 9 个字符。若不建立 projection-to-source 映射或严格限制为可证明的一对一规范化，后续 evidence offset 会漂移。

该问题直接违反任务合同第 8.3 节和验收标准中的可执行 EvidenceSpan 策略要求。

#### P1-2：最终门禁位置、fallback/retry 与消息边界覆盖描述错误

方案把 `AgentRuntime._call_gateway()` 描述为“最终序列化请求”边界，并声称 structured-output fallback/retry 会再次经过同一门禁。当前代码事实是：

- `_call_gateway()` 只把初始 `messages` 交给 `ModelGatewayClient`；
- `_chat_structured_impl()` 随后才构造 HTTP payload；
- `_chat_structured_json_fallback()` 在 Gateway 内追加 system message 并直接调用 `_request_with_retry()`，不会回到 `_call_gateway()`；
- 方案设计为逐条扫描 `message.content`，却把“同一请求内消息边界”列为已支持，无法检测跨两条 message 重组的号码；
- `AgentRuntime` 是共享 Runtime，在这里加门禁会影响所有使用者，与“其他 Agent 不复用”结论互相矛盾。

门禁可以放在 Runtime，也可以放在 Gateway，但必须按真实 payload 构造、fallback、retry 和共享影响说明完整不变量；当前版本没有满足合同第 8.4 节。

#### P1-3：候选任务不互斥，L4.5-11-3 没有当前先红证据

方案将“体温 `38.5℃` 不被脱敏”作为 L4.5-11-3 的先红证据，并声称当前 `_redact_free_text()` 对所有连续数字一视同仁。项目经理直接调用当前实现复验：

```text
体温 38.5℃           -> 体温 38.5℃
血压 120/80 mmHg     -> 血压 120/80 mmHg
心率 72 次/分        -> 心率 72 次/分
血糖 5.6 mmol/L      -> 血糖 5.6 mmol/L
500 mg               -> 500 mg
```

当前实现只匹配连续 11 位手机号和 18 位身份证号，所列临床数字测试在未改源码时已经是 green。与此同时，L4.5-11-1 已要求临床数字保护并允许修改 `context.py`，L4.5-11-3 又修改同一逻辑，范围发生重叠。方案第 4.3 节提出“其余连续数字 token 一律脱敏”，还会重新引入需要临床白名单兜底的宽泛匹配面，不符合本次有限威胁模型的收敛目标。

这违反任务合同第 8.5 节“真实先红、范围互斥、单一验收结论”的要求。

#### P2-1：数据所有权和保护对象仍有未闭合声明

- `ActiveObservationContext.value` 与 `normalized_value` 的类型为 `Any`，不能仅凭结构化 DTO 就断言“不涉及原文或 PII”；应描述其权威来源和投影责任。
- 威胁模型把姓名列入结构化身份保护对象，但当前 Intake 模型输入只有 `message_id/content`，方案没有定义姓名来自哪个结构化字段、如何检测或为何作为非目标。
- `[REDACTED:2]` 的姓名示例既不等长，也没有有限识别机制。

### 12.3 验收裁决

- `L4.5-11-0` 状态改为：**返工中 / 第 1 轮验收未通过**；
- `AR-B-031` 保持 P1 打开；
- 不接受当前 v2 方案为后续实现授权；
- **不发布 `L4.5-11-1`、`L4.5-11-2` 或 `L4.5-11-3`**；
- L5 继续 NO-GO，两个 LangGraph 公共开关继续默认关闭；
- 仅发布 `L4.5-11-0-R1` 文档限定返工，允许修订本轮两份交付文档，不允许修改代码、测试或项目经理台账。

限定返工任务：`docs/dev-handoff/agent-refactor-l4-5-11-0-rework-1-task.md`。

---

## 13. 限定返工交付（R1）

> 返工任务编号：L4.5-11-0-R1
> 执行起点：`a76168d886ddca9b2f64868015d65eb58ec851b5`
> 返工日期：2026-07-22
> 状态：**返工已交付，申请重新验收**

### 13.1 执行起点

| 项目 | 值 |
|---|---|
| 执行起点 exact HEAD | `a76168d886ddca9b2f64868015d65eb58ec851b5` |
| 分支 | `codex/l4-5-11-context-privacy-hardening` |
| 开始工作树状态 | 干净（`git status --short` 无输出） |

### 13.2 修正内容逐项记录

#### P1-1：EvidenceSpan 等长替换修正

| 修正项 | 位置 | 修正前 | 修正后 |
|---|---|---|---|
| 占位符形式 | 4.3 决策 3 | `[REDACTED:N]` | `█` 重复 N 次（U+2588） |
| 长度复验 | 5.2 等长替换规则 | 声称 `[REDACTED:11]` = 11 字符 | 复验：`[REDACTED:11]` = 13 字符；`█` × 11 = 11 字符 |
| NFKC 处理 | 5.1 策略比较 | "投影前对原文做 NFKC" | **替换在原始文本上进行**；NFKC 仅用于匹配辅助 |
| 坐标不变量 | 5.2 关键不变量 | 未明确 | 新增：替换在原始文本上进行，不经过 NFKC；每个匹配字符替换为一个 `█` |

**复验命令与结果：**

```powershell
$env:PYTHONUTF8='1'
uv run python -c "print(len('[REDACTED:11]'), len('█' * 11))"
# 输出: 13 11

uv run python -c "
import unicodedata
raw = '体温㍑后疼痛'
nfkc = unicodedata.normalize('NFKC', raw)
print(f'原文: {len(raw)} chars, NFKC: {len(nfkc)} chars')
"
# 输出: 原文: 6 chars, NFKC: 9 chars
```

**结论**：`[REDACTED:N]` 标签形式不等长；`█`（U+2588）经 NFKC 后长度不变；替换必须在原始文本上进行。

#### P1-2：最终门禁位置与覆盖修正

| 修正项 | 位置 | 修正前 | 修正后 |
|---|---|---|---|
| 门禁位置 | 6.1 | `AgentRuntime._call_gateway()` | `ModelGatewayClient._chat_structured_impl()` 入口 |
| 扫描对象 | 6.2 | 初始 `messages` 列表 | 最终 HTTP payload `payload["messages"]` |
| fallback 覆盖 | 6.2 | 声称 fallback 经过 `_call_gateway()` | 修正：fallback 在 Gateway 内部追加 system message，不回到 Runtime；Gateway 门禁覆盖 fallback |
| retry 覆盖 | 6.2 | 声称 retry 经过 `_call_gateway()` | 修正：retry 在 Gateway 内部；Gateway 门禁每次 retry 重新扫描 |
| 跨 message 重组 | 6.2 | 声称逐条扫描覆盖 | 修正：Gateway 门禁拼接所有 content 后扫描，覆盖跨 message 重组 |
| 共享影响 | 6.5 | "其他 Agent 不复用" | 明确：Gateway 被所有 Agent 使用；门禁需限制为 Intake-only 或明确非目标 |

**代码事实复验：**

- `_chat_structured_json_fallback()`（`app/core/gateway.py:514`）追加 system message 后直接调用 `_request_with_retry()`，不回到 `_call_gateway()`
- `_chat_structured_impl()`（`app/core/gateway.py:338`）内每次 retry 重新构造 payload
- 结论：Gateway Payload 门禁比 Runtime 门禁覆盖更完整

#### P1-3：候选任务修正

| 修正项 | 修正前 | 修正后 |
|---|---|---|
| L4.5-11-3 性质 | 独立实施任务"临床数字白名单" | 改为 L4.5-11-1 的**负向回归测试** |
| L4.5-11-3 先红 | 虚假先红：声称当前误脱敏体温 | 修正：当前 `_PII_PATTERNS` 精确匹配 11/18 位，不误伤临床数字；先红为验证投影后临床数字仍保留 |
| L4.5-11-3 范围 | 修改 `app/agent_runtime/context.py` | 只新增测试，不修改生产代码 |
| 任务重叠 | L4.5-11-1 和 L4.5-11-3 都修改 `context.py` | 修正：L4.5-11-3 只追加测试，无重叠生产职责 |

**复验命令与结果：**

```powershell
uv run python -c "
import re
_PII_PATTERNS = (re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)'), re.compile(r'(?<!\d)\d{17}[\dXx](?!\d)'))
def _redact_free_text(value):
    for pattern in _PII_PATTERNS:
        value = pattern.sub('[REDACTED]', value)
    return value

tests = ['体温 38.5℃', '血压 120/80 mmHg', '心率 72 次/分', '血糖 5.6 mmol/L', '500 mg']
for t in tests:
    result = _redact_free_text(t)
    print(f'{t!r:30} -> {result!r}')
"
# 输出: 所有临床数字都保留，未被脱敏
```

#### P2-1：数据所有权修正

| 修正项 | 位置 | 修正前 | 修正后 |
|---|---|---|---|
| `ActiveObservationContext` | 1.2 数据所有权矩阵 | "结构化事实，不涉及原文" | "`value`/`normalized_value: Any` 可能含患者派生数据；权威来源是 Intake 抽取结果" |
| 审计 digest | 1.2 数据所有权矩阵 | "不记录原始 PII" | "digest 计算输入含 canonical input 和 messages，但 digest 本身不可逆" |
| 姓名保护 | 3.1 保护对象 | 将姓名列为结构化身份字段 | 修正：当前 IntakeMessage 不含结构化姓名字段；自由文本姓名列为残余风险 |
| 非目标 | 3.5 | 未列姓名 | 新增："自由文本中的姓名"为非目标 |

### 13.3 修订后的候选任务

| 编号 | 名称 | 结果 | 依赖 | 先红证据 | 允许文件 |
|---|---|---|---|---|---|
| L4.5-11-1 | Intake 入口投影层 | USER 层不含裸 PII | L4.5-11-0-R1 | 手机号在 USER 层未被脱敏（原文 JSON） | `app/agents/intake_extraction.py`、`app/agent_runtime/context.py` |
| L4.5-11-2 | Gateway 最终隐私门禁 | 命中时零 Gateway 请求 | L4.5-11-1 | `_chat_structured_impl()` 直接发送，无隐私检查 | `app/core/gateway.py`、`app/agent_runtime/specs.py` |
| L4.5-11-3 | 临床数字保留回归测试 | 确认临床数字不被误脱敏 | L4.5-11-1 | 当前无入口投影；实施后验证临床数字保留 | `tests/test_l4_5_11_1_intake_privacy_projection.py`（追加测试） |

**依赖图：**

```
L4.5-11-0-R1（本返工：方案修订）
   │
   ▼
L4.5-11-1（Intake 入口投影层）
   │
   ├──→ L4.5-11-3（临床数字保留回归测试）
   │
   └──→ L4.5-11-2（Gateway 最终门禁）
            │
            ▼
      AR-B-031 关闭评估
```

**推荐下一任务：L4.5-11-1 Intake 入口投影层**

### 13.4 核对证据

| 检查项 | 命令 | 结果 |
|---|---|---|
| HEAD | `git rev-parse HEAD` | `a76168d886ddca9b2f64868015d65eb58ec851b5` |
| 工作区 | `git status --short` | 干净 |
| L0 文档契约 | `uv run pytest --override-ini addopts= -q tests/test_l0_1_contract.py` | 131 passed |
| diff 检查 | `git diff --check` | 通过 |

### 13.5 修改范围声明

| 范围 | 是否修改 | 说明 |
|---|---|---|
| 生产代码 | ❌ 否 | 未修改 |
| 测试代码 | ❌ 否 | 未修改 |
| 方案文档 | ✅ 是 | 修订 v2 方案 |
| Handoff 文档 | ✅ 是 | 追加 R1 返工交付 |

### 13.6 交付声明

本返工状态：**返工已交付，申请重新验收**。

交付内容：
1. `docs/01_agent部分优化/L4.5-11模型输入隐私收敛方案-v2-2026-07-21.md`（v2.1 修订版）
2. `docs/dev-handoff/agent-refactor-l4-5-11-0.md`（追加 R1 返工交付）

未修改生产代码、测试、依赖、配置或项目经理台账。

---

*返工日期：2026-07-22*
*返工人：开发测试执行者*

## 14. PM 重新验收与接管修订（R1-A）

> 核验日期：2026-07-22
> R1 交付提交：`30f919e94eb4e1da4599d790699efe003736cab3`
> R1 执行起点：`a76168d886ddca9b2f64868015d65eb58ec851b5`
> 接管人：Codex（工程项目经理）
> 状态：**R1 原交付仍未通过；依据用户授权由 PM 直接接管修订，修订结果待重新验收**

### 14.1 R1 提交与范围核验

| 检查项 | 结果 |
|---|---|
| R1 父提交 | 与返工执行起点 `a76168d...` 一致 |
| R1 修改范围 | 仅本 handoff 与 v2 方案两份允许文档 |
| 生产代码/测试/依赖/配置 | 未修改 |
| `git diff --check a76168d..30f919e` | 通过 |
| R1 提交后工作区 | 干净 |

### 14.2 重新验收仍未闭合的事实

| 级别 | 未闭合项 | 代码/文档证据 | 处置 |
|---|---|---|---|
| P1 | 最终门禁位置与真实请求次数仍写反 | `AgentRuntime._call_gateway()` 对真实 Gateway 传 `max_requests=1`；`_chat_structured_impl()` 在该参数存在时于 fallback 前 `break`；`_request_with_retry()` 的传输重试复用同一 payload | 改为 Intake-only Runtime 发送前门禁，并逐条证明目标路径只可能有一次请求 |
| P1 | Gateway 方案无法同时满足“共享 Gateway”与“只影响 Intake” | R1 将门禁放在共享 `ModelGatewayClient`，但没有定义可靠启用谓词；还要求 core 使用 `RuntimeErrorCode`，会造成 core → agent_runtime 反向依赖 | 门禁留在持有 `AgentSpec` 的 Runtime；Gateway API 和依赖方向不变 |
| P1 | 有限威胁模型没有可执行的跨 message 坐标规则 | 文档一处称支持，一处称不覆盖；“拼接 content”既无边界 token 也无原始坐标 | 定义 ASCII/全角 digit/X、三个精确分隔符、虚拟 `B` 和 `(message_index, raw_char_index)` token 流 |
| P1 | 仍残留宽泛数字 + 临床白名单策略 | v2.1 第 4.3 节仍写“其余连续数字 token 一律脱敏” | 删除该路径；只匹配有限身份 grammar，临床六类作为负向不变量 |
| P1 | 候选任务仍争用同一测试所有权 | L4.5-11-1 已要求临床数字测试，L4.5-11-3 又追加同一测试文件；依赖图还残留“临床数字白名单” | 合并临床负向回归到 L4.5-11-1，删除候选任务 3，只保留两个互斥任务 |

上述问题仍属于原 `AR-B-031` 范围，没有发现新的范围外 P0/P1。根据用户“若还不通过，直接接手完成”的授权，本轮不再发布 R2 返工任务。

### 14.3 PM 直接修订结果

方案文档升级为 v2.2，并完成以下收敛：

1. **有限 grammar**：只覆盖 ASCII/全角 digit/X、连续手机号/身份证号和三种精确手机号分隔形式；其他字符是硬边界；不宣称“所有 PII/Unicode”。
2. **确定性坐标**：shadow token 必须携带原始 message/字符坐标；跨 message 的 `B` 为零宽；逐 message 回写 `█`，每条 Python 字符长度不变。
3. **Grounding 所有权**：EvidenceSpan 始终属于原始 `source_message_id.content`；先投影再 JSON 序列化，验证器继续使用原文。
4. **真实最终边界**：Runtime 在调用 Gateway 方法前对 Intake-only messages 扫描；命中/扫描异常均固定失败，Gateway 方法调用 0 次。
5. **fallback/retry 证明**：目标 Runtime 路径传 `max_requests=1`，不会进入 JSON fallback，传输尝试也不会扩张；Legacy/其他 Agent/直接 Gateway 明确为非目标。
6. **两任务拆分**：L4.5-11-1 独占 projector/scanner、入口集成和临床负向测试；L4.5-11-2 独占 Runtime/specs、零调用与审计测试。

### 14.4 修改范围

本接管修订阶段只修改：

- `docs/01_agent部分优化/L4.5-11模型输入隐私收敛方案-v2-2026-07-21.md`
- `docs/dev-handoff/agent-refactor-l4-5-11-0.md`

未修改生产代码、测试、依赖、配置和项目经理台账。修订提交完成后，由 PM 对该提交执行 L0 文档契约、diff、范围、Git 跟踪和 clean exact-HEAD 复验，再单独写入最终验收事务。

### 14.5 接管修订预提交验证

| 检查项 | 结果 |
|---|---|
| L4.5-11-1 当前真实 red | 以手机号构造 `IntakeExtractionInput` 并调用 `build_intake_context()`；确认 USER 层仍含原值 |
| L4.5-11-2 当前真实 red | 直接调用 `_call_gateway()` 传入不安全 Intake messages；确认 fake Gateway 调用 1 次 |
| 方案结构断言 | v2.2、有限 grammar、原始坐标、`max_requests=1`、两个候选任务均存在；旧 L4.5-11-3/Gateway Payload 方案不存在 |
| 字符长度复验 | `len('[REDACTED:11]') == 13`；`len('█' * 11) == 11` |
| L0 文档契约 | `131 passed` |
| `git diff --check` | 通过 |
| 修改文件 | 仅 v2 方案与本 handoff 两份文档 |
