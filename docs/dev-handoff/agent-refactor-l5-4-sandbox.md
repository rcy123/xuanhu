# L5-4 修改后全量重检与旧 review 失效（Offline Sandbox）交付

## 1. 交付状态与执行起点

- 状态：**L5-4 已交付，申请验收**；执行者不声明 accepted、clinical approved 或 production ready。
- 分支：`codex/l4-5-11-context-privacy-hardening`。
- 包含已发布任务书的 clean exact release HEAD / 本交付 exact parent：`7ee8286ce56c406a392468d21d66a565155ce9f0`。
- 范围保持为固定虚构、合成、纯离线、单元测试与 in-memory reference coordinator；未读取 `.env`、ignored `data/` 或 `.codex_tmp`，未启动 Runtime、HTTP、容器、数据库、队列、Gateway、LangGraph、Legacy 或外部服务。
- 本交付没有修改 L5-1～L5-3、配置、依赖、锁文件、PM 台账或任务书。

## 2. 真实 collection RED 与开发过程

生产模块不存在、工作区唯一变更为新增专项测试时，在第 7 节完整 fake 环境与 `UV_OFFLINE=1` 下运行：

```powershell
uv run pytest tests/test_l5_4_sandbox_modify_full_recheck.py -q
```

真实结果为退出码 `2`，`collected 0 items / 1 error`；顶层导入固定失败为：

```text
ModuleNotFoundError: No module named 'app.agent_runtime.sandbox_recheck'
```

当时 exact HEAD 为 `7ee8286...`，production module 尚不存在；没有空模块、skip、xfail、条件绕过或先写实现。

实现后的首批 2 项为 `2 passed`。扩大完整性覆盖后真实出现 `4 failed, 16 passed`：graph、rule bundle、dataset 与未知 adapter 变更已通过入口预验证，但组合快照仍只按 formula/profile 内容判断 authority 是否变化。修复为单一 `_authority_changed(...)` 规范后 `20 passed`；继续补齐初始状态、失败重启、回执恢复、zero-write schema/digest/bundle 和组合快照逐项不一致场景，最终为 `32 passed`。失败历史如实保留，没有弱化断言。

## 3. 实现摘要

`app/agent_runtime/sandbox_recheck.py` 新增 strict frozen L5-4 reference contract：

- `SandboxRevisionCommandV1` 只接收 expected current ref、唯一 command/run/trace ID、完整 candidate `SandboxSafetySubjectV1`、完整 `SandboxRuleBundleV1` 与新 checkpoint/interrupt；入口使用 canonical round-trip strict 重解析，不信任 caller 自报状态；
- revision/run/invalidation/receipt 全部 append-only，sequence 连续，引用由完整 canonical content 派生；combined snapshot 绑定唯一 current revision pointer 与完整 private L5-3 snapshot；
- session 和 formula/profile artifact identity 保持不变，domain state 与 formula revision 必须精确 `+1`；formula/profile 内容或 revision、graph、adapter、rule bundle、evaluator、dataset、manifest 任一 authority 改变才可接受；
- pre-validation 的 missing/extra/schema/digest/跳号/同内容/bundle 不一致固定、chainless 拒绝，snapshot 逐字不变；旧 expected ref、exact retry 或 ID 冲突固定返回 `replayed_or_conflict`；
- 一个 coordinator-owned `RLock` 包围 adapter、private review store/coordinator、snapshot/restart、stage/resume 与 completion probe；不暴露 private store/coordinator 引用；
- 接受 modification 后，同一事务追加 revision/run/invalidation/receipt、切换 current，并记录旧 subject/result/explanation/review-render digests 与全部同 revision challenge/event refs；旧记录不删除、不覆盖；
- production 直接调用 accepted `SandboxSafetyRuleAdapter().evaluate(...)`，输入为完整 candidate subject 和完整 bundle；不注入结果 port，不复用旧 result/issues，不做增量规则子集；
- `ALLOW` 在 private copy-on-write L5-3 store 创建唯一新 challenge，返回一次 plaintext nonce 并进入 `review_required`；`BLOCK` 保存完整新 result 且不创建 challenge；
- modification 已接受后，评估运行失败或 review 初始化失败仍提交新 revision 与旧 authority invalidation，分别进入 `recheck_failed` / `review_setup_failed`，completion 始终 blocked；输入 schema/digest/limit 等入口无效仍保持 zero-write；
- snapshot restore 在 outer lock 内重建完整记录、交叉引用、private L5-3 store，并对每个已保存新 result 用其自身 subject/bundle/command/run/trace 完整重算；任一不一致固定 chainless 拒绝；
- `stage_current_review(...)` / `resume_current_review(...)` 只继续当前 `review_required` challenge；`completion_eligibility(...)` 每次重读 exact-current revision/result/source/marker/checkpoint/challenge/event，仅当前 `ALLOW + confirm` 返回 eligible；
- 不提供 `complete`、`record`、`done`、`export` 或副作用方法。

## 4. 状态机、原子性与恢复证据

```text
current applied modify_fixture
        |
        +-- invalid command ----------------------> fixed reject / zero write
        |
        +-- accepted revision N -> N+1
                |
                +-- full result BLOCK ------------> blocked
                +-- full result ALLOW + challenge -> review_required
                |                                  +-- confirm -> eligible
                |                                  +-- reject/pending/modify -> blocked
                +-- evaluation failure -----------> recheck_failed
                +-- review setup failure ----------> review_setup_failed
```

- 32 个相同 expected-current 命令并发时精确 `1 review_required + 31 replayed_or_conflict`，只新增一组 revision/run/invalidation/receipt、一个 challenge 和一次新 nonce delivery；exact retry 不再发 nonce。
- pending、confirmed、BLOCK、unknown-adapter failure、review-setup failure 均可由 combined snapshot fake restart；回执保持幂等，nonce factory 调用数不增加。
- 当前 review 为 `confirm` 后不可直接再次 revision；只有当前 challenge 的唯一 applied action 为 `modify_fixture` 才能进入下一次 N→N+1。
- 64 formula items + 256 issues 的 true-max 使用 accepted adapter 完整重检，256 个 issues 全部保留，新 result 的 subject digest 精确绑定 candidate。
- current pointer、run cardinality/sequence、receipt link、invalidation challenge refs、command identity/cross-reference 任一不一致，即使相关 derived ref 随伪造内容重算，restore 仍固定 chainless 拒绝。

## 5. 最终门禁证据

除 calibrated full 只移除 `APP_ENV` 外，均使用第 7 节完整 fake 环境与 `UV_OFFLINE=1`。

| 门禁 | 最终结果 |
|---|---|
| L5-4 专项 | `32 passed in 2.81s` |
| accepted L5-3 + L5-2 + L5-1 合并回归 | `91 passed in 18.24s`（59 + 18 + 14） |
| Safety 回归（仓库实际合同文件） | `71 passed, 3 deselected in 1.91s` |
| L4.5-11 privacy 回归（仓库实际合同文件） | `76 passed in 4.98s` |
| Ruff | `All checks passed!` |
| mypy | `Success: no issues found in 1 source file`；只有既有 `pymilvus.*` unused-section note |
| L0 | `131 passed in 2.15s` |
| 强制 `APP_ENV=sandbox-test` 全量 | `1 failed, 1747 passed, 362 deselected in 148.20s`；唯一失败为既有 `tests/test_config.py::test_load_with_defaults` 期望 `local` |
| 只移除 `APP_ENV` 的校准全量 | `1748 passed, 362 deselected in 146.95s` |

L5-4 任务书门禁段误列了当前分支不存在的 `test_agent_graph_safety*.py` 与 `test_context_privacy*.py` 文件名；该命令在 collection 前以 file-not-found 退出，没有被计作测试证据。按 accepted L5-1～L5-3 任务书与 handoff 的实际合同文件，重新运行 `tests/test_safety_rule_engine.py` 以及 `tests/test_l4_5_11_1_intake_privacy_projection.py`、`tests/test_l4_5_11_2_runtime_privacy_guard.py`，结果如上。没有创建占位文件或伪造通过。

## 6. 专项覆盖

最终 32 项覆盖：

1. 正常修改、旧 result/explanation/challenge/event/completion authority invalidation、新完整 result/challenge 与 review 前 blocked；
2. 当前 confirm 后 exact-current eligible，旧 checkpoint 始终 blocked，副作用能力不存在；
3. BLOCK 无新 challenge、评估失败和 review-setup 失败均保持旧 authority 已失效；
4. formula、profile、graph、rule bundle、dataset 与 adapter authority 变化；未知 adapter fixed fail closed；
5. true-max 64 formula + 256 issues 完整重检；
6. missing/extra、revision 跳号、同内容、旧 expected ref、digest 与 bundle 不一致的 zero-write；
7. 32-thread 单次 accepted、exact retry 与 command/run/trace 单次使用；
8. pending/confirmed/blocked/failed restart、receipt 恢复与 nonce 不重发；
9. combined snapshot derived refs、sequence、cardinality、cross-reference、current pointer 与 invalidation set 不一致拒绝；
10. strict frozen DTO、输入/输出 copy isolation、fixed chainless error；
11. AST 离线 import/capability 边界与 public contract importability。

## 7. 受控 fake 环境

```powershell
$env:APP_ENV='sandbox-test'
$env:DB_URL='postgresql://sandbox:sandbox@127.0.0.1:9/sandbox'
$env:REDIS_URL='redis://127.0.0.1:9/0'
$env:MODEL_GATEWAY_BASE_URL='http://127.0.0.1:9/v1'
$env:MODEL_GATEWAY_API_KEY='sandbox-test-key-not-a-secret'
$env:EMBEDDING_GATEWAY_BASE_URL='http://127.0.0.1:9/v1'
$env:EMBEDDING_GATEWAY_API_KEY='sandbox-test-key-not-a-secret'
$env:CHAT_MODEL='sandbox-test-model'
$env:EMBEDDING_MODEL='sandbox-test-embedding'
$env:EMBEDDING_DIM='8'
$env:AGENT_RUNTIME_VERSION='legacy'
$env:XUANHU_LANGGRAPH_PUBLIC_ENABLED='false'
$env:UV_OFFLINE='1'
```

所有 fixture 均为测试内 inline 的固定虚构/合成技术数据。没有真实个人、患者、临床、公开生产或人体研究数据；没有文件数据源、网络、wall-clock wait 或外部系统访问。

## 8. 精确范围、限制、提交与回退

本交付只新增：

- `app/agent_runtime/sandbox_recheck.py`
- `tests/test_l5_4_sandbox_modify_full_recheck.py`
- `docs/dev-handoff/agent-refactor-l5-4-sandbox.md`

限制：in-memory snapshot 只模拟 restart，不提供进程外 durability；clock、nonce factory 与 signature verifier 是注入的测试边界；本模块不是 Runtime、API、持久层、真实 completion/export 或专业准入实现。

使用单一开发提交，消息为 `feat: add L5-4 offline full recheck coordinator`，exact parent 必须为 `7ee8286ce56c406a392468d21d66a565155ce9f0`。Git SHA 不能在包含本文的同一提交内自引用；冻结后由 `git rev-parse HEAD` 与独立 Reviewer/CI/PM 验收记录锚定。若验收失败，以该单一交付提交执行 `git revert <delivery-commit>` 并保留全部证据，不 reset 或覆盖历史。

## 9. 第 1 次独立验收（未通过）

- 冻结 delivery：`d5b8f0e775aac4c9d2ac89d6c9b8c6991a2e186a`；exact parent `7ee8286ce56c406a392468d21d66a565155ce9f0`；只新增第 8 节三个文件；Review/CI 前后 clean。
- 独立 CI：L5-4 `32`、L5-3 `59`、L5-2 `18`、L5-1 `14`、Safety `71 passed, 3 deselected`、privacy `76`、L0 `131`、Ruff/mypy/lock/AST/diff/scope/tracked/clean 全通过；校准全量 `1748 passed, 362 deselected`；强制环境为 `1 failed, 1747 passed, 362 deselected` 且仅既有 defaults 差异。
- 独立 Reviewer：P0=0、P1=0、P2=2、P3=0，结论 rework required。
- P2-1：combined restore 没有逐 revision 验证 namespace/session/thread/review schema 的继承和 challenge 关联；在重派生相关 refs 后，terminal blocked schema 或三 revision 链中间 namespace 可漂移并被接受。
- P2-2：review source/challenge setup 异常被捕获后，revision render digest 路径在保护范围外再次调用 source build；source build failure 会返回动态异常且不提交已接受 modification 的 invalidation。
- PM 结论：**未接受 / 发布 L5-4-R1 限定返工**（`ACC-20260722-032`、`DEC-20260722-025`）。保留本 delivery、RED/GREEN、CI 与 findings；L5 仍为 3/4，L6 未开始。

## 10. L5-4-R1 revision authority 与 review-setup 原子性返工

### 10.1 状态与起点

- 状态：**L5-4-R1 已交付，申请验收**；不声明 accepted、clinical approved 或 production ready。
- clean management release / exact parent：`9382fc7e411d90530cb4abb93479272d8655b7dd`；其中保留失败 delivery `d5b8f0e`、`ACC-20260722-032`、`DEC-20260722-025` 与 R1 任务书，没有 reset、amend、覆盖或删除失败证据。
- R1 只修改原三个 L5-4 文件；未修改任务书、PM 台账、accepted L5-1/L5-2/L5-3、配置、依赖、锁文件、Runtime 或外部边界。

### 10.2 真实 R1 RED

production 保持 `d5b8f0e` 行为、工作区唯一修改为两组 R1 回归时，使用完整 fake env 与 `UV_OFFLINE=1` 运行两个测试节点（两个 authority 参数子例 + 一个 source-build 子例），结果为退出码 `1`、`3 failed in 2.24s`：

1. terminal BLOCK revision 的 `review_schema_version` 改为 `forged-review-schema.v9`，同步重算 revision/run/invalidation/receipt refs 与 current pointer 后，restore 错误接受；
2. 三 revision 链中间 revision namespace 改为其他值，完整重算后续 command digest、revision/run/invalidation/receipt refs 与 current pointer 后，restore 错误接受；
3. `SandboxReviewSourceV1.build` 抛出含动态文本的 `RuntimeError` 时，异常保护范围外的第二次 build 原样抛错，snapshot 没有新增 revision/invalidation。

没有先改 production、skip、xfail、浅层旧-ref 特判或弱化原 32 项。

### 10.3 finding 关闭

- 新 `_challenge_matches_revision(...)` 把 exact challenge 与 revision 的 schema、namespace/session/thread、checkpoint/interrupt、state/formula、adapter/graph、subject/result/rule/dataset/render digests 全部绑定；
- 每个 child revision 额外验证 record/subject test session 对齐，以及 namespace/test-session/thread 从 parent 精确继承；terminal no-challenge 状态只能继承 parent review schema；
- historical `review_required` revision 必须仍能在完整 private review snapshot 定位 exact source/challenge，且其后继存在时唯一 event 必须是 applied `modify_fixture`；删除 review history 与 invalidation 两侧后重算不能恢复；
- ALLOW 路径只构建一次 `SandboxReviewSourceV1`，成功时缓存其 render digest；source/store/coordinator/challenge 任一步失败均不重复调用，统一进入 `review_setup_failed`；
- source build 尚未成功时 revision 保存完整新 safety result、`review_render_digest=None`、`challenge_ref=None`，仍原子追加 revision/run/invalidation/receipt、切换 current、阻断 completion；restart 与 exact retry 保持固定、幂等且无动态异常文本。

两组 R1 回归最终 `3 passed in 1.89s`；完整专项扩展为 `35 passed in 2.70s`，原 32 项全部保持。

### 10.4 R1 最终门禁

| 门禁 | R1 结果 |
|---|---|
| L5-4/R1 专项 | `35 passed in 2.70s` |
| accepted L5-3 + L5-2 + L5-1 合并回归 | `91 passed in 19.27s`（59 + 18 + 14） |
| Safety 回归 | `71 passed, 3 deselected in 1.78s` |
| L4.5-11 privacy 回归 | `76 passed in 4.60s` |
| Ruff | `All checks passed!` |
| mypy | `Success: no issues found in 1 source file`；只有既有 `pymilvus.*` unused-section note |
| L0 | `131 passed in 2.35s` |
| AST 离线边界 | `1 passed in 1.90s` |
| `uv lock --check` | `Resolved 84 packages in 3ms`；lock 未改变 |
| 强制 `APP_ENV=sandbox-test` 全量 | `1 failed, 1750 passed, 362 deselected in 147.83s`；唯一为既有 `test_load_with_defaults` 默认值差异 |
| 只移除 `APP_ENV` 的校准全量 | `1751 passed, 362 deselected in 148.32s` |

### 10.5 提交、限制与回退

- 单一 R1 开发提交消息为 `fix: close L5-4 revision authority gaps`；exact parent 必须为 `9382fc7e411d90530cb4abb93479272d8655b7dd`；只含本 handoff 第 8 节三个文件。
- Git SHA 无法在包含本文的同一提交中自引用；冻结后以 `git rev-parse HEAD`、提交消息和本节约定共同报告，由独立 Reviewer/CI/PM 锚定。
- 边界仍为 fixed-fictitious/synthetic、offline unit/in-memory；不提供进程外 durability、Runtime、HTTP、DB、真实 completion/export、临床或公开生产能力。
- 若 R1 验收失败，以单一 R1 delivery 执行 `git revert <r1-delivery-commit>` 并保留全部历史，不 reset 或覆盖。

## 11. L5-4-R1 独立验收（未通过）

- 冻结 R1 delivery：`8b345b9cb807b64409a118d8c18d8ce7b8d41835`；exact parent `9382fc7e411d90530cb4abb93479272d8655b7dd`；只修改三个允许文件；Review/CI 前后 clean。
- 独立 CI：专项 `35`、L5-3 `59`、L5-2 `18`、L5-1 `14`、Safety `71/3 deselected`、privacy `76`、L0 `131`、Ruff/mypy/lock/AST/diff/scope/tracked/clean 全通过；校准全量 `1751 passed, 362 deselected`；强制环境仅既有 defaults 差异。
- 独立 Reviewer：P0=0、P1=0、P2=2、P3=0；R1 原 terminal schema/middle identity drift 与 source-build fixed atomic commit 已 resolved。
- 新 P2-1：restore 没有复用 live 完整 command predicate；可接受 subject/bundle adapter mismatch 的 resultless terminal，或复用 parent checkpoint/interrupt 的 child。
- 新 P2-2：initial event 未绑定 exact challenge/applied state；terminal outer record 可清空 challenge ref、改 status 后继续借用 private snapshot 中同 revision source/challenge/current marker。
- PM 结论：**未接受 / 发布 L5-4-R2 限定返工**（`ACC-20260722-033`、`DEC-20260722-026`）。保留初始/R1 deliveries 与全部证据；L5 仍为 3/4，L6 未开始。

## 12. L5-4-R2 live/restore 预验证与 review 状态族返工

### 12.1 状态与起点

- 状态：**L5-4-R2 已交付，申请验收**；执行者不声明 accepted、clinical approved 或 production ready。
- clean management release / exact parent：`84c8f6459ac086457dfb9218575ac2360c4068af`；其中保留初始/R1 失败 deliveries、`ACC-20260722-032/033`、`DEC-20260722-025/026` 与两份返工任务书。
- R2 只修改原三个 L5-4 文件；没有修改 R2 任务书、PM 台账、accepted L5-1/L5-2/L5-3、配置、依赖、锁文件、Runtime 或外部边界。

### 12.2 真实 R2 RED

production 保持 `8b345b9` 行为、工作区唯一修改为四类 R2 回归时，使用完整 fake env 与 `UV_OFFLINE=1` 运行三个测试节点（一个双参数），结果为退出码 `1`、`4 failed in 2.32s`：

1. 合法 unknown-adapter `recheck_failed` snapshot 的 bundle adapter 改回 v1，重建 command digest 与全部 outer refs 后 restore 错误接受 subject/bundle mismatch；
2. 合法 BLOCK child 的 checkpoint/interrupt 改为 parent 值并重派生完整链后，restore 错误接受 live 入口不可能产生的复用；
3. 同 subject/result 先有 historical applied-modify、再有 current pending challenge 的合法 L5-3 snapshot 中，combined initial 指向 pending challenge并重派生 ref 后，restore 错误借用历史 modify event；
4. 正常 `review_required` 外层记录改成 `review_setup_failed`、清空 outer challenge ref 并重派生全部 outer refs，但保留 private source/challenge/current marker，restore 错误接受。

没有先改 production、skip、xfail、只保留 stale ref 或弱化原 35 项。

### 12.3 根因族关闭

- `_command_is_prevalidated(command, parent)` 已提取为 coordinator 外纯函数；live `apply_revision` 与 restore 重建的 canonical command 调用同一实现；
- shared predicate 完整覆盖 session/artifact identity、state/formula `+1`、canonical authority change、subject↔bundle version/digest/evaluator/adapter 对齐，以及相对 parent 的新 checkpoint/interrupt；status/resultless 不跳过；
- initial `modify_applied` 现在要求 exact challenge 存在、`state=applied`，且 exact challenge 自己精确一个 `modify_fixture` event；不再从同 subject 的其他 challenge 借 event；
- historical `review_required` 有后继时要求 exact challenge applied 与 exact-owned single modify event；current `review_required` 继续由完整 private L5-3 snapshot 表达 pending/claimed/applied/expired；
- blocked/recheck_failed/review_setup_failed 除 outer challenge ref 为空外，还必须不存在任何同 subject private source 及由 exact subject/result 派生的 challenge；因此保留 private review authority 后改 outer status 固定拒绝；
- 任一 restore 失败继续在 private store 可用前归一化为固定 chainless `SandboxRecheckError`。

四类 R2 回归最终 `4 passed in 1.94s`；完整专项扩展为 `39 passed in 2.79s`，R1 与原 32 项全部保持。

### 12.4 R2 最终门禁

| 门禁 | R2 结果 |
|---|---|
| L5-4/R2 专项 | `39 passed in 2.79s` |
| accepted L5-3 + L5-2 + L5-1 合并回归 | `91 passed in 19.88s`（59 + 18 + 14） |
| Safety 回归 | `71 passed, 3 deselected in 1.76s` |
| L4.5-11 privacy 回归 | `76 passed in 4.37s` |
| Ruff / mypy | `All checks passed!`；production 1 source / 0 issues；仅既有 `pymilvus.*` note |
| L0 | `131 passed in 2.20s` |
| AST 离线边界 | `1 passed in 1.73s` |
| `uv lock --check` | `Resolved 84 packages in 3ms`；lock 未改变 |
| 强制 `APP_ENV=sandbox-test` 全量 | `1 failed, 1754 passed, 362 deselected in 148.35s`；唯一为既有 defaults 差异 |
| 只移除 `APP_ENV` 的校准全量 | `1755 passed, 362 deselected in 146.89s` |

### 12.5 提交、限制与回退

- 单一 R2 开发提交消息为 `fix: unify L5-4 live and restored state checks`；exact parent 必须为 `84c8f6459ac086457dfb9218575ac2360c4068af`；只含本 handoff 第 8 节三个文件。
- Git SHA 由冻结后的 `git rev-parse HEAD`、提交消息和本节共同报告，由独立 Reviewer/CI/PM 锚定。
- 边界仍为 fixed-fictitious/synthetic、offline unit/in-memory；不提供进程外 durability、Runtime、HTTP、DB、真实 completion/export、临床或公开生产能力。
- 若 R2 验收失败，以单一 R2 delivery 执行 `git revert <r2-delivery-commit>` 并保留全部历史，不 reset 或覆盖。

## 13. L5-4-R2 独立验收（未通过）

- 冻结 R2 delivery：`23a561a5c972cd9fb0103fb7d39ebbe7ad841cbb`；exact parent `84c8f6459ac086457dfb9218575ac2360c4068af`；只修改三个允许文件；Review/CI 前后 clean。
- 独立 CI：专项 `39`、L5-3 `59`、L5-2 `18`、L5-1 `14`、Safety `71/3 deselected`、privacy `76`、L0 `131`、Ruff/mypy/lock/AST/diff/scope/tracked/clean 全通过；校准全量 `1755 passed, 362 deselected`；强制环境为 `1 failed, 1754 passed, 362 deselected` 且仅既有 defaults 差异。
- 独立 Reviewer：P0=0、P1=0、P2=1、P3=0；R1 的 shared command predicate 与 terminal retained-authority 两项均 resolved。
- 剩余 P2：only-initial combined snapshot 可继续引用 historical applied-modify challenge，即使 private L5-3 snapshot 已有新的 same-scope pending challenge 且 current marker 已改变；outer chain 尚未闭合到完整 issue projection/current marker。
- PM 结论：**未接受 / 发布 L5-4-R3 限定返工**（`ACC-20260722-034`、`DEC-20260722-027`）。保留初始/R1/R2 deliveries 与全部证据；L5 仍为 3/4，L6 未开始。

## 14. L5-4-R3 current issue projection 根因闭合

### 14.1 状态与起点

- 状态：**L5-4-R3 已交付，申请验收**；执行者不声明 accepted、专业批准或 production ready。
- clean management release / exact parent：`f35d58ca24f8f8f7d4e6e4ebd8a180915d69e81a`；初始/R1/R2 失败 deliveries、`ACC-20260722-032/033/034`、`DEC-20260722-025/026/027` 与全部返工任务书继续 append-only 保留。
- R3 只修改原三个 L5-4 文件；未修改任务书、PM 台账、accepted L5-1/L5-2/L5-3、配置、依赖、锁文件、Runtime 或外部边界。

### 14.2 真实 R3 RED 与 GREEN

production 保持 `23a561a` 行为、工作区唯一 production 变化尚未开始时，先增加三项状态关系回归；完整 fake env 与 `UV_OFFLINE=1` 下结果为退出码 `1`、`3 failed in 2.32s`，三项均因旧实现 **DID NOT RAISE**：

1. only-initial outer chain 仍指向 historical applied-modify challenge，但 private same-scope current 已被后来 pending issue 取代；
2. BLOCK terminal outer chain 保持不变，但 private same-scope current 已不再是 terminal parent；同步更新 invalidation challenge set 并重算 `invalidation_ref` 后旧 restore 仍错误接受；
3. outer initial + current child 跳过 private same-scope 中间 issue；同步更新 invalidation challenge set 并重算 `invalidation_ref` 后旧 restore 仍错误接受。

因此拒绝不依赖 stale derived ref，也没有删除 historical challenge/event。根因修复后，三项负例与跨 scope 正例定向为 `4 passed in 1.96s`；跨 scope 正例随后进一步强化为 private issue 顺序 `initial -> other-scope -> current child`，单项为 `1 passed in 2.30s`；最终完整专项为 `43 passed in 3.46s`，R2 的 39 项全部保留。

### 14.3 单一 projection/current 不变量

- 新 `_issue_projection_and_current_are_integral(...)` 是 restore 唯一新增的 R3 根因 predicate：先以 initial revision 的 exact challenge/checkpoint/interrupt 定位显式 `issue_sequence`，再按 issue sequence 投影同 namespace/session/thread 的完整 suffix；
- 该 same-scope suffix 必须与 outer revision chain 的全部非空 `challenge_ref` 精确同序、同基数；initial 之前的同 scope 历史允许保留，其他 scope 的 issue 可在 initial 与 child 之间穿插并被忽略；
- expected current 由状态统一选择：only-initial `modify_applied` 指向 initial，current `review_required` 指向自身，`blocked/recheck_failed/review_setup_failed` 指向 parent；同 scope current marker 必须精确唯一并与 expected issue sequence、challenge、checkpoint 一致；
- current review source 的 exact 查询同步限定 namespace/session/thread/checkpoint/interrupt，避免其他 scope 使用相同 subject/result 时被错误计入；shared live/restore command predicate、exact event ownership、terminal authority absence 与 source-build single-call atomic commit 均保持不变。

### 14.4 R3 最终门禁

| 门禁 | R3 结果 |
|---|---|
| L5-4/R3 专项 | `43 passed in 3.46s` |
| accepted L5-3 | `59 passed in 2.21s` |
| accepted L5-2 | `18 passed in 6.55s` |
| accepted L5-1 | `14 passed in 13.67s` |
| Safety 回归 | `71 passed, 3 deselected in 1.80s` |
| L4.5-11 两项 privacy 回归 | `76 passed in 4.76s` |
| Ruff / mypy | `All checks passed!`；production 1 source / 0 issues；仅既有 `pymilvus.*` unused-section note |
| L0 | `131 passed in 2.31s` |
| AST 离线边界 | `1 passed in 1.80s` |
| `uv lock --check` | `Resolved 84 packages in 3ms`；lock 未改变 |
| 强制 `APP_ENV=sandbox-test` 全量 | `1 failed, 1758 passed, 362 deselected in 148.94s`；唯一为既有 `tests/test_config.py::test_load_with_defaults` 的 `local` / `sandbox-test` defaults 差异 |
| 只移除 `APP_ENV` 的校准全量 | `1759 passed, 362 deselected in 146.89s` |

最终内容冻结前重新执行双全量，没有复用跨 scope 正例强化前的全量结果。全部 fixture 继续是 inline fixed-fictitious/synthetic 技术数据；未读取 `.env`、ignored `data/` 或 `.codex_tmp`，未启动网络、应用或外部服务。

### 14.5 范围、提交、限制与回退

- 提交前 `git diff --check`、exact 三文件 scope 与 tracked 检查必须通过；单一 R3 开发提交消息为 `fix: close L5-4 current issue projection`，exact parent 为 `f35d58ca24f8f8f7d4e6e4ebd8a180915d69e81a`，提交后工作区必须 clean。
- Git SHA 无法在包含本文的同一提交内自引用；冻结后由 `git rev-parse HEAD`、提交消息和本节 exact parent 共同报告，并由独立 Reviewer/CI/PM 锚定。
- 边界仍为 fixed-fictitious/synthetic、offline unit/in-memory；不提供进程外 durability、Runtime、HTTP、DB、真实 completion/export、专业准入或公开生产能力。
- 若 R3 验收失败，以单一 R3 delivery 执行 `git revert <r3-delivery-commit>` 并保留全部历史，不 reset、amend 或覆盖。

## 15. L5-4-R3 独立验收（未通过）

- 冻结 R3 delivery：`b7cbbffd76a11646ffe9209d1b5a8ec610720358`；exact parent `f35d58ca24f8f8f7d4e6e4ebd8a180915d69e81a`；只修改三个允许文件；Review/CI 前后 clean。
- 独立 CI：专项 `43`、L5-3 `59`、L5-2 `18`、L5-1 `14`、Safety `71/3 deselected`、privacy `76`、L0 `131`、Ruff/mypy/lock/AST/diff/scope/tracked/clean 通过；calibrated full `1759 passed, 362 deselected`；forced full 首次除 defaults 外有一次 L3 deadline code 偏差，精确节点 5 次与完整 forced full 复验均未复现，复验为 `1 failed, 1758 passed, 362 deselected`，首次证据保留。
- 独立 Reviewer：P0=0、P1=0、P2=1、P3=0；R3 same-scope projection/current 三项目标与 R2/R1 历史 findings 均 resolved/保持关闭。
- 剩余 P2：restore current-source 查询已限定 exact scope，但 completion consumer 仍按全 store subject/result 计数；other-scope 同内容 source 存在时，exact current review 已 applied confirm 后 completion 仍错误 blocked。
- PM 六项定向 `6 passed`，随后独立复现 `applied=applied; completion=blocked; matching_sources=2`。PM 结论：**未接受 / 发布 L5-4-R4 限定返工**（`ACC-20260722-035`、`DEC-20260722-028`）。L5 仍为 3/4，L6 未开始。

## 16. L5-4-R4 completion exact-current source 闭合

### 16.1 状态与起点

- 状态：**L5-4-R4 已交付，申请验收**；执行者不声明 accepted、专业批准或 production ready。
- clean management release / exact parent：`d27ee992f691f65fa62e23b645f3b1f2112e9d2a`；其中 append-only 保留初始～R3 失败 deliveries、全部独立验收证据、`ACC-20260722-035`、`DEC-20260722-028` 与 R4 任务书。
- R4 只修改原三个 L5-4 文件；未修改 R4 任务书、PM 台账、accepted L5-1/L5-2/L5-3、配置、依赖、锁文件、Runtime 或外部边界。

### 16.2 真实 RED 与 GREEN

production 保持 `b7cbbff` 行为时，先新增一个独立端到端回归：构造合法 private issue 顺序 `initial -> other-scope -> exact-current-child` 并 restart，使用 exact current delivery 完成 stage 与 confirm resume。stage 返回 `staged`、resume 返回 `applied`，但 exact completion 仍实际返回 `blocked`；测试期望 `eligible`，真实 RED 为退出码 `1`、`1 failed in 2.32s`，失败断言精确为 `blocked != eligible`。

该回归同时覆盖错误 namespace、thread、checkpoint 均保持 `blocked`，并检查 other-scope source/challenge/current marker 在 confirm 后 append-only 保留。修复后定向为 `1 passed in 1.83s`，最终完整专项为 `44 passed in 3.10s`，R3 的 43 项全部保留。

### 16.3 shared exact-source predicate

- 新增纯 `_source_matches_revision_exactly(source, revision)`，通过只读结构协议接收 private source record，完整比较 namespace、test session、thread、checkpoint、interrupt、`safety_subject`、`safety_result`，并要求 `explanation_result is None`；
- combined restore 对 current `review_required` 的 exact source 基数检查与 `completion_eligibility()` 每次重读均调用该同一 predicate，不再维护两套字段子集；
- completion 对 exact marker/checkpoint/challenge/event 的各自唯一性、accepted L5-3 eligibility 调用、错误查询 fixed blocked 语义均未放宽；other-scope 记录既不计入 exact source，也未被删除；
- R3 `_issue_projection_and_current_are_integral(...)` 未改；R2 shared command predicate、R1 exact event/source-build 原子性及原状态机不变量继续保持。

### 16.4 R4 最终门禁

| 门禁 | R4 结果 |
|---|---|
| L5-4/R4 专项 | `44 passed in 3.10s` |
| accepted L5-3 | `59 passed in 2.64s` |
| accepted L5-2 | `18 passed in 7.02s` |
| accepted L5-1 | `14 passed in 13.90s` |
| Safety 回归 | `71 passed, 3 deselected in 1.80s` |
| L4.5-11 两项 privacy 回归 | `76 passed in 4.53s` |
| Ruff / mypy | `All checks passed!`；production 1 source / 0 issues；仅既有 `pymilvus.*` unused-section note |
| L0 | `131 passed in 2.23s` |
| AST 离线边界 | `1 passed in 1.80s` |
| `uv lock --check` | `Resolved 84 packages in 3ms`；lock 未改变 |
| 强制 `APP_ENV=sandbox-test` 全量 | `1 failed, 1759 passed, 362 deselected in 148.74s`；唯一为既有 `tests/test_config.py::test_load_with_defaults` 的 `local` / `sandbox-test` defaults 差异 |
| 只移除 `APP_ENV` 的校准全量 | `1760 passed, 362 deselected in 146.52s` |

本轮强制与 calibrated 全量均未出现新的偶发结果。全部 fixture 继续是 inline fixed-fictitious/synthetic 技术数据；未读取 `.env`、ignored `data/` 或 `.codex_tmp`，未启动网络、应用或外部服务。

### 16.5 范围、提交、限制与回退

- 提交前 `git diff --check`、exact 三文件 scope 与 tracked 检查必须通过；单一 R4 开发提交消息为 `fix: bind L5-4 completion to exact source`，exact parent 为 `d27ee992f691f65fa62e23b645f3b1f2112e9d2a`，提交后工作区必须 clean。
- Git SHA 无法在包含本文的同一提交内自引用；冻结后由 `git rev-parse HEAD`、提交消息和本节 exact parent 共同报告，并由独立 Reviewer/CI/PM 锚定。
- 边界仍为 fixed-fictitious/synthetic、offline unit/in-memory；不提供进程外 durability、Runtime、HTTP、DB、真实 completion/export、专业准入或公开生产能力。
- 若 R4 验收失败，以单一 R4 delivery 执行 `git revert <r4-delivery-commit>` 并保留全部历史，不 reset、amend 或覆盖。

## 17. L5-4-R4 独立验收（未通过）

- 冻结 R4 delivery：`c71832b2e1f188c9acc5ebd3d4f76ca1a38a8e43`；exact parent `d27ee992f691f65fa62e23b645f3b1f2112e9d2a`；只修改三个允许文件；Review/CI 前后 clean。
- 独立 CI：专项 `44`、L5-3 `59`、L5-2 `18`、L5-1 `14`、Safety `71/3 deselected`、privacy `76`、L0 `131`、Ruff/mypy/lock/AST/diff/scope/tracked/clean 通过；forced full `1 failed, 1759 passed, 362 deselected` 且仅 defaults 差异；calibrated full `1760 passed, 362 deselected`。
- 独立 Reviewer：P0=0、P1=0、P2=1、P3=0；R4 exact-current completion 主 finding 与 R3～R1 历史 findings 均关闭/保持关闭。
- 剩余 P2：terminal absence 仍调用全 store subject/authority 查询；`review_setup_failed` terminal 添加合法 other-scope 同内容 source/challenge 后，same-scope parent marker 与 outer chain 不变，restore 仍错误拒绝。PM 独立复现 `terminal=review_setup_failed; restore=rejected`。
- PM 结论：**未接受 / 发布 L5-4-R5 authority qualification matrix 架构收敛**（`ACC-20260722-036`、`DEC-20260722-029`）。同根因连续两轮，停止症状补丁；初始～R4 失败历史全部保留；L5 仍为 3/4，L6 未开始。

## 18. L5-4-R5 authority qualification matrix 架构收敛

### 18.1 状态与起点

- 状态：**L5-4-R5 已交付，申请验收**；执行者不声明 accepted、专业批准或 production ready。
- clean management release / exact parent：`fca217b8311b206b5229c1ebbbd13b7cbf743706`；其中 append-only 保留初始～R4 失败 deliveries、全部独立验收证据、`ACC-20260722-036`、`DEC-20260722-029` 与 R5 收敛任务书。
- R5 只修改原三个 L5-4 文件；未修改 R5 任务书、PM 台账、accepted L5-1/L5-2/L5-3、配置、依赖、锁文件、Runtime 或外部边界。

### 18.2 关系矩阵真实 RED 与 GREEN

production 保持 `c71832b` 行为时，先新增三项关系矩阵回归，真实 RED 为退出码 `1`、`2 failed, 1 passed in 2.44s`：

1. 由真实 source-build failure 产生 `review_setup_failed` terminal，再为相同 terminal subject/result 在其他 namespace/thread/checkpoint/interrupt 建立合法 source/challenge/current；private snapshot 合法、outer chain 与 same-scope parent marker 不变，但旧 restore 错误 fixed reject；
2. 在 terminal 自己的 namespace/session/thread/checkpoint/interrupt 建立同 revision source/challenge，旧实现与期望均 fixed chainless reject，因此该矩阵负例在 RED 阶段已通过；
3. AST/source ownership 发现旧 `_authority_refs/_source_refs/_subject_source_refs` 仍同时服务 historical、current 与 terminal，结构断言失败。

收敛实现后，三项矩阵为 `3 passed in 1.85s`；最终完整专项为 `47 passed in 3.16s`，R4 的 44 项（包括 exact-current confirm→eligible 与 R3 cross-scope projection）全部保持。other-scope terminal restart 连续 round-trip 保留全部 private 记录；same-revision terminal 继续返回固定、无 cause/context 的拒绝。

### 18.3 有限 authority qualification matrix

- `_source_matches_same_revision(...)` 是唯一 same-revision identity predicate，仅比较 namespace、test session、thread、checkpoint、interrupt 与 subject；
- `_source_matches_revision_exactly(...)` 组合 same-revision predicate，再比较 result 与 explanation absence；restore current source 与 completion consumer 继续共同复用它；
- `_same_revision_authority_refs(...)` 只从 same-revision source refs 派生对应 challenge/event refs，terminal presence/absence 精确使用该投影；other scope 不计入，同 revision scope 精确计入；
- 宽 collection 明确更名为 `_historical_invalidation_authority_refs(...)`，accepted subject/result collection 语义保持不变；生产 AST ownership 精确只有两个调用：live invalidation 使用 `current`，restore old-ref 核对使用 `prior`；
- initial 与 historical review challenge/event ownership 改为直接 exact challenge ref 查询，不借 historical invalidation helper；R3 `_issue_projection_and_current_are_integral(...)` 的 namespace/session/thread scope 及 expected-current 规则未改。

结构回归还证明旧三个含混 helper 已不存在、exact-current 与 same-revision authority helper 各自只组合一次 same-revision predicate。每种关系现在只有一个具名 authority helper，停止继续堆叠调用点字段补丁。

### 18.4 R5 最终门禁

| 门禁 | R5 结果 |
|---|---|
| L5-4/R5 专项 | `47 passed in 3.16s` |
| accepted L5-3 | `59 passed in 2.38s` |
| accepted L5-2 | `18 passed in 6.68s` |
| accepted L5-1 | `14 passed in 13.59s` |
| Safety 回归 | `71 passed, 3 deselected in 1.81s` |
| L4.5-11 两项 privacy 回归 | `76 passed in 4.55s` |
| Ruff / mypy | `All checks passed!`；production 1 source / 0 issues；仅既有 `pymilvus.*` unused-section note |
| L0 | `131 passed in 2.25s` |
| AST 离线边界 | `1 passed in 1.95s` |
| `uv lock --check` | `Resolved 84 packages in 3ms`；lock 未改变 |
| 强制 `APP_ENV=sandbox-test` 全量 | `1 failed, 1762 passed, 362 deselected in 147.41s`；唯一为既有 `tests/test_config.py::test_load_with_defaults` 的 `local` / `sandbox-test` defaults 差异 |
| 只移除 `APP_ENV` 的校准全量 | `1763 passed, 362 deselected in 147.28s` |

首次分层静态检查发现一个等价嵌套条件的 Ruff `SIM102`，手工合并为单一条件后，专项、全部分层门禁与双全量均对最终内容重新执行；没有复用修正前结果。本轮双全量没有新的偶发结果。全部 fixture 继续是 inline fixed-fictitious/synthetic 技术数据；未读取 `.env`、ignored `data/` 或 `.codex_tmp`，未启动网络、应用或外部服务。

### 18.5 范围、提交、限制与回退

- 提交前 `git diff --check`、exact 三文件 scope 与 tracked 检查必须通过；单一 R5 开发提交消息为 `refactor: converge L5-4 authority qualification`，exact parent 为 `fca217b8311b206b5229c1ebbbd13b7cbf743706`，提交后工作区必须 clean。
- Git SHA 无法在包含本文的同一提交内自引用；冻结后由 `git rev-parse HEAD`、提交消息和本节 exact parent 共同报告，并由独立 Reviewer/CI/PM 锚定。
- 边界仍为 fixed-fictitious/synthetic、offline unit/in-memory；不提供进程外 durability、Runtime、HTTP、DB、真实 completion/export、专业准入或公开生产能力。
- 若 R5 验收失败，以单一 R5 delivery 执行 `git revert <r5-delivery-commit>` 并保留全部历史，不 reset、amend 或覆盖。

## 19. L5-4-R5 独立验收（通过）

- 冻结 R5 delivery：`847076e86275edc6a92470dbb749a695d9177757`；exact parent `fca217b8311b206b5229c1ebbbd13b7cbf743706`；只修改三个允许文件；Review/CI 前后 clean。
- 独立 Reviewer：P0=0、P1=0、P2=0、P3=0；finite authority qualification matrix、helper ownership、other-scope/same-revision terminal、R4 completion、R3 projection/current 与 R2/R1 历史 finding 全部通过。
- 独立 CI：矩阵 `3`、L5-4 `47`、L5-3 `59`、L5-2 `18`、L5-1 `14`、Safety `71/3 deselected`、privacy `76`、L0 `131`、Ruff/mypy/lock/AST/diff/scope/tracked/clean 全通过；forced full `1 failed, 1762 passed, 362 deselected` 且仅既有 defaults 差异；calibrated full `1763 passed, 362 deselected`。
- PM 六项矩阵/组合复验 `6 passed in 2.39s`；exact HEAD 不变且 clean。
- PM 结论：**L5-4 与 R5 accepted**（`ACC-20260722-037`、`DEC-20260722-030`），关闭 `R-L5-RECHECK-001`。初始～R4 失败历史全部保留。
- 本节只完成 L5-4 单项验收；L5-1～L5-4 现为 4/4 individually accepted，但 L5 整体仍须在包含 acceptance management 事务的 clean exact HEAD 执行最终组合 Review/CI/PM 后另行关闭；L6 未开始。

## 20. L5 最终组合第 1 轮（未通过，L5-4 reopened）

- 冻结 exact HEAD：`be33ffc92ca37bffe69c1f40967f44aea7f7596d`；acceptance parent `847076e86275edc6a92470dbb749a695d9177757`；工作区前后 clean。
- 最终独立 CI：L5 组合 `138 passed`，calibrated full `1763 passed, 362 deselected`；forced full 仅既有 defaults 差异；Safety/Runtime/Legacy/privacy/public flag/AST/Ruff/mypy/L0/lock/scope/tracked/clean 全通过。
- 最终 Reviewer：P0=0、P1=0、P2=1、P3=0；唯一 P2 为 current `review_required` child 没有继承 parent review schema authority，同步改变 child/private schema 并重派生完整 refs 后 restore 仍接受。
- PM 跨层 `13 passed`，并独立复现 parent v1 / child v2 的 `review_required` snapshot `restore=accepted`。
- 结论：最终组合 **未通过**（`ACC-20260722-038`、`DEC-20260722-031`）；保留第 19 节和 `ACC-037` 的历史单项结论，但 L5-4 与 `R-L5-RECHECK-001` 重新打开，发布 R6；L5 当前 3/4，L6 未开始。

## 21. L5-4-R6 review schema authority inheritance 闭合

### 21.1 状态与精确起点

- 状态：**L5-4-R6 已交付，申请验收**；执行者不声明 accepted、专业批准或 production ready。
- clean management release / exact parent：`6fe77cd008d83e3ad34e32509cefe63a802a27ab`；其中保留 R5 delivery、历史单项 acceptance、最终组合第 1 轮失败证据、`ACC-20260722-038`、`DEC-20260722-031` 与 R6 任务书。
- R6 只修改原三个 L5-4 文件；未修改 R6 任务书、PM 六台账、accepted L5-1/L5-2/L5-3、配置、依赖、锁文件、Runtime、Legacy 或外部边界。

### 21.2 真实 RED 与 GREEN

exact `6fe77cd` 的 production 内容仍为 R5 `847076e` 行为。production diff 为空、工作区唯一变更为三项 R6 回归时，完整 fake env 与 `UV_OFFLINE=1` 下定向运行，真实 RED 为退出码 `1`、`2 failed, 1 passed in 2.43s`：

1. 创建正常 current `review_required` child，把 child 与 exact private challenge 的 schema 从 `sandbox-review-challenge.v1` 同步改为 `sandbox-review-challenge.v2`；按 canonical authority 重算 challenge ref、对应 transition ref、checkpoint/current marker，再重算 outer revision/run/invalidation/receipt/current refs。private snapshot 可单独通过 strict store restore，parent 仍为 v1，旧 combined restore 错误接受；测试期望固定、chainless 拒绝且输入零 mutation。
2. AST 结构断言找不到 status 分支之前的统一相邻 child schema inheritance guard，证明旧条件只属于 non-`review_required` 分支。
3. 正常 `initial -> review_required -> blocked` 多 revision 链保持同一 initial schema并可连续 restart，RED 阶段已通过。

没有依赖 stale ref、单侧 schema 变化、删除 private history、skip、xfail 或弱化 R5 的 47 项。PM 最终组合第 1 轮的同类复现继续保留在第 20 节；本次回归将其转化为可重复的完整重派生证据。

最小实现后，三项定向为 `3 passed in 1.89s`；完整 L5-4 专项为 `50 passed in 3.31s`，R5 的 47 项全部保留。

### 21.3 单一 schema chain authority

- `_snapshot_is_integral(...)` 在每个 child 的 reconstructed command 建立后、任何 status-specific 分支之前，无条件要求 `revision.review_schema_version == prior.review_schema_version`；因此整条 outer revision chain 等价继承 initial schema。
- `review_required` 的 exact challenge 仍由 `_challenge_matches_revision(...)` 要求 `sandbox_schema_version == revision.review_schema_version`；current source/challenge/current marker、historical applied review、terminal absence、R5 finite authority qualification matrix 均未放宽。
- live 创建路径未改：仍消费 accepted L5-3 固定 schema；没有新增 schema migration、版本协商、registry 或第二套 authority。
- 任一 restore 不一致仍归一化为固定 `SANDBOX_RECHECK_REJECTED`，无动态 payload、无异常 cause/context、无部分 mutation。
- 新结构测试定位 child loop 的唯一 shared inheritance guard，并要求它不读取 `revision.status` 且位于第一个 status branch 之前；功能正例证明 review-required 与 terminal child 共享同一 initial schema并可 restart。

### 21.4 R6 最终门禁

除 calibrated full 只移除 `APP_ENV` 外，全部使用第 7 节完整 fake env 与 `UV_OFFLINE=1`：

| 门禁 | R6 结果 |
|---|---|
| R6 三项定向 / L5-4 完整专项 | `3 passed in 1.89s`；`50 passed in 3.31s` |
| accepted L5-3 | `59 passed in 3.13s` |
| accepted L5-2 | `18 passed in 8.54s` |
| accepted L5-1 | `14 passed in 19.33s` |
| Safety / privacy | `71 passed, 3 deselected in 2.61s`；`76 passed in 5.80s` |
| Runtime/Legacy / public flag | `57 passed in 2.48s`；`10 passed in 1.95s` |
| AST 离线与结构边界 | `6 passed in 1.98s` |
| Ruff / mypy | `All checks passed!`；production 1 source / 0 issues；仅既有 `pymilvus.*` unused-section note |
| L0 | 最终 handoff 内容复跑 `131 passed` |
| `uv lock --check` | `Resolved 84 packages in 5ms`；lock 未改变 |
| 强制 `APP_ENV=sandbox-test` 全量 | `1 failed, 1765 passed, 362 deselected in 148.47s`；唯一为既有 `tests/test_config.py::test_load_with_defaults` 的 `local` / `sandbox-test` defaults 差异 |
| 只移除 `APP_ENV` 的校准全量 | `1766 passed, 362 deselected in 153.44s` |

全部 fixture 继续是 inline fixed-fictitious/synthetic 技术数据；未读取 `.env`、ignored `data/` 或 `.codex_tmp`，未启动网络、应用、容器、数据库或外部服务。本轮没有新的偶发结果。

### 21.5 范围、提交、限制与回退

- 提交前 `git diff --check`、exact 三文件 scope 与 tracked 检查必须通过；单一 R6 开发提交消息为 `fix: enforce L5-4 review schema inheritance`，exact parent 为 `6fe77cd008d83e3ad34e32509cefe63a802a27ab`，提交后工作区必须 clean。
- Git SHA 无法在包含本文的同一提交内自引用；冻结后由 `git rev-parse HEAD`、提交消息和本节 exact parent 共同报告，并由新的独立 Reviewer/CI/PM 锚定。
- 边界仍为 fixed-fictitious/synthetic、offline unit/in-memory；不提供进程外 durability、Runtime、HTTP、DB、真实 completion/export、专业准入或公开生产能力。L5 当前仍为 3/4，L6 未开始。
- 若 R6 验收失败，以单一 R6 delivery 执行 `git revert <r6-delivery-commit>` 并保留全部历史，不 reset、amend 或覆盖。

## 22. L5-4-R6 独立验收（通过）

- 冻结 delivery：`f6b790d48cb4198bd9786fa94bcdc235a67c3de2`；parent `6fe77cd008d83e3ad34e32509cefe63a802a27ab`；仅本 handoff 第 21 节约定的 3 个 tracked 文件，前后 exact HEAD/clean 与 diff check 通过。
- 独立 Reviewer：P0=0、P1=0、P2=0、P3=0；确认重派生 RED 真实，共享 schema inheritance guard 位于所有 child status 分支之前，失败 restore 固定、chainless、零 mutation，R5 不变量未放宽。
- 独立 CI：R6 定向 `3`、L5-4 `50`、L5-3/2/1 `59/18/14`、Safety `71/3 deselected`、privacy `76`、纯离线 Runtime/Legacy/public `57`、public flag `10`、AST `6`、L0 `131`、Ruff/mypy/lock 全通过。
- CI 口径纠正：一次误把 integration-marked golden 与 unit 混跑，被仓库隔离夹具在任何资源启动前固定拒绝；改用纯离线 57 项集合后全绿。该记录不属于产品失败，无外部副作用。
- 双全量：forced `1 failed, 1765 passed, 362 deselected`，唯一仍为 `test_load_with_defaults` 的 local vs sandbox-test 既有差异；calibrated 仅移除 `APP_ENV`，为 `1766 passed, 362 deselected`。
- PM 独立探针：R6 三项 `3 passed`；不一致 child 固定拒绝且输入不变，合法 review/terminal 多状态链可 restart，共享 guard 位于状态分支之前；HEAD 未变、工作区 clean。
- 结论：**通过 / accepted**（`ACC-20260722-039`、`DEC-20260722-032`）；再次关闭 `R-L5-RECHECK-001`，L5-1～L5-4 恢复 4/4 individually accepted。
- 后续：本节不关闭 L5 整体。必须从包含本 acceptance 事务的新 clean exact HEAD 调用新的最终组合 Reviewer、独立 CI 与 PM 探针，不得复用 `ACC-20260722-038` 失败轮结果；L6 未发布、未开始。
- 边界：固定虚构/合成、offline unit/in-memory reference composition 不变；不授权 Runtime、HTTP、DB、Gateway、真实 clinical/patient/public production。

## 23. L5 最终组合第 2 轮（未通过，L5-3/L5-4 reopened）

- 冻结 exact HEAD：`4ffbaff7374bd6a13b1a9d058e9c920709593119`；R6 delivery `f6b790d` 与单项 acceptance 全部保留；工作区前后 clean。
- 最终独立 CI：L5-1/2/3/4 `14/18/59/50`、组合 `141`、calibrated full `1766 passed, 362 deselected`；forced full 只含既有 defaults 差异；35 个核心 required paths 全部 tracked。
- PM 跨层：`13 passed`；常规 live composition、并发、完整重评估、失效/待确认、离线边界与默认关闭均保持。
- 最终 Reviewer：P0=0、P1=0、P2=1、P3=0。把真实 initial applied review snapshot 的固定 v1 整体协调为 v2并重派生全部 L5-3 refs 后，L5-3 store 与 L5-4 initial-only coordinator 均接受；R6 child==parent guard 在无 child 时不会执行。
- 结论：最终组合 **未通过**（`ACC-20260722-040`、`DEC-20260722-033`）；R5/R6 单项 acceptance 历史保留，但 L5-3/L5-4 与两个工程风险重新打开，L5 当前 2/4。
- R7 所有权：修复必须位于 L5-3 shared snapshot restore；L5-4 production 不增加重复条件，只新增 composition 回归证明下层拒绝向上传递。L6 未发布、未开始。

## 24. L5-3/4-R7 shared fixed schema authority 交付（2026-07-23）

### 24.1 精确起点与两层 RED

- 状态：**R7 已交付，申请独立验收**；exact parent 为 clean management release `54e357f89f5d6f206dd7ae685151cf242e32e0d1`。
- production 未修改时，L5-3 issued/applied、shared-loop AST 与本文件 initial-only composition 共 4 项真实为 `4 failed in 2.54s`。本层负例使用真实 `modify_applied` 初始 review，把 fixed v1 完整改成 v2，重算 challenge/attempt/event/transition refs、checkpoint/current 绑定、initial revision ref 与 outer current pointer；旧 private store 和旧 outer coordinator 都接受。
- 最小修复只落在 `sandbox_review.py::_snapshot_is_integral` 的公共 challenge loop：每个 challenge 在任何状态分支前必须等于 live issue 共用的 `_REVIEW_SCHEMA_VERSION`。`sandbox_recheck.py` production 零 diff，本层通过构造 `SandboxInMemoryReviewStore(snapshot=...)` 自动继承下层拒绝。
- 修复后 4 项定向 `4 passed in 2.08s`；initial-only mismatch 固定返回 `SANDBOX_RECHECK_REJECTED`，无 cause/context，输入 canonical bytes 不变。正常 L5-4 多 revision chain 与 R6 child==parent guard 均保持。

### 24.2 L5-4 回归与门禁

- L5-4 完整专项最终 `51 passed in 3.55s`；既有 50 项全部保留，并新增 initial-only 完整重派生 composition。R6 current-child 用例不再断言 private v2 可恢复，而是证明 shared store 与 outer coordinator 双层 fixed reject；该校准符合 R7 单一 authority，不改变 R6 的 outer inheritance 约束。
- L5-3 完整专项 `62 passed in 2.35s`；L5-1/L5-2 `14/18`；Safety `71/3 deselected`；privacy `76`；离线 Runtime/Legacy/public `57/11 deselected`；public flag `10`；AST/结构 `5`；L0 `131`；Ruff、两 production mypy、lock 与 diff check 全部通过。
- 首次 forced full 为 `2 failed, 1768 passed, 362 deselected`：既有 defaults 差异外出现一次仓库已记录过的 L3 参数偏差；同 forced 环境下该四参数族连续 5 轮全绿后，完整 forced 复跑为 `1 failed, 1769 passed, 362 deselected` 且唯一剩 defaults 差异。首次证据保留。仅移除 `APP_ENV` 的 calibrated full 为 `1770 passed, 362 deselected`。

### 24.3 范围、限制、提交与回退

- R7 单一提交只含 `app/agent_runtime/sandbox_review.py`、两层专项与两份 handoff；未修改本层 production、任务书、PM 六台账、配置、依赖或锁文件。提交消息为 `fix: anchor L5 review schema restore authority`，exact parent 为 `54e357f89f5d6f206dd7ae685151cf242e32e0d1`。
- 全部执行使用 inline fixed-fictitious/synthetic、offline/in-memory fixture；未读取 `.env`、ignored `data/`、`.codex_tmp`，未访问网络或启动服务。
- Git SHA 由提交后外部报告；后续必须在该 exact clean delivery 上执行新的独立 Reviewer/CI/PM，不能复用 final R2。当前不声明 accepted；L5 仍为 2/4，L6 未发布、未开始。
- 若 R7 验收失败，对单一 R7 delivery 执行 `git revert <r7-delivery-commit>`，保留历史且不 reset/amend。

---

**R7 已交付，申请独立验收。**

## 25. L5-3/4-R7 独立 shared acceptance（通过）

- 精确 delivery `d3ee3ce48fd39c115df30d8aad446edac14770a6` / parent `54e357f89f5d6f206dd7ae685151cf242e32e0d1`；5 文件 scope/tracked/diff/exact/clean 全通过；`sandbox_recheck.py` production 零 diff。
- 独立 Reviewer P0/P1/P2/P3 全 0；独立 CI R7 `4`、L5-3/4 `62/51`、calibrated full `1770 passed, 362 deselected`，全部相邻/静态门禁通过；forced 只含既有 defaults 差异。
- PM 六项 `6 passed`；initial-only composition 由下层 shared store 固定拒绝，R6 child==parent 与 R5 finite qualification ownership 均保持。
- 结论：**L5-3/L5-4 与 R7 accepted**（`ACC-20260723-041`、`DEC-20260723-034`）；`R-L5-RESUME-001` 与 `R-L5-RECHECK-001` 再次关闭，L5 恢复 4/4 individually accepted。
- 后续：必须从包含本 shared acceptance 的新 clean exact HEAD 执行全新的 final R3 Reviewer/CI/PM；本节不关闭 L5，不启动 L6。
- 边界仍为 fixed-fictitious/synthetic、offline unit/in-memory reference composition；不授权 Runtime、HTTP、持久层、真实 clinical/patient/public production。
