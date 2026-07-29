# L6-PROD-1：LangGraph 病历生成产品接线任务

## 状态

- 发布日期：2026-07-28
- 状态：已交付 / 工程门禁通过 / 独立复核待完成
- 前置：L5-PROD 本地门禁通过；Claude Code 独立复核仍因外部 403 阻塞
- 默认发布开关：关闭

## 目标

将 L5-PROD 已应用的 `doctor_review`、当前处方与当前 `safety_result` 作为唯一权威输入，确定性生成病历，不调用模型、不读取 `state_snapshot` 中的临床结果、不把临床载荷写入 Graph State。

## 范围

1. `record` 阶段的 LangGraph `/advance` 执行确定性 Record 节点；
2. 校验当前 `doctor_review`、处方、安全结果、SafetyRuleRun 与完成态 GraphRun 的引用闭包；
3. 同一 Domain 事务写入：
   - `medical_record` artifact 与 `record_consistency` gate；
   - `medical_records` 兼容投影；
   - 最小审计/Outbox；
   - session `record -> done` 与 state version；
4. 完成并可重放现有 advance command claim；
5. 前端在 `record` 阶段提供“生成病历”，完成后继续复用现有 Record API/Panel；
6. PostgreSQL/Redis 隔离集成测试覆盖公开 `/advance`、幂等、篡改阻断与重启后读取。

## 明确不做

- 不复用 sandbox 内存 store 作为生产权威；
- 不调用 RecordAgent/LLM 生成自由文本；
- 不删除 Legacy Record API；
- 不开启公开流量开关；
- 不宣称具备临床发布资格。

## 验收

- 只有 confirm/modify 的当前 doctor review 可生成病历；
- safety 不是 passed、引用 stale/伪造、投影不一致时 fail closed；
- artifact、gate、MedicalRecord 投影、session/outbox 原子提交；
- Graph State 仅含 UUID/revision/gate 引用；
- 相同幂等键不产生重复病历；
- `/records/latest` 可读取生成投影；
- 前端测试、后端单元/集成、Ruff、mypy、迁移门禁通过。
