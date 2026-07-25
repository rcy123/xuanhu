# L7-1 Evidence Schema & Policy（Sandbox）

## 发布信息

| 项目 | 内容 |
|---|---|
| 状态 | 已发布 / 待交付 |
| 发布人 | Codex（工程项目经理） |
| 发布日期 | 2026-07-25 |
| 基线 | `6b49238`（L6-4 交付提交） |
| 依赖 | L0～L6 全部 accepted / engineering complete |
| 阻塞 | 无活跃工程阻塞 |
| 交付文件 | `docs/dev-handoff/agent-refactor-l7-1-sandbox.md`（本文件） |

## 目标

在个人学习、非临床、仅合成数据沙盒范围内，实现 Evidence/RAG 增强的第一层：**Evidence 数据模型与约束策略**。

具体目标：

1. **定义 `SandboxEvidencePacket`（确定性、不可变、纯数据）**：
   - `evidence_id: str` — 确定性派生（`sha256(content + source_type + source_id)`），非随机
   - `source_type: Literal["theory", "case", "formula", "herb"]` — 证据来源类型，与 Agent 上下文严格绑定
   - `source_id: str | None` — 原始来源标识（如 chunk ID、文献 ID）
   - `content: str` — 证据原文片段
   - `content_digest: str` — `sha256(content)`，验证内容完整性
   - `retrieval_trace_id: str | None` — 检索 trace ID 末端（可空，沙盒阶段允许未接入真实检索链时留空）
   - 使用 pydantic frozen model（`model_config = ConfigDict(frozen=True)`），与 L5/L6 沙盒 DTO 模式一致
   - **不包含**：患者数据、模型输出、时间戳、随机数、网络标识

2. **定义 `EvidenceSourcePolicy`（纯函数，验证 source type 与 agent 上下文的合法性）**：
   - `allowed_sources_for(context: AgentContext) -> set[str]`：
     - `syndrome` 上下文 → `{"theory", "case"}` ​​（证型依据只能引用理论/医案）
     - `formula` 上下文 → `{"formula", "herb"}` （处方依据只能引用方剂/本草）
     - `modification` 上下文 → `{"formula", "herb"}`（加减依据只能引用方剂/本草）
     - `inquiry` / `sufficiency` 上下文 → `set()`（问诊/完备性不引用 Evidence）
   - 其他上下文 → `set()`（未显式允许的上下文不可引用 Evidence）
   - 纯函数：同一 `(context, evidence_packet)` → 同一 boolean 结果（确定性、无副作用）

3. **定义 `SandboxEvidenceScope`（证据可见性规则）**：
   - `is_visible(evidence, run_context) -> bool`：
     - 默认规则：evidence 仅在同一 `trace_id` run 内可见
     - 跨 run 引用 → `False`（除非显式策略允许）
     - 源策略交叉校验：evidence 的 `source_type` 必须在调用者的 `allowed_sources` 内
   - 不维护全局注册表、不依赖外部存储、纯函数判定

4. **定义 `RAGUnavailablePolicy`（枚举 + 纯函数判定）**：
   ```python
   class RAGUnavailablePolicy(str, enum.Enum):
       FALLBACK_TO_MODEL_KNOWLEDGE = "fallback_to_model_knowledge"
       HARD_BLOCK = "hard_block"
   ```
   - `decide_retrieval_behavior(rag_available: bool, policy: RAGUnavailablePolicy) -> RetrievalBehavior`：
     - `rag_available=True` → 正常检索
     - `rag_available=False, policy=HARD_BLOCK` → `RetrievalBehavior.BLOCKED`
     - `rag_available=False, policy=FALLBACK_TO_MODEL_KNOWLEDGE` → `RetrievalBehavior.FALLBACK`
   - `RetrievalBehavior` 枚举：
     - `RETRIEVE`：执行正常 RAG 检索
     - `FALLBACK`：不使用 RAG 检索，但允许模型基于自身知识（标记 `evidence_mode=model_knowledge_only`）
     - `BLOCKED`：上下文被阻断，代理不应继续

5. **建立 `SandboxEvidenceVerifier`（确定性校验器）**：
   - `verify_citation(citation_source_type: str, evidence_packet: SandboxEvidencePacket | None, policy: EvidenceSourcePolicy, context: str) -> CitationVerdict`：
     - 若 `citation_source_type not in allowed_sources_for(context)` → `INVALID_SOURCE_TYPE`
     - 若 `evidence_packet is None and citation exists` → `MISSING_EVIDENCE`（禁止无证据的伪引用）
     - 若证据 `content_digest != sha256(content)` → `TAMPERED_CONTENT`
     - 通过 → `PASS`
   - 纯函数、无副作用、确定性

## 非目标

- 不实现真实 RAG 检索节点（属于 L7-2）
- 不实现 claim-to-evidence 映射与链表（属于 L7-3）
- 不实现 RAG 评估集（属于 L7-4）
- 不修改 L5～L6 已 accepted 的 sandbox 模块（`sandbox_safety`、`sandbox_explanation`、`sandbox_review`、`sandbox_recheck`、`sandbox_record`）
- 不接入真实 LangGraph `Command`、Runtime、HTTP、容器、部署、DB、RAG、Gateway、Embedding 或外部服务
- 不连接真实患者数据、真实病历、真实知识库或生产模型日志
- 不生成真实临床诊断、治疗建议、处方或医疗决策
- 不修改 Legacy engine/review/record、配置、依赖、前端、UI 或部署
- 不声称临床有效、医疗安全、法规合规或获得专业批准

## 允许修改范围

只允许修改/新增以下文件，全部 tracked：

1. `app/agent_runtime/sandbox_evidence.py` — 新增文件，包含 `SandboxEvidencePacket`、`EvidenceSourcePolicy`、`SandboxEvidenceScope`、`RAGUnavailablePolicy`、`SandboxEvidenceVerifier`
2. `tests/test_sandbox_evidence_l7_1.py` — L7-1 唯一专项测试
3. `docs/dev-handoff/agent-refactor-l7-1-sandbox.md` — 交付 handoff

允许从以下沙盒模块只读引用已 accepted 的类型（import，不修改）：
- `sandbox_review` 中的 `SandboxReviewAction`
- `sandbox_safety` 中的 `SandboxSafetyDecision`
- `sandbox_record` 中的 `_StrictFrozenModel`（纯继承，不修改）

## 禁止修改范围

- 禁止修改 `sandbox_record.py` 中 L6-1/L6-2/L6-3 已验收的任何代码
- 禁止修改 `sandbox_safety.py`（L5-1）、`sandbox_explanation.py`（L5-2）、`sandbox_review.py`（L5-3）、`sandbox_recheck.py`（L5-4）的任何代码、测试或 handoff
- 禁止修改 `pyproject.toml`、`README.md`、配置、依赖、前端、Legacy、Runtime、DB、Gateway、PM 台账
- 禁止修改 L0～L6 任何已验收的管理文档、验收记录、决策记录
- 禁止读取 `.env`、ignored `data/` 或任何外部存储
- 禁止网络调用、子进程、真实文件写入（专项测试的临时 in-memory 数据除外）
- 禁止在 evidence 数据模型或策略中包含随机数、时间戳、UUID v4 等非确定性来源
- 禁止将证据策略绑定到具体模型提示词或 Agent 实现逻辑（L7-1 只定义数据和策略层）

## 先红后绿要求

1. 在未新增 `sandbox_evidence.py` 时，以真实 RED 证明以下缺口：
   - 无 `SandboxEvidencePacket` 数据结构定义
   - 无 `EvidenceSourcePolicy` 来约束 source type → agent context 映射
   - 无 `SandboxEvidenceScope` 来约束证据可见性
   - 无 `RAGUnavailablePolicy` 定义（不可用行为未标准化）
   - 无 `SandboxEvidenceVerifier` 来校验 citation→evidence 关联

2. 修复后 GREEN 必须覆盖：
   - `SandboxEvidencePacket` 可通过确定性构造函数创建，`evidence_id` 由 content 派生
   - **同一 `(content, source_type, source_id)` 三元组 → 同一 `evidence_id`**
   - 不同三元组 → 不同 `evidence_id`
   - `EvidenceSourcePolicy.allowed_sources_for()` 对 syndrome/formula/modification/inquiry 返回正确允许集
   - `SandboxEvidenceScope.is_visible()` 对同 run / 跨 run / 源不匹配 返回正确可见性
   - `RAGUnavailablePolicy.decide_retrieval_behavior()` 对 4 种组合（available/unavailable × HARD_BLOCK/FALLBACK）返回正确行为
   - `SandboxEvidenceVerifier.verify_citation()` 对 INVALID_SOURCE_TYPE / MISSING_EVIDENCE / TAMPERED_CONTENT / PASS 四种判定都正确
   - AST 边界：无 `open/print/breakpoint/exec/eval/compile`、无 network/socket/http 调用
   - 不新增未被批准的 import 根（继承 L6 已批准的集合）

## 验收标准

### 独立 Review
- P0/P1/P2/P3 全为 0
- 不修改任何 accepted L0～L6 代码的前提下，L7-1 模块独立可测

### 独立 CI
- L7-1 专项测试全部通过（expected counts：≥ 25 passed）
- L6-1/L6-2/L6-3/L6-4 专项回归全部通过（`12 + 32 + 15 + 13 passed`）
- L5-1/2/3/4 回归专项全部通过（`14/18/84/60 passed`）
- Safety `71/3 deselected`、privacy `76`、L0 `131`、Ruff/mypy/lock 全通过
- scope/tracked/diff/exact/clean 全通过

### PM 探针
1. **数据结构确定性**：同一 `(content, source_type, source_id)` → 同一 `evidence_id`
2. **Source policy 完备性**：syndrome/formula/modification/inquiry/unknown 五组策略返回值正确
3. **Scope 判定**：同 run → visible；跨 run → not visible；源不匹配 → not visible
4. **RAG 不可用策略**：4 种组合行为正确（block 阻断 / fallback 降级标记 / retrieve 正常）
5. **Verifier 四路径**：INVALID_SOURCE_TYPE、MISSING_EVIDENCE、TAMPERED_CONTENT、PASS 均通过

## 停止条件

- 任何修改超出允许文件范围 → 停止，重新发布
- 任何真实患者/临床数据进入测试 → 立即停止
- 需要修改 L5/L6 已 accepted 代码才能通过 → 停止，发布对应 rework 而非在当前任务中修复
- 发现 P0/P1 → 停止交付，发布 bounded rework
- Evidence 数据模型含随机/非确定性标识生成 → 停止，降级为哈希派生
- 任务试图实现真实 RAG 检索或 claim links（属于 L7-2/L7-3 范围）→ 停止，要求重新裁剪范围

## 记录要求

1. 开发交付时更新 `agent-refactor-l7-1-sandbox.md` handoff
2. 不得由开发交付声明替代 PM 验收
3. 验收通过后，PM 追加 `ACC-YYYYMMDD-NNN` 验收记录、更新任务台账和当前状态
4. L7-1 验收通过后，L7 阶段状态更新为"进行中"

## 状态边界

- 本任务发布不等于 L7 完成（需 L7-1～L7-4 全部验收通过后标记）
- L7-1 验收后不自动开始 L7-2，需 PM 另行发布
- 真实临床、患者服务、公开生产继续 NO-GO
- G1～G6、EXT-001、EXT-002 继续 `deferred_for_clinical_use`

## 与 L5/L6 沙盒的设计一致性

1. **数据结构风格**：
   - `SandboxEvidencePacket` 使用 pydantic frozen model，与 `SandboxMedicalRecordData` 一致的 `_StrictFrozenModel` 基类
   - 异常复用 `SandboxRecordError` 模式（chainless、payload-free ValueError 子类）
   - 不作 `model_validate` / `model_dump` 之外的序列化假设

2. **策略即纯函数**：
   - `EvidenceSourcePolicy.allowed_sources_for()` 不维护可变状态，不查配置，不调用外部
   - `SandboxEvidenceScope.is_visible()` 入参即出参，没有副作用
   - `RAGUnavailablePolicy.decide_retrieval_behavior()` 是枚举值的封闭有限映射
   - `SandboxEvidenceVerifier.verify_citation()` 是纯函数的 4 路分支

3. **不与 L0～L6 沙盒耦合**：
   - L7-1 不引用 or 依赖于 `sandbox_record.py` / `sandbox_review.py` / `sandbox_safety.py` 的内部实现
   - 仅引用已 accepted 的 DTO 基类和枚举（只读类型继承）
   - 不修改任何已有 sandbox 模块
