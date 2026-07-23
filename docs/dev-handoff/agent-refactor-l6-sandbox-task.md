# L6 病历子图（Sandbox）

## 发布信息

| 项目 | 内容 |
|---|---|
| 状态 | 已发布 / 待交付 |
| 发布人 | Codex（工程项目经理） |
| 发布日期 | 2026-07-23 |
| 基线 | `25fc0a1`（L5 关闭管理提交） |
| 依赖 | L5-PREP-0、L5-1、L5-2、L5-3、L5-4 全部 accepted；L5 final R5 `c052c501` engineering complete |
| 阻塞 | 无活跃工程阻塞；AR-B-031 已关闭；R-L5-RESUME-001 / R-L5-RECHECK-001 已关闭 |
| 交付文件 | `docs/dev-handoff/agent-refactor-l6-sandbox-task.md`（本文件） |

## 目标

在个人学习、非临床、仅合成数据沙盒范围内，实现**无 RAG 的病历子图核心骨架**，完成从已确认 review state 到最终病历的确定性组装、验证与持久化路径。

具体目标：

1. 新建强类型 `SandboxMedicalRecordData` DTO，只包含可序列化的固定字段；
2. 实现 `SandboxRecordAssembler`：从已确认的 L5-3/L5-4 review state 确定性构建病历 JSON，不调用模型；
3. 实现 `SandboxRecordVerifier`：验证文本关键字段与 JSON 一致、无新症状/诊断/方药、review ID 一致；
4. 实现 `SandboxRecordPersistence`：session + reviewed revision + version 的幂等落盘（in-memory reference store）；
5. 建立 `SandboxRecordNarration` 占位：只输出允许润色的文本段，不输出 review/formula/安全结论/新建议；
6. 完成 `session.done` 事件适配与 sandbox eligibility probe。

## 非目标

- 不接入真实 LangGraph `Command`、Runtime、HTTP、容器、部署、DB、RAG、Gateway 或外部服务；
- 不连接真实患者数据、真实病历、真实知识库或生产模型日志；
- 不生成真实临床诊断、治疗建议、处方或医疗决策；
- 不实现完整 Narration 模型调用（只留占位接口和固定测试输出）；
- 不接入 Evidence/RAG 增强（属于 L7）；
- 不修改 accepted L5-1/L5-2/L5-3/L5-4 生产代码、handoff 或验收记录；
- 不修改 Legacy engine/review/record、配置、依赖、前端、UI 或部署；
- 不声称临床有效、医疗安全、法规合规或获得专业批准。

## 允许修改范围

只允许新增以下文件，全部 tracked：

1. `sandbox_record.py` — 病历组装、验证与持久化核心模块；
2. `tests/test_sandbox_record.py` — L6 唯一专项测试；
3. `docs/dev-handoff/agent-refactor-l6-sandbox.md` — 交付 handoff。

允许从 `sandbox_review.py` 读取已 accepted 的 review state 类型和常量（只读引用，不修改）。

## 禁止修改范围

- 禁止修改 `sandbox_safety.py`（L5-1）、`sandbox_explanation.py`（L5-2）、`sandbox_review.py`（L5-3）、`sandbox_recheck.py`（L5-4）的任何代码、测试或 handoff；
- 禁止修改 `pyproject.toml`、`README.md`、配置、依赖、前端、Legacy、Runtime、DB、Gateway、PM 台账；
- 禁止修改 L0～L5 任何已验收的管理文档、验收记录、决策记录；
- 禁止读取 `.env`、ignored `data/` 或任何外部存储；
- 禁止网络调用、子进程、文件写入（专项测试的临时 in-memory store 除外）。

## 先红后绿要求

1. 在未修改生产代码时，以真实 RED 证明以下缺口：
   - 无 `SandboxMedicalRecordData` 时 review state 无法转换为病历；
   - 无 assembler 时 JSON 字段缺失或类型错误；
   - 无 verifier 时不一致文本/JSON 被接受；
   - 无 persistence 时重复 done 事件产生重复病历；
   - narration 占位输出超出允许字段范围。
2. 修复后 GREEN 必须覆盖：
   - 合法 review state → 完整病历 JSON；
   - 字段篡改 → verifier 固定拒绝；
   - 重复 done → 幂等返回同一病历；
   - narration 只输出润色文本段，不输出医疗结论；
   - 无 review confirm 时 blocked。

## 验收标准

### 独立 Review
- P0/P1/P2/P3 全为 0；
- 不修改 accepted L5 代码的前提下，L6 模块独立可测；
- review state → record 的转换是确定性的（同输入同输出）。

### 独立 CI
- L6 专项测试全部通过；
- L5-1/2/3/4 回归专项全部通过（`14/18/84/60`）；
- Safety `71/3 deselected`、privacy `76`、L0 `131`、Ruff/mypy/lock 全通过；
- 校准全量 `1801 passed, 362 deselected`（或当前基线等价）；
- scope/tracked/diff/exact/clean 全通过。

### PM 探针
- 六项定向探针：
  1. 合法 confirm review → 完整病历 JSON；
  2. 字段缺失/类型错误 → verifier 固定拒绝；
  3. 重复 done 事件 → 幂等返回同一 version；
  4. narration 输出超出允许范围 → 固定截断或拒绝；
  5. 无 review confirm → blocked / unavailable；
  6. 篡改 review ID → verifier 拒绝。

## 停止条件

- 任何修改超出允许文件范围 → 停止，重新发布；
- 任何真实患者/临床数据进入测试 → 立即停止，按重新开门矩阵处理；
- 需要修改 L5 代码才能通过 → 停止，发布 L5 rework 而非在 L6 中修复；
- 发现 P0/P1 → 停止交付，发布 bounded rework；
- 性能或资源超出沙盒合理范围 → 记录并停止，不优化到生产级别。

## 记录要求

1. 开发交付时更新 `agent-refactor-l6-sandbox.md` handoff，记录：
   - 实际修改文件清单；
   - RED/GREEN 证据；
   - 独立 Review/CI 结果；
   - 未决风险和边界。
2. 不得由开发交付声明替代 PM 验收。
3. 验收通过后，PM 追加 `ACC-YYYYMMDD-NNN` 验收记录、更新任务台账和当前状态。

## 状态边界

- 本任务发布不等于 L6 完成，也不等于 L7/L8/L9 授权；
- L6 完成后仍需 final composition 验收才能标记 L6 accepted；
- 真实临床、患者服务、公开生产、商业/机构接入继续 NO-GO；
- G1～G6、EXT-001、EXT-002 继续 `deferred_for_clinical_use`。
