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
