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
