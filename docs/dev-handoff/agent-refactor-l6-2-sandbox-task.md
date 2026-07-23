# L6-2 病历一致性验证（Sandbox）

## 发布信息

| 项目 | 内容 |
|---|---|
| 状态 | 已发布 / 待交付 |
| 发布人 | Codex（工程项目经理） |
| 发布日期 | 2026-07-23 |
| 基线 | `0e4336b`（L6-1 验收提交） |
| 依赖 | L5-PREP-0、L5-1、L5-2、L5-3、L5-4、L6-1 全部 accepted |
| 阻塞 | 无活跃工程阻塞 |
| 交付文件 | `docs/dev-handoff/agent-refactor-l6-2-sandbox-task.md`（本文件） |

## 目标

在个人学习、非临床、仅合成数据沙盒范围内，实现病历子图的第二层：**一致性验证器**。

具体目标：

1. 实现 `SandboxRecordConsistencyVerifier`：
   - 验证 assembled record 的文本关键字段与 JSON 一致
   - 验证 record 中无新症状/诊断/方药（与 source review state 比较）
   - 验证 review confirm ID 与原始 challenge 一致
2. 验证边界：
   - 篡改 `reviewed_formula` 字段 → verifier 固定拒绝
   - 注入新症状/诊断 → verifier 固定拒绝
   - 篡改 `review_confirm_ref` → verifier 固定拒绝
   - 合法 record → verifier 通过
3. 建立 L6-2 专项测试：
   - 合法 record → 通过
   - 字段篡改 → 固定拒绝
   - 注入新内容 → 固定拒绝

## 非目标

- 不实现 persistence / 幂等落盘（属于 L6-3）
- 不实现 narration / 文本润色（属于 L6-4）
- 不修改 L6-1 已 accepted 的 DTO/Assembler 核心逻辑（只允许在 `sandbox_record.py` 内新增 verifier 类）
- 不接入真实 LangGraph `Command`、Runtime、HTTP、容器、部署、DB、RAG、Gateway 或外部服务
- 不连接真实患者数据、真实病历、真实知识库或生产模型日志
- 不生成真实临床诊断、治疗建议、处方或医疗决策
- 不修改 accepted L5-1/L5-2/L5-3/L5-4 生产代码、handoff 或验收记录
- 不修改 Legacy engine/review/record、配置、依赖、前端、UI 或部署
- 不声称临床有效、医疗安全、法规合规或获得专业批准

## 允许修改范围

只允许修改/新增以下文件，全部 tracked：

1. `sandbox_record.py` — 在现有文件内新增 `SandboxRecordConsistencyVerifier` 类（不修改 DTO/Assembler 部分）
2. `tests/test_sandbox_record_l6_2.py` — L6-2 唯一专项测试
3. `docs/dev-handoff/agent-refactor-l6-2-sandbox.md` — 交付 handoff

允许从 `sandbox_review.py`、`sandbox_recheck.py` 读取已 accepted 的类型和常量（只读引用，不修改）。

## 禁止修改范围

- 禁止修改 `sandbox_record.py` 中 L6-1 已验收的 `SandboxMedicalRecordData` 和 `SandboxRecordAssembler` 的任何代码
- 禁止修改 `sandbox_safety.py`（L5-1）、`sandbox_explanation.py`（L5-2）、`sandbox_review.py`（L5-3）、`sandbox_recheck.py`（L5-4）的任何代码、测试或 handoff
- 禁止修改 `pyproject.toml`、`README.md`、配置、依赖、前端、Legacy、Runtime、DB、Gateway、PM 台账
- 禁止修改 L0～L5 任何已验收的管理文档、验收记录、决策记录
- 禁止读取 `.env`、ignored `data/` 或任何外部存储
- 禁止网络调用、子进程、文件写入（专项测试的临时 in-memory store 除外）

## 先红后绿要求

1. 在未修改生产代码时，以真实 RED 证明以下缺口：
   - 无 verifier 时篡改 `reviewed_formula` 被接受
   - 无 verifier 时注入新症状/诊断被接受
   - 无 verifier 时篡改 `review_confirm_ref` 被接受
2. 修复后 GREEN 必须覆盖：
   - 合法 record → verifier 通过
   - 篡改 `reviewed_formula` → verifier 固定拒绝
   - 注入新症状 → verifier 固定拒绝
   - 篡改 confirm ref → verifier 固定拒绝
   - 篡改 `safety_result` → verifier 固定拒绝

## 验收标准

### 独立 Review
- P0/P1/P2/P3 全为 0
- 不修改 accepted L5/L6-1 代码的前提下，L6-2 模块独立可测

### 独立 CI
- L6-2 专项测试全部通过
- L5-1/2/3/4 回归专项全部通过（`14/18/84/60`）
- L6-1 专项 `12 passed`
- Safety `71/3 deselected`、privacy `76`、L0 `131`、Ruff/mypy/lock 全通过
- 校准全量 `1813 passed, 350 deselected`（或当前基线等价）
- scope/tracked/diff/exact/clean 全通过

### PM 探针
- 五项定向探针：
  1. 合法 record → verifier 通过
  2. 篡改 `reviewed_formula` → 固定拒绝
  3. 注入新症状/诊断 → 固定拒绝
  4. 篡改 `review_confirm_ref` → 固定拒绝
  5. 篡改 `safety_result` → 固定拒绝

## 停止条件

- 任何修改超出允许文件范围 → 停止，重新发布
- 任何真实患者/临床数据进入测试 → 立即停止
- 需要修改 L5/L6-1 代码才能通过 → 停止，发布对应 rework 而非在当前任务中修复
- 发现 P0/P1 → 停止交付，发布 bounded rework

## 记录要求

1. 开发交付时更新 `agent-refactor-l6-2-sandbox.md` handoff
2. 不得由开发交付声明替代 PM 验收
3. 验收通过后，PM 追加 `ACC-YYYYMMDD-NNN` 验收记录、更新任务台账和当前状态

## 状态边界

- 本任务发布不等于 L6 完成，也不等于 L6-3/L6-4/L7 授权
- L6-2 完成后由 PM 另行发布 L6-3
- 真实临床、患者服务、公开生产继续 NO-GO
- G1～G6、EXT-001、EXT-002 继续 `deferred_for_clinical_use`
