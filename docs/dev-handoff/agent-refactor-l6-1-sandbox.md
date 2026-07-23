# L6-1 病历数据模型与确定性组装（Sandbox）交付

## 1. 交付状态与执行起点

- 状态：**已交付，申请验收**；本文不作 `accepted`、`sandbox_scope_satisfied` 或任何临床/专业准入声明。
- 分支：`codex/l4-5-11-context-privacy-hardening`。
- 包含已发布任务书的 clean exact HEAD / 本交付 exact parent：`71af1f3b025c3d9b7775f05204f316cd96f91a84`（L6-1 任务发布提交）。
- 开始时 `git status --short --branch` 仅显示分支行，工作区 clean；最近提交与任务书发布一致。
- 范围保持为固定虚构、合成、纯离线、单元测试与 in-memory reference assembler；未读取 `.env`、ignored `data/` 或 `.codex_tmp`，未启动 Runtime、HTTP、容器、数据库、队列、Gateway、LangGraph、Legacy 或外部服务。
- 本交付没有修改 L5-1/L5-2/L5-3/L5-4、配置、依赖、锁文件、PM 台账或任务书。

## 2. 真实 collection RED

在生产模块不存在、工作区唯一变更为新增专项测试时，在第 7 节完整 fake 环境与 `UV_OFFLINE=1` 下运行：

```powershell
uv run pytest tests/test_sandbox_record_l6_1.py -q -rs
```

真实结果为退出码 `2`，`collected 0 items / 1 error`；顶层导入固定失败为：

```text
ModuleNotFoundError: No module named 'app.agent_runtime.sandbox_record'
```

当时 exact HEAD 为 `71af1f3...`，production module 尚不存在；没有空模块、skip、xfail、条件绕过或先写实现。

## 3. 实现摘要

`app/agent_runtime/sandbox_record.py` 新增 strict frozen L6-1 reference contract：

- frozen、strict、`extra="forbid"` 的 `SandboxMedicalRecordData` DTO，所有嵌套集合使用 tuple；
- DTO 字段全部可序列化、固定：`record_id` / `session_id` / `revision_id` / `reviewed_formula` / `safety_result` / `review_confirm_ref` / `assembled_at` / `record_version` / `disclaimer`；
- `record_id` 由完整 canonical content 派生（`sandbox-record-` + SHA-256），篡改任一字段均导致 `record id mismatch`；
- 服务端固定 `disclaimer = "sandbox_assemble_only_not_a_medical_record"`，不依赖模型生成；
- `SandboxRecordAssembler` 只从 accepted L5-4 recheck snapshot **确定性**构建病历，不调用任何模型、不生成自由文本；
- assembler 只读取 current `review_required` + `ALLOW` + applied `CONFIRM` attempt；非该状态固定 fail closed；
- 字段缺失、类型错误、checkpoint 不匹配、review confirm ref 篡改均固定拒绝（`SandboxRecordError`）；
- 所有失败只抛固定 `SandboxRecordError`，异常消息不带 payload，`__cause__` / `__context__` 均为 `None`；
- assembler `__slots__ == ()`，不持有 evaluator、cache、clock、random 或进程状态。

模块只 import Python 标准库、Pydantic 与已 accepted 的 `sandbox_recheck` / `sandbox_review` / `sandbox_safety`；不 import 或调用 Settings、DB、Gateway、Legacy、review/record/export（L6-2/L6-3/L6-4 范围）、network 或应用 Runtime。

## 4. 专项覆盖与零调用证据

专项保留任务书规定的 PM 探针覆盖，并证明：

1. 合法 confirm review state → 完整病历 JSON（所有必填字段存在且类型正确）；
2. 字段缺失（空 revisions）→ assembler 固定拒绝，输入不变；
3. 类型错误（None 输入）→ assembler 固定拒绝；
4. 篡改 review confirm ref → assembler 固定拒绝；
5. 同输入同输出（确定性）：同 confirmed state 产生 byte-identical record；
6. 不同 `assembled_at` 产生不同 `record_id`（抗碰撞）；
7. DTO 字段不可变（frozen + tuple）；
8. AST import probe 限定生产模块只能使用批准的纯本地依赖根；无 settings/env/data/gateway/review/record/export/network import；
9. assembler 无 `open/print/breakpoint/exec/eval/compile` 调用，`__slots__ == ()`；
10. 错误固定、chainless、payload-free。

## 5. GREEN、静态与回归证据

除特别说明外，下列命令均使用第 7 节全部显式 fake 环境覆盖：

| 门禁 | 结果 |
|---|---|
| `uv run pytest tests/test_sandbox_record_l6_1.py -q -rs` | `12 passed in 3.90s`；真实 collection RED 为退出码 `2`、`collected 0 items / 1 error`、`ModuleNotFoundError: No module named 'app.agent_runtime.sandbox_record'` |
| `uv run pytest tests/test_l5_1_sandbox_safety_adapter.py tests/test_l5_2_sandbox_safety_explanation.py tests/test_l5_3_sandbox_reviewer_interrupt_resume.py tests/test_l5_4_sandbox_modify_full_recheck.py -q -rs` | `176 passed in 24.07s`（含 L6-1 共 `188 passed`；L5 四层 `14/18/84/60` 回归无变化）|
| `uv run pytest tests/test_safety_rule_engine.py -q -rs` | `71 passed, 3 deselected in 2.25s` |
| `uv run pytest tests/test_l0_1_contract.py -q -rs` | `131 passed in 2.30s` |
| `uv run ruff check app/agent_runtime/sandbox_record.py tests/test_sandbox_record_l6_1.py` | `All checks passed!` |
| `uv run mypy app/agent_runtime/sandbox_record.py` | `Success: no issues found in 1 source file`；仅既有 `pymilvus.*` unused-section note |
| `uv lock --check` / `git diff --check` | lock `Resolved 84 packages in 2ms`；diff check 无错误 |
| 只移除 `APP_ENV`、保留全部 fake endpoint 的校准全量 | `1813 passed, 362 deselected in 150.06s`；无失败 |

实现后的首次专项运行为 `10 passed, 2 failed`：`__context__` 在两条固定拒绝路径仍残留原 ValidationError。改为 `_build_record(...)` 返回 `None`、在 `assemble(...)` 中于 `except` 块外统一抛固定 `SandboxRecordError()` 后，所有路径 `__cause__` / `__context__` 均为 `None`，最终 `12 passed`。失败历史如实保留，没有弱化断言。

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

1. `app/agent_runtime/sandbox_record.py`：新增纯离线 DTO 与确定性 assembler；
2. `tests/test_sandbox_record_l6_1.py`：新增唯一 L6-1 专项；
3. `docs/dev-handoff/agent-refactor-l6-1-sandbox.md`：本交付/验收载体。

未修改 PM 台账、Legacy、RAG、UI、Domain 医疗事实、配置、依赖、migration、public flag、Runtime、Gateway、L5 review/recheck、record/export（L6-2/L6-3/L6-4 范围）或部署材料。

未决限制：当前实现不包含 verifier（L6-2）、persistence / 幂等落盘（L6-3）、narration / 文本润色（L6-4）；不接入真实 LangGraph `Command`、Runtime、HTTP 或外部服务。真实临床、患者服务、商业/公开生产和人体研究继续 NO-GO。

若独立验收失败，应以单一交付提交为单位执行 `git revert <delivery-commit>`，保留 RED、GREEN 和失败证据；不得 reset 或覆盖历史。

## 8. 交付提交约定

- 使用单一开发交付提交，提交消息：`feat: add L6-1 sandbox medical record DTO and assembler`。
- exact parent 必须为 release HEAD `71af1f3b025c3d9b7775f05204f316cd96f91a84`。
- Git SHA 取决于包含本文的最终 tree，无法在同一提交正文中自引用尚未生成的 SHA；冻结后以 `git rev-parse HEAD`、交付消息和本文约定共同报告 delivery exact HEAD，由项目经理在独立验收章节锚定。
- 提交必须只含第 7 节三个文件；提交后全部 tracked，工作区 clean。

---

**已交付，申请验收。**

---

## PM 验收结论（ACC-20260723-049）

| 项目 | 结果 |
|---|---|
| 验收人 | Codex（工程项目经理） |
| 验收日期 | 2026-07-23 |
| 结论 | **通过 / accepted** |
| 专项测试 | L6-1 专项 `12 passed in 2.10s` |
| L5 回归 | 四层组合 `176 passed in 21.14s`（14/18/84/60） |
| 校准全量 | `1813 passed, 350 deselected in 134.66s` |
| PM 探针 | 6/6 全部通过 |
| scope/tracked/diff/exact/clean | 通过 |
| 验收依据 | `ACC-20260723-049`、`DEC-20260723-042`、`DEC-20260723-043` |

**L6-1 已验收。下一动作：发布 L6-2 病历一致性验证（Sandbox）。**

---

## L6-1 adversarial 复审补充（2026-07-24）

L6-2 复审期间对 L6-1 进行 23 项 adversarial 独立探针，全部通过：

- DTO 绑定（12 项）：record_id 由 SHA-256 覆盖全部 8 个 canonical 字段派生，篡改任一字段触发 `record_id mismatch` 拒绝；frozen、`extra="forbid"`、strict 全部有效
- Assembler 边界（5 项）：wrong thread_id/checkpoint_id/session_id/namespace 全部固定 `SandboxRecordError`；formula 提取与 snapshot subject 一致
- 确定性 + 抗碰撞（3 项）：同输入字节级相同；不同 `assembled_at` 产生不同 record_id
- 静态边界（3 项）：无 `open/print/exec/eval`，无网络 token，`__slots__==()`

**L6-1 confirmed accepted，无实质缺陷。** L6-2 的 bytes/str 双序列化缺陷局限于 L6-2 verifier 和 assembler 的 else 分支，不连坐 L6-1 的 DTO/Assembler 核心逻辑。
