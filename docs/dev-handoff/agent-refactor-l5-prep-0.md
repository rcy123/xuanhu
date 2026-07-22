# L5-PREP-0 交付与验收

## 1. 发布与范围

| 项目 | 事实 |
|---|---|
| 发布基线 | `ad8cbf038cb3e0a18c9ce40f88d5ee235c04a4d7` |
| 分支 | `codex/l4-5-11-context-privacy-hardening` |
| 发布状态 | `ACC-20260722-015`；bounded 文档/治理任务 |
| 生产代码 | 未修改；accepted production baseline 仍为 `ada23c77` |
| 唯一 writer | 主 Agent / Codex（工程项目经理） |
| 只读输入 | 仓库审计、安全架构、合成数据/测试设计三个子 Agent；均未写文件 |

本交付只覆盖个人学习、非临床、仅合成数据的离线工程沙盒。真实临床、患者服务、公开生产、商业使用、医疗机构接入和人体研究继续 NO-GO。

## 2. 实际交付

### 2.1 新增

- `docs/01_agent部分优化/L5个人学习工程沙盒准入包-2026-07-22.md`
- `docs/dev-handoff/agent-refactor-l5-prep-0-task.md`
- `docs/dev-handoff/agent-refactor-l5-prep-0.md`
- `docs/dev-handoff/agent-refactor-l5-1-sandbox-task.md`

### 2.2 范围注记

- `docs/01_agent部分优化/L5进入前专业安全预审报告-2026-07-19.md`
- `docs/01_agent部分优化/Agent整体大修实施计划-LangGraph版.md`
- `docs/01_agent部分优化/adr/ADR-005-doctor-review-interrupt.md`

只增加当前沙盒适用范围和 superseding 权威链接；原 G1～G6、专业责任、NO-GO 和历史内容未删除、未重写为通过。

### 2.3 管理事务

- `项目管理/00-当前状态.md`
- `项目管理/01-任务台账.md`
- `项目管理/02-阻塞与风险.md`
- `项目管理/03-验收记录.md`
- `项目管理/04-决策记录.md`
- `项目管理/05-文档索引.md`

这六本账原子记录 L5-PREP-0 accepted、离线 L5 工程沙盒 `sandbox_scope_satisfied`、真实/公开轨道 NO-GO，以及 L5-1 Sandbox 任务 published / not implemented。

## 3. 仓库只读审计

### 3.1 数据与凭据

- 当前 tracked tree 未确认真实患者/可识别个人数据、生产日志、聊天记录、数据库 dump 或真实密钥；该结论是静态工作树扫描，不覆盖实际数据库、Docker volume、供应商日志或外部系统。
- tracked 29 条 triage 工程 seed 有版本、非临床 label 和 canonical digest；新 L5 数据政策仍要求补 generator/固定构造、raw digest 和 identifier scan 证据，当前只作既有参考。
- ignored `data/` 的 7 个 JSON 包未命中常见真实姓名/电话/身份证/邮箱/地址/就诊标识，但缺逐包 provenance manifest；全部标记 `not_admitted_provenance_unverified` / `logically_quarantined`，未删除、未提交、不得用于 L5。
- ignored `.env` 含非占位敏感 key 形态和两个非 loopback HTTPS 网关终端；值未输出、服务未连接。它是用户本地敏感配置，L5-PREP/L5-1 禁止加载、使用或提交。
- ignored `.codex_tmp` 属本地复验日志、requirements 和 SBOM；独立脱敏扫描在生成的依赖/包元数据中得到 73 个 phone-like 数字、36 个 ID-like 数字和 99 个 email-like 命中，另有 loopback test/dev 连接串形态。这些更像包元数据/数字误报，但未逐项完成 provenance-aware 分类，因此统一 `not_admitted`，不复制原值，也不声称已证明无个人数据。

### 3.2 运行/部署边界

- `AGENT_RUNTIME_VERSION` 默认 `legacy`，`XUANHU_LANGGRAPH_PUBLIC_ENABLED=False`，前端 LangGraph flag 未设置即关闭。
- 该事实不能扩写为“全部医疗工作流默认关闭”：应用默认监听 `0.0.0.0`，Legacy consult/review/record 等路由仍注册，doctor header 可选且不是认证，Compose 向宿主发布多项端口。
- 因此当前只准入离线、fake/in-memory、零网络的 L5-1 adapter 单元任务。FastAPI、HTTP/E2E、Compose、数据库、RAG、网关、importer 和部署继续 NO-GO。

## 4. 工程设计证据

准入包已经定义：

- intended/excluded use 和所有重新开门触发；
- 五类治理状态与 G0～G6/EXT-001/002 双轨映射；
- 合成数据 manifest、禁止项和疑似真实数据停止/逻辑隔离/授权删除流程；
- L5-1 immutable deterministic adapter、固定 rule bundle、`decision_subject_digest` / `run_envelope_digest`；
- L5-2 explanation 非干预和逐 issue 引用；
- L5-3 Sandbox Reviewer 测试身份、一次性 challenge、expiry/replay/cross-session/CAS；
- L5-4 全产物原子失效、全量重检和 consumer-side completion/export eligibility；
- fail-closed、性能/资源、日志脱敏、回退、P0～P3 和停止条件。

当前 Legacy Safety/Review/Record 缺口被记录为 RED 基线，没有通过文档重解释为已修复。

## 5. 已执行验证

所有 Python 验收均在当前 PowerShell 进程把 DB/Redis/model/embedding 终端覆盖为不可用 loopback fake 值，公共 LangGraph flag 显式为 `false`；未使用本地 `.env`，未发起网络连接。

| 门禁 | 结果 |
|---|---|
| L0 文档契约 + triage seed 契约 | `140 passed in 2.47s` |
| `scripts.evaluate_triage_precheck --fail-on-mismatch` | 29/29 工程期望匹配；canonical SHA-256 `0343df40...e2bbe`；明确 `not_for_clinical_signoff` |
| Markdown 相对链接初检 | 13 个候选/管理文件，44 个相对链接，断链 0 |
| `git diff --check` 初检 | 通过；仅 line-ending 提示，无 whitespace error |

任务最终状态迁移、独立 Review 记录和 L5-1 发布记录写入后，重跑链接、状态一致性、L0、diff/scope/tracked 检查，结果追加在本节下方。

## 6. 已知限制与风险处置

| 风险 | 处置 |
|---|---|
| 现有应用无全局 sandbox hard-off | L5-1 只允许单元级 adapter；runtime/HTTP/E2E/deploy NO-GO；触发即停止 |
| ignored `.env` 具有外部连接能力 | 不使用、不输出、不提交、不擅自删除；所有验收显式 fake 覆盖且禁止网络 |
| ignored RAG fixtures 来源证据不足 | `not_admitted` / 逻辑隔离；L5 禁止读取/import；补齐 manifest 后另行评估 |
| 历史医疗/Doctor 文档可能被误读 | 新准入包和管理索引设为当前沙盒权威；旧专业轨道和门禁明确保留 |
| 合成测试/工程 Review 的证明上限 | 不证明临床有效、真实环境安全、隐私/法律/伦理/监管合规或专业批准 |

## 7. 独立 Review

### 7.1 第 1 轮

- Reviewer 未参与写作或三个只读输入流；在候选冻结后复核 actual diff。
- 结论：P0=0、P1=0、P2=1、P3=1，未通过。
- P2：`.codex_tmp` 的 pattern “无命中”陈述不准确；Reviewer 的脱敏扫描得到 73 phone-like、36 ID-like、99 email-like，当前更像生成包元数据/误报，但未完成 provenance-aware 分类。
- P3：任务书把专业门禁与工程沙盒状态混为只允许两类。
- 修订只记录 pattern 计数、未知性和 not-admitted 处置，并拆分两类状态语义；未改范围或架构。

### 7.2 第 2 轮

- Reviewer 对 refrozen candidate 复核两项修订及新问题。
- 结论：**PASS**；P0=0、P1=0、P2=0、P3=0。
- Reviewer 另确认：13 个变更均为任务允许 Markdown；无代码/测试/依赖/配置/前端/历史变化；无伪造审批或“全部公共/临床默认关闭”误导；G1～G6/EXT 触发完整；L5-1 bounded、offline、unit-only、unimplemented。

## 8. 项目经理验收

| 项目 | 结论 |
|---|---|
| 范围 | 通过；13 个授权 Markdown，生产代码/测试/配置/依赖/data/frontend/history 零变化 |
| 数据/凭据 | 通过；未确认真实患者数据或 tracked secret；ignored 对象均 not admitted，外部存储保持 unknown |
| 架构/测试计划 | 通过；L5-1～L5-4 工程不变量、失效模式、受控矩阵和停止条件完整 |
| 自动化 | L0、seed、evaluator、links、state、diff 通过；纯文档变更无需全量非集成回归 |
| 独立复审 | R1 P2/P3 已关闭；R2 P0/P1/P2/P3 全 0 |
| 专业边界 | 未关闭/伪造 G1～G6 或 EXT；真实/公开轨道继续 NO-GO |
| 最终结论 | **通过 / accepted**；L5 离线工程沙盒 `sandbox_scope_satisfied` |

最终允许表述：

> L5 个人学习工程沙盒准入完成，可以发布 L5-1 离线确定性 adapter 工程任务；真实临床、患者服务、应用 runtime、公开生产和外部连接继续 NO-GO。
