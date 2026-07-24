# L6-2 病历一致性验证（Sandbox）交付

## 1. 交付状态与执行起点

- 状态：**已交付，申请验收**；本文不作 `accepted`、`sandbox_scope_satisfied` 或任何临床/专业准入声明。
- 分支：`codex/l6-1-sandbox-record`。
- 包含已发布任务书的 clean exact HEAD / 本交付 exact parent：`37b652a...`（L6-2 任务发布提交）。
- 开始时 `git status --short --branch` 仅显示分支行，工作区 clean；最近提交与任务书发布一致。
- 范围保持为固定虚构、合成、纯离线、单元测试与 in-memory reference verifier；未读取 `.env`、ignored `data/` 或 `.codex_tmp`，未启动 Runtime、HTTP、容器、数据库、队列、Gateway、LangGraph、Legacy 或外部服务。
- 本交付没有修改 L5-1/L5-2/L5-3/L5-4、L6-1 DTO/Assembler 核心逻辑、配置、依赖、锁文件、PM 台账或任务书。

## 2. 真实 collection RED

在生产模块不存在、工作区唯一变更为新增专项测试时，在第 7 节完整 fake 环境与 `UV_OFFLINE=1` 下运行：

```powershell
uv run pytest tests/test_sandbox_record_l6_2.py -q -rs
```

真实结果为退出码 `2`，`collected 0 items / 1 error`；顶层导入固定失败为：

```text
ImportError: cannot import name 'SandboxRecordConsistencyVerifier' from 'app.agent_runtime.sandbox_record'
```

当时 exact HEAD 为 `37b652a...`，production verifier 尚不存在；没有空模块、skip、xfail、条件绕过或先写实现。

## 3. 实现摘要

`app/agent_runtime/sandbox_record.py` 在现有 L6-1 DTO/Assembler 下方新增 `SandboxRecordConsistencyVerifier` 类：

- `__slots__ == ()`，不持有 evaluator、cache、clock、random 或进程状态；
- `verify(record, recheck_snapshot=...)` 返回 `bool`，所有路径 fail-closed（异常 → `False`）；
- 验证内容：
  1. `review_confirm_ref` 与原始 recheck snapshot 中 applied CONFIRM event 的 `resume_attempt_ref` 一致；
  2. `session_id` 与 current revision 的 `test_session_id` 一致；
  3. `reviewed_formula` 与 current revision subject 的 `formula_items` 完全匹配；
  4. `safety_result` 与 current revision 的 `result.model_dump(mode="json")` 完全匹配；
  5. `revision_id` 与 current revision 的 `revision_ref` 去前缀后一致；
  6. `record_id` 由完整 canonical content 重新派生，与 record 的 `record_id` 一致；
- 空 revisions、非 `review_required` 状态、非 `ALLOW` decision、无 challenge_ref、非 `applied` challenge、非 `CONFIRM` event 均固定返回 `False`；
- 不调用任何模型、不生成自由文本、不修改输入、不持有状态；
- 只 import 已 accepted 的 `SandboxRecheckSnapshotV1`、`SandboxSafetyDecision`、`SandboxReviewAction`；不 import Settings、DB、Gateway、Legacy、network。

## 4. 专项覆盖与零调用证据

专项保留任务书规定的 PM 探针覆盖，并证明：

1. 合法 confirm review state → verifier 通过；
2. 篡改 `reviewed_formula`（注入新 item）→ verifier 固定拒绝；
3. 注入新字段到 `safety_result` → verifier 固定拒绝；
4. 篡改 `review_confirm_ref` → verifier 固定拒绝；
5. 篡改 `safety_result` decision（`allow` → `block`）→ verifier 固定拒绝；
6. 同 snapshot 重组装 → 两次均通过且 record 相等；
7. `None` snapshot → verifier 固定拒绝；
8. 空 revisions snapshot → verifier 固定拒绝；
9. 不同 snapshot（不同 formula revision）→ verifier 固定拒绝；
10. `__slots__ == ()`；
11. AST import probe 限定生产模块只能使用批准的纯本地依赖根；无 `open/print/breakpoint/exec/eval/compile` 调用。

RED 测试使用 `model_construct` 绕过 DTO 验证，证明无 verifier 时篡改后的 DTO 对象可被构造（但 verifier 会拒绝）。

## 5. GREEN、静态与回归证据

除特别说明外，下列命令均使用第 7 节全部显式 fake 环境覆盖：

| 门禁 | 结果 |
|---|---|
| `uv run pytest tests/test_sandbox_record_l6_2.py -q -rs` | `16 passed in 2.38s`；真实 collection RED 为退出码 `2`、`collected 0 items / 1 error`、`ImportError: cannot import name 'SandboxRecordConsistencyVerifier'` |
| `uv run pytest tests/test_sandbox_record_l6_1.py tests/test_sandbox_record_l6_2.py tests/test_l5_1_sandbox_safety_adapter.py tests/test_l5_2_sandbox_safety_explanation.py tests/test_l5_3_sandbox_reviewer_interrupt_resume.py tests/test_l5_4_sandbox_modify_full_recheck.py -q -rs` | `204 passed in 22.33s`（含 L6-1 `12` + L6-2 `16`；L5 四层 `14/18/84/60` 回归无变化）|
| `uv run pytest tests/test_safety_rule_engine.py tests/test_l0_1_contract.py -q -rs` | `202 passed, 3 deselected in 2.38s` |
| `uv run ruff check app/agent_runtime/sandbox_record.py tests/test_sandbox_record_l6_2.py` | `All checks passed!` |
| `uv run mypy app/agent_runtime/sandbox_record.py` | `Success: no issues found in 1 source file`；仅既有 `pymilvus.*` unused-section note |
| `uv lock --check` / `git diff --check` | lock `Resolved 84 packages in 1ms`；diff check 无错误 |
| 只移除 `APP_ENV`、保留全部 fake endpoint 的校准全量 | `1829 passed, 362 deselected in 136.66s`；无失败 |

正式命令前在同一 PowerShell 进程显式设置：

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
```

实施与测试没有读取或显示本地 `.env`，没有读取 ignored `data/` 或 `.codex_tmp`，没有启动应用、FastAPI、HTTP/E2E、容器、数据库、Redis、Milvus、RAG、模型/embedding gateway 或其他网络/外部服务。没有真实/可关联个人数据、有效凭据、Prompt、nonce/signature 或临床内容进入 fixture、日志、异常或提交。

## 7. 实际范围、未决限制与回退

本交付只包含任务合同允许的三个文件：

1. `app/agent_runtime/sandbox_record.py`：在现有文件内新增 `SandboxRecordConsistencyVerifier` 类（不修改 L6-1 DTO/Assembler 部分）；
2. `tests/test_sandbox_record_l6_2.py`：新增唯一 L6-2 专项测试；
3. `docs/dev-handoff/agent-refactor-l6-2-sandbox.md`：本交付/验收载体。

未修改 PM 台账、Legacy、RAG、UI、Domain 医疗事实、配置、依赖、migration、public flag、Runtime、Gateway、L5 review/recheck、L6-1 record DTO/assembler、record/export（L6-3/L6-4 范围）或部署材料。

未决限制：当前实现不包含 persistence / 幂等落盘（属于 L6-3）、narration / 文本润色（属于 L6-4）；不接入真实 LangGraph `Command`、Runtime、HTTP 或外部服务。真实临床、患者服务、商业/公开生产和人体研究继续 NO-GO。

若独立验收失败，应以单一交付提交为单位执行 `git revert <delivery-commit>`，保留 RED、GREEN 和失败证据；不得 reset 或覆盖历史。

## 8. 交付提交约定

- 使用单一开发交付提交，提交消息：`feat: add L6-2 sandbox record consistency verifier`。
- exact parent 必须为 release HEAD `37b652a...`。
- Git SHA 取决于包含本文的最终 tree，无法在同一提交正文中自引用尚未生成的 SHA；冻结后以 `git rev-parse HEAD`、交付消息和本文约定共同报告 delivery exact HEAD，由项目经理在独立验收章节锚定。
- 提交必须只含第 7 节三个文件；提交后全部 tracked，工作区 clean。

---

**已交付，申请验收。**

---

## PM 验收结论

| 项目 | 结果 |
|---|---|
| 验收人 | Codex（工程项目经理） |
| 初审日期 | 2026-07-23 |
| 初审结论 | ~~通过 / accepted（`ACC-20260723-050`）~~ — **已撤回** |
| 复审日期 | 2026-07-24 |
| 复审结论 | **未接受 / bounded R1**（`ACC-20260724-050R`、`DEC-20260724-045`） |
| 复审发现 | P2=2：verifier bytes/str 快照双重序列化（fail-closed 拒绝合法输入）、assembler 同名缺陷传播；10 项 adversarial 探针 A9 bytes 快照被拒绝 |
| 处置 | 撤回 accepted；发布 `L6-2-R1` 限定返工；L6-3 发布撤回 |

**L6-2 第 1 次交付未通过。下一动作：执行者按 `agent-refactor-l6-2-sandbox-rework-1-task.md` 先红后绿交付 L6-2-R1。**

---

## L6-2 通过 R1 恢复 accepted（2026-07-24）

L6-2-R1（`a913377`）通过 PM 独立验收，两个 P2 缺陷已关闭（`ACC-20260724-050R1`、`DEC-20260724-046`）。L6-2 标记 accepted（通过 R1）。详见 R1 handoff。
