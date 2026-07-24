# L6-4 病历文本润色与最终组合（Sandbox）

## 发布信息

| 项目 | 内容 |
|---|---|
| 状态 | 已发布 / 待交付 |
| 发布人 | Codex（工程项目经理） |
| 发布日期 | 2026-07-24 |
| 基线 | `7f1a6a9`（L6-3 交付提交） |
| 依赖 | L5-PREP-0、L5-1、L5-2、L5-3、L5-4、L6-1、L6-2、L6-3 全部 accepted |
| 阻塞 | 无活跃工程阻塞 |
| 交付文件 | `docs/dev-handoff/agent-refactor-l6-4-sandbox-task.md`（本文件） |

## 目标

在个人学习、非临床、仅合成数据沙盒范围内，实现病历子图的第四层（最后一层）：**确定性文本叙述与全量跨层组合**。

具体目标：

1. 实现 `SandboxRecordNarration`（确定性、无模型、纯结构化转文本）：
   - `narrate(record: SandboxMedicalRecordData) -> str` — 将已组装/已验证/已存储的 structured record 转换为人类可读的固定格式叙述文本
   - **不使用 LLM/模型调用**：叙述完全基于 record 字段的确定性模板拼接（session_id、revision_id、reviewed_formula 各项、safety_result 决策、review_confirm_ref、assembled_at、record_id 等）
   - 同一 record 多次 `narrate` → 字符串完全相同
   - 不同 record（不同 record_id）→ 字符串不同
   - 不存储 narration 结果到 Store（narration 是纯函数，不产生副作用）
   - 不调用 `open/print/breakpoint/exec/eval/compile`，不发起网络调用

2. 实现最终跨层组合集成验证：
   - 全流水线演练：`SandboxMedicalRecordData`（L6-1 DTO）→ `SandboxRecordAssembler`（L6-1）→ `SandboxRecordConsistencyVerifier`（L6-2）→ `SandboxRecordStore`（L6-3）→ `serialize_record`（L6-3）→ `SandboxRecordNarration`（L6-4）
   - 验证各层之间契约一致：Assembler 输出可被 Verifier 接受、Verifier 通过后 Store 可存储、Store 存储后可序列化、序列化后可叙述
   - 篡改字段后 Verifier 拒绝、Store put 拒绝、Narration 输出反映篡改（确定性）

3. 验证边界：
   - 合法 record → narration 输出稳定、完整、可读
   - 不同 record → narration 输出不同
   - 跨层全链：assembler → verifier → store → serialize → narrate 全部通过
   - 单层失败（verifier 拒绝 / store 拒绝）→ 全链固定失败

4. 建立 L6-4 专项测试：
   - narration 确定性（同 record → 同字符串）
   - narration 区分性（不同 record → 不同字符串）
   - narration 字段覆盖（输出包含全部关键字段）
   - 全链组合演练通过
   - 篡改后全链阻断

## 非目标

- 不使用 LLM、模型调用或自由文本生成；narration 是纯确定性模板函数
- 不修改 L6-1 已 accepted 的 DTO/Assembler 核心逻辑
- 不修改 L6-2 已 accepted 的 Verifier 核心逻辑
- 不修改 L6-3 已 accepted 的 Store/serialize 核心逻辑
- 不接入真实 LangGraph `Command`、Runtime、HTTP、容器、部署、DB、RAG、Gateway 或外部服务
- 不连接真实患者数据、真实病历、真实知识库或生产模型日志
- 不生成真实临床诊断、治疗建议、处方或医疗决策
- 不修改 accepted L5-1/L5-2/L5-3/L5-4、L6-1/L6-2/L6-3 生产代码、handoff 或验收记录
- 不修改 Legacy engine/review/record、配置、依赖、前端、UI 或部署
- 不声称临床有效、医疗安全、法规合规或获得专业批准

## 允许修改范围

只允许修改/新增以下文件，全部 tracked：

1. `sandbox_record.py` — 在现有文件内新增 `SandboxRecordNarration` 类（不修改 L6-1 DTO/Assembler、L6-2 Verifier、L6-3 Store/serialize 部分）
2. `tests/test_sandbox_record_l6_4.py` — L6-4 唯一专项测试
3. `docs/dev-handoff/agent-refactor-l6-4-sandbox.md` — 交付 handoff

允许从 `sandbox_record.py` 自身、`sandbox_review.py`、`sandbox_recheck.py` 读取已 accepted 的类型和常量（只读引用，不修改）。

## 禁止修改范围

- 禁止修改 `sandbox_record.py` 中 L6-1 已验收的 `SandboxMedicalRecordData`、`SandboxRecordAssembler`、L6-2 已验收的 `SandboxRecordConsistencyVerifier`、L6-3 已验收的 `SandboxRecordStore`、`serialize_record` 的任何代码
- 禁止修改 `sandbox_safety.py`（L5-1）、`sandbox_explanation.py`（L5-2）、`sandbox_review.py`（L5-3）、`sandbox_recheck.py`（L5-4）的任何代码、测试或 handoff
- 禁止修改 `pyproject.toml`、`README.md`、配置、依赖、前端、Legacy、Runtime、DB、Gateway、PM 台账
- 禁止修改 L0～L5 任何已验收的管理文档、验收记录、决策记录
- 禁止读取 `.env`、ignored `data/` 或任何外部存储
- 禁止网络调用、子进程、真实文件写入（专项测试的临时 in-memory store 除外）
- 禁止在 narration 中调用任何 LLM/模型 API、free-text generation 或非确定性文本构造

## 先红后绿要求

1. 在未修改生产代码时，以真实 RED 证明以下缺口：
   - 无 narration 函数时无确定性文本输出
   - 无最终组合时各层独立但无端到端演练
2. 修复后 GREEN 必须覆盖：
   - `SandboxRecordNarration.narrate(record)` 返回确定性字符串
   - 同 record → 相同叙述字符串
   - 不同 record → 不同叙述字符串
   - 叙述包含 record 的关键字段信息
   - 全链：assembler → verifier → store → serialize → narrate 全部通过
   - 篡改字段 → verifier 拒绝 / store 拒绝（至少一层阻断）
   - AST 边界：无 `open/print/breakpoint/exec/eval/compile`、无 network/socket/http 调用
   - 不新增未被批准的 import 根（继承 L6-3 已批准的集合）

## 验收标准

### 独立 Review
- P0/P1/P2/P3 全为 0
- 不修改 accepted L5/L6-1/L6-2/L6-3 代码的前提下，L6-4 模块独立可测

### 独立 CI
- L6-4 专项测试全部通过
- L6-1/L6-2/L6-3 专项 `12 + 32 + 15 passed`
- L5-1/2/3/4 回归专项全部通过（`14/18/84/60`）
- Safety `71/3 deselected`、privacy `76`、L0 `131`、Ruff/mypy/lock 全通过
- scope/tracked/diff/exact/clean 全通过

### PM 探针
- 五项定向探针：
  1. narration 确定性（同 record → 同字符串）
  2. narration 区分性（不同 record → 不同字符串）
  3. narration 字段完整性（输出包含 session_id、revision_id、reviewed_formula 等）
  4. 全链组合通过（assembler → verifier → store → serialize → narrate）
  5. 篡改阻断（篡改字段后至少一层拒绝）

## 停止条件

- 任何修改超出允许文件范围 → 停止，重新发布
- 任何真实患者/临床数据进入测试 → 立即停止
- 需要修改 L5/L6-1/L6-2/L6-3 代码才能通过 → 停止，发布对应 rework 而非在当前任务中修复
- 发现 P0/P1 → 停止交付，发布 bounded rework
- narration 引入 LLM/模型调用或非确定性行为 → 停止，降级为占位实现

## 记录要求

1. 开发交付时更新 `agent-refactor-l6-4-sandbox.md` handoff
2. 不得由开发交付声明替代 PM 验收
3. 验收通过后，PM 追加 `ACC-YYYYMMDD-NNN` 验收记录、更新任务台账和当前状态
4. L6-4 验收通过后，L6 阶段整体可标记 **已完成 / engineering complete**

## 状态边界

- 本任务发布不等于 L6 完成（需验收通过后标记）
- L6-4 完成后 L6 子任务全部完成，由 PM 执行全量组合关闭验收
- L7 在 L6 关闭前保持未发布
- 真实临床、患者服务、公开生产继续 NO-GO
- G1～G6、EXT-001、EXT-002 继续 `deferred_for_clinical_use`

## 与 L6-1/L6-2/L6-3 的设计一致性

1. **Narration 的确定性**：
   - `narrate()` 必须仅基于 `SandboxMedicalRecordData` 字段的确定性模板拼接
   - 不得使用随机数、时间戳、模型调用或任何非确定性输入
   - 同一 record 在任意时间调用 `narrate()` 必须返回完全相同的字符串

2. **异常复用**：
   - Narration 模块不引入新的异常类型
   - 参数验证失败时复用已有的 `SandboxRecordError`（chainless、payload-free）

3. **Narration 与 Store 的关系**：
   - Narration 不写入 Store，不修改任何状态
   - Narration 是纯函数：输入 record → 输出 str，无副作用

4. **跨层组合**：
   - 最终组合测试只做集成演练，不修改各层已验收的语义
   - 组合测试使用已有 public API 和类，不访问内部实现细节
