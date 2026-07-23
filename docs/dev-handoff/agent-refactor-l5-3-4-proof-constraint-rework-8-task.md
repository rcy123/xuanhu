# L5-3/4-R8 live/restore proof constraint 同源限定返工任务书

> 状态：已发布 / 待交付
> 发布日期：2026-07-23
> 依据：`ACC-20260723-042`、`DEC-20260723-035`
> 执行起点：包含本任务书与 final R3 失败记录的 clean exact management release HEAD

## 唯一目标

让 live `SandboxTestReviewProofV1`、persisted `_SealedAttemptV1` 与 `SandboxTestReviewEventV1` 对以下三个测试 identifier 共用一个 constrained string type：

- `sandbox_test_reviewer_id`
- `sandbox_test_signature_scheme`
- `sandbox_test_key_id`

约束保持为 strict string、长度 1～128、匹配当前 `_IDENTIFIER_PATTERN`。本任务不改变 fixed role/organization/qualification literals、digest、签名验证行为或任何外部接口。

## 根因与必须先红

live proof 使用三组相同 `Field(min_length=1, max_length=128, pattern=...)`，但 sealed attempt/event 对同字段只声明 `str`。final R3 已证明 applied snapshot 可把字段协调改为 live DTO 拒绝的值，重算 attempt/event/transition refs 后 restore 仍接受。

production 未修改时必须留下以下真实 RED：

1. **L5-3 全字段矩阵**：三个字段分别覆盖 empty、129-char、pattern-invalid，共 9 个 applied snapshot cases；attempt 与 event 同步修改，重算 attempt ref、event ref 与引用该 attempt 的 transitions。旧 store 必须真实接受，测试期望 fixed `SANDBOX_REVIEW_REJECTED` 且输入不变。
2. **共享类型结构**：live proof、sealed attempt、review event 的 9 个 field annotations 必须全部引用同一个具名 constrained alias；旧代码结构断言失败。
3. **合法边界正例**：1-char 与 128-char identifier 可通过 public proof DTO；正常 live applied snapshot 与未修改历史可 round-trip，不误拒绝 fixed literals/digests。
4. **L5-4 composition**：至少对三字段各一个代表性 coordinated invalid snapshot 完整重派生 private refs，并证明 outer coordinator 在旧代码错误接受、修复后固定拒绝且输入不变。

不得使用 stale ref、只改 attempt 不改 event、删除记录、skip/xfail 或动态异常文本制造 RED。

## 最小修复合同

- 在 `sandbox_review.py` 定义一个私有、具名 `Annotated[str, Field(...)]` alias，三模型九个 annotation 全部引用；不得复制 Field 条件。
- Pydantic model construction/restore 必须在进入 `_snapshot_is_integral` 的关系校验前拒绝 invalid persisted value；公开错误仍由 store/coordinator fixed、chainless 边界归一化。
- L5-4 production 不修改；通过 `SandboxInMemoryReviewStore(snapshot=...)` 自动继承。
- R7 fixed schema、R6 child inheritance、R5 finite qualification、L5-3 R1～R5 的因果/顺序/current 不变量全部保持。

## 允许修改范围

只允许修改以下 5 个文件：

- `app/agent_runtime/sandbox_review.py`
- `tests/test_l5_3_sandbox_reviewer_interrupt_resume.py`
- `tests/test_l5_4_sandbox_modify_full_recheck.py`
- `docs/dev-handoff/agent-refactor-l5-3-sandbox.md`
- `docs/dev-handoff/agent-refactor-l5-4-sandbox.md`

不得修改 `sandbox_recheck.py` production、L5-1/L5-2、配置、依赖、锁文件、PM 六台账或本任务书。五文件外任一修改必须停止并交回项目经理。

## 门禁与交付

- 新回归加入后 L5-3 不得少于 `72` 项，L5-4 不得少于 `54` 项；既有 `62/51` 全部保持。
- L5-1/L5-2、Safety、privacy、Runtime/Legacy/public、public flag、AST、Ruff、mypy、L0、lock、双全量、diff/scope/tracked/clean 全部重跑。
- fixture 固定虚构/合成、inline、offline、in-memory；不读取 `.env`、ignored `data/`、`.codex_tmp`，不访问外部资源或启动服务。
- 单一开发提交，exact parent 必须为包含本任务书的 clean management release；提交只含上述 5 个 tracked 文件，两 handoff 同时记录 RED/GREEN、门禁、限制与回退。
- R8 独立 Reviewer/CI/PM 通过后创建 shared acceptance；随后仍须从新 clean exact HEAD 执行全新的 final Reviewer/CI/PM，不能复用 final R3，L6 保持未发布、未开始。
