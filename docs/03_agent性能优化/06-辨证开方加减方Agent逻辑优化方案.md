# 辨证—开方—加减方 Agent 逻辑优化方案

> 状态：✅ 已实施（P0/P1/P2/Reranker 全部实现；P0+P1 可投产；P2+Reranker 需真实后端 A/B 评测）
> 实施报告：[06-实施评估报告-2026-08-06.md](06-实施评估报告-2026-08-06.md)
> 关联文档：[03-优化计划与排期.md](03-优化计划与排期.md)、[阶段优化记录-OP3](阶段优化记录-OP3Milvus异步化与状态缓存-2026-08-06.md)
> 日期：2026-08-06

---

## 1. 问题诊断

### 1.1 当前架构

```
┌──────────────┐     ┌──────────────────┐     ┌──────────────────────┐
│ 辨证 agent    │ ──→ │ base_formula agent │ ──→ │ modification agent   │
│ (1 × LLM)    │     │ (1 × LLM)          │     │ (1 × LLM)            │
│              │     │                    │     │                      │
│ RAG:         │     │ RAG:               │     │ RAG:                 │
│  query =     │     │  query =           │     │  query =             │
│  key=value   │     │  证型=；治法=；     │     │  证型=；治法=；       │
│  拼接        │     │  症状=             │     │  症状=               │
│  sources =   │     │  sources =         │     │  sources =           │
│  theory+case │     │  formula+herb+case │     │  formula+herb+case   │
└──────────────┘     └──────────────────┘     └──────────────────────┘
                            ↑                        ↑
                            │         完全相同的 RAG 参数！       │
                            └──────────┬─────────────────────┘
                                       │
              retrieve_formula_evidence(retriever, trusted_syndrome.output,
                                        input_payload.context_observations)
```

### 1.2 核心缺陷

| # | 问题 | 影响 |
|---|------|------|
| **P0** | Modification RAG 与 base_formula RAG 使用**完全相同的检索参数**——query 相同、sources 相同、top_k 相同。Modification 明明知道已选的基础方（`input_payload.base_formula` 已传入），RAG 检索却完全没利用 | 加减方的 RAG 检索不会返回单味药特性、药对配伍等加减决策真正需要的知识；两次 RAG 检索结果高度重叠，白白浪费一次 embedding + Milvus 往返 |
| **P1** | Base formula 只输出**1 套方子**，后续 modification 在这唯一切入点上做加减。如果选方本身就偏了，modification 在错误方向上微调，最终结果全偏 | 单路径无纠错机制，选方错误无法恢复 |
| **P2** | 辨证 RAG query 是 `key=value` 拼接（如 `chief_complaint.symptom=咳嗽；present_illness.cough=干咳少痰`），与知识库中医案的叙事性自然语言处于不同的语义空间 | 向量相似度检索可能找不到真正相似的医案——embedding 模型对结构化 key=value 和医学叙事的编码方式不同 |
| **P3** | `build_formula_query` 是纯代码函数（证型=；治法=；症状=），未利用辨证 agent 在 LLM 推理过程中已经做的"这个证对应什么方"的医学判断 | 检索 query 缺乏医学推理的提炼，可能过于泛化 |
| **Reranker** | 当前 reranker 是 MVP 加权线性求和（`vector×0.65 + fulltext×0.25 + source_priority×0.10`），未利用语义级别的深度相关性判断 | 向量检索 top-K 结果中，排名靠前的不一定是真正和 query 语义最相关的——加权求和无法捕捉深层的医学语义匹配 |

---

## 2. P0 · Modification RAG 差异化（立即执行）

### 2.1 现状分析

`execute_modification_draft`（[formula_draft.py:962-963](../app/agents/formula_draft.py#L962)）：

```python
# 当前代码：与 base_formula 完全相同的 RAG 调用
retrieved_evidence = tuple(
    await retrieve_formula_evidence(
        retriever,
        trusted_syndrome.output,           # ← 仅有 syndrome
        formula_input.context_observations  # ← 仅有 observations
    )
)
```

但 `ModificationDraftInput`（[formula.py:295-324](../app/schemas/formula.py#L295)）**已经有**：

```python
base_formula: FormulaComposition          # 基础方全文（含每味药的名称和剂量）
base_formula_rationale: str | None        # 为什么选这个方
base_confidence: float                    # 选方置信度
```

这些信息**完全没有被 RAG 检索利用**。

### 2.2 设计

#### 2.2.1 检索策略差异化

```
base_formula RAG（保持不变）:
  sources = ("formula", "herb", "case")
  目标: "什么方子治这个证"——方证对应

modification RAG（差异化）:
  sources = ("herb", "case")             ← 不再搜 formula，基础方已定
  目标: "这个方子的药怎么加减"——药对配伍 + 加减经验
```

#### 2.2.2 新增 `build_modification_query`

在 [reasoning_retrieval.py](../app/rag/reasoning_retrieval.py) 新增：

```python
# modification RAG 专用 sources
MODIFICATION_PRIMARY_SOURCES: tuple[str, ...] = ("herb", "case")

def build_modification_query(
    syndrome: Any,
    observations: Sequence[Any],
    base_formula: Any,                    # ← 新增：基础方信息
    *,
    max_chars: int | None = None,
) -> str:
    """构造加减方检索 query。

    与 build_formula_query 的关键区别：
    - 包含基础方名称和组成，让检索聚焦该方的加减经验
    - 强调待调症状，引导检索药对配伍和单药特性
    - 不再重复证型/治法主导（base 阶段已覆盖）
    """
    limit = max_chars if max_chars is not None else get_settings().rag_query_max_chars

    parts: list[str] = []

    # 1. 基础方信息（核心差异化）
    formula_name = getattr(base_formula, "name", None)
    if formula_name:
        parts.append(f"基础方={formula_name}")

    # 2. 方剂组成（让检索找到涉及相同药味的加减医案）
    herbs = getattr(base_formula, "herbs", None) or ()
    if herbs:
        herb_text = "、".join(
            f"{h.name}{h.dose}{h.unit}" if hasattr(h, "dose") else h.name
            for h in herbs
        )
        parts.append(f"组成={herb_text}")

    # 3. 证型与治法（保留但不主导）
    name = getattr(syndrome, "syndrome", None)
    if name:
        parts.append(f"证型={name}")

    # 4. 症状摘要（加权：强调待调症状）
    symptom_query = build_syndrome_query(observations, max_chars=limit // 2)
    if symptom_query:
        parts.append(f"待调症状={symptom_query}")

    query = "；".join(parts)
    if len(query) > limit:
        query = query[:limit]
    return query or ""
```

#### 2.2.3 新增 `retrieve_modification_evidence`

```python
async def retrieve_modification_evidence(
    retriever: Any,
    syndrome: Any,
    observations: Sequence[Any],
    base_formula: Any,
    *,
    top_k: int | None = None,
    logger_extra: dict[str, Any] | None = None,
) -> list[Evidence]:
    """加减方阶段检索。使用 herb+case 源，query 含基础方信息。"""
    settings = get_settings()
    k = top_k or settings.rag_formula_top_k
    query = build_modification_query(syndrome, observations, base_formula)
    if not query:
        logger.warning("modification RAG: 无可检索的查询，跳过检索（空证据模式）")
        return []
    return await _retrieve_with_degrade(
        retriever,
        query=query,
        primary_sources=list(MODIFICATION_PRIMARY_SOURCES),
        top_k=k,
        stage="modification",
        logger_extra=logger_extra,
    )
```

#### 2.2.4 `execute_modification_draft` 改动

```python
# formula_draft.py: execute_modification_draft（约 line 958-964）

base_formula = input_payload.base_formula  # 已有

# --- 改动前 ---
# retrieved_evidence = tuple(
#     await retrieve_formula_evidence(retriever, trusted_syndrome.output,
#                                     formula_input.context_observations)
# )

# --- 改动后 ---
retrieved_evidence: tuple[Evidence, ...] = ()
if rag_active and retriever is not None:
    retrieved_evidence = tuple(
        await retrieve_modification_evidence(
            retriever,
            trusted_syndrome.output,
            formula_input.context_observations,
            base_formula,                   # ← 传入基础方
        )
    )
```

#### 2.2.5 证据继承（可选增强）

Modification agent 的 context 可以同时包含：
- **base_formula 的 RAG 证据**（方剂知识，继承自 base 阶段，无需重搜）
- **modification 的 RAG 证据**（herb-level 检索结果）

在 [langgraph_reasoning.py:1014-1030](../app/services/langgraph_reasoning.py#L1014) 处，`mod_input` 可增加字段携带 base 阶段的 `retrieved_evidence`：

```python
mod_input = ModificationDraftInput(
    # ... 现有字段 ...
    base_retrieved_evidence_ids=tuple(
        e.evidence_id for e in base_result_evidence  # 从 base_result 获取
    ),
)
```

然后在 `build_formula_context` 中将继承的证据一并注入 modification 的 LLM context。

### 2.3 改动清单

| 文件 | 改动 | 风险 |
|------|------|------|
| [reasoning_retrieval.py](../app/rag/reasoning_retrieval.py) | 新增 `MODIFICATION_PRIMARY_SOURCES`、`build_modification_query`、`retrieve_modification_evidence` | 极低——纯增量 |
| [formula_draft.py](../app/agents/formula_draft.py) | `execute_modification_draft`: 调用 `retrieve_modification_evidence` 替代 `retrieve_formula_evidence`，传入 `base_formula` | 低——仅改 RAG 调用，不改 agent 逻辑 |
| [formula.py](../app/schemas/formula.py) | `ModificationDraftInput` 可选增加 `base_retrieved_evidence_ids` | 低 |

### 2.4 预期收益

| 指标 | Before | After |
|------|--------|-------|
| Mod RAG 与 Base RAG 结果重叠度 | ~80%（估计） | **~20%**（不同 sources + 不同 query） |
| Mod RAG 返回 herb-level 证据占比 | ~30%（淹没在 formula 结果中） | **~70%**（herb+case 源为主） |
| Mod agent 获得的加减相关知识 | 泛化的方剂知识 | 该基础方特有的药对配伍、剂量调整经验 |

### 2.5 验收标准

- [ ] `build_modification_query` 单元测试：包含基础方名 + 组成 + 证型 + 待调症状
- [ ] modification agent 集成测试通过（20 个 L4.4 reasoning subgraph 测试）
- [ ] `perf_reasoning_traffic.py` 真实流量无回归
- [ ] modification RAG 检索结果包含 herb 类证据（可通过 metrics/logs 验证）

---

## 3. P1 · 多套基础方方案（按置信度排序 + 阈值筛选 + 医师选择）

### 3.1 现状分析

`BaseFormulaDraft`（[formula.py:230-262](../app/schemas/formula.py#L230)）当前输出：

```python
class BaseFormulaDraft(BaseModel):
    decision: FormulaDraftDecision
    base_formula: FormulaComposition | None     # ← 只有 1 套
    rationale: str | None
    confidence: float
    # ...
```

编排层（[langgraph_reasoning.py:990-1011](../app/services/langgraph_reasoning.py#L990)）拿到唯一的 `base_formula` 后直接传给 modification。

**问题**：临床实践中，同一证型常对应多个候选方。如"风寒束表"可选川芎茶调散（偏头痛）、荆防败毒散（偏全身痛）、止嗽散（偏咳嗽）。当前单路径选一个，选偏了后面全偏。

### 3.2 设计

#### 3.2.1 核心理念：输出所有合理方案，由医师最终决策

不追求"AI 自动选出唯一最佳方子"，而是：
1. Base formula agent 输出多套**侧重不同**的候选方案，每套附置信度
2. 按**置信度阈值**过滤低质量方案
3. 全部通过阈值的方案**按置信度降序排列**，并行跑 modification
4. **全部加减结果**呈现给医师，由医师根据临床经验做最终选择

这样既利用了 LLM 的多路径探索能力，又尊重了医师的决策权。

#### 3.2.2 目标架构

```
base_formula agent ──→ 输出 ≥2 套候选方（按置信度降序）
  (输出 N 套方子,         │
   各有侧重角度)           ├── 置信度 ≥ 阈值 → 保留
                          ├── 置信度 < 阈值  → 丢弃
                          │
                          ↓ 通过阈值的方案 (M 套)
                          │
          ┌───────────────┼───────────────┐
          ↓               ↓               ↓
    modification    modification    modification
    (方案 A 加减)   (方案 B 加减)   (方案 C 加减)
    (并行)          (并行)          (并行)
          │               │               │
          └───────────────┴───────────────┘
                          ↓
              全部 M 套加减结果（按 base 置信度降序）
                          ↓
                    前端展示 → 医师选择
```

#### 3.2.3 Schema 变更

**新增 `BaseFormulaAlternative`：**

```python
class BaseFormulaAlternative(BaseModel):
    """单套基础方候选方案。"""
    model_config = ConfigDict(frozen=True, extra="forbid")

    formula: FormulaComposition
    angle: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="该方案的侧重角度，如'偏重祛风止痛，适用于头痛明显者'"
    )
    rationale: str = Field(min_length=1, max_length=600)
    confidence: float = Field(ge=0, le=1)
    modification_query: str | None = Field(
        default=None,
        max_length=600,
        description="该方加减阶段的检索方向提示"
    )
```

**修改 `BaseFormulaDraft`：**

```python
class BaseFormulaDraft(BaseModel):
    decision: FormulaDraftDecision
    # --- 保留向后兼容 ---
    base_formula: FormulaComposition | None = None   # 取 alternatives[0].formula
    rationale: str | None = None                      # 取 alternatives[0].rationale
    # --- 新增 ---
    alternatives: tuple[BaseFormulaAlternative, ...] = Field(
        default=(),
        min_length=0,
        max_length=4,
        description="侧重不同的基础方候选方案，按置信度降序排列（RAG 模式下≥2套）"
    )
    confidence: float = Field(ge=0, le=1)
    # ... 其余字段不变
```

#### 3.2.4 置信度阈值配置

在 [config.py](../app/core/config.py) 新增：

```python
# 基础方多方案筛选
base_formula_confidence_threshold: float = Field(
    default=0.45,
    ge=0.0,
    le=1.0,
    description="基础方候选方案的置信度阈值——低于此值的方案被丢弃，不进入 modification"
)
base_formula_min_alternatives: int = Field(
    default=1,
    ge=1,
    le=4,
    description="至少保留的基础方候选数（即使低于阈值也保留 top-N）"
)
base_formula_max_alternatives: int = Field(
    default=3,
    ge=2,
    le=4,
    description="最多保留的基础方候选数"
)
```

**筛选逻辑**：

```python
def filter_alternatives(
    alternatives: Sequence[BaseFormulaAlternative],
    threshold: float,
    min_keep: int,
    max_keep: int,
) -> list[BaseFormulaAlternative]:
    """按置信度阈值筛选候选方案。

    规则：
    1. 先按置信度降序排列
    2. 过滤低于阈值的方案
    3. 至少保留 min_keep 套（取 top-N，即使低于阈值）
    4. 最多保留 max_keep 套
    """
    # 始终按置信度降序
    sorted_alts = sorted(alternatives, key=lambda a: a.confidence, reverse=True)

    # 阈值过滤
    qualified = [a for a in sorted_alts if a.confidence >= threshold]

    # 保底：至少 min_keep 套
    if len(qualified) < min_keep:
        qualified = sorted_alts[:min_keep]

    # 封顶：最多 max_keep 套
    return qualified[:max_keep]
```

#### 3.2.5 Prompt 工程

Base formula agent system prompt 需引导输出多套方案：

```
你必须输出至少 2 套侧重不同的基础方方案（最多 3 套）。
每套方案必须有明确的侧重角度（angle 字段），说明该方适合哪种临床表现偏重。
方案之间必须在方剂选择或组成上有实质差异——不能仅是剂量微调。
按置信度降序排列（最推荐的在前）。

示例：
- angle: "偏重祛风止痛，适用于头痛明显、恶寒轻者"
  confidence: 0.82
  formula: 川芎茶调散（川芎10g 荆芥10g 防风10g 白芷10g 羌活10g 细辛3g 薄荷6g 甘草6g）

- angle: "偏重解表散寒，适用于全身酸痛、恶寒重者"
  confidence: 0.71
  formula: 荆防败毒散（荆芥10g 防风10g 羌活10g 独活10g 柴胡10g 前胡10g 桔梗10g 枳壳10g 茯苓15g 川芎6g 甘草6g）
```

#### 3.2.6 编排层改动

[langgraph_reasoning.py](../app/services/langgraph_reasoning.py) 中 `run_reasoning_draft_formula_node` 的核心逻辑变更：

```python
from app.core.config import get_settings

# ---- 阶段 1：基础方草稿（输出多套方案）----
base_result = await execute_base_formula_draft(...)
raw_alternatives = base_result.output.alternatives

# 降级：模型只输出了 1 套，兼容旧行为
if len(raw_alternatives) < 2:
    raw_alternatives = (BaseFormulaAlternative(
        formula=base_result.output.base_formula,
        angle="默认方案",
        rationale=base_result.output.rationale or "",
        confidence=base_result.output.confidence,
    ),)

# 置信度阈值筛选
settings = get_settings()
alternatives = filter_alternatives(
    raw_alternatives,
    threshold=settings.base_formula_confidence_threshold,
    min_keep=settings.base_formula_min_alternatives,
    max_keep=settings.base_formula_max_alternatives,
)

# ---- 阶段 2：并行跑 modification（每套通过筛选的方案一个）----
async def _run_mod_for_alternative(
    i: int,
    alt: BaseFormulaAlternative,
) -> tuple[int, FormulaExecutionResult | None]:
    mod_input = ModificationDraftInput(
        # ... 标准字段 ...
        base_formula=alt.formula,
        base_formula_rationale=alt.rationale,
        base_confidence=alt.confidence,
        modification_query_hint=alt.modification_query,  # 新增字段
    )
    mod_run_spec = RunSpec(
        run_id=_commit_run_id(claim, f"modification_{i}"),
        # ...
    )
    stage = await _run_formula_stage_with_retry(
        claim, repository, syndrome_result,
        execute_modification_draft, mod_run_spec,
        input_payload=mod_input,
        agent_spec=build_modification_draft_agent_spec(),
        step_label=f"modification_alt_{i}",
    )
    return (i, stage[0] if stage else None)

# 并行执行，保持原始索引（按置信度顺序）
mod_tasks = [
    _run_mod_for_alternative(i, alt)
    for i, alt in enumerate(alternatives)
]
mod_results_raw = await asyncio.gather(*mod_tasks)
# 按原始索引排序，保持置信度降序
mod_results_raw.sort(key=lambda x: x[0])
mod_results = [
    (alternatives[i], result)
    for i, result in mod_results_raw
    if result is not None
]

# ---- 阶段 3：全部结果持久化，供医师选择 ----
# 所有通过阈值且 modification 成功的方案，全部落库
# 前端按 base 置信度降序展示，由医师做最终选择
# 不在此层自动选出"唯一最佳方案"
```

#### 3.2.7 LLM 调用次数分析

| 场景 | 当前 | P1 后（N=2 并行） | P1 后（N=3 并行） |
|------|------|-------------------|-------------------|
| Syndrome | 1 | 1 | 1 |
| Base formula | 1 | 1 | 1 |
| Modification | 1 | 2（并行） | 3（并行） |
| 比较器 | — | —（不需要） | —（不需要） |
| **有效延迟** | 1+1+1 串行 | 1+1+1（并行 mod max） | 1+1+1（并行 mod max） |
| **总调用次数** | 3 | 4 | 5 |

关键：modification 并行跑，延迟不叠加。去掉自动比较器后，P1 不再引入额外的 LLM 调用。

### 3.3 改动清单

| 文件 | 改动 | 风险 |
|------|------|------|
| [formula.py](../app/schemas/formula.py) | 新增 `BaseFormulaAlternative`；`BaseFormulaDraft` 增加 `alternatives` 字段；`ModificationDraftInput` 增加 `modification_query_hint` | 中——Schema 变更，需兼容旧测试 |
| [config.py](../app/core/config.py) | 新增 `base_formula_confidence_threshold`、`base_formula_min_alternatives`、`base_formula_max_alternatives` | 极低 |
| [formula_draft.py](../app/agents/formula_draft.py) | `execute_base_formula_draft`: 适配多方案输出；`assemble_base_formula_output` 处理 alternatives；新增 `filter_alternatives` | 中 |
| [langgraph_reasoning.py](../app/services/langgraph_reasoning.py) | `run_reasoning_draft_formula_node`: 阈值筛选 + 并行 modification（去掉自动比较器） | 中——编排层核心路径 |
| Base formula prompt | 更新 system prompt 引导输出多套方案（按置信度降序） | 低 |

### 3.4 预期收益

| 指标 | Before | After |
|------|--------|-------|
| 开方路径数 | 1（单点故障） | 2-3（多路径并行探索） |
| 医师选择空间 | 接受/拒绝 1 套 | 从按置信度排序的 M 套中选择 |
| 低质量方案风险 | 无过滤机制 | 置信度阈值自动筛除 |
| 辨证→最终方剂的一致性 | 依赖单次选方准确度 | 多路径并行降低选方偏差影响 |

### 3.5 验收标准

- [ ] `BaseFormulaDraft.alternatives` 在 RAG 模式下至少包含 2 套方案
- [ ] 各套方案的 `angle` 字段有实质性差异（非仅剂量微调）
- [ ] 置信度阈值筛选正确：低于阈值的被丢弃，至少保留 min_keep 套
- [ ] 并行 modification 全部成功执行，结果按置信度降序排列
- [ ] L4.4 reasoning subgraph 20 个集成测试全部通过
- [ ] 真实流量 benchmark 开方质量无回归
- [ ] 延迟增幅 ≤ 15%（并行 modification 不应显著增加 wall-clock 时间）

---

## 4. P2 · 辨证 Query LLM 改写

### 4.1 现状分析

`build_syndrome_query`（[reasoning_retrieval.py:136-163](../app/rag/reasoning_retrieval.py#L136)）当前输出：

```
chief_complaint.symptom=咳嗽；chief_complaint.course=一周；
present_illness.cough=干咳少痰；present_illness.sore_throat=咽痛；
present_illness.chills=恶寒；present_illness.fever=发热；
four_diagnosis.tongue=舌淡红苔薄白；four_diagnosis.pulse=脉浮紧
```

这是**结构化表单拼接**（`fact_key=value；fact_key=value；...`），与知识库中医案的**叙事性自然语言**处于不同的语义空间：

```text
知识库医案 embedding 时的文本分布（来自 `knowledge_chunks.content`）：
"张某，男，35岁。咳嗽一周，干咳少痰，咽痒即咳，遇风加重。伴恶寒发热，无汗，
头痛，全身酸痛。舌淡红苔薄白，脉浮紧。辨证：风寒束表，肺气失宣。..."
```

向量检索基于语义相似度。`key=value` 拼接和医学叙事之间的语义距离，可能导致相似病例检索不到。

### 4.2 设计

#### 4.2.1 改写目标

用一次**轻量 LLM 调用**将结构化 observation 改写为医案首段风格的病情描述：

```
输入（结构化）:
  chief_complaint.symptom=咳嗽, course=一周
  present_illness.cough=干咳少痰, sore_throat=咽痛, chills=恶寒, fever=发热
  four_diagnosis.tongue=舌淡红苔薄白, pulse=脉浮紧

                        ↓ LLM 改写

输出（医案首段风格）:
  "患者咳嗽一周，干咳少痰，咽痒即咳，伴恶寒发热，无汗。舌淡红苔薄白，脉浮紧。"
```

LLM 改写相比模板拼凑的核心价值：
- **同义展开**：知道"恶寒发热 + 脉浮紧 + 苔薄白"组合在医案中常描述为"风寒束表之象"，可主动融入
- **文本分布对齐**：补全医案中常见但 observations 里缺失的过渡性描述（"伴"、"无汗"、"遇风加重"）
- **医学术语规范**：把 `key=value` 中的缩略表达展开为医案用词（如 `sore_throat=咽痛` → `咽痒即咳`）

#### 4.2.2 模型配置

在 [config.py](../app/core/config.py) 新增：

```python
# ── Query 改写模型配置 ──
rag_query_rewrite_enabled: bool = Field(
    default=False,
    description="是否启用辨证 query LLM 改写（默认关闭，需主动开启）"
)
rag_query_rewrite_model: str = Field(
    default="",
    description=(
        "Query 改写专用模型名称。为空时复用 chat_model。"
        "改写任务不需要医学推理能力，建议使用轻量快速模型（如 qwen3.7-flash 或更小模型）以控制延迟。"
    )
)
rag_query_rewrite_model_temperature: float = Field(
    default=0.1,
    ge=0.0,
    le=1.0,
    description="改写模型 temperature（低温度保证输出稳定、可复现）"
)
rag_query_rewrite_model_max_tokens: int = Field(
    default=400,
    ge=100,
    le=1000,
    description="改写模型最大输出 token 数"
)
rag_query_rewrite_timeout_seconds: float = Field(
    default=3.0,
    ge=0.5,
    le=10.0,
    description="改写调用超时秒数（超时降级为原始 query）"
)
```

模型配置说明：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `rag_query_rewrite_enabled` | `false` | 总开关，关闭时完全跳过改写 |
| `rag_query_rewrite_model` | `""`（复用 chat_model） | 可指定独立轻量模型 |
| `rag_query_rewrite_model_temperature` | `0.1` | 极低温度，保证确定性输出 |
| `rag_query_rewrite_model_max_tokens` | `400` | 医案首段 ~200 字已足够 |
| `rag_query_rewrite_timeout_seconds` | `3.0` | 超时降级，不阻塞辨证 |

**模型选型建议**：

| 选项 | 延迟 | 质量 | 适用场景 |
|------|------|------|----------|
| 复用 chat_model（不填） | ~500ms-1s | 好 | 初期验证，无需额外部署 |
| `qwen3.7-flash` 或同等 flash 模型 | ~200-400ms | 好 | 推荐——快速且质量足够 |
| 更小的专用模型 | ~100-200ms | 中等 | 延迟敏感场景 |

#### 4.2.3 实现

在 [reasoning_retrieval.py](../app/rag/reasoning_retrieval.py) 新增：

```python
# 改写 prompt（轻量、模板化）
_SYNDROME_REWRITE_SYSTEM = """你是中医病历书写助手。将结构化病情信息改写为医案首段风格的自然语言描述。

规则：
1. 以"患者"开头，按主诉→现病史→舌脉的顺序组织
2. 使用中医病历常用表达（如"伴"、"无汗"、"遇X加重"）
3. 不要编造输入中没有的症状
4. 不要添加辨证结论（证型、治法）
5. 输出纯文本，不超过 400 字
6. 舌脉信息保留标准表述
"""

_SYNDROME_REWRITE_USER = """请将以下结构化病情改写为医案首段风格：

{observations_text}

仅输出改写后的病情描述文本，不要加任何前缀或说明。"""


def _format_observations_for_rewrite(
    observations: Sequence[Any],
) -> str:
    """把 observations 格式化为 LLM 易于理解的文本。"""
    lines: list[str] = []
    for item in observations:
        key = getattr(item, "fact_key", "")
        value = _fact_text(getattr(item, "value", None))
        if not value:
            continue
        label = _FACT_KEY_LABELS.get(key, key)
        lines.append(f"  {label}: {value}")
    return "\n".join(lines)


# fact_key → 人读标签映射（仅常用键）
_FACT_KEY_LABELS: dict[str, str] = {
    "chief_complaint.symptom": "主诉症状",
    "chief_complaint.course": "病程",
    "present_illness.cough": "咳嗽",
    "present_illness.sputum": "痰",
    "present_illness.sore_throat": "咽喉",
    "present_illness.chills": "恶寒",
    "present_illness.fever": "发热",
    "present_illness.body_ache": "身痛",
    "present_illness.headache": "头痛",
    "four_diagnosis.tongue": "舌象",
    "four_diagnosis.pulse": "脉象",
    # ... 其余按需补充
}


async def rewrite_syndrome_query(
    observations: Sequence[Any],
    *,
    runtime: Any,  # AgentRuntime
    max_chars: int | None = None,
) -> str:
    """用 LLM 将结构化 observations 改写为医案首段风格。

    改写失败时降级为原始 build_syndrome_query（不阻断辨证流程）。
    由 rag_query_rewrite_enabled 配置控制开关。
    """
    settings = get_settings()

    # 总开关关闭 → 直接返回结构化 query
    if not settings.rag_query_rewrite_enabled:
        return build_syndrome_query(observations, max_chars=max_chars)

    limit = max_chars if max_chars is not None else settings.rag_query_max_chars

    observations_text = _format_observations_for_rewrite(observations)
    if not observations_text.strip():
        return build_syndrome_query(observations, max_chars=limit)

    # 选择模型：优先用专用改写模型，否则复用 chat_model
    rewrite_model = settings.rag_query_rewrite_model or settings.chat_model

    try:
        response = await runtime.run_lightweight(
            system=_SYNDROME_REWRITE_SYSTEM,
            user=_SYNDROME_REWRITE_USER.format(observations_text=observations_text),
            model=rewrite_model,
            max_tokens=settings.rag_query_rewrite_model_max_tokens,
            temperature=settings.rag_query_rewrite_model_temperature,
            timeout=settings.rag_query_rewrite_timeout_seconds,
        )
        rewritten = response.content.strip()
        if rewritten and len(rewritten) > limit:
            rewritten = rewritten[:limit]
        return rewritten or build_syndrome_query(observations, max_chars=limit)
    except Exception:
        logger.warning("syndrome query LLM 改写失败，降级为结构化 query")
        return build_syndrome_query(observations, max_chars=limit)
```

#### 4.2.4 A/B 对比方案

改写后的 query 是否真能提升检索质量？建议先做离线对比：

```python
# 离线评估脚本思路
async def evaluate_rewrite_quality():
    test_cases = load_syndrome_test_cases()  # 从真实 session 采集
    for case in test_cases:
        # A: 原始 key=value query
        original_query = build_syndrome_query(case.observations)
        original_results = await retriever.retrieve(original_query, ...)

        # B: LLM 改写 query
        rewritten_query = await rewrite_syndrome_query(case.observations)
        rewritten_results = await retriever.retrieve(rewritten_query, ...)

        # 对比：top-K 重叠度、与人工标注的 relevance 对比
        compare(original_results, rewritten_results, case.relevance_labels)
```

### 4.3 改动清单

| 文件 | 改动 | 风险 |
|------|------|------|
| [config.py](../app/core/config.py) | 新增 `rag_query_rewrite_enabled`、`rag_query_rewrite_model`、`rag_query_rewrite_model_temperature`、`rag_query_rewrite_model_max_tokens`、`rag_query_rewrite_timeout_seconds` | 极低 |
| [reasoning_retrieval.py](../app/rag/reasoning_retrieval.py) | 新增 `rewrite_syndrome_query`、`_format_observations_for_rewrite`、`_FACT_KEY_LABELS` | 低——降级机制保证不阻断检索 |
| [syndrome_draft.py](../app/agents/syndrome_draft.py) | `execute_syndrome_draft`: 调用 `rewrite_syndrome_query` 替代 `build_syndrome_query` | 低——通过 `rag_query_rewrite_enabled` 控制开关 |
| 评估脚本 | 离线 A/B 对比改写前后检索质量 | — |

### 4.4 预期收益

| 指标 | Before | After（预估） |
|------|--------|--------------|
| 辨证 RAG query 格式 | `key=value；key=value` | 医案叙事风格自然语言 |
| 与知识库医案的语义空间对齐 | ❌ 不同分布 | ✅ 同分布 |
| 辨证检索命中相关医案 | 偏少（语义距离大） | 预期提升（待 A/B 验证） |
| 辨证阶段延迟增加 | — | ~200-500ms（LLM 改写，可被更好的检索质量对冲） |

### 4.5 验收标准

- [ ] `rewrite_syndrome_query` 单元测试：输出以"患者"开头，不含 fact_key
- [ ] `rag_query_rewrite_enabled=false` 时行为不变（不调用改写）
- [ ] LLM 改写失败时正确降级为 `build_syndrome_query`
- [ ] 模型配置项全部可调，切换模型后改写行为正确
- [ ] A/B 对比离线评估：改写后检索结果 top-5 与人工标注的相关医案重叠度 ≥ 改写前
- [ ] 辨证 agent 集成测试通过

---

## 5. Reranker 升级 · Cross-Encoder / LLM Reranker

### 5.1 现状分析

当前 reranker（[reranker.py](../app/rag/reranker.py)）是 MVP 加权线性求和：

```python
# 当前实现
final_score = vector_weight * vector_score       # 0.65 * 向量相似度
            + fulltext_weight * fulltext_score   # 0.25 * 全文得分
            + source_priority_weight * source_priority  # 0.10 * 来源权重
```

**问题**：
- 线性加权无法捕捉深层的医学语义匹配——向量检索 top-K 中排第 3 的 chunk 可能比排第 1 的更精准地回答了 query
- 向量相似度衡量的是"文本相似"，不是"医学相关"——一段讲"川芎性味"的文字和 query "川芎茶调散治疗风寒头痛"向量距离可能相近，但医学上不是最佳答案
- `source_priority` 是粗粒度的（formula > herb > case？还是相反？），无法区分同一 source 内不同 chunk 的医学相关性

**Cross-Encoder / LLM Reranker 解决的就是这个问题**：在 ANN 召回 top-K（如 K=20）之后，用一个更强的模型对每个 (query, chunk) pair 做深度相关性打分，重排后取 top-N（如 N=8）。

reranker:
    RERANKER_GATEWAY_BASE_URL=https://www.dmxapi.cn/v1
    RERANKER_GATEWAY_API_KEY=${RERANKER_GATEWAY_API_KEY}   # 从环境 / .env 注入，勿在代码或文档中硬编码
    RERANKER_MODEL=jina-reranker-m0

### 5.2 设计

#### 5.2.1 目标架构

```
                        MVP Reranker (当前)              Cross-Encoder / LLM Reranker (目标)
                        
query ──→ Milvus ANN ──→ top-20 hits           query ──→ Milvus ANN ──→ top-20 hits
              │                                              │
              ↓                                              ↓
         merge + dedup                                  merge + dedup
              │                                              │
              ↓                                              ↓
        加权求和重排                                    Cross-Encoder/LLM
        (vector×0.65                                  逐对打分 (query, chunk_i)
         + fts×0.25                                         │
         + source×0.10)                                     ↓
              │                                         按新分数重排
              ↓                                              │
          top-8 结果                                        ↓
                                                       top-8 结果
```

#### 5.2.2 两种 Reranker 方案

| 方案 | 机制 | 延迟 | 精度 | 部署要求 |
|------|------|------|------|----------|
| **A. Cross-Encoder** | 专用 reranker 模型（如 `BAAI/bge-reranker-v2-m3`），输入 (query, chunk) pair，输出 [0,1] 相关度分数 | ~10-50ms/pair × 20 pairs = ~200ms-1s（可批量） | 高（专门训练用于相关性判断） | 需 GPU 或模型网关支持 |
| **B. LLM Reranker** | 用 LLM 对每个 chunk 做 0-10 分相关性评判，一次性输入 query + 多个 chunks | ~1-3s（单次调用，批量评分） | 最高（医学语义理解） | 复用现有 LLM 网关 |

#### 5.2.3 推荐方案：Cross-Encoder 为主 + LLM 为可选增强

- **Cross-Encoder** 延迟可控（~200ms-1s），精度足够，成熟方案
- **LLM Reranker** 精度更高但延迟大（~1-3s），可作为需要极致质量时的增强选项

#### 5.2.4 模型配置

在 [config.py](../app/core/config.py) 新增：

```python
# ── Reranker 模型配置 ──
rag_reranker_enabled: bool = Field(
    default=False,
    description="是否启用 Cross-Encoder / LLM Reranker（默认关闭，关闭时使用 MVP 加权求和）"
)
rag_reranker_provider: str = Field(
    default="cross_encoder",
    pattern="^(cross_encoder|llm)$",
    description="Reranker 类型：cross_encoder（专用模型）或 llm（LLM 评判）"
)
rag_reranker_model: str = Field(
    default="",
    description=(
        "Reranker 模型名称。cross_encoder 模式下为 reranker 模型名（如 BAAI/bge-reranker-v2-m3）；"
        "llm 模式下为 LLM 模型名（为空时复用 chat_model）。"
    )
)
rag_reranker_top_k: int = Field(
    default=20,
    ge=10,
    le=50,
    description="送入 reranker 的候选 chunk 数量（从 ANN 召回中取 top-K）"
)
rag_reranker_final_top_k: int = Field(
    default=8,
    ge=3,
    le=20,
    description="Reranker 重排后最终返回的 chunk 数量"
)
rag_reranker_timeout_seconds: float = Field(
    default=5.0,
    ge=1.0,
    le=15.0,
    description="Reranker 调用超时秒数（超时降级为 MVP 加权求和）"
)
```

#### 5.2.5 Cross-Encoder 实现

在 [reranker.py](../app/rag/reranker.py) 新增：

```python
async def cross_encoder_rerank(
    query: str,
    merged_hits: Sequence[MergedHit],
    *,
    model: str,
    top_k: int = 8,
    timeout: float = 5.0,
) -> list[Evidence]:
    """使用 Cross-Encoder 模型对候选 chunk 逐对打分重排。

    Args:
        query: 原始检索 query。
        merged_hits: 合并去重后的候选列表（最多 rag_reranker_top_k 条）。
        model: Cross-Encoder 模型名。
        top_k: 最终返回条数。
        timeout: 超时秒数。

    Returns:
        按 Cross-Encoder 相关度分数降序排列的 Evidence 列表。
    """
    if not merged_hits:
        return []

    # 构造 (query, chunk_content) pairs
    pairs = [
        {
            "query": query,
            "document": hit.content_snippet or hit.title or "",
        }
        for hit in merged_hits
    ]

    try:
        # 调用 reranker 模型网关
        scores = await _call_reranker_api(
            model=model,
            pairs=pairs,
            timeout=timeout,
        )
    except Exception:
        logger.warning("Cross-Encoder reranker 调用失败，降级为 MVP 加权求和")
        return rerank(merged_hits, top_k=top_k)

    # 按新分数重排
    scored = list(zip(merged_hits, scores))
    scored.sort(key=lambda x: x[1], reverse=True)

    evidences: list[Evidence] = []
    for rank, (hit, score) in enumerate(scored[:top_k], start=1):
        evidence = Evidence(
            evidence_id=uuid.uuid4().hex,
            source_type=hit.source_type,
            source_id=hit.source_id,
            chunk_id=hit.chunk_id,
            title=hit.title,
            content_snippet=hit.content_snippet,
            score=round(max(0.0, min(1.0, score)), 6),
            rank=rank,
            metadata={
                "vector_score": hit.vector_score,
                "fulltext_score": hit.fulltext_score,
                "reranker_score": round(score, 6),
                "reranker_model": model,
            },
        )
        evidences.append(evidence)

    return evidences


async def llm_rerank(
    query: str,
    merged_hits: Sequence[MergedHit],
    *,
    model: str,
    top_k: int = 8,
    timeout: float = 5.0,
    runtime: Any = None,
) -> list[Evidence]:
    """使用 LLM 对候选 chunk 做 0-10 分相关性评判。

    一次性将 query + N 个 chunks 送入 LLM，让 LLM 对每个 chunk 打分。
    精度最高但延迟较大。
    """

    _LLM_RERANK_SYSTEM = """你是中医文献相关性评审员。
对每个候选文献片段，根据其与查询的医学相关性打分（0-10 分）。

评分标准：
- 9-10: 高度相关，直接回答查询中的医学问题
- 7-8: 相关，提供了有用的背景知识
- 5-6: 部分相关，涉及相同主题但不够精准
- 3-4: 弱相关，仅涉及边缘概念
- 0-2: 不相关

返回 JSON 格式：{"scores": [8, 6, 3, ...]}（与输入顺序一一对应）"""

    if not merged_hits:
        return []

    chunks_text = "\n\n".join(
        f"[{i}] {hit.title}\n{hit.content_snippet[:300]}"
        for i, hit in enumerate(merged_hits)
    )

    try:
        response = await runtime.run_lightweight(
            system=_LLM_RERANK_SYSTEM,
            user=f"查询：{query}\n\n候选文献：\n{chunks_text}",
            model=model,
            max_tokens=500,
            temperature=0.0,
            timeout=timeout,
        )
        scores = _parse_llm_rerank_scores(response.content)
    except Exception:
        logger.warning("LLM reranker 调用失败，降级为 MVP 加权求和")
        return rerank(merged_hits, top_k=top_k)

    # ... 后续重排逻辑同 cross_encoder_rerank
```

#### 5.2.6 检索流程集成

修改 [retriever.py](../app/rag/retriever.py) 的 `hybrid_search` 方法：

```python
# retriever.py: hybrid_search（约 line 403-414）

merged = merge_deduplicate(vector_hits, fulltext_hits, primary_sources)

if not merged:
    return []

await self._backfill_content_snippets(merged)

# --- 改动前 ---
# evidences = rerank(merged, top_k=top_k, ...)

# --- 改动后 ---
settings = get_settings()
if settings.rag_reranker_enabled and len(merged) > settings.rag_reranker_final_top_k:
    # 先取 top-K 送入 reranker
    candidates = merged[:settings.rag_reranker_top_k]

    if settings.rag_reranker_provider == "llm":
        evidences = await llm_rerank(
            query=query,
            merged_hits=candidates,
            model=settings.rag_reranker_model or settings.chat_model,
            top_k=settings.rag_reranker_final_top_k,
            timeout=settings.rag_reranker_timeout_seconds,
            runtime=self._runtime,  # 需注入
        )
    else:
        evidences = await cross_encoder_rerank(
            query=query,
            merged_hits=candidates,
            model=settings.rag_reranker_model,
            top_k=settings.rag_reranker_final_top_k,
            timeout=settings.rag_reranker_timeout_seconds,
        )
else:
    # 降级：MVP 加权求和
    evidences = rerank(merged, top_k=top_k)
```

#### 5.2.7 Reranker 与 Query 改写的协同

```
Query 改写 (P2)                Reranker (P5)
     │                              │
     ↓                              ↓
 更好的 query                 对召回结果重排
 → 更高召回质量              → 更精准的 top-N
     │                              │
     └──────────┬───────────────────┘
                ↓
        最终 RAG 证据质量
        = 召回质量 × 重排质量
```

两者是互补的：P2 提升 ANN 召回的**上限**，Reranker 提升最终排序的**精度**。

### 5.3 改动清单

| 文件 | 改动 | 风险 |
|------|------|------|
| [config.py](../app/core/config.py) | 新增 `rag_reranker_enabled`、`rag_reranker_provider`、`rag_reranker_model`、`rag_reranker_top_k`、`rag_reranker_final_top_k`、`rag_reranker_timeout_seconds` | 极低 |
| [reranker.py](../app/rag/reranker.py) | 新增 `cross_encoder_rerank`、`llm_rerank`；保留原 `rerank` 作为降级路径 | 低——纯增量，原有 MVP 路径不变 |
| [retriever.py](../app/rag/retriever.py) | `hybrid_search`: 根据配置选择 reranker | 低——通过 feature flag 控制 |
| 模型网关 | 如需部署 Cross-Encoder 模型，需新增 reranker API endpoint | 中——依赖基础设施 |

### 5.4 预期收益

| 指标 | Before（MVP 加权） | After（Cross-Encoder） | After（LLM Reranker） |
|------|-------------------|----------------------|---------------------|
| 重排依据 | 线性加权（不可学习） | 深度语义匹配（训练得到） | 医学语义理解（推理得到） |
| top-8 医学相关性 | 基线 | **显著提升**（预期 +15-25%） | **最高**（预期 +20-30%） |
| 额外延迟 | 0ms | ~200ms-1s | ~1-3s |
| 部署复杂度 | 无 | 中（需 reranker 模型） | 低（复用 LLM 网关） |

### 5.5 验收标准

- [ ] `rag_reranker_enabled=false` 时行为不变（MVP 加权求和）
- [ ] Cross-Encoder 模式下，检索结果分数由 reranker 模型给出（非加权求和）
- [ ] LLM Reranker 模式下，返回结果附带 LLM 评分
- [ ] Reranker 调用失败时正确降级为 MVP 加权求和
- [ ] `perf_benchmark.py` 检索延迟增幅在预期范围内
- [ ] 真实流量检索质量不下降

---

## 6. P3 · 辨证 Agent 输出开方 RAG Query（可选，待评估）

### 6.1 现状分析

`build_formula_query`（[reasoning_retrieval.py:166-190](../app/rag/reasoning_retrieval.py#L166)）是**纯代码函数**：

```python
def build_formula_query(syndrome, observations):
    parts = []
    parts.append(f"证型={syndrome.syndrome}")
    parts.append(f"治法={syndrome.treatment_principle}")
    parts.append(f"症状={build_syndrome_query(observations)}")
    return "；".join(parts)
```

它机械地拼接证型 + 治法 + 症状，不涉及 LLM 推理。

但辨证 agent 的 LLM 在推理过程中，**已经内在地做了**"这个证型应该对应哪些方剂"的判断。如果能让这种判断显式化为检索 query，可能比代码拼接的 query 更精准。

### 6.2 设计

#### 6.2.1 `SyndromeDraft` 增加可选字段

```python
class SyndromeDraft(BaseModel):
    # ... 现有字段不变 ...

    # 新增：建议的开方 RAG 检索 query（可选，仅 RAG 模式生效）
    suggested_formula_rag_query: str | None = Field(
        default=None,
        max_length=600,
        description=(
            "辨证 agent 对开方阶段应检索方向的建议。"
            "由 LLM 基于辨证推理自然生成，非强制。"
            "为 None 时编排层退回到 build_formula_query 代码构造。"
        ),
    )
```

#### 6.2.2 编排层使用逻辑

在 `retrieve_formula_evidence` 增加优先级逻辑：

```python
async def retrieve_formula_evidence(
    retriever: Any,
    syndrome: Any,
    observations: Sequence[Any],
    *,
    top_k: int | None = None,
    logger_extra: dict[str, Any] | None = None,
) -> list[Evidence]:
    settings = get_settings()
    k = top_k or settings.rag_formula_top_k

    # 优先使用 LLM 生成的 query；降级到代码构造
    suggested = getattr(syndrome, "suggested_formula_rag_query", None)
    if suggested and suggested.strip():
        query = suggested
        logger.info("formula RAG: 使用辨证 agent 建议的检索 query")
    else:
        query = build_formula_query(syndrome, observations)

    if not query:
        logger.warning("formula RAG: 无可检索的查询，跳过检索（空证据模式）")
        return []
    return await _retrieve_with_degrade(
        retriever,
        query=query,
        primary_sources=list(FORMULA_PRIMARY_SOURCES),
        top_k=k,
        stage="formula",
        logger_extra=logger_extra,
    )
```

### 6.3 风险评估

| 风险 | 缓解 |
|------|------|
| **LLM 偏差级联**：辨证结论有偏差 → 生成的 query 有偏差 → RAG 检索偏向错误方向 → 开方全偏 | `suggested_formula_rag_query` 仅为可选字段；编排层可配置不使用 LLM query；A/B 对比可量化偏差影响 |
| **幻觉 query**：LLM 生成了指向不存在的方剂的 query | 检索结果的 relevance score 天然过滤——找不到就是找不到，返回空或低分结果 |
| **边际收益不确定**：代码构造的 query 已覆盖证型+治法+症状，LLM query 能好多少？ | 需要离线 A/B 对比；如果提升不显著，P3 可降级为不做 |

### 6.4 收益判断

P3 的收益取决于一个先验问题：**代码构造的 `证型=风寒束表；治法=疏风散寒；症状=...` 是否已经足够精准？**

如果答案是"基本足够"，P3 的边际收益很小，不值得引入 LLM 偏差风险。如果答案是"证型+治法+症状不足以区分相似方剂"，P3 才有价值。

**建议**：等 P2 + Reranker 的 A/B 评估框架就绪后，顺便对比"代码 query vs LLM query"的开方 RAG 检索质量，再决定 P3 是否推进。

### 6.5 改动清单

| 文件 | 改动 | 风险 |
|------|------|------|
| [syndrome.py](../app/schemas/syndrome.py) | `SyndromeDraft` 增加 `suggested_formula_rag_query` 可选字段 | 极低——向后兼容 |
| [reasoning_retrieval.py](../app/rag/reasoning_retrieval.py) | `retrieve_formula_evidence`: 优先使用 suggested query | 低 |
| Syndrome agent prompt | 引导 LLM 输出 `suggested_formula_rag_query` | 低 |

---

## 7. 整体架构对比

### Before

```
Syndrome agent (1 LLM)
  │ RAG: key=value 拼接 → theory+case
  │ 输出: syndrome + treatment_principle
  ↓
Base Formula agent (1 LLM)
  │ RAG: 证型=；治法=；症状= → formula+herb+case
  │ 输出: 1 套 base_formula
  ↓
Modification agent (1 LLM)
  │ RAG: 证型=；治法=；症状= → formula+herb+case  ← 与 base 完全相同！
  │ 输出: modified_formula
  ↓
MVP 加权重排（固定权重）
  ↓
最终方剂（1 条路径，无纠错，医师只能接受或拒绝）
```

### After

```
Syndrome agent (1 LLM)
  │ RAG: LLM改写医案风格 → theory+case                         [P2]
  │ 输出: syndrome + treatment_principle
  │       (+ suggested_formula_rag_query 可选)                  [P3]
  ↓
Base Formula agent (1 LLM)
  │ RAG: 证型=；治法=；症状= → formula+herb+case
  │ 输出: M 套候选方（≥2），按置信度降序                          [P1]
  │       置信度 < 阈值 → 丢弃                                   [P1]
  ↓
┌─ Modification agent (并行) ─→ 方案 A (川芎茶调散 加减)
│   RAG: 基础方=川芎茶调散；组成=...；待调症状= → herb+case     [P0]
│   继承 base RAG 证据 + 补充 herb-level 检索                   [P0]
│
├─ Modification agent (并行) ─→ 方案 B (荆防败毒散 加减)
│   RAG: 基础方=荆防败毒散；组成=...；待调症状= → herb+case
│
└─ Modification agent (并行) ─→ 方案 C (止嗽散 加减)
    RAG: 基础方=止嗽散；组成=...；待调症状= → herb+case
  ↓
Cross-Encoder / LLM Reranker（替换 MVP 加权）                   [Reranker]
  ↓
全部 M 套加减结果，按置信度降序 → 前端展示 → 医师选择             [P1]
```

---

## 8. 配置项汇总

所有新增配置在 [config.py](../app/core/config.py)：

```python
# ═══════════════════════════════════════════════════════════════
# P1: 基础方多方案筛选
# ═══════════════════════════════════════════════════════════════
base_formula_confidence_threshold: float = 0.45  # 置信度阈值
base_formula_min_alternatives: int = 1           # 至少保留数
base_formula_max_alternatives: int = 3           # 最多保留数

# ═══════════════════════════════════════════════════════════════
# P2: Query 改写模型
# ═══════════════════════════════════════════════════════════════
rag_query_rewrite_enabled: bool = False                    # 总开关
rag_query_rewrite_model: str = ""                         # 改写模型（空=复用 chat）
rag_query_rewrite_model_temperature: float = 0.1
rag_query_rewrite_model_max_tokens: int = 400
rag_query_rewrite_timeout_seconds: float = 3.0

# ═══════════════════════════════════════════════════════════════
# Reranker: Cross-Encoder / LLM Reranker
# ═══════════════════════════════════════════════════════════════
rag_reranker_enabled: bool = False                        # 总开关
rag_reranker_provider: str = "cross_encoder"              # cross_encoder | llm
rag_reranker_model: str = ""                              # reranker 模型名
rag_reranker_top_k: int = 20                              # 送入 reranker 的候选数
rag_reranker_final_top_k: int = 8                         # 重排后返回数
rag_reranker_timeout_seconds: float = 5.0
```

---

## 9. 实施顺序与依赖

```
P0 (Modification RAG 差异化) ─────────────────────────────────
  │  依赖: 无
  │  改动量: ~80 行（3 个文件）
  │  风险: 极低
  │  收益: 立即消除 modification RAG 浪费
  │
  ├──→ P1 (多套基础方方案)
  │     依赖: P0 完成后，modification 已有差异化 RAG，
  │            每套方案的 RAG 检索各自精准
  │     改动量: ~250 行（5 个文件 + prompt 更新）
  │     风险: 中（schema 变更 + 编排层重构）
  │     收益: 多路径并行 + 置信度排序 + 医师选择
  │
  ├──→ P2 (辨证 query LLM 改写)
  │     依赖: 无（可与 P0 并行）
  │     改动量: ~120 行（3 个文件）
  │     风险: 低（降级机制 + 独立模型配置）
  │     收益: 可能提升辨证 RAG 检索质量（需 A/B 验证）
  │
  ├──→ Reranker 升级
  │     依赖: 无（可与 P0/P2 并行）
  │     改动量: ~180 行（3 个文件）
  │     风险: 中（依赖 reranker 模型部署）
  │     收益: 提升所有阶段 RAG 检索最终排序质量
  │
  └──→ P3 (辨证输出 RAG query)
        依赖: P2 + Reranker A/B 框架就绪后评估
        改动量: ~50 行（3 个文件）
        风险: 中（LLM 偏差级联）
        收益: 不确定（待评估）
```

**建议节奏**：
- **第 1 轮**：P0（立即） + P2（并行） + Reranker（并行）——三者改动独立，风险可控
- **第 2 轮**：P1（核心）——依赖 P0 完成，最大的架构提升
- **第 3 轮**：P3（可选）——待 P2 + Reranker A/B 数据决定

---

## 10. 综合风险评估

| 风险 | 等级 | 缓解 |
|------|------|------|
| Schema 变更破坏兼容性（P1） | 中 | `BaseFormulaDraft.base_formula` 保留，`alternatives` 为新增字段；旧测试验证向后兼容 |
| LLM 调用次数增加（P1） | 中 | Modification 并行执行，有效延迟不叠加 |
| LLM 改写质量不稳定（P2） | 低 | 降级机制保证改写失败退回原始 query；独立模型配置可灵活切换 |
| Reranker 模型不可用 | 中 | 降级为 MVP 加权求和；feature flag 控制 |
| LLM 偏差级联（P3） | 中 | 可选字段 + feature flag，不做默认行为 |
| 集成测试回归 | 中 | 每轮改动后跑 20 个 L4.4 reasoning subgraph 测试 + `perf_reasoning_traffic.py` |

---

## 11. 验收总览

- [ ] **P0**: modification RAG 检索结果与 base RAG 检索结果差异化（source type + query 内容验证）
- [ ] **P1**: base formula 输出 ≥2 套有实质性差异的方案；置信度阈值筛选正确；并行 modification 全部成功；结果按置信度降序；L4.4 集成测试通过
- [ ] **P2**: 辨证 query 改写为医案风格；模型配置全部可切换；降级机制正常；A/B 对比检索质量不下降
- [ ] **Reranker**: 关闭时 MVP 行为不变；Cross-Encoder 模式分数来源正确；LLM 模式附带评分；降级机制正常
- [ ] **P3**: （待 P2 + Reranker A/B 数据决定是否推进）
- [ ] 全链路 `perf_benchmark.py` 无性能回归
- [ ] 真实 reasoning 流量 after 数据采集
