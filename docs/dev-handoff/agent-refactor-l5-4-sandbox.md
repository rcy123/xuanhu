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
