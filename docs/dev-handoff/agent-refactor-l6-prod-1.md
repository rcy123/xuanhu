# L6-PROD-1 交付：LangGraph Record 产品接线

## 结论

- 基线：`0a460c1e88f52323b471f20e77db8eca3ee0bf3f`
- 工程状态：**本地工程 accepted**；三个 Codex 子 agent 独立复验的 L6 路径最终 `ACCEPT`。
- 默认发布开关保持关闭；未替换或删除 Legacy Record API。

## 产品合同

- `record` 阶段的 LangGraph `/advance` 执行确定性 Record 节点，不调用模型。
- 唯一输入是 PostgreSQL 中当前 doctor review、当前处方、当前 passed Safety、
  `SafetyRuleRun` 与完成态 `GraphRun` 的版本绑定引用闭包。
- stale、伪造、非 passed、非 confirm/modify 或投影不一致均 fail closed。
- `medical_record` artifact、`record_consistency` gate、`MedicalRecord` 兼容投影、
  session `record -> done`、audit、Outbox 与 Domain commit 原子提交。
- 同一 advance 幂等键可重放，不产生重复病历。
- Graph State 只保存 artifact/gate/command 引用，不保存病历正文或临床快照。
- `/records/latest` 继续读取兼容投影；前端在 record 阶段提供“生成病历”动作并刷新。

## 验证证据

- L6 独立真实集成：2 项通过。
- L5/L6/Recovery 产品联跑纳入最终 non-integration/integration 门禁。
- 最终 integration：`397 passed, 1 xfailed, 2381 deselected in 668.11s`。
- 最终非 integration：`2381 passed, 398 deselected in 168.87s`。
- 前端 `24 files / 187 tests passed in 53.61s`，typecheck、lint、build 通过。
- 全仓 Ruff、`mypy app scripts`（175 files）、lock、diff check 通过。

## 独立复验

- 初审：**REWORK**，P0/P1/P2/P3=`0/1/2/1`；发现
  `SafetyRuleRun`/`DoctorReview` projection closure、claim crash/threat
  matrix 与 manual guard 缺口。
- R1：**ACCEPT**，P0/P1/P2/P3=`0/0/0/0`。
- 最终：**ACCEPT**，P0/P1/P2/P3=`0/0/0/0`。

## 残余边界

- 确定性病历是工程投影，不构成临床病历质量或专业审批。
- product-ready/public/full 与 Legacy removal 继续禁止。
