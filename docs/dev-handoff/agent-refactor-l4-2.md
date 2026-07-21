# L4-2 FormulaDraftAgent 交接（AR-B-027 第 3 轮限定返工）

## 变更范围

L4-2 仍只实现独立 FormulaDraftAgent：单次模型调用生成 `base_formula`、`modifications`、`candidate_formula`，不写 State/DB、不路由、不调用 Safety、不实现 HITL/RAG/API/UI，也未修改 Legacy PrescriptionAgent 或 ModificationAgent。

涉及文件：

- `app/schemas/formula.py`
- `app/agent_runtime/formula_verifier.py`
- `app/agents/formula_draft.py`
- `app/agents/syndrome_draft.py`
- `app/agents/prompts/formula_draft_v1.jinja2`
- `app/agents/prompts/manifest.yaml`
- `tests/test_l4_2_formula_draft.py`

## 可信 Syndrome 来源边界

本轮采用允许边界 1：Formula 直接消费受信 L4-1 执行边界产生的进程内部结果。

- `execute_syndrome_draft()` 的公开包装器和 Formula 消费函数共享一个仅存在于工厂闭包中的弱引用身份注册表。注册操作没有模块级入口，只有包装器调用真实 L4-1 执行并成功通过 canonicalize 与 verifier 后才会发生。
- 注册表以真实返回对象的 `id` 和 `weakref` 双重确认具体实例，同时保存该次真实执行的 `RunSpec`、canonical `RunArtifact`、权威输入和 canonical `SyndromeDraft`。对象销毁后记录自动移除，避免对象 ID 重用命中。
- Formula 不读取 `SyndromeExecutionResult` 上的任何公开或私有字段作为 capability。手工构造同型对象、构造 `_TrustedSyndromeExecution`、手工 passed report、赋值 `_trusted_execution`、`model_copy()` 或 deep copy 均没有注册表身份，固定拒绝。
- `_TrustedSyndromeExecution` 仅是兼容/回归攻击形状，本身明确不受信；直接导入和构造它不会授予 Formula 权限。
- Formula 模型上下文中的 `syndrome`、`treatment_principle`、`syndrome_basis`、`differential` 只从注册表保存的真实 L4-1 执行记录重建。调用方 `FormulaDraftInput.syndrome_draft` 不会成为权威临床来源。
- 兼容参数 `syndrome_artifact` / `syndrome_run_spec` 仅用于固定拒绝旧调用方式；无论二者是否自洽，只要调用方显式提供其中任意一个（包括显式 `None`），公开入口即在 gateway 前返回 `FORMULA_SYNDROME_DRAFT_INVALID`。

已删除旧结论：调用方提供 `SyndromeDraft + RunSpec + RunArtifact + digest` 并不能建立可信来源；三者可以由调用方一起伪造，彼此自洽不代表来自 L4-1。

## AR-B-027 核心回归

`test_forged_syndrome_text_with_valid_fact_ids_is_zero_call` 构造：

- schema 合法、`completed` 的 SyndromeDraft；
- 合法 no-RAG 契约；
- 引用真实 active fact IDs；
- 任意伪造 `syndrome`、`treatment_principle`、basis 文本；
- 同时创建与伪造内容完全自洽的 Syndrome RunSpec 和 RunArtifact。

该成套裸值提交到公开 Formula 入口后固定拒绝，Formula gateway 调用数为 0。

`test_handcrafted_privateattr_bundle_with_passed_report_is_zero_call` 进一步完整复现第 2 轮绕过：手工 canonical verifier passed report、直接构造 `_TrustedSyndromeExecution`、构造 `SyndromeExecutionResult`、强制赋值 `_trusted_execution`，所有内容和 provenance 自洽且引用真实 active facts；Formula 仍拒绝且 gateway 为 0。

`test_copy_of_real_syndrome_execution_result_is_zero_call` 验证即使来源对象原本由真实 L4-1 成功产生，其复制对象也不会命中具体实例身份注册表。

测试 helper 使用 `_NOT_PROVIDED` sentinel 区分“未提供”和“显式传入 None”，未使用 `syndrome_artifact or default_artifact`。缺 artifact、缺 RunSpec 用例均将真正的 `None` 传入生产入口，并断言 gateway 调用为 0。所有 Formula 错误路径均重新确认 gateway 为 0。

## 固定契约

| 项目 | 值 |
|---|---|
| Formula input/output schema | `formula-draft-input.v1` / `formula-draft.v1` |
| AgentSpec | `formula-draft-agent.v1` |
| Prompt | `formula_draft_v1.jinja2` |
| Policy | `formula-draft-policy.no-rag.v1` |
| Stage | `READY_FOR_FORMULA` |
| Evidence mode | `model_knowledge_only` |
| Confidence 上限 | `0.65` |
| review_required | `true` |
| claim_evidence_links | 空 |
| Model attempts | 1 |
| Tool permissions | `READ_STATE` only |

## 实际验证结果

- `uv run pytest tests/test_l4_2_formula_draft.py -q -rs` → **42 passed in 1.96s**（最终复跑）
- `uv run pytest tests/test_l4_1_syndrome_draft.py tests/test_l4_2_formula_draft.py tests/test_l2_5_repository_outbox.py tests/test_advance_api.py -q -rs` → **97 passed, 4 warnings in 36.05s**
- `uv run pytest -q -rs` → **1407 passed, 1 xfailed, 14 warnings in 239.35s**
- `uv run ruff check .` → **All checks passed**
- `uv run mypy app` → **Success: no issues found in 114 source files**
- `uv lock --check` → **Resolved 83 packages**
- `git diff --check` → **通过**

## 明确未实现

- L4-3 FormulaConsistencyVerifier、药味规范化、剂量/单位换算
- L4-4 ReasoningSubgraph、revision、条件边、回问和下游失效
- SafetyRuleEngine、Safety 决策、Doctor Review/HITL
- RAG、citation、formula/herb retrieval
- API/UI 或 `/advance` 正式接入
- Legacy Agent 行为修改
- DB migration、旁路 commit 或 Git commit
