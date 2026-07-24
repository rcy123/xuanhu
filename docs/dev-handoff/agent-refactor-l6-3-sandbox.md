# L6-3 病历持久化（Sandbox）交付

## 1. 交付状态与执行起点

- 状态：**已交付，申请验收**；本文不作 `accepted`、`sandbox_scope_satisfied` 或任何临床/专业准入声明。
- 分支：`codex/l6-3-sandbox-record`。
- 基线：`a913377`（L6-2-R1 验收提交）。
- 开始时工作区 clean；仅修改/新增任务书允许的三个 tracked 文件（见第 7 节）。
- 范围保持为固定虚构、合成、纯离线、单元测试与 in-memory store；未读取 `.env`、ignored `data/` 或 `.codex_tmp`，未启动 Runtime、HTTP、容器、数据库、队列、Gateway、LangGraph、Legacy 或外部服务。
- 本交付没有修改 L5-1/L5-2/L5-3/L5-4、L6-1 DTO/Assembler 核心逻辑、L6-2 Verifier 核心逻辑、配置、依赖、锁文件、PM 台账或任务书。

## 2. 真实 collection RED

在生产模块新增代码不存在、工作区唯一变更为新增专项测试时，在完整 fake 环境与 `UV_OFFLINE=1` 下运行：

```powershell
uv run pytest tests/test_sandbox_record_l6_3.py -q -rs
```

真实结果为退出码 `2`，`collected 0 items / 1 error`；顶层导入固定失败为：

```text
ImportError: cannot import name 'SandboxRecordStore' from 'app.agent_runtime.sandbox_record'
```

当时 exact HEAD 为 `a913377`，`SandboxRecordStore` 与 `serialize_record` 尚不存在；没有空模块、skip、xfail、条件绕过或先写实现。

## 3. 实现摘要

`app/agent_runtime/sandbox_record.py` 在现有 L6-1 DTO/Assembler 与 L6-2 Verifier 下方新增：

### `SandboxRecordStore`

- `__slots__ = ("_records",)`，仅持有内部 in-memory 字典；不持有 clock、random、network、DB、文件句柄；
- `put(record)` — 对同一 `record_id` 幂等：相同 record 重复写入不报错、不增加版本；相同 `record_id` 但字段不同 → 固定拒绝（`SandboxRecordError`）；
- `get(record_id)` — 返回已存 record；未命中 → 固定 `raise SandboxRecordError()`（chainless、payload-free）。

### `serialize_record`

```python
def serialize_record(record: SandboxMedicalRecordData) -> bytes:
    return canonical_review_bytes(record.model_dump(mode="json"))
```

- 使用 `canonical_review_bytes`（`sort_keys=True`），与 L6-1 `_record_id` 内部 `_digest` 使用相同的 canonical 序列化；
- 同一 record 多次序列化 → 字节级相同；
- 不同 record（不同 `record_id`）→ 字节不同；
- 不写入真实文件、不调用 `open`、不连接 DB、不发起网络调用。

### 设计一致性

- `SandboxRecordError`：Store 的所有固定拒绝路径复用已有的 `SandboxRecordError`（已在 L6-1 中定义），不新增异常类型，保持 chainless、payload-free；
- Store 只做持久化语义，不做字段一致性校验；不引用 `SandboxRecordConsistencyVerifier`；
- 调用方自行决定是否在 put 前调用 verifier 校验 record。

## 4. 专项覆盖与零调用证据

专项保留任务书规定的 PM 探针覆盖，并证明：

1. 幂等 put（同 record 重复）→ 不报错、存储内容不变；
2. 篡改后同 record_id put（`model_construct` 绕过 DTO）→ 固定 `SandboxRecordError`（chainless、payload-free）；
3. get 命中 → 返回原 record，所有字段等价；
4. get miss → `SandboxRecordError`（chainless、payload-free）；
5. `serialize_record` 同输入 → 字节级相同；
6. `serialize_record` 不同输入 → 字节不同；
7. `serialize_record` 输出与 `canonical_review_bytes(record.model_dump(mode="json"))` 完全一致；
8. `SandboxRecordStore.__slots__ == ("_records",)`；
9. AST import probe：无新增 import 根（`imported_roots ≤ {__future__, collections, enum, hashlib, json, pydantic, typing, app}`）；
10. AST 边界：`open/print/breakpoint/exec/eval/compile` 与 network token 零调用；
11. 多 record 共存：store 可容纳 5 个不同 record 并独立检索。

RED 测试证明：
- 无 Store 时，同一 `record_id` 的不同内容可共存（DTO 层无拒绝机制）；
- 无 `serialize_record` 时，`model_dump_json()`（field-order）与 `canonical_review_bytes`（sort_keys）产生不同字节，证明缺少确定性落盘函数。

## 5. GREEN、静态与回归证据

| 门禁 | 结果 |
|---|---|
| `uv run pytest tests/test_sandbox_record_l6_3.py -q -rs` | `15 passed in 3.06s`；真实 collection RED 为退出码 `2`、`collected 0 items / 1 error`、`ImportError: cannot import name 'SandboxRecordStore'` |
| `uv run pytest tests/test_sandbox_record_l6_1.py tests/test_sandbox_record_l6_2.py tests/test_sandbox_record_l6_3.py -q -rs` | `59 passed in 6.72s`（L6-1 `12` + L6-2 `32` + L6-3 `15`）；无回归 |

L6-1/L6-2 `12 + 32 passed` 保持。

## 6. 实际范围、未决限制与回退

本交付只包含任务合同允许的三个文件：

1. `app/agent_runtime/sandbox_record.py`：在现有文件内新增 `SandboxRecordStore` 类与 `serialize_record` 纯函数（不修改 L6-1 DTO/Assembler 与 L6-2 Verifier 部分）；
2. `tests/test_sandbox_record_l6_3.py`：L6-3 唯一专项测试（RED 2 项 + GREEN 13 项）；
3. `docs/dev-handoff/agent-refactor-l6-3-sandbox.md`：本交付/验收载体。

未修改 PM 台账、Legacy、RAG、UI、Domain 医疗事实、配置、依赖、migration、public flag、Runtime、Gateway、L5 review/recheck、L6-1 record DTO/assembler、L6-2 verifier 或部署材料。

未决限制：当前实现不包含 narration / 文本润色（属于 L6-4）、最终组合 / 跨层集成（属于 L6-4）；不接入真实 LangGraph `Command`、Runtime、HTTP 或外部服务。真实临床、患者服务、商业/公开生产和人体研究继续 NO-GO。

若独立验收失败，应以单一交付提交为单位执行 `git revert <delivery-commit>`，保留 RED、GREEN 和失败证据；不得 reset 或覆盖历史。

## 7. 交付提交约定

- 使用单一开发交付提交，提交消息：`feat: add L6-3 sandbox record store and serialize`。
- exact parent 必须为 release HEAD `a913377`（L6-2-R1 验收提交）。
- 提交必须只含第 6 节三个文件；提交后全部 tracked，工作区 clean。

---

**已交付，申请验收。**

---

## PM 验收结论

| 项目 | 结果 |
|---|---|
| 验收人 | （待填写） |
| 验收日期 | （待填写） |
| 验收结论 | （待填写） |
