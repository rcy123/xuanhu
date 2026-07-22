# L5-4-R6 review schema authority inheritance 闭合

## 发布信息

| 项目 | 内容 |
|---|---|
| 状态 | 已发布 / 待交付 |
| 发布人 | Codex（工程项目经理） |
| 基线 delivery | R5 `847076e86275edc6a92470dbb749a695d9177757`；保留 |
| 历史单项 acceptance | `be33ffc` / `ACC-20260722-037`；保留但由 final `ACC-20260722-038` reopened |
| 最终组合失败 | exact `be33ffc`；Reviewer P0=0、P1=0、P2=1、P3=0；CI 全绿 |
| 依据 | `DEC-20260722-031`、原 L5-4/R1～R5 任务书与 final finding |
| 执行起点 | 包含本任务书的 clean exact management release HEAD，由项目经理提交后报告 |

## 唯一目标

在原三个 L5-4 文件内建立单一 review schema chain authority：每个 child revision 的 `review_schema_version` 必须精确继承 parent，等价整条 outer chain 继承 initial。revision/challenge 自洽不能替代 parent inheritance。

不实现 schema migration、版本协商或第二套 schema registry；不改 accepted L5-3 固定 schema；不改 R5 authority qualification matrix 或其他已关闭根因。

## 必须先红

production 保持 `847076e` 行为，新增至少两项证据：

1. **重派生 current child 负例**：创建正常 current `review_required` child；把 child 与其 exact private challenge schema 从 v1 同步改为 v2；按 L5-3 canonical 规则重算 challenge ref、受影响 transition refs、checkpoint/current challenge refs，再重算 outer revision/run/invalidation/receipt/current refs。private snapshot 与 outer refs 均自洽，R5 restore 旧行为必须真实接受；测试期望 fixed chainless reject。
2. **结构/链正例**：正常 initial→review-required→terminal 或多 child 链全部保持同一 initial schema并可 restart；existing terminal schema drift、middle identity、R5 matrix、R4 completion、R3 projection/current tests 全部保持。

记录 exact R5 production、真实 RED 数字和 PM 同类复现。不得依赖 stale ref、只改变一侧 schema、删除 private history、skip/xfail 或放宽现有断言。

## 必须修复

- 在 restore 对每个 child 的统一相邻链校验中，无条件要求 `revision.review_schema_version == prior.review_schema_version`；不得只放在 terminal/non-review-required 分支；
- current/historical `review_required` 继续要求 exact challenge 的 `sandbox_schema_version == revision.review_schema_version`；terminal 继续继承 parent；
- 该继承检查必须位于 status 分支之前或由单一纯 helper 表达，结构测试证明所有 child status 共用；
- live 创建路径仍使用 accepted L5-3 固定 schema，不新增迁移/协商；
- 任一 restore 失败保持固定、无动态 payload、无异常链；零部分 mutation。

## 保持与门禁

- 新回归加入后 L5-4 专项不得少于 `49` 项；R5 的 47 项全部保持；
- L5-1～L5-3、Safety、privacy、Runtime/Legacy、public flag、AST、Ruff、mypy、L0、lock、双全量、diff/scope/tracked/clean 全部重跑；
- R6 单项 Reviewer/CI/PM 通过后不得直接关闭 Goal：先创建新的 L5-4 acceptance 管理提交，再从新 clean exact HEAD 重新调用新的最终组合 Reviewer/CI/PM；
- 全部 fixture 固定虚构/合成、inline、offline、in-memory；不读取 `.env`、ignored `data/`、`.codex_tmp`，不访问外部资源。

## 允许修改范围与交付

- `app/agent_runtime/sandbox_recheck.py`
- `tests/test_l5_4_sandbox_modify_full_recheck.py`
- `docs/dev-handoff/agent-refactor-l5-4-sandbox.md`

除此之外全部禁止。创建单一 R6 开发提交，exact parent 必须为本任务书发布后的 clean management HEAD，只含以上三文件。handoff 追加 RED/GREEN、schema chain authority、完整门禁、限制与回退。执行者只能声明“L5-4-R6 已交付，申请验收”；L5 当前 3/4，L6 未开始。
