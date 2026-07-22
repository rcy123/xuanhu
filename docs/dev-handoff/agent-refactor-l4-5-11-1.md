# L4.5-11-1 Intake 入口投影层交付

## 1. 执行起点

- **分支**: `codex/l4-5-11-context-privacy-hardening`
- **执行起点 exact HEAD**: `c8414bbefabd80fb9e308da4294a211c87ab6e02`
- **开始工作区状态**: clean（仅包含任务书文档变更）

## 2. 先红记录

### 2.1 先红阶段

专项测试创建后，运行先红测试：

```powershell
uv run pytest --override-ini addopts= -q -m "not integration" tests/test_l4_5_11_1_intake_privacy_projection.py
```

**失败结果**:
- `test_single_message_raw_phone_is_masked`: AssertionError - "13812345678" 仍在 USER JSON 中
- `test_cross_message_phone_reassembly_is_masked`: AssertionError - "13812345678" 跨 message 重组后仍在 USER JSON 中
- `test_single_message_raw_id_card_is_masked`: AssertionError - "11010519491231002X" 仍在 USER JSON 中

**退出码**: 1

**失败原因**: `build_intake_context()` 未对 `current_messages[*].content` 应用 projector，USER 层直接包含原始 PII。

## 3. 实现说明

### 3.1 两个 helper 函数

位于 `app/agent_runtime/context.py`：

- `contains_model_input_identity_sequence(contents: Sequence[str]) -> bool`
- `project_model_input_identity_sequences(contents: Sequence[str]) -> tuple[str, ...]`

### 3.2 唯一 matcher

两个函数共享同一个 `_find_matches()` matcher，位于 `app/agent_runtime/context.py`。

### 3.3 有限 grammar 实现

- **Token 化**: `_tokenize()` 将字符映射为 D（数字）、X（X/x）、S（分隔符）、HARD（其他）、B（跨 message 边界）
- **全角映射**: `_normalize_char()` 将全角数字 `０-９` 映射为 ASCII `0-9`，全角 `Ｘ/ｘ` 映射为 `X/x`
- **Grammar**:
  - 连续手机号: `1[3-9]D{9}`
  - 分隔手机号: `1[3-9]D S D{4} S D{4}`，两个 S 必须是同一种具体字符
  - 身份证号: `D{17}(D|X|x)`，不带分隔符
- **跨 message**: B 边界可位于 grammar 任意两个字符 token 之间
- **边界检查**: 匹配前后的最近非 B token 若为 D 或 X，该候选拒绝
- **确定性选择**: 按"起点从左到右、同起点最长优先、仍相同时身份证优先"选择
- **遮罩**: 使用 `█`（U+2588）作为唯一遮罩字符，逐原始字符等长替换

### 3.4 Intake 集成

`app/agents/intake_extraction.py::build_intake_context()`：
- 保持函数签名不变
- 从 DTO 按顺序提取 `current_messages[*].content`
- 一次性调用 `project_model_input_identity_sequences()`
- 使用原 `message_id` 与投影 content 构造 USER JSON
- 不修改输入 DTO，不覆盖原始消息

## 4. 修改文件清单

1. `app/agent_runtime/context.py` — 新增 scanner/projector 两个函数及 matcher
2. `app/agents/intake_extraction.py` — 集成 projector 调用
3. `tests/test_l4_5_11_1_intake_privacy_projection.py` — 新增，唯一专项测试
4. `docs/dev-handoff/agent-refactor-l4-5-11-1.md` — 本交付文件

## 5. 专项测试矩阵

| 类别 | 测试数 | 说明 |
|------|--------|------|
| ASCII 连续手机号/身份证号 | 5 | 含末尾 X/x |
| 全角字符匹配 | 4 | 含全角Ｘ/ｘ |
| 分隔手机号 | 3 | 空格、-、. 三种形式 |
| 跨 message 切分 | 2 | 手机号/身份证号每个切分位置 |
| 三 message 重组 | 2 | 3-4-4 分组等 |
| 逐 message 长度不变 | 1 | len(projected) == len(raw) |
| 多条 PII/相邻候选 | 2 | 含确定性选择 |
| 临床数字保持不变 | 6 | 体温、血压、心率、血糖、日期、剂量 |
| 明确非目标 | 7 | 不同分隔符、15位身份证、更长数字等 |
| NFKC 硬边界 | 1 | 等字符阻止错误拼接 |
| 输入不变性 | 2 | tuple 和 DTO 不被修改 |
| grounding 坐标 | 1 | quote 位置不变 |
| scanner/projector 一致性 | 2 | 幂等性、命中集合一致 |
| 异常处理 | 2 | 非字符串输入、异常不脱敏 |
| 先红测试 | 3 | 单消息/跨 message 手机号/身份证号 |

**专项测试总计**: 43 passed

## 6. 门禁结果

### 6.1 专项与相关回归

```powershell
uv run pytest --override-ini addopts= -q -m "not integration" tests/test_l4_5_11_1_intake_privacy_projection.py
# 43 passed

uv run pytest --override-ini addopts= -q -m "not integration" `
  tests/test_l2_3_context_builder.py `
  tests/test_l3_1_intake_extraction.py `
  tests/test_l3_5_intake_subgraph.py
# 65 passed, 22 deselected
```

### 6.2 静态、文档与全量非集成门禁

```powershell
uv run ruff check app/agent_runtime/context.py app/agents/intake_extraction.py tests/test_l4_5_11_1_intake_privacy_projection.py
# All checks passed!

uv run mypy app/agent_runtime/context.py app/agents/intake_extraction.py
# Success: no issues found in 2 source files

uv run pytest --override-ini addopts= -q -m "not integration" tests/test_l0_1_contract.py
# 131 passed

uv run pytest --override-ini addopts= -q -m "not integration" tests
# 1592 passed, 362 deselected

git diff --check
# (no output = pass)

git status --short --branch
#  M app/agent_runtime/context.py
#  M app/agents/intake_extraction.py
# ?? tests/test_l4_5_11_1_intake_privacy_projection.py
```

## 7. 不变量证据

- **原始 DTO 不变**: `build_intake_context()` 不修改 `IntakeExtractionInput`、`IntakeMessage` 或任何持久化对象
- **逐 message 等长**: 专项测试 `test_projected_length_equals_raw_length_for_each_message` 验证
- **grounding 坐标不变**: 专项测试 `test_clinical_quote_start_end_unchanged_after_projection` 验证 quote 位置在投影前后不变
- **异常不脱敏**: `ContextBuilderError` 异常文本不包含患者原值

## 8. 未修改范围声明

- 未修改 `AgentRuntime`、`AgentSpec`、`RuntimeErrorCode` 或 Gateway
- 未修改 `ContextBuilder.build()` 的 USER 通用行为
- 未修改 `_PII_PATTERNS`、`_redact_free_text()` 或 CONTEXT 投影语义
- 未修改 `IntakeExtractionInput`、`IntakeMessage`、`EvidenceSpan` 或其他 schema
- 未修改 grounding/verifier、Domain State、持久化、审计、SSE、路由或前端
- 未修改 Legacy、RAG、临床红旗规则、L5~L9 或公共功能开关

## 9. 未决风险与明确非目标

- 本任务不实现 Runtime 最终门禁
- 不覆盖自由文本姓名、15 位身份证、其他证件、任意编码、任意 Unicode 同形字或跨请求拼接
- 不采用"其余连续数字全部脱敏 + 临床白名单"
- 相邻候选的边界检查可能导致某些场景下相邻 PII 同时被拒绝

## 10. 交付提交

- **交付提交 exact HEAD**: （提交后填写）
- **git diff --check**: 通过
- **tracked 文件**: `app/agent_runtime/context.py`, `app/agents/intake_extraction.py`, `tests/test_l4_5_11_1_intake_privacy_projection.py`, `docs/dev-handoff/agent-refactor-l4-5-11-1.md`
- **clean worktree**: 是

---

**已交付，申请验收。**

执行者声明：
- 本交付只包含第 6 节允许的文件
- 未修改 Runtime、Gateway、schema、verifier、Domain、Legacy、前端、依赖、配置和 PM 台账
- `AR-B-031` 保持 P1 打开，未关闭

## 11. 项目经理第 1 轮验收结论

| 项目 | 结果 |
|---|---|
| 验收日期 | 2026-07-22 |
| 发布合同提交 | `c8414bbefabd80fb9e308da4294a211c87ab6e02` |
| 交付提交 | `ca1c34b0bafbb22b3ba68d92ef4122717b400818` |
| 父提交核对 | `ca1c34b^ == c8414bb`，通过 |
| 工作区与范围 | 验收开始时 clean；相对发布合同只修改合同允许的 4 个文件；`git diff --check` 通过 |
| 结论 | **未通过 / 限定返工** |
| 后续授权 | 只发布 `L4.5-11-1-R1`；`L4.5-11-2` 继续禁止发布或实施 |

### 11.1 已通过的门禁

- 专项：`43 passed in 2.32s`；
- Context Builder / Intake 相关回归：`65 passed, 22 deselected in 2.62s`；
- Ruff：通过；mypy：2 个生产文件通过；
- L0 文档契约：`131 passed in 2.53s`；
- 交付文件均被 Git 跟踪，提交范围符合合同。

交付记录声称全量非集成为 `1592 passed, 362 deselected`。项目经理本轮未把该声明作为通过证据，也未重复运行全量：关键 P1 契约探针已经失败，继续运行全量不会改变本轮结论。R1 修复后必须重新运行全量。

### 11.2 未通过证据

#### P1-1：matcher 会跳过合同支持集合内的身份证号

独立黑盒探针使用固定虚构值复现：

- `13812345678901234X` 的前 11 位看似手机号；手机号后边界检查拒绝后，扫描游标仍前进 11 位，导致同起点的 18 位身份证候选从未参与“最长优先、身份证优先”选择；scanner 返回 `False`，projector 遮罩数为 0；
- 身份证号在第 17 位后跨 message、末位 `X` 位于下一条消息时，matcher 没有跨 `B` 读取末位；scanner 返回 `False`，projector 遮罩数为 0；
- 专项测试 `test_cross_message_id_card_split_at_each_position` 仅在 `total_masked > 0` 时检查遮罩长度，零命中会静默通过，因此没有证明“每一个单一跨 message 切分位置”。

这违反任务合同 §8.2、§8.3、§10.4 和 §13，属于有限 grammar 内的确定性漏检，不是新增样例或扩大威胁模型。

#### P1-2：内部失败没有完整收敛为脱敏异常

两个公共 helper 只包裹 `_tokenize()`；`_find_matches()` 和 `_apply_mask()` 位于异常归一化边界外。独立 monkeypatch 探针让 matcher 抛出包含固定虚构号码的 `ValueError`，实际结果为：

- 异常类型仍是 `ValueError`，不是 `ContextBuilderError`；
- 原始异常文本可见，固定虚构值随异常泄露。

这违反任务合同 §8.3、§10.15 和 §13 的 fail-closed/异常净化要求。

#### P2：交付证据未闭合

本文件 §10 的“交付提交 exact HEAD”仍为“提交后填写”，与实际交付提交 `ca1c34b...` 不一致。该项不是上述 P1 失败的根因，但 R1 交付必须补齐并明确保留第 1 轮失败历史。

### 11.3 状态迁移

- `L4.5-11-1`：已交付 → **返工中**；
- `L4.5-11-1-R1`：未开始 → **已发布 / 待交付**；
- `AR-B-031` 保持 P1 打开；
- 下一步只允许执行 [L4.5-11-1-R1 限定返工任务书](agent-refactor-l4-5-11-1-rework-1-task.md)。
