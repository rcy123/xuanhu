# L4.5-11-1-R1 Intake 入口投影层限定返工任务

## 1. 发布信息

| 项目 | 内容 |
|---|---|
| 任务编号 | `L4.5-11-1-R1` |
| 发布日期 | 2026-07-22 |
| 发布人 | Codex（工程项目经理） |
| 状态 | **已发布 / 待交付** |
| 返工输入 | `ca1c34b0bafbb22b3ba68d92ef4122717b400818` |
| 原发布合同 | `c8414bbefabd80fb9e308da4294a211c87ab6e02` 中的 `L4.5-11-1` 任务书 |
| 验收结论 | `ACC-20260722-007`：第 1 轮未通过 / 限定返工 |
| 关联阻塞 | `AR-B-031`、`R-MATCH-001`、`R-FAILCLOSED-001` |
| 交付载体 | 原 handoff `agent-refactor-l4-5-11-1.md`，追加 R1 交付，不覆盖历史 |

执行者必须从“包含本返工合同的 clean exact HEAD”开始，在 handoff 记录该 HEAD。返工输入是未验收交付，不是已接受生产基线。

## 2. 单一目标

只修复第 1 轮验收发现的两个合同内 P1 缺陷并加强对应测试：

1. scanner 在同一起点独立收集身份证、连续手机号和分隔手机号候选，再按原合同确定性规则选择；被边界拒绝的较短候选不得推动外层扫描游标，也不得遮蔽更长身份证候选；
2. 身份证末位必须允许跨一个或多个 `B` 读取，使 18 个字符之间的每个单一 message 切分位置都命中；
3. 两个公共 helper 的 token 化、匹配和坐标回写全流程都必须 fail closed：任何内部异常只向外抛固定脱敏 `ContextBuilderError`，异常字符串、cause/context 和日志均不得包含输入原值。

本返工不扩大 grammar，不重做入口集成，不实现 Runtime 门禁。

## 3. 允许修改范围

只允许修改：

1. `app/agent_runtime/context.py`
2. `tests/test_l4_5_11_1_intake_privacy_projection.py`
3. `docs/dev-handoff/agent-refactor-l4-5-11-1.md`（追加 R1 交付证据）

`app/agents/intake_extraction.py` 的第 1 轮集成已经通过范围和相关回归，本返工不得修改。项目管理台账由项目经理维护，执行者不得修改。

## 4. 禁止事项

- 不新增手机号、身份证或其他 PII grammar、字符类、白名单、编码层和 Unicode 归一化；
- 不修改 Runtime、Gateway、schema、verifier、Domain、Legacy、RAG、前端、依赖、配置、迁移或现有其他测试；
- 不拆分模块，不恢复 B1～B133，不以追加孤立样例替代有限语法和不变量测试；
- 不弱化边界检查、逐 message 等长、幂等、DTO 不变或 grounding 坐标合同；
- 不关闭 `AR-B-031`，不发布或实施 `L4.5-11-2`。

需要修改禁区、扩大 grammar 或改变坐标模型时立即停止并提交事实，不得顺手扩张。

## 5. 强制实现与测试合同

### 5.1 同起点候选与扫描推进

- 外层扫描到某个原始起点时，必须先评估该起点全部合同内候选；
- `13812345678901234X` 以及 ASCII `x`、全角数字/`Ｘ/ｘ` 等合同内等价形式必须按 18 字符身份证命中并等长遮罩；
- 较短手机号候选因后边界为 `D/X` 被拒绝时，外层只能按正常起点推进，不能跳过可能的同起点身份证；
- 候选进入统一集合后，继续遵守“起点从左到右、同起点最长 token span 优先、仍相同时身份证优先”；不得依赖分支顺序模拟优先级。

### 5.2 跨 message 身份证末位

- 读取前 17 个 digit 后，必须按合同跳过零宽 `B` 再检查第 18 位 `D/X/x`；
- 测试必须覆盖身份证 18 个字符之间的全部 17 个单一切分位置，包括第 17 位之后、末位 `X/x/Ｘ/ｘ` 在下一条消息的情况；
- 每个 case 都必须无条件断言 scanner 为 `True`、总遮罩数为 18、逐 message 长度不变；禁止使用 `if total_masked > 0` 之类让零命中通过的条件断言。

### 5.3 完整 fail-closed

- `contains_model_input_identity_sequence()` 和 `project_model_input_identity_sequences()` 均须覆盖 token 化、matcher 和 projector/坐标回写全过程；
- `_find_matches()` 或 `_apply_mask()` 被 monkeypatch 为抛出含固定虚构原值的异常时，公共 API 必须抛 `ContextBuilderError`；
- 对外异常消息固定且脱敏，不含原异常文本、输入、命中片段或坐标；使用不会通过异常链展示原值的方式抛出；
- 测试必须同时检查 `str(exc)`、异常 cause/context 和 `caplog`，证明固定虚构值不可见；scanner 和 projector 的失败路径都要覆盖。

## 6. 先红后绿与验收门禁

先只修改专项测试，在 `ca1c34b` 行为上记录以下真实 red，再修改生产代码：

1. 手机号样式前缀的 18 位身份证漏检；
2. 身份证在第 17 位后跨 message 漏检；
3. matcher / mask 内部异常没有归一化且泄露固定虚构值。

修复后运行：

```powershell
uv run pytest --override-ini addopts= -q -m "not integration" tests/test_l4_5_11_1_intake_privacy_projection.py

uv run pytest --override-ini addopts= -q -m "not integration" `
  tests/test_l2_3_context_builder.py `
  tests/test_l3_1_intake_extraction.py `
  tests/test_l3_5_intake_subgraph.py

uv run ruff check `
  app/agent_runtime/context.py `
  app/agents/intake_extraction.py `
  tests/test_l4_5_11_1_intake_privacy_projection.py

uv run mypy app/agent_runtime/context.py app/agents/intake_extraction.py
uv run pytest --override-ini addopts= -q -m "not integration" tests/test_l0_1_contract.py
uv run pytest --override-ini addopts= -q -m "not integration" tests
git diff --check
git status --short --branch
```

专项不得少于原有 43 项；相关回归不得少于 `65 passed, 22 deselected`；全量非集成不得少于交付声明的 `1592 passed, 362 deselected`，新增 R1 测试按实际增加。所有门禁必须在最终交付代码上重新运行。

## 7. 交付记录与通过标准

在原 handoff 追加“R1 交付”章节，至少记录：

- 包含本合同的执行起点 clean exact HEAD；
- 三类先红的测试名、退出码和原因，不记录真实患者值；
- matcher 控制流和完整异常边界的修复说明；
- 全部新增/强化测试与无条件断言说明；
- 专项、相关、Ruff、mypy、L0、全量、diff 的最终结果；
- 相对执行起点只修改本合同 3 个允许文件；
- R1 交付 exact commit、tracked 文件和 clean worktree；
- 明确写“已返工交付，申请重新验收”，不得自称通过。

项目经理只在独立黑盒探针、合同测试、全部门禁、范围和 exact-HEAD 证据同时通过后接受。通过后才可以考虑发布 `L4.5-11-2`。

## 8. 唯一下一动作

开发测试执行者领取 `L4.5-11-1-R1`，先红后绿完成上述限定返工并在原 handoff 追加交付申请。其他任务保持冻结。
