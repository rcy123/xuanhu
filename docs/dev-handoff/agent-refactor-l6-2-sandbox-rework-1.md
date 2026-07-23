# L6-2-R1 病历一致性验证限定返工（bytes/str 快照双重序列化）交付

## 1. 交付状态与执行起点

- 状态：**已交付，申请验收**；本文不作 `accepted`、`sandbox_scope_satisfied` 或任何临床/专业准入声明。
- 分支：`codex/l6-2-sandbox-rework-1`。
- 基线 / 本交付 exact parent：`61105b9`（L6-2-R1 任务发布提交，`docs: revert L6-2 acceptance and publish L6-2-R1 rework`）。
- 开始时 `git status --short --branch` 显示分支行，工作区 clean；HEAD 为 `61105b9`，与任务书发布一致。
- 范围保持为固定虚构、合成、纯离线、单元测试与 in-memory reference verifier/assembler；未读取 `.env`、ignored `data/` 或 `.codex_tmp`，未启动 Runtime、HTTP、容器、数据库、队列、Gateway、LangGraph、Legacy 或外部服务。
- 本交付没有修改 L5-1/L5-2/L5-3/L5-4、L6-1 DTO/Assembler 核心逻辑、配置、依赖、锁文件、PM 台账或任务书。

## 2. 真实 RED

在未修复生产代码、工作区唯一变更为新增 R1 专项测试时，在第 7 节完整 fake 环境下运行：

```powershell
uv run pytest tests/test_sandbox_record_l6_2.py::TestRecordConsistencyVerifierR1Red -q -rs
```

真实结果（修复前）为 `4 passed` —— 但这 4 项断言的是**缺陷行为**（bytes/str 被拒绝）。即：修复前，`TestRecordConsistencyVerifierR1Red` 的 4 个测试在断言 `result is False` / `pytest.raises(SandboxRecordError)` 时通过，证明缺陷确实存在：

- `test_r1_red_verifier_bytes_snapshot_is_rejected`：verifier 对 bytes 快照返回 `False`（应为 `True`）—— 缺陷确认。
- `test_r1_red_verifier_str_snapshot_is_rejected`：verifier 对 str 快照返回 `False`（应为 `True`）—— 缺陷确认。
- `test_r1_red_assembler_bytes_snapshot_raises`：assembler 对 bytes 快照抛 `SandboxRecordError`（应成功）—— 缺陷确认。
- `test_r1_red_assembler_str_snapshot_raises`：assembler 对 str 快照抛 `SandboxRecordError`（应成功）—— 缺陷确认。

修复后，这 4 个测试的断言被翻转（assert `is True` / `isinstance(record, ...)`），与 `TestR1InputTypeMatrixVerifier` / `TestR1InputTypeMatrixAssembler` 的 GREEN 矩阵一致。缺陷行为与修复行为的对照在测试 docstring 与本节中如实记录，未弱化任何断言。

关键洞察（来自 DEC-20260724-045）：`canonical_review_bytes(value)` 不是幂等的。`canonical_review_bytes(canonical_review_bytes(x))` ≠ `canonical_review_bytes(x)`。当 `value` 已是 `bytes`/`str` 时，`json.dumps` 把它当作字符串字面量序列化，`model_validate_json` 收到字符串字面量而非对象，报 `Input should be an object`，被 `except Exception` 吞掉 → fail-closed 拒绝合法输入。

## 3. 实现摘要

`app/agent_runtime/sandbox_record.py` 在 L6-1 DTO/Assembler 与 L6-2 verifier 之间新增模块级 helper `_parse_recheck_snapshot(value) -> SandboxRecheckSnapshotV1 | None`，并在 verifier `_verify` 与 assembler `_build_record` 的输入解析分支统一调用：

- `SandboxRecheckSnapshotV1` 实例 → 直接返回；
- `bytes` → `model_validate_json(value, strict=True)`（Pydantic 接受 bytes）；
- `str` → `model_validate_json(value, strict=True)`（Pydantic 接受 str）；
- `dict` → `model_validate(value, strict=True)`；
- 其他类型 / 解析失败 → 返回 `None`（verifier → `False`，assembler → `None` → `SandboxRecordError`）。

helper 用单层 `try/except Exception: return None`，不泄露 payload、不携带 chain。verifier/assembler 的字段提取、字段比较、record 构建逻辑**完全未改动**——仅替换了原先内联的 `isinstance`/`canonical_review_bytes`/`model_validate_json` 模式为一次 `_parse_recheck_snapshot` 调用。

模块 docstring 更新为反映 L6-1 + L6-2 共存。import 集合未新增根（`contextlib.suppress` 曾被尝试后撤回，最终用裸 `try/except`，保持 L6-1 import 集合 `__future__/hashlib/typing/pydantic/app` 不变，`test_l6_1_red_no_settings_env_network_imports` 继续通过）。

## 4. 专项覆盖与零调用证据

R1 在保留原有 16 项 L6-2 测试的基础上，新增 3 个测试类共 16 项：

1. `TestRecordConsistencyVerifierR1Red`（4 项）：bytes/str 快照在 verifier/assembler 上的缺陷→修复对照；
2. `TestR1InputTypeMatrixVerifier`（6 项）：instance→True、dict→True、bytes→True、str→True、garbage→False、None→False；
3. `TestR1InputTypeMatrixAssembler`（6 项）：instance→record、dict→record、bytes→record、str→record、garbage→`SandboxRecordError`、None→`SandboxRecordError`。

合计 L6-2 专项 `32 passed`（原 16 + 新 16）。原始 5 项字段篡改探针（formula/confirm_ref/safety_result/decision/injected field）全部保留并通过。

verifier `__slots__ == ()`、无 `open/print/breakpoint/exec/eval/compile` 调用、AST import 根限定（沿用 L6-1 测试）继续通过。

## 5. GREEN、静态与回归证据

除特别说明外，下列命令均使用第 7 节全部显式 fake 环境覆盖：

| 门禁 | 结果 |
|---|---|
| `uv run pytest tests/test_sandbox_record_l6_2.py -q -rs` | `32 passed in 2.69s` |
| `uv run pytest tests/test_sandbox_record_l6_1.py tests/test_sandbox_record_l6_2.py tests/test_l5_1_sandbox_safety_adapter.py tests/test_l5_2_sandbox_safety_explanation.py tests/test_l5_3_sandbox_reviewer_interrupt_resume.py tests/test_l5_4_sandbox_modify_full_recheck.py tests/test_safety_rule_engine.py tests/test_l0_1_contract.py -q -rs` | `422 passed, 3 deselected in 24.16s`（L6-1 `12` + L6-2 `32`；L5 四层 `14/18/84/60`；Safety `71/3 deselected`；L0 `131`，回归无变化）|
| `uv run ruff check app/agent_runtime/sandbox_record.py tests/test_sandbox_record_l6_2.py` | `All checks passed!` |
| `uv run mypy app/agent_runtime/sandbox_record.py` | `Success: no issues found in 1 source file`；仅既有 `pymilvus.*` unused-section note |
| `uv lock --check` / `git diff --check` | lock `Resolved 84 packages in 1ms`；diff check 无错误 |
| 只移除 `APP_ENV`、保留全部 fake endpoint 的校准全量 | `1845 passed, 362 deselected in 123.39s`；无失败 |

修复过程中出现的真实失败如实保留：首次修复用 `contextlib.suppress` 导致 L6-1 的 `test_l6_1_red_no_settings_env_network_imports` 因新增 `contextlib` import 根失败（`assert 'contextlib' not in approved roots`）；改为裸 `try/except` 后通过。R1 RED 测试首次断言缺陷行为（`is False` / `raises`）通过，修复后断言翻转前曾有 4 项 `failed`（缺陷已被修复），翻转断言后全绿。失败历史未弱化任何断言。

正式命令前在同一 PowerShell 进程显式设置：

```powershell
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

1. `app/agent_runtime/sandbox_record.py`：新增 `_parse_recheck_snapshot` helper；verifier `_verify` 与 assembler `_build_record` 的输入解析分支替换为调用 helper（不修改 DTO、`_record_id`、`_revision_id`、`_digest`、assembler 公共签名与字段提取、verifier 字段比较）；
2. `tests/test_sandbox_record_l6_2.py`：新增 R1 RED 对照 + 输入类型矩阵 GREEN，保留原 16 项；
3. `docs/dev-handoff/agent-refactor-l6-2-sandbox-rework-1.md`：本交付/验收载体。

未修改 PM 台账、Legacy、RAG、UI、Domain 医疗事实、配置、依赖、migration、public flag、Runtime、Gateway、L5 review/recheck、L6-1 record DTO/assembler 字段提取、record/export（L6-3/L6-4 范围）或部署材料。

未决限制：当前实现不包含 persistence / 幂等落盘（属于 L6-3）、narration / 文本润色（属于 L6-4）；不接入真实 LangGraph `Command`、Runtime、HTTP 或外部服务。真实临床、患者服务、商业/公开生产和人体研究继续 NO-GO。

若独立验收失败，应以单一交付提交为单位执行 `git revert <delivery-commit>`，保留 RED、GREEN 和失败证据；不得 reset 或覆盖历史。

## 8. 交付提交约定

- 使用单一开发交付提交，提交消息：`fix: parse recheck snapshot by input type (L6-2-R1)`。
- exact parent 必须为 release HEAD `61105b9`。
- Git SHA 取决于包含本文的最终 tree，无法在同一提交正文中自引用尚未生成的 SHA；冻结后以 `git rev-parse HEAD`、交付消息和本文约定共同报告 delivery exact HEAD，由项目经理在独立验收章节锚定。
- 提交必须只含第 7 节三个文件；提交后全部 tracked，工作区 clean。

---

**已交付，申请验收。**
