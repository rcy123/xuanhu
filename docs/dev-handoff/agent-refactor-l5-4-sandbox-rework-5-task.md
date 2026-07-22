# L5-4-R5 authority qualification matrix 架构收敛

## 发布信息

| 项目 | 内容 |
|---|---|
| 状态 | 已发布 / 待交付 |
| 发布人 | Codex（工程项目经理） |
| 初始～R4 失败 deliveries | `d5b8f0e`、`8b345b9`、`23a561a`、`b7cbbff`、`c71832b`；全部保留 |
| 失败验收 | `ACC-20260722-036`；独立 Reviewer P0=0、P1=0、P2=1、P3=0 |
| 收敛触发 | R3/R4 连续两轮 scope qualification 调用点漂移；禁止继续单点字段补丁 |
| 依据 | `DEC-20260722-029` 与原 L5-4/R1～R4 任务书 |
| 执行起点 | 包含本任务书的 clean exact management release HEAD，由项目经理提交后报告 |

## 唯一目标

在原三个 L5-4 文件内建立有限、具名、可审计的 authority qualification matrix，使 current、terminal、issue projection 与 historical invalidation 不再借用语义不同的查询 helper。修复 other-scope terminal false reject 只是矩阵收敛的验收样例，不是单行补丁目标。

## 必须先红：关系矩阵

production 保持 `c71832b` 行为，先新增至少三组回归并记录真实 RED/GREEN：

1. **other-scope terminal 正例**：由真实 source-build failure 得到 `review_setup_failed` terminal；为相同 terminal subject/result 在其他 namespace/thread/checkpoint/interrupt 创建合法 source/challenge/current marker；outer chain 与 same-scope parent marker 不变。restart 必须成功且 round-trip 不删记录；R4 旧实现真实拒绝。
2. **same-revision terminal 负例**：对同一 terminal，在它自己的 namespace/session/thread/checkpoint/interrupt 建立 source/challenge；同步保持 private snapshot 合法。restart 必须 fixed chainless reject，证明修复不是完全删除 terminal absence。
3. **historical/current 校准**：R4 other-scope current confirm→eligible、错误 scope/checkpoint blocked、R3 other-scope issue projection 正例均保持；至少增加一个结构或行为断言，证明 historical invalidation helper 没有被 terminal/current 调用，且其既有 collection 语义未改变。

所有 combined snapshot 改动必须保持 private L5-3 snapshot 自身合法并同步必要 derived refs；不得依赖 stale ref、删除记录或宽松基数。

## 必须实现：有限 helper 所有权

### A. same-revision identity

- 单一纯 predicate 比较 source record 与 revision 的 namespace、test session、thread、checkpoint、interrupt、subject；
- `exact-current` source predicate 必须组合该 predicate，再比较 result 与 explanation absence，不复制 scope 字段；
- terminal presence/absence 只使用 same-revision source refs，以及从这些 source refs 派生的 same-revision challenge/event；other scope 不计入，same revision scope 精确计入。

### B. same-scope issue/current

- R3 `_issue_projection_and_current_are_integral(...)` 继续只以 namespace/session/thread 定义 scope；不得升级为 checkpoint scope，也不得纳入其他 scope；
- only-initial/current-review/terminal-parent expected current 规则不变。

### C. historical invalidation

- 将当前宽 `_authority_refs` 明确命名/隔离为 historical invalidation authority collection；其 accepted subject/result collection 语义保持；
- 它只允许用于 live invalidation 生成与 restore 对 `old_challenge_refs/old_event_refs` 的核对；不得用于 initial/current/terminal presence/absence；
- initial/historical review exact event ownership 应直接从 exact challenge/event 证明，不得借 historical helper。

### D. 结构收敛

- production 中每种关系只有一个具名 helper；禁止第二套相同字段子集；
- 测试以 AST/source ownership 或等价行为矩阵证明 historical helper 的允许调用点有限，并证明 exact-current 组合 same-revision predicate；
- 不重写 accepted L5-3 状态机，不引入第二套 decision/digest/异常 authority。

## 保持与门禁

- 新矩阵回归加入后 L5-4 专项不得少于 `47` 项；R4 的 44 项全部保留；
- R4 completion、R3 projection/current、R2 command/state、R1 source-build/revision authority 与原 32 项全部保持；
- L5-3/L5-2/L5-1、Safety、privacy、L0、Ruff、mypy、AST、lock、双全量、diff/scope/tracked/clean 全部重跑；
- 强制 full 既有 defaults 差异和任何偶发结果原样记录；calibrated full 只移除 `APP_ENV`；
- 全部 fixture 固定虚构/合成、inline、offline、in-memory；不读取 `.env`、ignored `data/`、`.codex_tmp`，不访问外部资源。

## 允许修改范围与交付

- `app/agent_runtime/sandbox_recheck.py`
- `tests/test_l5_4_sandbox_modify_full_recheck.py`
- `docs/dev-handoff/agent-refactor-l5-4-sandbox.md`

除此之外全部禁止。创建单一 R5 开发提交，exact parent 必须为本任务书发布后的 clean management HEAD，只含以上三文件。handoff 追加 RED/GREEN、矩阵/helper ownership、门禁、限制与回退。执行者只能声明“L5-4-R5 已交付，申请验收”；独立 Reviewer 必须审查整个关系矩阵而非只复跑 terminal 样例，独立 CI 不复用 R4 结果；L5 保持 3/4，L6 未开始，直至最终关闭。
