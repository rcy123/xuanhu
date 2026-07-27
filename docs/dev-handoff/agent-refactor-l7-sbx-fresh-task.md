# L7-SBX-FRESH Evidence/RAG 离线参考组合整体任务书

> 任务状态：已发布 / 待交付
> 发布日期：2026-07-27
> 当前管理基线：`97ebd5ec3b215550fc312471c583495997c22d90`
> 当前代码基线：`e8e0973e527abc238466b0b0d0734ca4c3a35083`
> 授权来源：`ACC-20260727-058`、`DEC-20260727-054`
> 轨道：L7-SBX（offline、fixed-synthetic、unit/in-memory reference composition）
> 交付文档：`docs/dev-handoff/agent-refactor-l7-sbx-fresh.md`

## 1. 发布原因与任务身份

旧 L7 发布 `e8f0666`、实现 `c5b7152` 和本地验收草稿已由 `21004b9` 撤回或封存，不属于当前基线。本任务是从当前已重新验收的 L5-SBX/L6-SBX 代码树出发发布的全新合同，不读取、恢复、cherry-pick、比较或复用旧 L7 内容。

`L7-SBX-FRESH` 是一个有限、单模块、单专项测试面的整体 L7 沙盒任务。它关闭实施路线第 12 节在 offline/fixed-synthetic/in-memory reference composition 范围内的 EvidencePacket、source policy、检索 trace、claim-to-evidence、citation verifier、降级策略、无伪 citation、Context data/tool 投影、checkpoint-safe 重放和质量回归合同；不授予 L7-PROD。

## 2. 目标

新增一个独立于模型节点、产品 RAG 与现有 L0～L6 实现的沙盒证据参考组合：

1. 严格冻结的 EvidencePacket：evidence ID、source type、source/chunk ID、score/rank、content digest、retrieval trace。
2. 固定 source policy：Syndrome 只允许 `theory/case`；Formula 只允许 `formula/herb`，并按 claim kind 进一步限制。
3. 两个确定性离线 retrieval node（Syndrome/Formula），只消费已准入的 fixed-synthetic in-memory bundle，不调用模型、网络或数据库。
4. retrieval trace 继承调用方 graph run/trace，并生成确定性的 retrieval run identity；不同 graph run 的 evidence 不可互见。
5. 以 `SandboxEvidenceStore` 模拟 `agent_evidences` 的 append-only、幂等、canonical in-memory reference persistence；不声称完成产品数据库表。
6. 为 Syndrome basis 及 Formula 的方名、药味、剂量、加减理由建立 claim-to-evidence links。
7. Citation Verifier 检查 evidence ID、source type、当前 run 可见性、content/link digest 和必需 claim link 完整性。
8. 实现 `model_knowledge_only` fallback 与 `hard_block` 两种显式策略；无 Evidence 时引用集合必须为空，禁止伪 citation/出处。
9. Evidence 只能进入显式 `context_data_tool` 投影，禁止生成或进入 system/developer prompt layer。
10. 提供 canonical snapshot/restore 和确定性 replay；RAG 不可用不得破坏既有 no-RAG/checkpoint 路径。
11. 建立固定合成质量集，覆盖可追溯性、错误 source type、run 隔离、fallback/hard block、篡改、撤权和重放。

## 3. 明确非目标

- 不修改或接线 `app/rag/*`、Milvus、PostgreSQL、Redis、embedding/model gateway、HTTP/API、Runtime、MainGraph、GraphRunner、部署或前端。
- 不修改现有 L0～L6、L4.5-11 的源码、测试、schema、handoff 或 accepted 合同。
- 不修改 `SyndromeDraft`、`FormulaDraft`、`SandboxMedicalRecordData` 或任何生产/现有沙盒 DTO；本任务输出独立的 enriched reference DTO。
- 不写真实 `agent_evidences` 数据库表、迁移、ORM 或 repository；in-memory store 只证明参考语义。
- 不使用真实患者、医师、机构、临床、商业、公开或人体研究数据；不读取 `.env`、ignored `data/`、stash、旧提交或未跟踪 `.claude/`。
- 不宣称 scanner 是全面 PII 检查、固定 fixture 是真实合成来源证明、或工程通过等于临床/隐私/法务/伦理批准。
- 不授予 L7-PROD、L8、L9 或任何生产接线。

## 4. 允许修改范围

开发交付只允许新增以下三个 tracked 文件：

```text
app/agent_runtime/sandbox_evidence.py
tests/test_sandbox_evidence_l7.py
docs/dev-handoff/agent-refactor-l7-sbx-fresh.md
```

允许从当前 HEAD 的 `app/agent_runtime/context.py`、`state.py`、`sandbox_review.py`、`sandbox_recheck.py`、`sandbox_record.py`、`app/schemas/syndrome.py`、`app/schemas/formula.py` 读取公共类型和已验收合同；禁止修改这些文件，禁止导入其私有 helper 作为新的 authority。

PM 在发布、验收事务中可修改 `docs/01_agent部分优化/项目管理/00`～`05` 和本任务书；开发实现者不得修改 PM 台账。

## 5. 必须实现的公共合同

具体命名可在 handoff 中做不改变语义的微调，但以下能力必须可从 `sandbox_evidence.py` 导入并独立测试：

- 严格模型：`SandboxEvidencePacketV1`、`SandboxRetrievalTraceV1`、`SandboxEvidenceBundleV1`、`SandboxEvidenceClaimV1`、`SandboxClaimEvidenceLinkV1`、`SandboxEvidenceResultV1`、`SandboxEvidenceContextV1`、`SandboxEvidenceStoreSnapshotV1`。
- policy：Syndrome/Formula agent kind、source type、claim kind、fallback policy 的闭集枚举与 exact mapping。
- authority：fixed bundle registry/authorizer 与 injected claim verifier；调用方字段、自带 digest 或 snapshot 不能自证授权。
- nodes：Syndrome/Formula offline retrieval node；同输入、同 bundle、同 run envelope 必须得到 byte-identical 输出。
- verifier/integrator：检查来源、run、digest、links 完整性，输出 `rag_supported`、`model_knowledge_only` 或固定 hard-block failure。
- store/replay：append-only in-memory put/get、同 key 同 bytes 幂等、同 key 异 bytes 拒绝、canonical snapshot/restore、按 graph run 可见性过滤。
- pipeline/reference composition：retrieve → store → claim link → verify → context projection；no-RAG 分支不调用 retrieval/claim verifier，引用为空。

所有 Pydantic authority DTO 使用 `frozen=True`、`extra="forbid"`、strict 校验；标识符和 digest 必须有有限长度/格式；公开 scope 参数要求 exact built-in `str`，拒绝标量子类、hidden/extra/private state 和错误模型图。

## 6. Source policy 与 claim 完整性

| Agent | Claim kind | 允许 source type |
|---|---|---|
| Syndrome | `syndrome_basis` | `theory`, `case` |
| Formula | `formula_name` | `formula` |
| Formula | `herb` | `herb` |
| Formula | `dosage` | `herb` |
| Formula | `modification_reason` | `formula`, `herb` |

- `rag_supported` 时，输入中每个 required claim 必须至少有一个可见、有效 link；不得用一个未知/错误类型 evidence 补齐。
- `model_knowledge_only` 时 evidence packets、claim links、citation/context references 必须全部为空。
- `hard_block` 时返回固定、payload-free、chainless failure；不得降级为知识回答。
- EvidencePacket 必须绑定 bundle digest、content digest、graph/run/trace、retrieval node 和结果 rank；rank 唯一且从 1 连续。

## 7. 资源与错误边界

最少实施以下资源上限，具体常量写入 handoff：

- bundle items ≤ 256；单次 result items ≤ 32；claims/links ≤ 128；单 snippet UTF-8 ≤ 1024 bytes；query UTF-8 ≤ 8 KiB；canonical snapshot ≤ 256 KiB。
- 超限全部拒绝，不静默截断；rank 只对已准入且未超限结果排序。
- 错误使用固定枚举，至少区分 schema、version、digest、limit、authority、source policy、run visibility、claim completeness、unavailable、integrity/internal；消息不得包含 query、evidence content、claim 文本、异常 payload、密钥或签名。
- 所有对外错误必须 `raise ... from None` 或等效 chainless 结果。

## 8. 有限威胁模型

覆盖公共能力路径内的同进程注入、authorizer/verifier 回调重入、实例方法遮蔽、authority/store/锁/容器替换、hidden/extra/private 模型状态、标量子类、跨 run 引用和 snapshot 自重算。外部回调前后必须复核 sealed identity/state；撤权后的新操作全部 fail-closed，重授权只能产生新的有效操作，不能使旧 snapshot 自行恢复权威。

不覆盖任意私有/类级篡改、绕锁写内存、解释器控制或进程隔离；不得把 Python 对象包装描述为安全沙箱或生产信任根。

## 9. 先红后绿

1. 先只新增专项测试，生产模块不存在时至少证明 collection/import RED，以及 policy/trace/citation/fallback/replay 能力缺失。
2. handoff 记录 RED 的精确命令和摘要；不得伪造先红历史。
3. 实现后专项 GREEN 必须覆盖本任务全部 11 项目标、source/claim 矩阵、资源上限和威胁模型探针。

## 10. 验收门禁

### 聚焦与组合

```powershell
$env:UV_OFFLINE='1'
uv run pytest tests/test_sandbox_evidence_l7.py -q
uv run pytest tests/test_l5_authority_rework.py tests/test_l5_1_sandbox_safety_adapter.py tests/test_l5_2_sandbox_safety_explanation.py tests/test_l5_3_sandbox_reviewer_interrupt_resume.py tests/test_l5_4_sandbox_modify_full_recheck.py tests/test_sandbox_record_l6_1.py tests/test_sandbox_record_l6_2.py tests/test_sandbox_record_l6_3.py tests/test_sandbox_record_l6_4.py tests/test_sandbox_evidence_l7.py -q
```

### 全量与静态

```powershell
$env:UV_OFFLINE='1'
uv run pytest -m "not integration" -q
uv run ruff check .
uv run ruff format --check .
uv run mypy app scripts
uv lock --check
git diff --check
```

既有全量与类型债务只作为校准点：当前权威记录为非 integration `1963 passed, 362 deselected`、`mypy app scripts` 159 个源码文件无错误；新增测试会提高 passed 数，环境/依赖导致的差异必须如实记录，不能机械要求旧数字不变。

### 独立 Review 与 PM 探针

- 独立 Reviewer 必须按 P0/P1/P2/P3 报告；P0/P1/P2/P3 全为 0 才能接受。
- PM 至少复验：wrong-source、cross-run、missing-link、fake citation、fallback、hard block、content/link tamper、bundle revoke/re-authorize、snapshot self-recompute、callback reentry/object replacement、no-RAG zero-call、deterministic replay。
- 验收必须绑定 exact implementation commit；开发者自报不能替代独立 Review 或 PM 复验。

## 11. 停止条件

- 需要修改允许列表外任何文件才能通过；
- 需要读取/恢复/比较旧 L7、stash、`.env`、真实数据或未跟踪 `.claude/`；
- 引入新依赖、锁文件或配置变化；
- 需要网络、DB、Milvus、Redis、模型、真实 checkpoint 或产品 Runtime；
- 发现 P0/P1，或同一 authority/integrity defect family 连续出现且补丁开始扩张；
- no-RAG、L5 或 L6 回归失败且不能证明与本任务无关。

命中停止条件后保留失败证据，由 PM 发布 bounded rework；不得顺手扩范围。

## 12. 交付记录要求

`docs/dev-handoff/agent-refactor-l7-sbx-fresh.md` 必须记录：

- 实际文件清单和实现合同；
- 真实 RED、GREEN、组合、全量、Ruff、format、mypy、lock、diff 原始摘要；
- threat/resource/source/claim 矩阵覆盖；
- exact implementation commit；
- 已知限制、未运行门禁和原因；
- 明确说明 L7-PROD、真实 RAG/DB/Runtime/临床/公开用途仍为 NO-GO。

## 13. 状态边界

- 本任务发布只授权 L7-SBX reference composition，不授权产品接线或专业使用。
- 只有全部验收门禁、独立 Review 和 PM 探针通过，L7-SBX 才能标记 engineering complete。
- L7-SBX engineering complete 仍不等于 L7-PROD、L8 或 L9 授权。
- G1～G6、EXT-001、EXT-002 继续 `deferred_for_clinical_use`；真实/公开用途触发时恢复 `external_approval_required`。
