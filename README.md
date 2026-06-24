# 悬壶 Xuanhu

悬壶是面向中医师的 B 端问诊辅助工作台。MVP 目标是跑通“问诊 -> 完备性判断 -> 辨证 -> 开方 -> 加减 -> 安全审核 -> 医师确认 -> 病历生成”的闭环，系统只做辅助决策，不替代医师诊断。

当前仓库处于研发基线确认阶段，已补齐产品、架构、接口、数据库、安全规则、UI 和交付说明。代码实现启动后，应以 `docs` 下文档作为研发基线。

## 核心边界

- 医师确认是病历生成前的必经点。
- 安全审核优先于体验；`blocker` / `high` 问题必须阻断，不提供“接受风险继续”。
- 妊娠状态 `pregnant` 与 `possible` 均按严格妊娠禁忌规则处理。
- 生产模型调用统一走内网模型网关，使用 `MODEL_GATEWAY_*` 配置口径。
- MVP 不接 HIS / EMR，不做自动处方签发。

## 文档导航

| 文档 | 用途 |
|---|---|
| [产品设计文档](docs/产品设计文档.md) | 产品定位、MVP 范围、交付清单 |
| [PRD](docs/prds/xuanhu/PRD.md) | 阶段计划、用户故事、验收策略 |
| [系统概设](docs/系统概设.md) | 总体架构、模块边界、部署形态 |
| [多 Agent 架构设计](docs/多Agent架构设计.md) | Agent 职责、State、回退机制 |
| [接口设计文档](docs/接口设计文档.md) | REST / SSE / 错误码 / 内部接口 |
| [详细设计文档](docs/详细设计文档.md) | 代码结构、数据模型、核心流程 |
| [数据库设计文档](docs/数据库设计文档.md) | PostgreSQL / Milvus / Redis 设计 |
| [安全审核规则设计文档](docs/安全审核规则设计文档.md) | 禁忌、剂量、妊娠、阻断规则 |
| [UI 设计文档](docs/UI设计文档.md) | 工作台页面、阶段展示、确认区 |
| [部署指南](docs/部署指南.md) | 环境变量、Docker Compose、健康检查 |
| [使用指南](docs/使用指南.md) | 医师使用流程与安全提示 |
| [知识库数据说明](docs/知识库数据说明.md) | 数据文件、字段规范、导入校验 |

## 数据样例

`data/` 目录包含 MVP 知识库样例数据和导入说明：

- `sample_formulas.json`
- `sample_herbs.json`
- `sample_dosage_units.json`
- `sample_acupoints.json`
- `sample_theory.json`
- `sample_cases.json`
- `rag_eval_queries.json`
- `import_commands.md`

## 研发进入条件

进入前后端并行开发前，应确认：

- 文档状态统一为“研发基线确认版”。
- P0 安全绕过冲突已消除。
- `PatientInfo.pregnancy_status` 枚举在接口、详细设计、安全规则中一致。
- 首批迁移包含 `dosage_units` 表。
- SSE `review.required` payload 使用 `modified_formula`。
