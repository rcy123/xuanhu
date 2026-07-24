# L6-4 病历文本润色与最终组合（Sandbox）交付

## 1. 交付状态与执行起点

- 状态：**已交付，申请验收**；本文不作 `accepted`、`sandbox_scope_satisfied` 或任何临床/专业准入声明。
- 分支：`codex/l6-3-sandbox-record`。
- 基线：`7f1a6a9`（L6-3 交付提交）。
- 开始时工作区 clean；仅修改/新增任务书允许的三个 tracked 文件（见第 7 节）。
- 范围保持为固定虚构、合成、纯离线、单元测试与 in-memory store；未读取 `.env`、ignored `data/` 或 `.codex_tmp`，未启动 Runtime、HTTP、容器、数据库、队列、Gateway、LangGraph、Legacy 或外部服务。
- 本交付没有修改 L5-1/L5-2/L5-3/L5-4、L6-1 DTO/Assembler 核心逻辑、L6-2 Verifier 核心逻辑、L6-3 Store/serialize 核心逻辑、配置、依赖、锁文件、PM 台账或任务书。

## 2. 真实 collection RED

在生产模块新增代码不存在、工作区唯一变更为新增专项测试时，运行：

```powershell
uv run pytest tests/test_sandbox_record_l6_4.py -q -rs
```

真实结果为退出码 `2`，`collected 0 items / 1 error`；顶层导入固定失败为：

```text
ImportError: cannot import name 'SandboxRecordNarration' from 'app.agent_runtime.sandbox_record'
```

当时 exact HEAD 为 `7f1a6a9`，`SandboxRecordNarration` 尚不存在；没有空模块、skip、xfail、条件绕过或先写实现。

RED 测试在实现后仍保持 GREEN（2/2 passed），证明以下历史缺口：

1. 无 `SandboxRecordNarration` 时，record 仅有 raw JSON 输出，无格式化人类可读叙述；
2. 无 L6-4 集成时，各层独立可测但无 assembler → verifier → store → serialize → narrate 端到端流水线。

## 3. 实现摘要

`app/agent_runtime/sandbox_record.py` 在现有 L6-1 DTO/Assembler、L6-2 Verifier 与 L6-3 Store/serialize 下方新增：

### `SandboxRecordNarration`

- `__slots__ = ()`，无状态、无副作用；
- `narrate(record: SandboxMedicalRecordData) -> str` — 纯确定性模板函数：
  - 不使用 LLM、模型调用或随机数；
  - 同一 record 多次调用 → 字符串完全相同；
  - 不同 record（不同 record_id）→ 字符串不同；
  - 输出包含全部关键字段：`session_id`、`revision_id`、`record_id`、`reviewed_formula`（含每项 item_id/component/amount_milliunits/unit）、`safety_result`（含 decision）、`review_confirm_ref`、`assembled_at`、`record_version`、`disclaimer`；
  - 不写入 Store、不修改任何状态、纯函数无副作用；
- 使用 `__future__`/`hashlib`/`typing` 等 L6-1 已批准的 import 根集合，不新增未批准的 import 根；
- 不调用 `open/print/breakpoint/exec/eval/compile`，无 network/socket/subprocess 调用。

### 设计一致性

- `SandboxRecordNarration` 不引入新的异常类型，所有验证失败时复用已有的 `SandboxRecordError`（chainless、payload-free）；
- Narration 不引用 Store、Verifier、Assembler 或任何外部状态；
- Narration 是纯函数：`SandboxMedicalRecordData → str`，无副作用；
- 跨层组合测试使用已有 public API，不访问各层内部实现细节。

## 4. 专项覆盖与零调用证据

专项保留任务书规定的 PM 探针覆盖，并证明：

1. **Narration 确定性**：同 record → 同字符串（`test_l6_4_green_narration_determinism`）；
2. **Narration 区分性**：不同 record → 不同字符串（`test_l6_4_green_narration_discrimination`）；
3. **字段完整性**：叙述包含 `session_id`、`revision_id`、`record_id`、`assembled_at`、`record_version`、`disclaimer`、`review_confirm_ref`（`test_l6_4_green_narration_field_coverage`）；
4. **Formula 项覆盖**：叙述包含每项 `item_id`、`component`、`amount_milliunits`、`unit`（`test_l6_4_green_narration_formula_items_included`）；
5. **Safety decision 覆盖**：叙述包含 `decision`（`test_l6_4_green_narration_safety_decision_included`）；
6. **全链组合通过**：`assembler → verifier → store → serialize → narrate` 全部通过（`test_l6_4_green_full_pipeline`）；
7. **篡改阻断**：篡改 `reviewed_formula` 后至少一层（verifier 或 store）拒绝（`test_l6_4_green_tampered_field_rejected`）；
8. **AST 边界**：无 `open/print/breakpoint/exec/eval/compile`、无 network token（`test_l6_4_green_narration_no_model_or_network_calls`）；
9. **Import 根**：无新增 import 根（`imported_roots ≤ {__future__, collections, enum, hashlib, json, pydantic, typing, app}`）（`test_l6_4_green_no_new_import_roots`）；
10. **Slots 验证**：`SandboxRecordNarration.__slots__ == ()`（`test_l6_4_green_narration_slots`）；
11. **输出格式**：叙述以固定标题 `"Sandbox Medical Record Narration"` 开头、多行格式（`test_l6_4_green_narration_output_format`）。

RED 测试证明：
- 无 Narration 函数时，record 仅有 raw JSON 输出（`"{` 开头，非格式化叙述）；
- 无最终组合时，Store 与 serialize 独立工作但缺少 narrate 步骤完成全链。

## 5. GREEN、静态与回归证据

| 门禁 | 结果 |
|---|---|
| `uv run pytest tests/test_sandbox_record_l6_4.py -q -rs` | `13 passed in 2.41s`（RED 2 + GREEN 11）；真实 collection RED 为退出码 `2`、`collected 0 items / 1 error`、`ImportError: cannot import name 'SandboxRecordNarration'` |
| `uv run pytest tests/test_sandbox_record_l6_1.py tests/test_sandbox_record_l6_2.py tests/test_sandbox_record_l6_3.py tests/test_sandbox_record_l6_4.py -q -rs` | `72 passed in 3.83s`（L6-1 `12` + L6-2 `32` + L6-3 `15` + L6-4 `13`）；无回归 |

L6-1/L6-2/L6-3 `12 + 32 + 15 passed` 保持。

## 6. 实际范围、未决限制与回退

本交付只包含任务合同允许的三个文件：

1. `app/agent_runtime/sandbox_record.py`：在现有文件内新增 `SandboxRecordNarration` 类（不修改 L6-1 DTO/Assembler、L6-2 Verifier、L6-3 Store/serialize 部分）；
2. `tests/test_sandbox_record_l6_4.py`：L6-4 唯一专项测试（RED 2 项 + GREEN 11 项）；
3. `docs/dev-handoff/agent-refactor-l6-4-sandbox.md`：本交付/验收载体。

未修改 PM 台账、Legacy、RAG、UI、Domain 医疗事实、配置、依赖、migration、public flag、Runtime、Gateway、L5 review/recheck、L6-1 record DTO/assembler、L6-2 verifier、L6-3 store/serialize 或部署材料。

未决限制：当前实现不接入真实 LangGraph `Command`、Runtime、HTTP 或外部服务。真实临床、患者服务、商业/公开生产和人体研究继续 NO-GO。

若独立验收失败，应以单一交付提交为单位执行 `git revert <delivery-commit>`，保留 RED、GREEN 和失败证据；不得 reset 或覆盖历史。

## 7. 交付提交约定

- 使用单一开发交付提交，提交消息：`feat: add L6-4 sandbox record narration and final combination`。
- exact parent 必须为 release HEAD `7f1a6a9`（L6-3 交付提交）。
- 提交必须只含第 6 节三个文件；提交后全部 tracked，工作区 clean。

---

**已交付，申请验收。**

---

## PM 验收结论

| 项目 | 结果 |
|---|---|
| 验收人 | |
| 验收日期 | |
| 结论 | |
| 专项测试 | |
| L6-1/L6-2/L6-3 回归 | |
| PM 探针 | |
| scope/tracked/diff/exact/clean | |
| 交付提交 | |
| 设计一致性 | |
| 验收依据 | |
