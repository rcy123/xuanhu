# L5-4-R1 revision authority 与 review-setup 原子性限定返工

## 发布信息

| 项目 | 内容 |
|---|---|
| 状态 | 已发布 / 待交付 |
| 发布人 | Codex（工程项目经理） |
| 原 release | `7ee8286ce56c406a392468d21d66a565155ce9f0` |
| 失败 delivery | `d5b8f0e775aac4c9d2ac89d6c9b8c6991a2e186a`；保留，不 reset/覆盖 |
| 失败验收 | `ACC-20260722-032`；独立 Reviewer P0=0、P1=0、P2=2、P3=0 |
| 依据 | 原 L5-4 任务书；L5 准入包 §7.6、§7.7、§8、§8.1；`DEC-20260722-025` |
| 执行起点 | 包含本任务书的 clean exact management release HEAD，由项目经理提交后报告 |

## 唯一目标

在原三个 L5-4 文件内关闭两项已复现 P2：combined snapshot/restart 必须逐 revision 闭合 identity、schema 与 review authority；ALLOW review setup 的 source 只构建一次，任一步 setup failure 都必须固定提交已接受 modification 的 invalidation，而不是泄露动态异常或回到旧 revision。

不得重写 L5-1/L5-3、放宽原 32 项、删除失败历史、扩大到 Runtime/HTTP/DB/Gateway/Legacy/外部系统或开始 L6。

## 必须先红

生产代码保持 `d5b8f0e` 行为时，先在原专项文件新增并运行以下两组回归；handoff 必须记录 exact management release、真实失败数与每项旧行为。禁止先改 production、skip 或 xfail。

### 1. revision authority restore

- 构造合法 BLOCK terminal revision；仅把其 `review_schema_version` 改为未知值，并重算 revision ref、run/invalidation/receipt link/ref 与 current pointer，使所有局部 derived refs 形式正确；restore 必须 fixed chainless reject，旧代码错误接受；
- 构造至少三 revision 链：第一和第二个新 challenge 均以 `modify_fixture` applied 后继续 N→N+1；改变中间 revision 的 namespace，再重算从该 revision 起受影响的 revision/run/invalidation/receipt refs 与 current pointer；restore 必须 fixed chainless reject，旧代码错误接受；
- 同组还要覆盖 test-session/subject 对齐、thread 继承、review-required challenge/schema/source/event 精确关联，以及 terminal no-challenge schema 继承；不能只测未重算 ref 的浅层损坏。

### 2. source-build failure atomicity

- 在 valid ALLOW modification 上让 `SandboxReviewSourceV1.build(...)` 抛出包含动态文本的异常；public `apply_revision(...)` 不得抛出该异常，必须返回固定 `review_setup_failed`；旧代码因保护范围外第二次 build 原样抛出并保持旧 snapshot；
- 新 snapshot 必须精确新增一条 revision/run/invalidation/receipt、切换 current、保存完整新 safety result、`challenge_ref=None`、`review_render_digest=None`，旧 challenge/event refs 全部进入 invalidation，completion blocked；
- fake restart 必须恢复相同 terminal 状态；exact command retry 固定 `replayed_or_conflict`，不新增记录或 nonce；错误消息、cause/context 和 snapshot 不含动态异常文本。

## 必须修复

### 1. 逐 revision authority 与 cross-reference

- 每个 child revision 的 namespace、test session 与 thread 必须和 parent 精确继承；record test session 必须和 subject test session 相等；artifact identity、state/formula `+1` 与 canonical authority change 保持原合同；
- `review_required` 必须在 private review snapshot 中定位其 exact source、challenge、schema 和 current/historical event：当前可 pending/applied，若已有后继 revision 则其唯一 applied action 必须为 `modify_fixture`；
- `blocked`、`recheck_failed`、`review_setup_failed` 没有新 challenge，必须继承前一已验证 review schema；不得接受 caller 或 snapshot 自报的新 schema；
- invalidation 必须仍等于该 parent revision 在完整 review snapshot 中派生出的全部 challenge/event refs；删除两边再重算不能消除历史 authority；
- restore 任一失败必须在 store 可用前 fixed chainless reject，不自动修补、降级或丢弃历史。

### 2. 单次 source build 与已接受 modification 的固定提交

- ALLOW 路径只允许调用 `SandboxReviewSourceV1.build(...)` 一次；成功时保存该对象及其 `review_render_digest`，后续 private store/challenge 均复用该 validated source；
- source build、copy-on-write store、review coordinator 或 challenge creation 任一步失败，统一进入 `review_setup_failed`，不得在异常保护范围外重复执行失败步骤；
- source 尚未成功时 revision 的 `review_render_digest=None`；source 已成功而后续失败时可保存由该 source 得出的 digest，但不得发布 partial new review snapshot/challenge；
- 不论 setup 在哪一步失败，revision/run/invalidation/receipt/current 必须一次性提交，旧 review authority 不能恢复；public status/error 固定且不携带动态文本。

## 原不变量与回归

- 原 32 项必须全部保持；normal/BLOCK/evaluation-failed/review-setup-failed、true-max、32 并发、restart/receipt、zero-write invalid input、exact-current completion 与 AST 边界均不得弱化；
- accepted L5-1/L5-2/L5-3 回归、Safety、privacy、L0、Ruff、mypy、lock、双全量与 exact scope/tracked/clean 沿用原任务书；
- 强制 `APP_ENV=sandbox-test` 的唯一既有 defaults 差异原样记录；calibrated full 只移除 `APP_ENV` 并保留全部 fake endpoints/`UV_OFFLINE=1`；
- 全部 fixture 继续 inline fixed-fictitious/synthetic；禁止读取 `.env`、ignored `data/`、`.codex_tmp`，禁止网络、服务、文件数据、wall-clock wait 或外部连接。

## 允许修改范围

- `app/agent_runtime/sandbox_recheck.py`
- `tests/test_l5_4_sandbox_modify_full_recheck.py`
- `docs/dev-handoff/agent-refactor-l5-4-sandbox.md`

除此之外全部禁止。不得修改本 R1 任务书、PM 台账、accepted L5-1/L5-2/L5-3、配置、依赖、锁文件、Runtime、Legacy 或 L6。

## 验收门禁

使用原任务书完整 fake env 与 `UV_OFFLINE=1`：

```powershell
uv run pytest tests/test_l5_4_sandbox_modify_full_recheck.py -q
uv run pytest tests/test_l5_3_sandbox_reviewer_interrupt_resume.py -q
uv run pytest tests/test_l5_2_sandbox_safety_explanation.py -q
uv run pytest tests/test_l5_1_sandbox_safety_adapter.py -q
uv run pytest tests/test_safety_rule_engine.py -q -rs
uv run pytest tests/test_l4_5_11_1_intake_privacy_projection.py tests/test_l4_5_11_2_runtime_privacy_guard.py -q -rs
uv run ruff check app/agent_runtime/sandbox_recheck.py tests/test_l5_4_sandbox_modify_full_recheck.py
uv run mypy app/agent_runtime/sandbox_recheck.py
uv run pytest tests/test_l0_1_contract.py -q -rs
uv lock --check
uv run pytest -q
```

另运行专项 AST、`git diff --check`、exact scope/tracked/clean。独立 Reviewer 必须重新执行原两项定向正确性复现并审查多 revision 继承族，而不是只确认新增测试通过。

## 交付

创建单一 R1 开发提交，exact parent 必须为本任务书发布后的 clean management HEAD，提交只含原三个允许文件。handoff 追加 R1 真实 RED/GREEN、两项 finding 关闭、原 32 项、完整门禁、限制与回退。执行者只能声明“L5-4-R1 已交付，申请验收”；不得声明 accepted、clinical approved 或 production ready。L5 仍为 3/4，L6 未开始，直至独立 Review/CI 与 PM 最终关闭。
