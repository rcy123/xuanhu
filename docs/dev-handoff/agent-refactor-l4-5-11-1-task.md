# L4.5-11-1 Intake 入口投影层任务

## 1. 发布信息

| 项目 | 内容 |
|---|---|
| 任务编号 | `L4.5-11-1` |
| 发布日期 | 2026-07-22 |
| 发布人 | Codex（工程项目经理） |
| 执行负责人 | 领取本任务的开发测试执行者 |
| 状态 | **已发布 / 待交付** |
| 审核 | 待项目经理工程验收 |
| 实施分支 | `codex/l4-5-11-context-privacy-hardening` |
| 发布输入 exact HEAD | `a00e5b3917a5d3ae38fe37aa091384eca66f99e4` |
| 生产代码基线 | `b97c9f9`（此后到发布输入仅有受控文档变更） |
| 方案基线 | `22feb390425c3b4fc4349980566a5c80314e60d0` 的 v2.2 方案 |
| 前置验收 | `ACC-20260722-005`：L4.5-11-0 通过 |
| 关联阻塞 | `AR-B-031`（P1，保持打开） |
| 交付与验收载体 | `docs/dev-handoff/agent-refactor-l4-5-11-1.md`（执行者新增） |
| 后续候选 | `L4.5-11-2`，本任务**不授权**其实施 |

执行者领取任务后，必须先记录“包含本任务合同的 clean exact HEAD”作为执行起点。若该 HEAD 的生产代码、允许文件内容或先红事实已与本合同不一致，按停止条件处理，不得静默套用本合同。

## 2. 权威关系与单一结果

- [当前状态](../01_agent部分优化/项目管理/00-当前状态.md) 是当前动作的唯一事实源；
- [任务台账](../01_agent部分优化/项目管理/01-任务台账.md) 是任务状态的唯一事实源；
- [v2.2 方案](../01_agent部分优化/L4.5-11模型输入隐私收敛方案-v2-2026-07-21.md) 定义有限威胁模型和两任务边界；
- 本文件是 `L4.5-11-1` 的唯一实施合同；回退前 v1、B1～B133、历史 ADR 和旧批准单均不构成实施授权。

本任务只有一个可验收结果：

> `build_intake_context()` 必须在 JSON 序列化前，对有序 `current_messages[*].content` 应用有限身份 scanner/projector；投影后每条消息与原文严格等长，USER 层不再包含本合同支持集合内的手机号/身份证号，同时原始 DTO、持久化消息和 grounding 坐标所有权保持不变。

本任务不实现 Runtime 最终门禁。入口投影验收通过不关闭 `AR-B-031`。

## 3. 已复现的当前事实与先红

### 3.1 代码事实

在发布输入 `a00e5b3` 上：

- `app/agents/intake_extraction.py::build_intake_context()` 在第 103～106 行从 DTO 复制原始 `content`；
- 第 119 行直接执行 `json.dumps(current_messages, ...)` 并传入 USER 层；
- USER 参数不经过 `ContextBuilder.project()`；
- `app/agent_runtime/context.py::_redact_free_text()` 只服务现有 CONTEXT 投影，并使用非等长 `[REDACTED]`；
- `EvidenceSpan` 坐标属于原始 `source_message_id.content`，由 grounding verifier 复核；
- 当前不存在本任务专项测试、任务交付文件或入口等长 projector。

### 3.2 可复现先红

发布时已执行：

```powershell
@'
import json
from uuid import uuid4
from app.agents.intake_extraction import build_intake_context
from app.schemas.intake import IntakeExtractionInput, IntakeMessage, IntakeMessageRole

def msg(content):
    return IntakeMessage(message_id=uuid4(), role=IntakeMessageRole.PATIENT, content=content)

single = IntakeExtractionInput(
    current_messages=(msg('phone 13812345678'),),
    historical_active_facts=(),
)
single_user = build_intake_context(single)[0].messages[-1].content

split = IntakeExtractionInput(
    current_messages=(msg('phone 138'), msg('12345678')),
    historical_active_facts=(),
)
split_payload = json.loads(build_intake_context(split)[0].messages[-1].content)

assert '13812345678' in single_user
assert split_payload[0]['content'].endswith('138')
assert split_payload[1]['content'] == '12345678'
print('single_raw=true')
print('cross_message_fragments_raw=true')
'@ | uv run python -
```

当前输出：

```text
single_raw=true
cross_message_fragments_raw=true
```

执行者必须先在唯一专项测试文件中把该缺口写成失败测试，记录真实 red，再修改生产代码。不得把上面的当前失败断言直接当作绿色验收测试。

## 4. 目标

1. 在 `app/agent_runtime/context.py` 提供一个 scanner 事实源和两个确定性纯函数：
   - `contains_model_input_identity_sequence(contents: Sequence[str]) -> bool`
   - `project_model_input_identity_sequences(contents: Sequence[str]) -> tuple[str, ...]`
2. 两个函数复用同一个有限 matcher，不能维护两套 grammar 或正则；
3. 在 `build_intake_context()` 中按原始顺序投影 `current_messages[*].content`，再与原 `message_id` 组合并 JSON 序列化；
4. 不修改输入 DTO；不修改、保存或覆盖任何原始消息；
5. 每条投影消息的 Python `len()` 必须与对应原文相同；
6. 非 PII 临床文本的 quote/offset 保持可由原始消息验证；
7. scanner 可由后续 `L4.5-11-2` 直接导入复用，但本任务不得实现 Runtime 门禁。

## 5. 非目标

本任务明确不做：

- 不修改 `AgentRuntime`、`AgentSpec`、`RuntimeErrorCode` 或 Gateway；
- 不实现 Gateway 零请求失败语义；
- 不修改 `ContextBuilder.build()` 的 USER 通用行为；
- 不改变现有 `_PII_PATTERNS`、`_redact_free_text()` 或 CONTEXT 投影语义；
- 不修改 `IntakeExtractionInput`、`IntakeMessage`、`EvidenceSpan` 或其他 schema；
- 不改变 grounding/verifier、Domain State、持久化、审计、SSE、路由或前端；
- 不覆盖自由文本姓名、15 位身份证、其他证件、任意编码、任意 Unicode 同形字或跨请求拼接；
- 不采用“其余连续数字全部脱敏 + 临床白名单”；
- 不恢复 B1～B133 或拆分 `context.py` 为多模块；
- 不修改 Legacy、RAG、临床红旗规则、L5～L9 或公共功能开关；
- 不声称已经满足完整隐私、法律、临床、伦理或机构合规。

## 6. 允许修改范围

执行者只允许修改或新增以下文件：

1. `app/agent_runtime/context.py`
2. `app/agents/intake_extraction.py`
3. `tests/test_l4_5_11_1_intake_privacy_projection.py`（新增，唯一专项测试所有者）
4. `docs/dev-handoff/agent-refactor-l4-5-11-1.md`（新增，交付与验收载体）

允许文件与最终交付文件必须一致。若某个允许文件实际无需修改，应在 handoff 说明；不得用其他文件替代其职责。

## 7. 禁止修改范围

除第 6 节外全部禁止，特别包括：

- `app/agent_runtime/runtime.py`
- `app/agent_runtime/specs.py`
- `app/core/gateway.py`、`app/core/exceptions.py`
- `app/schemas/**`
- `app/services/**`、其他 `app/agents/**`
- 现有 `tests/**` 文件
- `frontend/**`、`scripts/**`、`.github/**`
- `pyproject.toml`、`uv.lock`、依赖、迁移、配置、部署和环境文件
- `docs/01_agent部分优化/项目管理/**`
- 本任务书、v2.2 方案、历史方案、批准单、历史 ADR、既有交付和验收记录

发现必须修改禁区才能完成时立即停止，在 handoff 记录缺口、所需权限和建议范围，不得顺手扩张。

## 8. 有限 matcher 强制合同

### 8.1 token 与坐标

按调用者给出的 `contents` 顺序构造临时 token 流。每个真实字符 token 必须携带 `(message_index, raw_char_index)`：

| token | 接受字符 | 规范化结果 |
|---|---|---|
| `D` | ASCII `0-9`、全角 `０-９` | 一个 ASCII digit |
| `X` | ASCII `X/x`、全角 `Ｘ/ｘ` | 一个 ASCII `X/x` |
| `S` | 一个 ASCII 空格、`-`、`.` | 保留具体分隔符种类 |
| `B` | 相邻两条 content 之间 | 虚拟零宽边界，无原始坐标 |
| `HARD` | 以上集合外的任意字符 | 终止当前候选，不跨越 |

实现可以不用 `unicodedata.normalize()`，但行为必须等价于上述显式一一映射。禁止对整段文本先做 NFKC。一个原始字符若不能确定性映射为一个允许 token，就必须成为 `HARD`。

### 8.2 唯一支持 grammar

- 连续手机号：`1[3-9]D{9}`；
- 分隔手机号：`1[3-9]D S D{4} S D{4}`，两个 `S` 必须是同一种具体字符；
- 身份证号：`D{17}(D|X|x)`，不带分隔符；
- `B` 可位于上述 grammar 任意两个字符 token 之间，不计入字符数量；
- 匹配前后的最近非 `B` token 若为 `D` 或 `X`，该候选必须拒绝，防止从更长数字/身份序列中截取子串；
- 不得增加本节以外的格式、字符类、宽泛数字模式或临床例外。

### 8.3 遮罩与确定性

- 使用 `█`（U+2588）作为唯一遮罩字符；
- 对每个命中的 `D`、`X`、`S` 原始坐标各写入一个 `█`；`B` 不写入任何字符；
- 跨 message 命中分别回写各自原始副本，每条 content 长度独立保持；
- 同一输入中的所有不重叠命中都必须遮罩；
- 若出现候选重叠，按“起点从左到右、同起点最长 token span 优先、仍相同时身份证优先”选择，结果不得依赖 set/dict 遍历顺序；
- projector 不得原地修改输入 sequence 或字符串；返回新的 `tuple[str, ...]`；
- projector 必须幂等：对已投影结果再次投影，结果不变；
- `contains...` 与 `project...` 必须共享 matcher，并满足：存在命中时前者为 `True` 且后者发生对应遮罩；不存在命中时前者为 `False` 且文本逐字符不变；
- 输入不是字符串、token 化或坐标回写出现内部不变量错误时，抛出脱敏的 `ContextBuilderError`；异常文本、日志和 handoff 不得包含患者原值、命中片段或坐标。

## 9. Intake 集成强制合同

`build_intake_context()` 必须：

1. 保持函数签名不变；
2. 从已验证 DTO 按顺序提取 `current_messages[*].content`；
3. 一次性调用 `project_model_input_identity_sequences()`，以便识别跨 source-message 的 `B`；
4. 使用原 `message_id` 与对应投影 content 构造 USER JSON；
5. 保持 JSON 参数 `ensure_ascii=False, separators=(",", ":")` 不变；
6. 保持 SYSTEM、DEVELOPER、CONTEXT、token budget、prompt version 和消息顺序不变；
7. 不把投影结果写回 `IntakeExtractionInput`、`IntakeMessage` 或任何持久化对象；
8. 让 `ContextBuilderError` 继续由现有 `execute_intake_extraction()` 边界归一化为固定失败，不新增原文日志。

模型看到的遮罩区不得被反向写成患者事实；任何 quote 跨入遮罩区都应继续由现有 grounding 原文核对拒绝。本任务不修改 verifier 来放宽该行为。

## 10. 专项测试所有权与矩阵

所有新增测试只能位于 `tests/test_l4_5_11_1_intake_privacy_projection.py`。至少覆盖：

1. ASCII 连续手机号、ASCII 连续身份证号、末位 `X/x`；
2. 全角手机号、全角身份证号、全角 `Ｘ/ｘ`；
3. 空格、`-`、`.` 三种精确手机号分隔形式，且两个分隔符必须相同；
4. 连续手机号和身份证号的每一个单一跨 message 切分位置；
5. 至少一个三 message 重组（例如手机 3-4-4 分组）；
6. 跨 message 命中后逐条 `len(projected[i]) == len(raw[i])`；
7. 单条含多个 PII、相邻候选和确定性选择顺序；
8. 体温、血压、心率、血糖、日期、剂量逐值不变；
9. 不同分隔符、多个空格、下划线、斜杠、15 位身份证、数学字母数字、编码文本和更长数字序列作为明确非目标，保持不变；
10. `㍑` 等 NFKC 扩展字符是 `HARD`，前后内容不被错误拼接；
11. 输入 tuple、原始 `IntakeExtractionInput`、message id、顺序和 content 均不被修改；
12. USER JSON 只含投影副本，原始 DTO 仍含原文；
13. 临床 quote 在投影前后保持相同 start/end；跨入遮罩区的 quote 不得伪造成可 grounding 原文；
14. projector 幂等，scanner/projector 命中集合一致；
15. 非字符串/内部失败只产生固定脱敏异常，不把样例原值写入异常或日志。

不得复制 B1～B133 的无限样例表。参数化测试必须围绕本合同有限字符类、grammar、边界与不变量组织。

## 11. 先红后绿要求

实施顺序必须记录在 handoff：

1. 先新增专项测试，至少使“单消息裸手机号”和“跨 message 重组”因当前无 projector 而失败；
2. 运行专项测试并保存失败测试名、断言摘要和退出码；禁止保存完整患者样例到日志，测试仅使用固定虚构数据；
3. 再修改两个允许生产文件；
4. 运行专项测试转绿；
5. 运行相关回归、静态检查、L0 和全量非集成门禁；
6. 提交交付并在 clean exact HEAD 上申请验收。

如果测试在生产修改前意外全绿，说明先红假设不成立，立即停止并提交事实差异，不得继续制造实现。

## 12. 验收命令与通过标准

### 12.1 专项与相关回归

```powershell
uv run pytest --override-ini addopts= -q -m "not integration" tests/test_l4_5_11_1_intake_privacy_projection.py

uv run pytest --override-ini addopts= -q -m "not integration" `
  tests/test_l2_3_context_builder.py `
  tests/test_l3_1_intake_extraction.py `
  tests/test_l3_5_intake_subgraph.py
```

发布基线的相关回归口径为 `65 passed, 22 deselected`。交付时不得减少既有通过数；新增专项数以实际收集结果记录。

显式 `-m "not integration"` 是强制项：清空 `addopts` 后若不加 marker，上述文件会混合 unit/integration，并被 `tests/conftest.py` 的隔离钩子拒绝。

### 12.2 静态、文档与全量非集成门禁

```powershell
uv run ruff check `
  app/agent_runtime/context.py `
  app/agents/intake_extraction.py `
  tests/test_l4_5_11_1_intake_privacy_projection.py

uv run mypy app/agent_runtime/context.py app/agents/intake_extraction.py

uv run pytest --override-ini addopts= -q -m "not integration" tests/test_l0_1_contract.py

uv run pytest --override-ini addopts= -q -m "not integration" tests

git diff --check
git status --short --branch
```

发布基线的全量非集成口径为 `1549 passed, 362 deselected`。交付时不得减少既有通过数；若仓库在执行起点后新增了有归属的测试，按实际增长记录。

通过标准：

- 专项、相关回归、L0 与全量非集成全部通过；
- Ruff、mypy、`git diff --check` 通过；
- 没有收集或运行真实外部服务 integration；
- 最终提交只包含第 6 节文件；
- 正式测试和 handoff 已被 Git 跟踪；
- 交付 exact HEAD 工作区 clean。

## 13. 项目经理验收标准

项目经理只在以下条件全部满足时接受：

- diff 与本合同允许范围完全一致；
- 真实 red 已在生产修改前记录，且失败原因正是 USER 原文缺口；
- 两个约定函数存在、签名稳定、共用一个 matcher；
- 有限 grammar、全角一一映射、`B` 边界、确定性选择和 HARD 规则均有参数化测试；
- 所有命中逐原始字符等长遮罩，逐 message 长度不变；
- 原始 DTO、持久化消息、message id、顺序和 grounding 坐标所有权不变；
- 临床六类数字全部保持；明确非目标没有被悄悄纳入；
- 现有 `_PII_PATTERNS`、CONTEXT 投影、Runtime、Gateway、schema 和 verifier 未修改；
- 专项、相关回归、全量非集成、L0、Ruff、mypy 和 diff 门禁全部通过；
- 异常、日志、recorder 和交付证据不包含命中的原始值；
- exact commit 与 clean worktree 证据完整。

项目经理/Reviewer 将独立复现关键 red/green、检查 matcher 只有一个事实源，并在 handoff 写入结论。若项目经理同时成为实现者，则必须增加未参与实现的 reviewer；自审不能满足独立复核。

验收通过只表示可以发布 `L4.5-11-2`。它不关闭 `AR-B-031`、不恢复 L4.5 总验收、不切换默认后端，也不替代任何专业批准。

## 14. 停止条件

出现任一情况立即停止并在 handoff 记录：

- 执行起点不是包含本任务书的 clean exact HEAD，或基线事实已变化；
- 需要修改第 7 节禁区；
- 现有测试在生产修改前不能复现先红；
- 任一 message 的投影长度与原文不同；
- 必须整段 NFKC、引入 offset map、修改 EvidenceSpan 或放宽 grounding 才能通过；
- 临床数字被误脱敏，或需要字段/范围/单位白名单补洞；
- 需要新增 grammar、字符类、姓名识别、证件类型、编码层或跨请求逻辑；
- scanner 与 projector 需要两套 matcher 或结果不一致；
- 需要修改 Runtime/Gateway 才能完成入口投影；
- 异常、日志或测试输出泄露原始命中值；
- 相关/全量回归失败且根因不能在允许范围内确定性修复；
- 工作区含无法归属的改动，或正式交付文件未被 Git 跟踪；
- 发现新的范围外 P0/P1。

停止不是失败；它用于阻止任务重新退化为打地鼠式扩张。项目经理将根据证据决定范围变更、限定返工或新任务。

## 15. 回退方式

- 在尚无下游依赖时，完整回退本任务生产提交和专项测试；
- 或移除 `build_intake_context()` 的 projector 调用及本任务新增的两个 helper/matcher，恢复发布基线行为；
- 不得通过修改原始消息、Domain State、verifier 或 Gateway 来“补偿”回退；
- 回退后 `AR-B-031` 保持 P1 打开，必须重新进入项目经理决策。

## 16. 交付记录要求

`docs/dev-handoff/agent-refactor-l4-5-11-1.md` 至少记录：

1. 执行起点 exact HEAD、分支、开始工作区状态；
2. 先红提交/阶段、命令、失败测试名、退出码和失败原因；
3. 两个 helper 的实际签名和唯一 matcher 位置；
4. 有限 grammar、token/坐标、跨 message 与确定性选择的实现说明；
5. 修改文件清单和范围核对；
6. 专项测试矩阵与收集/通过数量；
7. 相关回归、L0、全量非集成、Ruff、mypy、diff 的完整命令和摘要；
8. 原始 DTO 不变、逐 message 等长、grounding 坐标不变的证据；
9. 异常/日志不含原值的证据；
10. 未决风险、明确非目标和停止条件触发情况；
11. 交付提交 exact HEAD、`git diff --check`、tracked 文件和 clean worktree；
12. 明确声明未修改 Runtime、Gateway、schema、verifier、Domain、Legacy、前端、依赖、配置和 PM 台账。

执行者只能写“已交付，申请验收”，不能自行把任务写成“已完成/已验收”，不能关闭 `AR-B-031` 或发布 `L4.5-11-2`。

## 17. 发布后的唯一下一动作

开发测试执行者从包含本合同的 clean exact HEAD 领取 `L4.5-11-1`，按第 11 节先红后绿实施并提交 handoff。项目经理验收通过前，`L4.5-11-2` 保持 planned，禁止提前实施。
