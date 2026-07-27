# L7-SBX-FRESH-R1 Authority 与状态完整性限定返工任务书

> 状态：已发布 / 返工中
> 发布日期：2026-07-27
> 返工基线：`aa651c280af6db748f1968168e98bdf76defe9cd`
> 失败实现：`ca9caa766018541ac60184a9aed524702ca83a8c`
> 失败验收：`ACC-20260727-059`
> 工程阻塞：`AR-B-037`、`R-L7-AUTHORITY-001`
> 原任务：[L7-SBX-FRESH](agent-refactor-l7-sbx-fresh-task.md)
> 交付文档：[agent-refactor-l7-sbx-fresh.md](agent-refactor-l7-sbx-fresh.md)

## 1. 返工原因

`ca9caa7` 的 76 个专项、414 个 L5/L6/L7 组合和 2039 个非 integration 测试全部通过，但独立 Claude Code Reviewer 判定 `REWORK`：P0=1、P1=3、P2=2、P3=1。测试绿未覆盖原任务书明确要求的 live authority、snapshot-external verifier、回调重入、实例遮蔽和同实例撤权不变量。

本轮只关闭以下七项；不增加新的 Evidence/RAG 功能，不修改 L0～L6，也不接入产品轨道。

## 2. 必须关闭的 findings

1. **P0 live registry/authorizer 缺失**：实现 fixed-synthetic bundle registry + live authorizer capability；所有 store/pipeline/retrieval/verify/restore 公共操作在明确线性化点查询当前授权，snapshot/digest/调用方 ALLOW 不能自证 authority。
2. **P1 verifier 非 snapshot-external**：将 claim verification 依赖倒置为公开 Protocol；可信 verifier 实例由调用方注入。模块不得提供可被 pipeline 当作外部权威的自包含 `CitationVerifier()` 默认实现，restore 也必须重新注入 verifier/authorizer。
3. **P1 callback reentry 未实现/未测试**：authorizer 与 verifier 必须真实接入操作路径；回调前后复核 registry、authorizer、verifier、store、exact RLock、内部容器、授权 epoch/digest 和受保护状态。重入尝试、对象替换或状态漂移全部 fail-closed；测试必须证明 callback 实际被调用并有明确断言。
4. **P1 实例方法遮蔽未 fail-closed**：敏感类使用 `__slots__`/禁止实例字典，并在内部组合中通过 exact class method 或不可替换委托调用；静默 evil method 不能绕过 put/get/verify/pipeline。
5. **P2 同实例撤权不存在**：同一 registry/store/pipeline 实例上 `revoke(bundle_digest)` 后所有新操作拒绝；只对已准入 digest 允许 `reauthorize`，重授权后的新操作可成功但旧 snapshot/result 不自动恢复权威。
6. **P2 graph_run/retrieval_run 混淆**：`get_bundles_for_graph_run(graph_run)` 必须真实按 `bundle.graph_run` 过滤；若保留 retrieval-run 查询则使用不同、准确命名。两个不同 graph run 不可互见，即便其他字段相同。
7. **P3 format/handoff 不实**：格式化两个 L7 Python 文件并修正 handoff；任何未通过或受既有债务影响的全仓门禁必须如实区分，不得写“already formatted”或“mypy app scripts 通过”而无可复现证据。

## 3. 允许修改范围

开发返工只允许修改：

```text
app/agent_runtime/sandbox_evidence.py
tests/test_sandbox_evidence_l7.py
docs/dev-handoff/agent-refactor-l7-sbx-fresh.md
```

PM 管理事务可修改 `docs/01_agent部分优化/项目管理/00`～`05` 与本任务书。其他文件全部禁止修改；禁止新增依赖、配置、迁移、数据或额外源码/测试文件。

## 4. 实现不变量

- registry 的 recognized 集合与 active authorization 状态是 snapshot 外的 live capability；registry/authorizer 必须由 composition root 注入 store/pipeline。
- revoke/reauthorize 使用单调 epoch 或等价不可回退标识；operation capture 必须绑定当前 epoch。旧 capture 在 reauthorize 后仍不可重放。
- authorizer/verifier 回调不得在持有不可重入锁时形成死锁；使用 exact `threading.RLock`、显式 reentry guard 和 callback-attempt 标记，回调异常统一固定、payload-free、chainless 拒绝。
- public operation 对 exact class、方法、锁、容器、协作者 identity/state 做 callback 前后 sealed 复核；禁止依赖实例可遮蔽方法。
- injected verifier 的可信 allowlist/case state 不进入 snapshot；restore 必须由调用方重新提供当前 verifier 与 live authorizer并重验所有恢复内容。
- 原有 digest、source/claim policy、fallback/hard block、resource/no-fake-citation 功能不得回归。

## 5. 先红后绿

先只修改 `tests/test_sandbox_evidence_l7.py`，在 `ca9caa7` 生产代码上真实 RED，至少覆盖：

- registry/authorizer API 缺失；
- 同实例 revoke 后 put/get/pipeline/restore 仍成功；
- snapshot 无外部 verifier/authorizer 仍 restore；
- callback authorizer/verifier 未被调用或重入未拒绝；
- 静默 method shadow 绕过；
- graph_run 查询按 retrieval_run 误过滤；
- scoped format 失败。

再修改 source 到 GREEN。不得删除或弱化原有 76 项测试；若旧测试与新 authority 合同冲突，应更新 fixture 注入而非放宽 authority。

## 6. 验收门禁

```powershell
$env:UV_OFFLINE='1'
uv run pytest tests/test_sandbox_evidence_l7.py -q
uv run pytest tests/test_l5_authority_rework.py tests/test_l5_1_sandbox_safety_adapter.py tests/test_l5_2_sandbox_safety_explanation.py tests/test_l5_3_sandbox_reviewer_interrupt_resume.py tests/test_l5_4_sandbox_modify_full_recheck.py tests/test_sandbox_record_l6_1.py tests/test_sandbox_record_l6_2.py tests/test_sandbox_record_l6_3.py tests/test_sandbox_record_l6_4.py tests/test_sandbox_evidence_l7.py -q
uv run pytest -m "not integration" -q
uv run ruff check app/agent_runtime/sandbox_evidence.py tests/test_sandbox_evidence_l7.py
uv run ruff format --check app/agent_runtime/sandbox_evidence.py tests/test_sandbox_evidence_l7.py
uv run mypy app/agent_runtime/sandbox_evidence.py
uv lock --check
git diff --check
```

此外如实运行并记录 `ruff check .`、`ruff format --check .`、`mypy app scripts`；如存在与 L7 diff 无关的既有债务，需列出命令、数量和基线归因，不得修改允许范围外文件。

独立 Reviewer 必须重新给出 P0/P1/P2/P3 全零；PM 必须复现 live revoke/re-authorize、callback 实际调用/重入、silent shadow、snapshot 无外权威拒绝、cross-graph isolation 和 scoped format。

## 7. 停止条件

- 需要修改允许范围外文件或恢复旧 L7；
- 只在测试中伪造 authorizer/verifier 而生产路径仍未注入；
- 通过新建 store 模拟 revoke、通过异常传播冒充 shadow 检测、或通过零断言冒充回调测试；
- 需要网络、DB、模型、真实数据、`.env`、stash 或未跟踪 `.claude/`；
- 发生死锁、无限递归、同一 authority finding 再次以 matcher/例外表扩张。

命中停止条件时保留 RED 与失败交付，停止修补并报告 PM。

## 8. 状态边界

- `ca9caa7` 保留为第一次失败交付，不重写或删除。
- R1 通过前 `L7-SBX-FRESH` 保持 rework，L7 为未完成。
- L7-PROD、真实 RAG/DB/Runtime、临床/公开/商业/机构使用继续 NO-GO。
