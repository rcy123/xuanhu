# L5-4 修改后全量重检与旧 review 失效（Offline Sandbox）

## 发布信息

| 项目 | 内容 |
|---|---|
| 状态 | 已发布 / 待交付 |
| 发布人 | Codex（工程项目经理） |
| 前置验收 | L5-3/R5 accepted：delivery `95be09a1b766c36eb4da411162b33a2efb4a1346`，验收提交 `d2ab8e75c33f18c2ac8d805ff037365c8ab0d363`，`ACC-20260722-030` |
| 依据 | L5 个人学习工程沙盒准入包 §7.1、§7.2、§7.6、§7.7、§8、§8.1；`DEC-20260722-024` |
| 执行起点 | 包含本任务书的 clean exact management release HEAD，由项目经理提交后报告 |

## 唯一目标

实现一个纯离线、线程安全、可 snapshot/restart 的 in-memory L5-4 reference coordinator：只在当前 L5-3 `modify_fixture` 已应用时接受 revision N→N+1；在一个外层 domain lock 内使旧 safety/explanation/challenge/review/completion authority 失效，使用 accepted L5-1 `SandboxSafetyRuleAdapter` 对新完整 subject + rule bundle 做一次全量重检，并在结果允许时用 accepted L5-3 coordinator 生成新 challenge。新 review 确认前完成资格始终 blocked。

本任务不实现真实 LangGraph、Runtime、HTTP、数据库、持久层、record/done/export、Legacy 或任何外部连接。

## 必须先红

只新增 `tests/test_l5_4_sandbox_modify_full_recheck.py`，production module 仍不存在时运行：

```powershell
uv run pytest tests/test_l5_4_sandbox_modify_full_recheck.py -q
```

必须记录真实 collection RED（缺少 `app.agent_runtime.sandbox_recheck`，exit 2）。禁止先建空模块、skip、xfail、只测 mock 返回值或修改既有 L5-1～L5-3 测试。

## 允许文件

只允许新增：

- `app/agent_runtime/sandbox_recheck.py`
- `tests/test_l5_4_sandbox_modify_full_recheck.py`
- `docs/dev-handoff/agent-refactor-l5-4-sandbox.md`

三个文件外任一修改都必须停止并交回项目经理。不得修改 accepted L5-1/L5-2/L5-3、配置、依赖、锁文件、Runtime、Legacy、PM 台账或其他测试。

## 必须实现的离线合同

### 1. Accepted source 与 revision command

- 初始化只接受一个通过 `SandboxReviewStoreSnapshotV1` 完整性校验的 L5-3 snapshot；按 current marker 定位精确 source/challenge/checkpoint/event，且当前唯一 applied action 必须为 `modify_fixture`。confirm/reject/pending/过期/非 current 或多记录一律固定拒绝；
- current revision authority 至少绑定 session、namespace/thread/checkpoint/interrupt、domain state/formula/profile revision、formula/profile content digest、graph/adapter/rule bundle/evaluator/dataset/manifest digest、result/explanation/review-render digest、challenge/event refs 与 L5-3 review schema；
- revision command 只接受 expected current revision ref、唯一 command/run/trace ID、完整 candidate `SandboxSafetySubjectV1`、完整 `SandboxRuleBundleV1` 与新 checkpoint/interrupt；session/formula/profile artifact identity 保持，`domain_state_version` 与 `formula_revision` 必须精确 `+1`，candidate canonical authority 必须与 current 不同；
- 缺字段、未知字段、旧 expected ref、revision 跳号、同内容伪修改或不一致 bundle 在任何写入前固定拒绝。命令和结果均为 strict frozen DTO，不接受调用方自报“已验证”。

### 2. 单一外层事务与 append-only 失效

- coordinator 独占一个 outer `RLock`；accepted L5-1 adapter 与 private L5-3 store/coordinator 只能在该锁内访问，任何 private store/coordinator 引用不得暴露；
- 预验证通过即视为 modification accepted：同一外层事务追加 revision/run/invalidation 记录、切换 current revision，并使旧 safety、explanation、全部同 revision challenge/review 与 completion authority stale/superseded；旧记录不得删除、覆盖、降级或原位改写；
- invalidation 记录必须逐项绑定旧 subject/result/explanation/review-render digest、全部旧 challenge/event refs、旧 revision ref 与新 revision ref，并由 canonical content 派生唯一 ref；不得仅写布尔 `stale=True`；
- L5-1 评估或新 review 初始化若失败，仍提交已接受的新 revision 与旧 authority 失效，状态为 fixed `recheck_failed` 或 `review_setup_failed`，完成资格 blocked；不得回滚到旧 review 继续授权；
- pre-validation 失败则整个 snapshot 逐字不变。相同 command 的 exact retry 幂等，不重复 revision/run/invalidation/challenge；plaintext nonce 只在首次成功 delivery 返回一次。

### 3. 强制完整 L5-1 重检

- production 必须直接调用 accepted `SandboxSafetyRuleAdapter().evaluate(...)`，传入完整 candidate subject 与完整 bundle；不得注入返回结果 port、复用旧 `SandboxSafetyResultV1`、复用旧 issue 子集或只计算“受影响规则”；
- adapter 的 full bundle/manifest/evaluator authority、64 formula items、256 issues、256 KiB canonical 上限和 deterministic digest 合同全部沿用；新 result 的 `decision_subject_digest` 必须逐字等于 candidate canonical digest，result/bundle/dataset authority 必须在写入前后复验；
- decision `BLOCK`：提交新 revision + 完整 result + 旧 authority 失效，状态 `blocked`，不得创建 challenge；
- decision `ALLOW`：从新 subject/result 构建新的 `SandboxReviewSourceV1`（新 explanation 可省略），在 private copy-on-write L5-3 store 中创建唯一新 challenge；全部步骤成功后才把新 review snapshot 与 revision records 一次性发布为 current，状态 `review_required`。

### 4. Private L5-3 continuation 与完成资格

- 对 `review_required` 只暴露受 outer lock 包装的 `stage_current_review(...)`、`resume_current_review(...)`，内部复用 accepted L5-3 coordinator；resume command 仍只携带 attempt ref；
- `completion_eligibility(...)` 是唯一无副作用 consumer probe：每次从当前 revision 重读 candidate subject/result、current L5-3 marker/source/challenge/checkpoint/event，并逐字验证 state/formula/profile/rule/dataset/graph/adapter/review schema 与全部 digests/ref；只在当前 result `ALLOW` 且当前 action `confirm` 时 eligible；
- 旧 checkpoint、旧 challenge/event、旧 result、缓存布尔、仅外键或任一过期 binding 永远 blocked；reject/modify、pending、recheck_failed、review_setup_failed、BLOCK 均 blocked；
- 不实现 `complete`、`record`、`done`、`export` 方法或任何副作用。未来消费者只能复用同等 exact-current probe，不能读取 cached eligibility。

### 5. Snapshot/restart、并发与固定失败

- combined snapshot 必须 strict frozen，包含 append-only revisions/runs/invalidations、唯一 current revision pointer 与完整 private L5-3 snapshot；所有 sequence、derived refs、cross-reference、cardinality、current pointer、状态/result/challenge/event 一致性在 restore 时重算；
- fake restart 后保持旧 authority blocked、新 pending/confirmed review 状态与幂等 command receipt；不得重发旧或新 plaintext nonce；
- 32 个相同 expected-current modification 并发必须精确一个进入 accepted recheck，其余固定 `replayed_or_conflict`；只新增一条 revision/run/invalidation，最多一个新 challenge；
- 所有公开错误/失败使用固定、无 payload 的 code/status，`__cause__` 与 `__context__` 均为空；不输出 fixture 原文、对象 repr、nonce 或动态异常文本。

### 6. 明确离线边界

- production imports 只允许标准库、Pydantic 与 accepted `sandbox_safety`、`sandbox_explanation`、`sandbox_review`；
- 禁止 settings/config/env/file/data、socket/HTTP、async client、subprocess、DB/ORM、Redis/Milvus、模型/embedding gateway、LangGraph Runtime/MainGraph、Legacy、record/export；
- 所有 fixture 固定虚构/合成且 inline；不得读取 `.env`、ignored `data/` 或 `.codex_tmp`，不得访问网络或启动应用/服务。

## 必须覆盖的专项证据

至少覆盖：

1. 正常 modify：revision/state `+1`、旧 safety/explanation/challenge/review/completion 同事务失效、完整新 result、新 challenge、review 前 blocked；
2. 新 challenge confirm 后 exact-current completion eligible；旧 checkpoint/review 始终 blocked；
3. 新 result BLOCK：旧 authority 已失效、无 challenge、完成 blocked；
4. formula/profile/rule bundle/dataset/graph/adapter/review DTO 任一版本或内容变化均使旧 authority 失效；支持的完整新 authority重新评估，未知版本 fixed fail closed；
5. 64 formula items + 256 issues 的 true-max 全量重检；证明新 result 不是旧 result 或 issue 子集复用；
6. expected ref 错误、revision 跳号、同内容、schema/digest/bundle 不一致在写入前 snapshot 不变；
7. L5-1 评估失败与 challenge 初始化失败在 modification accepted 后保持旧 authority 失效且完成 blocked；
8. exact command retry、conflicting command、32 并发的单次 accepted 与单记录 cardinality；
9. snapshot/restart 后 pending、confirmed、blocked、failed 与幂等 receipt 精确恢复，nonce 不重发；
10. combined snapshot 的 derived refs、sequence、cross-reference、current pointer、invalidation 集合任一不一致均 fixed chainless reject；
11. consumer 每次 exact-current 重读；缓存/旧 result/旧 review/仅 checkpoint ID 不授权；不存在 complete/record/done/export；
12. 固定失败、payload/异常链清洁、immutable copy isolation、无禁止 imports/calls/definitions。

## 门禁

除 calibrated full 只移除 `APP_ENV` 外，沿用完整 fake env 与 `UV_OFFLINE=1`：

```powershell
uv run pytest tests/test_l5_4_sandbox_modify_full_recheck.py -q
uv run pytest tests/test_l5_3_sandbox_reviewer_interrupt_resume.py -q
uv run pytest tests/test_l5_2_sandbox_safety_explanation.py -q
uv run pytest tests/test_l5_1_sandbox_safety_adapter.py -q
uv run pytest tests/test_agent_graph_safety.py tests/test_agent_graph_safety_extended.py tests/test_agent_graph_safety_performance.py -q
uv run pytest tests/test_context_privacy.py tests/test_context_privacy_extended.py tests/test_context_privacy_integration.py tests/test_l4_5_11_task1_privacy.py tests/test_l4_5_11_task2_privacy_gate.py -q
uv run ruff check app/agent_runtime/sandbox_recheck.py tests/test_l5_4_sandbox_modify_full_recheck.py
uv run mypy app/agent_runtime/sandbox_recheck.py
uv run pytest tests/test_l0_1_contract.py -q -rs
uv lock --check
uv run pytest -q
```

还必须运行 AST 禁止能力审计、`git diff --check`、exact scope/tracked/clean；强制全量只允许既有 `tests/test_config.py::test_load_with_defaults` 的 `APP_ENV` 默认值差异。只移除 `APP_ENV` 且保留全部 fake endpoints 的 calibrated full 必须零失败。

## 交付

- handoff 记录真实 RED/GREEN、状态机/事务/全量重检/失效/consumer/restart/并发证据、完整门禁、限制与回退；
- 创建一个开发提交，exact parent 为本任务发布后的 clean HEAD，提交只含三个允许文件；
- 执行者只能声明“L5-4 已交付，申请验收”，不得声明 accepted、clinical approved 或 production ready。
