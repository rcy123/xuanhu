# L7-1 Evidence Schema & Policy（Sandbox）交付

## 1. 交付状态与执行起点

- 状态：**已交付，申请验收**；本文不作 `accepted`、`sandbox_scope_satisfied` 或任何临床/专业准入声明。
- 分支：`codex/l6-3-sandbox-record`（沿用 L6-3 交付分支）。
- 基线：`6b49238`（L6-4 交付提交）。
- 开始时工作区仅包含任务书 `docs/dev-handoff/agent-refactor-l7-1-sandbox-task.md`；交付只新增任务书允许的两个 tracked 文件 + 本 handoff（见第 7 节）。
- 范围保持为固定虚构、合成、纯离线、单元测试与 in-memory data；未读取 `.env`、ignored `data/`，未启动 Runtime、HTTP、容器、数据库、队列、Gateway、LangGraph、Legacy 或外部服务。
- 本交付没有修改 L5-1/L5-2/L5-3/L5-4、L6-1 DTO/Assembler/Verifier/L6-2 Verifier/L6-3 Store/serialize/L6-4 Narration、配置、依赖、锁文件、PM 台账或任务书。

## 2. 真实 collection RED

在生产模块新增代码不存在、工作区只新增专项测试时，运行：

```powershell
uv run pytest tests/test_sandbox_evidence_l7_1.py -q -rs
```

真实结果为顶层导入失败（`ImportError: cannot import name 'SandboxEvidencePacket' from 'app.agent_runtime.sandbox_evidence'` —— 因为 `sandbox_evidence.py` 整个模块尚不存在）；`collected 0 items / 1 error`，退出码 `2`。

RED 测试在 L7-1 实现后全部转为 GREEN（48/48 passed），证明以下历史缺口：

1. 无 `SandboxEvidencePacket` 数据结构定义（确定性、不可变、纯数据）；
2. 无 `EvidenceSourcePolicy` 来约束 source type → agent context 映射；
3. 无 `SandboxEvidenceScope` 来约束证据可见性；
4. 无 `RAGUnavailablePolicy` 枚举或 `decide_retrieval_behavior` 纯函数；
5. 无 `SandboxEvidenceVerifier` 校验四路判定（INVALID_SOURCE_TYPE / MISSING_EVIDENCE / TAMPERED_CONTENT / PASS）。

## 3. 实现摘要

### 3.1 新增模块 `app/agent_runtime/sandbox_evidence.py`

全部数据结构和策略均为**纯函数 / frozen pydantic model**，无副作用、无 I/O、无模型调用、无网络：

#### `SandboxEvidencePacket`

- `_LocalStrictFrozenModel = BaseModel` + `ConfigDict(frozen=True, extra="forbid", strict=True)`，与 L6-1 `_StrictFrozenModel` 语义一致；
- 字段：`evidence_id`、`source_type`、`source_id`、`content`、`content_digest`、`retrieval_trace_id`；
- 字段约束：
  - `evidence_id: str` 必须匹配 `^sandbox-evidence-[0-9a-f]{64}$`，由 `sha256(canonical(content, source_type, source_id))` 派生；
  - `content_digest: str` 必须匹配 `sha256(content)`；
  - `source_type: Literal["theory", "case", "formula", "herb"]`，由 `ALLOWED_SOURCE_TYPES` 与 `EvidenceSourceKind` 共同锁定为闭合枚举；
  - `content` 最大 32 KiB，UTF-8 文本，最小长度 1；
- `model_validator(mode="after")` 强制：`content_digest == sha256(content)` AND `evidence_id == sha256(canonical(content, source_type, source_id))`，任何篡改或不一致直接 raise `ValidationError`；
- 提供 `build_evidence_packet(content=..., source_type=..., source_id=..., retrieval_trace_id=...)` 确定性构造函数 + `derive_content_digest(content)` 工具函数，保证引用透明。

#### `SandboxAgentContext`（`enum.StrEnum`）

闭合枚举：`SYNDROME`、`FORMULA`、`MODIFICATION`、`INQUIRY`、`SUFFICIENCY`。新增上下文必须通过后续任务添加，不允许运行时扩展。

#### `EvidenceSourcePolicy`

- 静态方法 `allowed_sources_for(context: str | SandboxAgentContext) -> frozenset[str]`；
- 内部表（模块级常量 `_SANDBOX_SOURCE_POLICY_TABLE`）由 PM 任务书规定的闭合映射：
  - `syndrome → {"theory", "case"}`
  - `formula → {"formula", "herb"}`
  - `modification → {"formula", "herb"}`
  - `inquiry → ∅`
  - `sufficiency → ∅`
- 未知上下文 → `frozenset()`（fail-closed）。

#### `SandboxEvidenceRunContext` + `SandboxEvidenceScope`

- `SandboxEvidenceRunContext` 是 frozen `BaseModel`，仅持有 `trace_id: str` 与 `context: str | SandboxAgentContext`；
- `SandboxEvidenceScope.is_visible(evidence, run_context, *, policy=None) -> bool` 是纯函数：
  - Rule 1：`evidence.retrieval_trace_id == run_context.trace_id`（缺失即不可见）；
  - Rule 2：`evidence.source_type` ∈ `policy.allowed_sources_for(run_context.context)`；
  - 不维护任何全局注册表；可在调用处传入自定义策略。

#### `RAGUnavailablePolicy` + `RetrievalBehavior` + `decide_retrieval_behavior`

- `RAGUnavailablePolicy(StrEnum)`：`FALLBACK_TO_MODEL_KNOWLEDGE`、`HARD_BLOCK`；
- `RetrievalBehavior(StrEnum)`：`RETRIEVE`、`FALLBACK`、`BLOCKED`；
- `decide_retrieval_behavior(rag_available: bool, policy: RAGUnavailablePolicy) -> RetrievalBehavior`：纯确定性 4 路分发；详见测试 `test_l7_1_green_rag_*`。

#### `CitationVerdict` + `SandboxEvidenceVerifier`

- `CitationVerdict(StrEnum)`：`PASS`、`INVALID_SOURCE_TYPE`、`MISSING_EVIDENCE`、`TAMPERED_CONTENT`；
- `SandboxEvidenceVerifier.verify_citation(*, citation_source_type, evidence_packet, policy, context) -> CitationVerdict`：纯函数四路：
  - 优先校验 `citation_source_type ∈ policy.allowed_sources_for(context)`，否则 `INVALID_SOURCE_TYPE`；
  - 然后交叉校验 `evidence_packet.source_type` 同上且等于 `citation_source_type`，否则 `INVALID_SOURCE_TYPE`；
  - `evidence_packet is None` → `MISSING_EVIDENCE`；
  - 最后校验 `content_digest == derive_content_digest(content)`，否则 `TAMPERED_CONTENT`；
  - 全部通过 → `PASS`。

#### 错误风格

- `SandboxEvidenceError(ValueError)`：`__slots__ = ()`，`super().__init__("SANDBOX_EVIDENCE_UNAVAILABLE")`；
- 显式 `__cause__`/`__context__` 都为 `None`、payload-free、chainless；
- 仅在显式 `reject_evidence()` 触发，避免渗透到外部。

### 3.2 关键设计一致性

1. **同 L5/L6 风格**：`enum.StrEnum`（与 `sandbox_safety.py`、`sandbox_review.py` 等对齐）；frozen pydantic model（不允许 `model_construct` 绕过校验，仅在测试中显式 bypass 验证 verifier 行为）；`_LocalStrictFrozenModel` 复刻 L6-1 `_StrictFrozenModel` 的语义（不在本模块再 import L6 的私有名，避免污染其公开 surface）。
2. **不入 L5/L6 沙盒的耦合**：仅引用 `hashlib` / `json` / `pydantic` 等已被接受的根；策略与 DTO 不读写任何全局状态；`__slots__` 不需要（纯 pydantic frozen 模型）；无文件、无 socket、无 subprocess。
3. **确定性**：所有随机数、时间戳、UUID v4 均不出现在 evidence 数据模型中；`evidence_id` 完全由 `sha256(content + source_type + source_id)` 派生；同一三元组必得同一 id。
4. **零侵入**：未对 `sandbox_record.py`、`sandbox_review.py`、`sandbox_safety.py`、`sandbox_recheck.py`、`sandbox_explanation.py` 任何一行做修改；PM 台账与 L0～L6 验收文档未触碰。

## 4. 专项覆盖与零调用证据

测试文件 `tests/test_sandbox_evidence_l7_1.py` 共 **48 项**（RED 5 + GREEN 38 + STATIC 5），覆盖任务书全部 PM 探针：

| 探针 | 测试 |
|---|---|
| 数据结构确定性（同三元组 → 同 id） | `test_l7_1_green_packet_deterministic_same_triple_same_id` |
| 数据结构区分性（不同三元组 → 不同 id） | `test_l7_1_green_packet_different_*` ×3 |
| Source policy 完备性（5 上下文 + 未知上下文 + 字符串别名） | `test_l7_1_green_source_policy_*` ×7 |
| Scope 判定（同 run / 跨 run / 源不匹配 / 缺失 trace / 不变量 / 无副作用） | `test_l7_1_green_scope_*` ×6 |
| RAG 不可用策略（4 路分发 + 2 路 `rag_available=True`） | `test_l7_1_green_rag_*` ×3 |
| Verifier 四路径 + packet↔citation 不一致 + inquiry 全 INVALID | `test_l7_1_green_verifier_*` ×6 |
| 错误路径：`content_digest` 篡改 / `evidence_id` 篡改 / 未知 source / 空 content | `test_l7_1_green_packet_rejects_*` ×4 |
| Immutability：`pydantic.ValidationError` on setter | `test_l7_1_green_packet_is_frozen` |
| 枚举与 closed sets：`ALLOWED_SOURCE_TYPES` / `SANDBOX_AGENT_CONTEXT_VALUES` | `test_l7_1_green_*` ×3 |
| 错误语义 chainless / payload-free | `test_l7_1_green_error_chainless_and_payload_free` |
| Slots（frozen policy/scope/verifier） | `test_l7_1_green_classes_have_empty_slots` |
| AST 边界：无 `open/print/breakpoint/exec/eval/compile`、无 network / os / .env 引用 | `test_l7_1_green_no_forbidden_calls`, `test_l7_1_green_no_environ_or_getenv` |
| Import 根：未越界 | `test_l7_1_green_no_new_import_roots` |
| 输入纯净（pure function 不改 input） | `test_l7_1_green_no_mutation_of_inputs`, `test_l7_1_green_scope_pure_function_no_mutation` |
| Canonical JSON 字节稳定 | `test_l7_1_green_pure_function_canonical_determinism` |
| RED gap tests（5） | `TestSandboxEvidenceRed.*` |

## 5. GREEN、静态与回归证据

| 门禁 | 结果 |
|---|---|
| `uv run pytest tests/test_sandbox_evidence_l7_1.py -q -rs` | `48 passed in 3.64s`（RED 5 + GREEN 38 + STATIC 5）；真实 collection RED 为退出码 2、`collected 0 items / 1 error`、`ImportError: cannot import name 'SandboxEvidencePacket'` |
| L6-1/L6-2/L6-3/L6-4 专项回归 | `12 + 32 + 15 + 13 = 72 passed`；无回归 |
| L5-1/L5-2/L5-3/L5-4 专项回归 | `14 + 18 + 84 + 60 = 176 passed`；无回归 |
| L5+L6+L7-1 全链 | `296 passed` |
| `uv run ruff check app/agent_runtime/sandbox_evidence.py tests/test_sandbox_evidence_l7_1.py` | `All checks passed!` |
| `uv run mypy app/agent_runtime/sandbox_evidence.py` | `Success: no issues found in 1 source file` |
| `uv lock --check` | 锁文件未变更（沿用 L6-4 锁），未引入新依赖 |

## 6. 实际范围、未决限制与回退

本交付只包含任务合同允许的三个文件：

1. `app/agent_runtime/sandbox_evidence.py`：纯新增模块（**未修改** `sandbox_record.py` / `sandbox_safety.py` / `sandbox_review.py` / `sandbox_recheck.py` / `sandbox_explanation.py` 任何已 accepted 代码）；
2. `tests/test_sandbox_evidence_l7_1.py`：L7-1 唯一专项测试（48 项）；
3. `docs/dev-handoff/agent-refactor-l7-1-sandbox.md`：本交付/验收载体。

未修改 PM 台账、Legacy、RAG、UI、Domain 医疗事实、配置、依赖、migration、public flag、Runtime、Gateway、L5 review/recheck、L6 record DTO/assembler/verifier/store/serialize/narration 或部署材料。

未决限制：当前实现不接入真实 LangGraph `Command`、Runtime、HTTP、RAG、Embedding、向量检索或外部服务。真实临床、患者服务、商业/公开生产和人体研究继续 NO-GO。L7-2（真实 RAG 检索节点）、L7-3（claim-to-evidence 映射链表）、L7-4（RAG 评估集）属于后续任务，不在本交付中预先实现。

若独立验收失败，应以单一交付提交为单位执行 `git revert <delivery-commit>`，保留 RED、GREEN 和失败证据；不得 reset 或覆盖历史。

## 7. 交付提交约定

- 使用单一开发交付提交，提交消息：`feat: add L7-1 sandbox evidence data model and policy`。
- exact parent 必须为基线 `6b49238`（L6-4 验收提交）。
- 提交必须只含第 6 节三个文件；提交后全部 tracked，工作区 clean。

---

**已交付，申请验收。**
