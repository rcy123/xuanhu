# L5-2-R1 port copy isolation 与 authority revalidation 限定返工

## 发布信息

| 项目 | 内容 |
|---|---|
| 状态 | 已发布 / 待交付 |
| 发布人 | Codex（工程项目经理） |
| 失败交付 | `335f7ad1f8b07535edec3420f39dea5fcef02e4c`（保留，不覆盖） |
| 失败证据 | `ACC-20260722-022`；独立 Review P0=0、P1=1、P2=0、P3=0 |
| 决策 | `DEC-20260722-016` |
| 执行起点 | 包含本合同的 clean exact management release HEAD，由项目经理提交后报告 |
| 交付文件 | `docs/dev-handoff/agent-refactor-l5-2-sandbox.md` |

## 目标

只关闭第 1 次交付的单一 P1：port request 与 verifier authority 共享 nested allowlist entry，导致 pre-call digest 校验不能约束返回后的 text authority。

R1 必须让 port 可见 DTO 与 verifier 使用的 allowlist/source authority 在对象身份和 canonical snapshot 上隔离，并在 port 返回后重验；不重写 L5-2 设计，不扩大到 L5-3 或外部系统。

## 必须先红

从 clean exact R1 release HEAD 开始，先只修改专项测试并记录 RED。至少新增并先红：

- `test_l5_2_port_request_nested_changes_cannot_change_allowlist_authority`
- `test_l5_2_port_request_entries_are_identity_isolated_from_verifier_snapshot`
- `test_l5_2_post_port_allowlist_and_source_snapshots_are_revalidated`

RED 必须复现 `335f7ad` 的共享引用行为；不得 skip、xfail、条件跳过或先改生产代码。

## 必须实现

- port 调用前冻结 source canonical bytes、source invariants、allowlist canonical bytes、allowlist digest 和独立 `rule_id→text` verifier mapping；
- 构造 port request 时逐字段创建新的 issue refs 与 allowlist entries；request nested entries 与 verifier allowlist entries 对象身份必须不同；
- verifier 在 port 返回后只使用 pre-call snapshot/mapping，不得从 port 可见 request 或共享 nested object 重建 authority；
- port 返回后重新 strict 解析/验证原 source input 与原 allowlist input，canonical bytes/digest/invariants 必须与 pre-call snapshot 相同；任何变化 fixed unavailable；
- 即使 port 可改变其 request nested DTO，最多影响 port 自己的临时对象；不能让非 snapshot text attached，不能改变 caller bundle/source；
- unavailable 继续 fixed、无 candidate text/异常 payload/cause/context/log；L5-1 source result byte-identical；
- 保持第 1 次交付已通过的 exact-reference、intervention、64 issues、8 KiB、immutability、zero import/call 和资源边界。

不得以只增加一次 pre-call 校验、依赖 frozen 标志、复用 request nested models 或文档声明关闭 P1。

## 允许修改范围

- `app/agent_runtime/sandbox_explanation.py`
- `tests/test_l5_2_sandbox_safety_explanation.py`
- `docs/dev-handoff/agent-refactor-l5-2-sandbox.md`

除此之外全部禁止。执行者不修改本任务书、L5-1 或 PM 台账。

## 回归与验收

继续使用原 L5-2 任务书全部 fake loopback 环境和禁用边界。至少运行：

```powershell
uv run pytest tests/test_l5_2_sandbox_safety_explanation.py -q -rs
uv run pytest tests/test_l5_1_sandbox_safety_adapter.py -q -rs
uv run pytest tests/test_safety_rule_engine.py -q -rs
uv run pytest tests/test_l4_5_11_1_intake_privacy_projection.py tests/test_l4_5_11_2_runtime_privacy_guard.py -q -rs
uv run ruff check app/agent_runtime/sandbox_explanation.py tests/test_l5_2_sandbox_safety_explanation.py
uv run mypy app/agent_runtime/sandbox_explanation.py
uv run pytest tests/test_l0_1_contract.py -q -rs
uv run pytest -q -rs
uv lock --check
git diff --check
```

精确 `APP_ENV=sandbox-test` defaults 冲突与只移除 `APP_ENV` 的校准全量继续分开记录，不修改范围外配置/测试。

## 停止条件

- 需要修改允许列表外文件、L5-1、配置、依赖或 public flag；
- 只能通过信任 port、删除/弱化测试、忽略 nested mutation 或共享 authority/request 对象关闭 finding；
- 不能保持 fixed unavailable、source byte stability、64/8 KiB/资源门槛；
- 需要网络、模型、Runtime、DB、Gateway、Legacy、review/record/export、L5-3 或真实医疗范围；
- 出现真实/可关联数据、凭据、无法归属 diff 或范围外 P0/P1。

## 交付要求

在原 handoff 追加 R1：release/delivery exact HEAD、真实 RED、identity isolation/snapshot/revalidation 证据、全部 GREEN/资源/回归/全量校准、scope/tracked/clean。创建单一 R1 开发提交，只能声明“已交付，申请验收”。
