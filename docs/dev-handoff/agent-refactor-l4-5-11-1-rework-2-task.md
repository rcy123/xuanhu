# L4.5-11-1-R2 matcher 线性性能限定返工任务书

> 发布日期：2026-07-22
> 发布角色：Codex（工程项目经理）
> 状态：已发布 / 待交付
> 唯一有效起点：`bdf155fb0ed0c0ce37ea0e6c75b7afac6f1aa6ef`
> 上游方案：`docs/01_agent部分优化/L4.5-11模型输入隐私收敛方案-v2-2026-07-21.md` v2.2
> 触发证据：L4.5-11 组合最终独立复审 P2 / `R-PERF-001`

## 1. 发布事实与失败保留

L4.5-11-1 与 L4.5-11-2 的隐私、正确性、范围和回归证据均已通过，但组合最终独立 Reviewer 在冻结提交 `bdf155f` 上触发 v2.2 §8.1 的 R4 性能停止线：合法的 4,000 字符纯数字 Intake message 使 accepted scanner/projector 各耗时约 `22～25 ms/message`，超过 `10 ms/message`。

项目经理在同一机器、同一 exact HEAD 以 10 次预热和 50 次采样独立复现：

| 输入 | scanner 中位数 | projector 中位数 |
|---|---:|---:|
| 4,000 个安全字母 | `1.689 ms` | `1.571 ms` |
| 4,000 个 ASCII 数字 | `24.161 ms` | `24.748 ms` |

该发现不构成隐私绕过，但形成受合法最大输入控制的 CPU 放大。组合验收第 1 轮因此未通过；`AR-B-031` 保持打开，既有通过证据和 P2 失败证据均不得覆盖或删除。

## 2. 唯一目标

在不改变 v2.2 有限身份 grammar、公共 API、逐原始坐标等长遮罩、fail-closed 语义和 Runtime 复用关系的前提下，使 digit-dense 最大合法输入的 scanner 与 projector 均回到可审计的线性性能，并在本机独立验收基准中达到：

- 单条 4,000 位数字消息：每个公共函数预热后中位数 `< 10 ms/message`；
- 8 条、每条 4,000 位数字消息：每个公共函数预热后中位数 `< 80 ms/batch`，即 `< 10 ms/message`；
- 正确性、异常边界、Gateway 零调用和非 Intake 不变量全部不回归。

性能优化必须来自 matcher 工作量收敛；不得通过缩短输入、跳过扫描、改变保护集合、放宽边界、缓存跨请求结果或提高/删除阈值制造通过。

## 3. 非目标

- 不扩大手机号/身份证号支持集合；
- 不加入姓名、15 位身份证、其他证件、任意 Unicode 同形字符或跨请求重组；
- 不修改 Runtime、Intake 集成、Gateway、持久化、Domain、grounding、临床规则、Legacy、公开开关或 L5～L9；
- 不重新定义 v2.2 的 `10 ms/message` 触发线；
- 不引入新依赖、后台缓存、原文日志或真实外部服务；
- 不借本轮重构 L4.5-11-1/2 已 accepted 的其他职责。

## 4. 允许与禁止文件

开发唯一允许修改：

1. `app/agent_runtime/context.py`
2. `tests/test_l4_5_11_1_intake_privacy_projection.py`
3. 新建 `docs/dev-handoff/agent-refactor-l4-5-11-1-rework-2.md`

除此之外全部禁止，尤其包括：

- `app/agents/intake_extraction.py`
- `app/agent_runtime/runtime.py`
- `app/agent_runtime/specs.py`
- `app/agent_runtime/gateway.py`
- `tests/test_l4_5_11_2_runtime_privacy_guard.py`
- 任何既有 Runtime、Intake、Legacy、临床、配置、前端、迁移和项目管理文件
- 本任务书本身

若无法在上述三文件内完成，立即停止并报告，不得自行扩大范围。

## 5. 必须保持的合同

### 5.1 matcher 与坐标

- `contains_model_input_identity_sequence()` 和 `project_model_input_identity_sequences()` 继续共享唯一 matcher；
- 输入仍是按顺序的 `Sequence[str]`，跨 message 的 `B` 边界语义不变；
- 全角 digit/X 仍按逐字符一对一映射；任何不受支持或一对多规范化字符仍是 HARD；
- 返回遮罩逐 message、逐 raw coordinate 等长；不修改原始 tuple、DTO、message id、顺序或持久化原文；
- 候选选择仍为起点从左到右、同起点最长优先、同长身份证优先；失败候选不得跳过同起点更长候选；
- 已投影结果仍幂等。

### 5.2 有限 grammar 与边界

必须完整保持 v2.2 支持集合：

- `1[3-9]D{9}`；
- `1[3-9]D S D{4} S D{4}`，两个 `S` 必须同为单个空格、`-` 或 `.`；
- `D{17}(D|X)`；
- `B` 可位于相邻 grammar token 之间；
- 最近非 `B` 的前后 token 为 digit/X 时拒绝候选；
- 临床数字和明确非目标仍保持原文。

### 5.3 fail-closed 与泄露边界

- 非字符串、token/matcher/mask 任意异常继续抛固定 `ContextBuilderError("identity sequence processing failed")`；
- 固定错误保持 `from None`，无原值、无 cause/context、无原值日志；
- Runtime 仍只复用该 scanner；本轮不得改变固定 Runtime 隐私错误码和 Gateway 零调用语义。

## 6. 先红要求

开发者必须从 clean exact `bdf155f` 开始，在修改生产代码前记录真实 RED：

1. 在专项测试中加入最大合法 digit-dense 性能回归；采用单调时钟、预热、多次采样和中位数，分别覆盖 scanner/projector 的单消息与 8-message batch；
2. 阈值必须对应本任务 §2，不得设置为当前实现可通过的宽松值；
3. 在未修改生产代码时运行该测试，必须因实际耗时超过阈值而失败；不得以 collection/import/fixture 错误代替；
4. 同时记录现有 digit-dense 正确性：4,000 位连续数字不是 18 位身份证候选，scanner 为 `False`、projector 原样返回；该正确性断言可以先通过，性能断言必须真实失败。

若基线无法稳定复现 RED，立即停止并报告项目经理，不得先改生产代码。

## 7. 实施约束

- 优化 `_find_matches()` 或其私有辅助结构时，matcher 对 token/原始坐标的公开语义必须不变；
- 对最大输入的 matcher 工作量必须随 token 数线性增长，不能对每个起点重复构造固定长候选列表造成高常数 CPU 放大；
- 可以使用有限固定 grammar 的预计算、状态扫描或等价无回溯匹配，但不得使用存在灾难性回溯的表达式；
- 不得把性能优化建立在“通常输入很短”或“Runtime 已收到投影文本”的假设上，直接 Runtime 绕过路径仍必须扫描最大合法输入；
- 不得修改 public function 签名或让 scanner/projector 使用不同匹配实现。

## 8. 测试与验收

开发交付前至少运行：

```powershell
uv run pytest -q tests/test_l4_5_11_1_intake_privacy_projection.py
uv run pytest -q tests/test_l4_5_11_2_runtime_privacy_guard.py
uv run pytest -q tests/test_agent_runtime.py tests/test_intake_extraction.py tests/test_agent_runtime_gap_closure.py tests/test_reasoning_subgraph.py tests/test_syndrome_agent.py tests/test_formula_agent.py
uv run ruff check app/agent_runtime/context.py tests/test_l4_5_11_1_intake_privacy_projection.py
uv run mypy app/agent_runtime/context.py
uv run pytest -q tests/test_l0_1_contract.py
uv run pytest -q -m "not integration"
git diff --check bdf155fb0ed0c0ce37ea0e6c75b7afac6f1aa6ef..HEAD
```

handoff 必须报告：

- RED 命令、失败断言、样本数、预热数、单条和 batch 实测；
- GREEN 的同协议性能结果；
- matcher 复杂度为何线性且不改变语义；
- 两专项、相关回归、Ruff、mypy、L0、全量非集成结果；
- exact parent、delivery commit、文件范围、tracked 和 clean 状态；
- 失败回退方式与残余威胁模型限制。

## 9. 独立验收要求

交付冻结且 clean 后，项目经理将并行调用未参与实现的 Reviewer 和 CI：

- Reviewer 独立复测性能协议，并审查 matcher 复杂度、等价语义、异常泄露、坐标/grounding、Runtime 零调用和非目标；
- CI 在同一 exact commit 运行 §8 全部门禁、范围和 tracked 检查；
- 项目经理另行复测单消息/8-message 性能与跨层假 Gateway 探针；
- 通过后重新执行 L4.5-11 组合最终复审；本轮交付本身不得声称关闭 `AR-B-031`。

## 10. 提交、回退与停止条件

- 唯一开发 writer 从 `bdf155f` 的 clean worktree 开始；实施期间不得存在第二个 writer；
- 单一开发提交消息：`perf: bound L4.5-11 identity matcher work`；
- exact parent 必须为本任务发布提交，而不是 `bdf155f`；开发开始前项目经理会创建包含本合同的独立发布提交；
- 若交付失败，优先以单一交付提交为单位回退；不得 reset 或覆盖失败历史；
- 出现必须改禁区、保护集合变化、Legacy/临床/Domain/L5 变化、新依赖、无法稳定满足阈值，或同一性能根因在本轮修复后再次复发时，立即停止并报告项目经理；
- 不 push、不合并、不部署、不调用真实外部服务。

本任务只授权一次限定的 matcher 性能收敛，不授权弱化 v2.2 验收门槛。
