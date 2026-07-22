# L5-4-R2 live/restore 预验证与 review 状态族闭合

## 发布信息

| 项目 | 内容 |
|---|---|
| 状态 | 已发布 / 待交付 |
| 发布人 | Codex（工程项目经理） |
| 初始失败 delivery | `d5b8f0e775aac4c9d2ac89d6c9b8c6991a2e186a`；保留 |
| R1 失败 delivery | `8b345b9cb807b64409a118d8c18d8ce7b8d41835`；保留，不 reset/覆盖 |
| 失败验收 | `ACC-20260722-033`；独立 Reviewer P0=0、P1=0、P2=2、P3=0 |
| 依据 | 原 L5-4/R1 任务书；准入包 §7.6、§7.7、§8、§8.1；`DEC-20260722-026` |
| 执行起点 | 包含本任务书的 clean exact management release HEAD，由项目经理提交后报告 |

## 唯一目标

在原三个 L5-4 文件内关闭两个剩余正确性根因族：restore 与 live `apply_revision` 必须复用同一个完整 revision-command 可达性谓词；initial/review-required/terminal 各状态必须用 exact challenge state、exact event ownership 与 private review authority presence/absence 闭合。

不得继续追加零散字段特判；不得修改 accepted L5-1/L5-2/L5-3、配置、依赖、任务书、PM 台账或扩大到 Runtime/HTTP/DB/Gateway/Legacy/外部系统/L6。

## 必须先红

production 保持 `8b345b9` 行为时，先在原专项文件新增下列回归，记录真实 RED 数字、旧行为与 exact HEAD。所有伪造都必须同步重算受影响的 command digest、revision/run/invalidation/receipt refs、父子 link 与 current pointer，证明拒绝来自语义不变量而非 stale ref。

### 1. shared full command prevalidation

- 从合法 unknown-adapter `recheck_failed` snapshot 出发，把 rule bundle adapter 改回 v1、保持 subject adapter v2；完整重派生后 restore 必须 fixed chainless reject，旧代码错误接受；
- 从合法 BLOCK child 出发，把 child checkpoint 与 interrupt 同时改为 parent 值；重建 command digest 和全部 outer refs 后 restore 必须 fixed chainless reject，旧代码错误接受；
- 至少再验证 subject/bundle rule version/digest/evaluator authority 的一致性仍由 shared predicate 覆盖，不能只修 adapter 两字段；
- production 必须把完整纯 predicate 提取到 coordinator 外或 static helper，live 与 restore 都调用同一实现；测试或审查应能证明没有第二套部分条件。

### 2. exact review state family

- 构造一个完全合法的 L5-3 snapshot：同一 subject/result 先有 historical applied `modify_fixture` challenge，再有 current pending challenge；伪造 combined initial revision 指向 pending challenge并重派生 outer ref。restore 必须拒绝，旧代码错误从历史 challenge 借用 modify event；
- 从正常 current `review_required` combined snapshot 出发，把 outer current revision 改为 `review_setup_failed`、清空 challenge ref 并重派生全部 outer refs，但保留 private source/challenge/current marker；restore 必须拒绝，旧代码错误接受；
- initial 必须要求 exact challenge `state=applied`、exact challenge 自己精确一个 `modify_fixture` event；review-required current 可 pending/claimed/applied/expired，historical-with-successor 必须 exact applied modify；
- blocked/recheck_failed/review_setup_failed 必须证明 private snapshot 没有与该 revision exact subject/result 对应的新 source、challenge 或 current marker；不能仅依赖外层 `challenge_ref=None`。

## 必须修复

### 1. 单一 command 可达性谓词

- 将当前 live `_command_is_prevalidated` 的全部条件提取为不访问 coordinator state 的纯函数，输入为 reconstructed `SandboxRevisionCommandV1` 与 parent revision；
- live 在任何写入前调用该函数；restore 对每条 child 重建 canonical command、核对 digest 后调用同一函数；
- predicate 必须包含 session/artifact identity、state/formula `+1`、canonical authority change、subject↔bundle version/digest/evaluator/adapter 对齐、checkpoint/interrupt 相对 parent 都为新值；
- status/result 是否存在不能跳过该 predicate；unknown adapter 只有 subject 与 bundle 一致时才可作为合法 `recheck_failed` 历史。

### 2. 状态驱动的 private authority 校验

- 为 exact revision 派生 source refs、challenge refs、event refs 与 current markers，不用同 subject 的其他 challenge/event 替代 exact ownership；
- initial `modify_applied`：exact challenge 存在且 applied；exact-owned event 精确一个且 action 为 `modify_fixture`；record/challenge/source 全 authority 一致；
- current `review_required`：exact source/challenge/current marker 存在并一致；pending/claimed/applied/expired 由 accepted L5-3 snapshot 决定，completion 仍只认 current applied confirm；
- historical `review_required`：有后继 revision 时 exact challenge 必须 applied，exact-owned event 精确一个 `modify_fixture`；
- terminal no-challenge 状态：同 revision 的 exact source/challenge/current marker 必须为空；历史其他 revision 记录继续 append-only 保留；
- 任一 restore 失败继续 fixed chainless，不自动清理、重写或降级输入。

## 原不变量与回归

- R1 两组回归与原 32 项全部保留：总专项不得少于 35 项加本轮新回归；
- source-build failure 仍只调用一次并固定提交 `review_setup_failed`；terminal schema/middle namespace 重派生拒绝继续成立；
- normal/BLOCK/evaluation failure/32 并发/restart/receipt/true-max/exact-current/zero-write/AST 不得弱化；
- accepted L5-1～L5-3、Safety、privacy、L0、Ruff、mypy、lock、双全量与 scope/tracked/clean 全部重跑；
- 全部 fixture 继续 fixed-fictitious/synthetic、inline、offline；不读取 `.env`、ignored `data/`、`.codex_tmp`，不访问外部资源。

## 允许修改范围

- `app/agent_runtime/sandbox_recheck.py`
- `tests/test_l5_4_sandbox_modify_full_recheck.py`
- `docs/dev-handoff/agent-refactor-l5-4-sandbox.md`

除此之外全部禁止。不得修改本 R2 任务书、PM 台账、accepted L5-1/L5-2/L5-3、配置、依赖、锁文件或 L6。

## 验收门禁

沿用 R1 完整 fake env、`UV_OFFLINE=1` 与命令：L5-4、L5-3、L5-2、L5-1、Safety、两项 privacy、Ruff、mypy、L0、AST、`uv lock --check`、双全量、diff/scope/tracked/clean。强制环境唯一既有 defaults 差异必须原样记录；calibrated full 只移除 `APP_ENV`。

独立 Reviewer 必须亲自重放上述四类重派生语义不一致，并审查 shared predicate 与全状态矩阵；独立 CI 不复用 R1 结果。

## 交付

创建单一 R2 开发提交，exact parent 必须为本任务书发布后的 clean management HEAD，只含原三个允许文件。handoff 追加 R2 RED/GREEN、两项根因族关闭、原回归、完整门禁、限制与回退。执行者只能声明“L5-4-R2 已交付，申请验收”；L5 仍为 3/4，L6 未开始，直至独立 Review/CI 与 PM 最终关闭。
