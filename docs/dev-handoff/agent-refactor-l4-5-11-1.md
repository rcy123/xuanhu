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
