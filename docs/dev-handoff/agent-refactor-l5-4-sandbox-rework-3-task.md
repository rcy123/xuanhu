# L5-4-R3 current marker 与 challenge issue projection 闭合

## 发布信息

| 项目 | 内容 |
|---|---|
| 状态 | 已发布 / 待交付 |
| 发布人 | Codex（工程项目经理） |
| 初始/R1/R2 失败 deliveries | `d5b8f0e775aac4c9d2ac89d6c9b8c6991a2e186a`、`8b345b9cb807b64409a118d8c18d8ce7b8d41835`、`23a561a5c972cd9fb0103fb7d39ebbe7ad841cbb`；全部保留，不 reset/覆盖 |
| 失败验收 | `ACC-20260722-034`；独立 Reviewer P0=0、P1=0、P2=1、P3=0 |
| 依据 | 原 L5-4/R1/R2 任务书；准入包 §7.6、§7.7、§8、§8.1；`DEC-20260722-027` |
| 执行起点 | 包含本任务书的 clean exact management release HEAD，由项目经理提交后报告 |

## 唯一目标

在原三个 L5-4 文件内，以单一 projection/current predicate 闭合 combined outer revision chain 与 private L5-3 same-scope challenge issue history：outer chain 不能继续引用已经不是 current 的历史 initial/parent challenge，也不能跳过中间同 scope challenge。

不得逐状态堆叠孤立特判；不得修改 accepted L5-1/L5-2/L5-3、配置、依赖、任务书、PM 台账或扩大到 Runtime/HTTP/DB/Gateway/Legacy/外部系统/L6。

## 必须先红

production 保持 `23a561a` 行为时，先在原专项文件新增至少三项回归，记录真实 RED 数字、旧行为与 exact HEAD。构造不一致时必须同步重算受影响的 refs，证明拒绝来自状态关系而非旧 ref。

### 1. only-initial current marker

- 从合法 only-initial `modify_applied` combined snapshot 出发，在 private L5-3 snapshot 中为相同 namespace/session/thread 发布一个新的 pending challenge；保持 outer initial 指向原 historical applied-modify challenge；restore 必须 fixed chainless reject；
- 旧 historical challenge/event 必须 append-only 保留；不能以删除历史记录得到通过。

### 2. terminal parent marker

- 从合法 BLOCK 或其他 terminal combined snapshot 出发，在 private snapshot 中为相同 scope 发布一个新的 pending challenge，使 current marker 不再指向 terminal parent challenge；outer terminal chain 保持不变；restore 必须 fixed chainless reject；
- terminal 自身仍不拥有 challenge；expected current 来自 parent，不得把 terminal absence 误写成同 scope 无 current。

### 3. same-scope issue suffix 无跳项

- 构造合法 initial + current `review_required` outer chain；在 private issue order 中于二者之间插入一个额外 same-scope challenge，最终 current marker 仍指向 child；同步更新 private snapshot 与 outer invalidation 派生 refs；restore 必须 fixed chainless reject；
- 其他 namespace/session/thread scope 的 issue 可以穿插并必须继续被忽略，避免把 private store 误限制为单一 scope。

## 必须修复

### 1. 单一 same-scope issue projection

- 以 initial revision 的 exact challenge 定位其 private checkpoint `issue_sequence`；
- 从该 sequence 起，取相同 namespace/session/thread scope 的全部 challenge refs，必须精确等于 outer revisions 中全部非空 `challenge_ref` 的有序元组，顺序与基数都一致；
- 允许相同 scope 在 initial 之前存在更早历史；允许任意其他 scope 穿插；不使用容器位置替代显式 issue sequence；
- 该关系必须由一个集中 helper/predicate 实现并由 restore 调用，不能分散复制三套状态逻辑。

### 2. 状态驱动的 expected current

- only-initial `modify_applied`：expected current 为 initial challenge/checkpoint；
- current `review_required`：expected current 为当前 revision 自己的 challenge/checkpoint；
- blocked/recheck_failed/review_setup_failed terminal：expected current 为 parent revision 的 challenge/checkpoint；
- private snapshot 对该 scope 的 current marker 必须精确一个，并与 expected challenge/checkpoint 一致；缺失、多余、指向其他历史或后来 issue 均 fixed chainless reject。

## 原不变量与回归

- R2 四项、R1 三项与原 32 项全部保留；专项不得少于 `42` 项；
- shared live/restore command predicate、exact event ownership、terminal same-revision authority absence、source-build single-call atomic commit、normal/BLOCK/evaluation failure/32 并发/restart/receipt/true-max/exact-current/zero-write/AST 均不得弱化；
- accepted L5-1～L5-3、Safety、privacy、L0、Ruff、mypy、lock、双全量与 scope/tracked/clean 全部重跑；
- 全部 fixture 继续 fixed-fictitious/synthetic、inline、offline；不读取 `.env`、ignored `data/`、`.codex_tmp`，不访问外部资源。

## 允许修改范围

- `app/agent_runtime/sandbox_recheck.py`
- `tests/test_l5_4_sandbox_modify_full_recheck.py`
- `docs/dev-handoff/agent-refactor-l5-4-sandbox.md`

除此之外全部禁止。不得修改本 R3 任务书、PM 台账、accepted L5-1/L5-2/L5-3、配置、依赖、锁文件或 L6。

## 验收门禁

沿用 R2 完整 fake env、`UV_OFFLINE=1` 与命令：L5-4、L5-3、L5-2、L5-1、Safety、两项 privacy、Ruff、mypy、L0、AST、`uv lock --check`、双全量、diff/scope/tracked/clean。强制环境唯一既有 defaults 差异必须原样记录；calibrated full 只移除 `APP_ENV`。

独立 Reviewer 必须在 exact delivery commit 亲自复现上述三类状态不一致，并检查 projection/current helper、跨 scope 正例和全部历史 finding；独立 CI 不复用 R2 结果。

## 交付

创建单一 R3 开发提交，exact parent 必须为本任务书发布后的 clean management HEAD，只含原三个允许文件。handoff 追加 R3 RED/GREEN、统一 projection/current 根因关闭、原回归、完整门禁、限制与回退。执行者只能声明“L5-4-R3 已交付，申请验收”；L5 仍为 3/4，L6 未开始，直至独立 Review/CI 与 PM 最终关闭。
