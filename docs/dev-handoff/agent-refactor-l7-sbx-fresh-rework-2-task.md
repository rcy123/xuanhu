# L7-SBX-FRESH-R2 Authority 回调内容密封架构收敛任务书

> 状态：已完成 / accepted
> 发布日期：2026-07-28
> 实现基线：`d8c10e344269d3821c7819ad93bfce7f51b11621`
> 失败验收：`ACC-20260728-060`
> 决策：`DEC-20260728-057`
> 工程阻塞：`AR-B-037`
> 风险：`R-L7-AUTHORITY-001`、`R-L7-SEAL-001`
> 原任务：[L7-SBX-FRESH](agent-refactor-l7-sbx-fresh-task.md)
> 上一返工：[L7-SBX-FRESH-R1](agent-refactor-l7-sbx-fresh-rework-1-task.md)
> 交付文档：[agent-refactor-l7-sbx-fresh.md](agent-refactor-l7-sbx-fresh.md)
> 最终实现：`da604d75a758f1b8941e849735453472208aff6f`
> 最终验收：`ACC-20260728-061`

## 1. 触发原因

`d8c10e3` 已实现 live registry/authorizer、injected verifier、同实例撤权、方法遮蔽防御和 callback 前后对象身份检查，并报告 L7 专项 `144 passed`、非 integration `2107 passed, 362 deselected`。但实现把“状态密封”缩减为容器 identity + `len()`：

- registry 只捕获 `_recognized` / `_reauthorizable` 的对象与长度；
- store 只捕获 `_bundles` 的对象与长度；
- 同一容器内 `clear + refill same N`、`delete + insert`、同 key 换 value 均保持 identity 与长度不变；
- 两个 `get_bundles_for_*` 查询路径的 per-bundle `registry.recognize()` 也未由 store 级 pre/post seal 包围。

因此 R1 合同的 exact protected state 仍未闭合，当前绿测不能作为 acceptance。相同 authority finding 已连续出现于多个候选，本轮不再追加零散 matcher，而是以一个中央不变量收敛全部 callback 边界。

## 2. 唯一中央不变量

每次调用不可信 `authorizer` 或 `verifier` 时，必须同时满足：

1. **Reference seal**：`_lock`、`_authorizer`、`_verifier`、`_registry`、`_store`、内部容器等受保护引用在 callback 前后保持 exact object identity；
2. **State seal**：registry/store 的全部受保护可变状态在 callback 前后具有完全相同的 callback-free canonical state digest；
3. **No partial continuation**：identity 或 state 任一漂移时，操作必须以固定、payload-free、chainless `SANDBOX_EVIDENCE_INTEGRITY_FAILURE` 终止；不得继续写入、返回数据或生成 citation；受污染状态必须恢复到 pre-callback exact snapshot，或永久 poison 并拒绝后续公共操作；
4. **Single owner**：canonical capture/digest/verify 只能由一个共享内部原语负责；Registry、Store、Pipeline 不得各自维护不一致的长度/例外表。

## 3. Callback-free canonical state

中央原语只处理受信边界内的 exact built-ins，不得调用外部对象或可覆盖 hook：

- `_recognized` 必须为 exact `dict[str, int]`；规范形式为按 key 排序的 exact `(str, int)` tuple；
- `_reauthorizable` 必须为 exact `set[str]`；规范形式为排序后的 exact `str` tuple；
- `_bundles` 必须为 exact `dict[str, bytes]`；规范形式为按 key 排序的 exact `(str, bytes)` tuple；
- `_epoch`、`_sealed`、`_reentry_guard` 使用 exact `int` / `bool`；`bool` 不得冒充 `int`；
- 容器与元素必须先做 `type(x) is ExpectedType` 检查；exact dict/set 使用 built-in descriptor 读取；exact str/bytes/int 才允许排序和 canonical serialization；
- 摘要使用确定性 SHA-256，域分隔必须绑定 schema/version 和字段名；禁止使用 `str()`、`repr()`、外部 `__eq__`/`__hash__`、property、Pydantic serializer/validator 或 authorizer/verifier 方法；
- capture 同时保存不可变 exact pre-state，以便检测后恢复或证明永久 poison；不得只保存 digest/长度。

如果无法在上述约束下安全形成规范状态，必须直接 fail-closed，不得退回 `len()` 或“尽力而为”。

## 4. 有限 operation × callback × state 矩阵

| 操作 | 不可信 callback | 必须密封的状态 |
|---|---|---|
| `Registry.recognize` | `authorizer.authorize` | registry refs；recognized exact items；reauthorizable exact members；epoch/reentry |
| `Registry.add_recognized` | `authorizer.authorize` | 同上；callback 之后才允许业务写入 |
| `Registry.reauthorize` | `authorizer.authorize` | 同上；旧 epoch/capture 不得重放 |
| `Store.put` | `registry.recognize` → authorizer | store refs/bundles/sealed/reentry + 完整 registry state |
| `Store.get` | `registry.recognize` → authorizer | 同上 |
| `Store.get_bundles_for_retrieval_run` | 每个 bundle 的 `registry.recognize` | 每次 callback 前后完整 store + registry state；不得只包围循环外长度 |
| `Store.get_bundles_for_graph_run` | 每个 bundle 的 `registry.recognize` | 同上 |
| `Pipeline.run` RAG 路径 | `verifier.verify` | pipeline refs/reentry + 完整 store + registry state + exact input refs |
| `Pipeline.run` fallback 路径 | `verifier.verify` | 同上 |
| `Pipeline.verify_claims` | `verifier.verify` | 同上 |

`snapshot()` 没有外部 callback；`restore()` 必须继续要求调用方注入当前 registry/authorizer，不能用 snapshot 自授权，也不能自动 recognize。

## 5. 允许与禁止范围

实现只允许修改：

```text
app/agent_runtime/sandbox_evidence.py
tests/test_sandbox_evidence_l7.py
docs/dev-handoff/agent-refactor-l7-sbx-fresh.md
```

PM 管理事务可修改本任务书与 `docs/01_agent部分优化/项目管理/00`～`05`。其他文件禁止修改。

禁止：

- 修改 L0～L6、`app/rag`、Runtime/API/DB/HTTP/配置/依赖/lockfile；
- 修改 Evidence DTO、source/claim policy、retrieval 内容、fallback/hard block/no-fake-citation 业务语义；
- 引入网络、数据库、模型、真实数据或旧 L7；
- 删除、改名或弱化现有测试来取得 GREEN；
- 以新建 store/registry 模拟同实例 mutation；
- 再增加仅检查长度、单个字段或异常白名单的 sibling patch。

## 6. RED-first family tests

先只修改测试并在 `d8c10e3` 生产代码上证明真实 RED。每项必须证明 callback 实际执行，并精确断言 `SANDBOX_EVIDENCE_INTEGRITY_FAILURE`：

1. `_recognized`：same-size `clear+refill`；`delete+insert`；same-key value replace；
2. `_reauthorizable`：same-size `clear+refill`；`delete+insert`；
3. `_bundles`：same-size `clear+refill`；`delete+insert`；same-key raw bytes replace；
4. nested pipeline→store→registry：verifier 从深层路径做 same-size mutation；
5. `get_bundles_for_retrieval_run` 与 `get_bundles_for_graph_run`：authorizer 在 per-bundle callback 做 same-size mutation；
6. 恶意 `__str__` / `__repr__` / scalar/container subclass：seal 生成不得触发 hook；exact type 不满足时 fail-closed；
7. 漂移失败后无部分成功：没有新 bundle、context、claim/citation 输出；后续状态恢复到 pre-state 或实例永久拒绝；
8. 原 R1/R2 的 object replacement、clear-to-zero、reentry、revoke/reauthorize、restore external-authority 测试全部保留。

至少覆盖三个容器的全部适用 same-size mutation 族；不以“一个示例测试通过”代表整个家族。

## 7. 验收门禁

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

还必须如实记录全仓 Ruff/format/mypy；如果失败，明确区分当前 diff 与基线债务。独立 Reviewer 必须在固定提交上重新检查矩阵并给出 P0/P1/P2/P3 全零；PM 必须独立复现至少三种 same-size mutation、两个查询路径和失败后无部分成功。

## 8. 停止条件

- 任一 callback 边界仍只使用 identity/length 而没有 canonical exact-state seal；
- same-size family 在修复后仍能绕过，或测试通过但 callback 未实际执行；
- digest 生成触发外部 hook、Pydantic callback 或 authorizer/verifier；
- mutation 被发现后仍能返回/写入部分成功结果，或受污染实例可继续被消费；
- 需要修改 allowlist 外文件、恢复旧 L7、访问 `.env`/密钥/真实数据/stash/`.claude/`；
- 通过扩大异常表、忽略 matcher 或弱化测试继续打地鼠。

命中停止条件时保留失败证据并返回 PM，不得自行宣称 residual risk 可接受。

## 9. 状态边界

- `ca9caa7`、`c57a100`、`9861beb`、`d8c10e3` 均保留为未验收候选和收敛证据，不得重写为 accepted；
- R2 通过前，`L7-SBX-FRESH` 保持 rework / 未完成；
- L7-PROD、真实 RAG/corpus、Milvus/DB/embedding/model gateway、产品 Runtime/HTTP、真实数据、临床、患者服务、公开/商业/机构使用继续 NO-GO。
