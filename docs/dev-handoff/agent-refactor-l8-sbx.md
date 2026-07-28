# L8-SBX 可观测性、评估与安全加固交付记录

## 1. 交付身份

| 项目 | 结果 |
|---|---|
| 任务 | `L8-SBX` |
| 发布基线 | `ac6e9c2`（实现基线 `da604d7`） |
| 实现提交 | `9e402c609e95a2283d75abc138b6b5ae8734f84d` |
| 验收记录 | `ACC-20260728-062` |
| 最终状态 | **accepted / engineering complete（SBX only）** |
| 数据与运行边界 | fixed-synthetic、offline、unit/in-memory reference composition |

## 2. 已交付能力

### L8-1 可观测性

- 严格、冻结、版本化的 Episode、节点轨迹、模型用量、失败归因和业务事件合同。
- Episode 绑定状态、graph/agent/prompt/schema/policy 版本及 evidence、verification、
  gate、human-intervention 引用。
- append-only in-memory store 支持幂等写入、冲突拒绝、canonical snapshot/restore、
  tamper rejection 和 storage-key index 恢复。
- 固定指标名与固定标签集合；标签值进行换行和指标分隔符消毒。
- metadata/details 拒绝原始 prompt、模型原文、临床/身份类字段和异常堆栈标记。

### L8-2 安全

- 有限、可审计、失败关闭的隐私字段匹配与 redaction。
- capability allowlist 与授权回调前后状态复核。
- 原子 budget reserve/consume/release；持久化消费量和幂等键，重建后不能重复扣费。
- prompt injection 有限分类；untrusted 指令不能提升为 system/policy。
- 统一安全 adapter 输出固定失败码，不透传敏感 payload。

### L8-3 故障与恢复

- 默认关闭、显式计划控制的七类故障注入与固定归因。
- fault plan 在注入后持久化计数，重建 injector 不能绕过 `max_injections`。
- bounded retry、deadline、state-version precondition 和 single-use resume。
- RecoverySession 首次及后续状态均持久化；重复 resume 和状态冲突失败关闭。
- side-effect ledger 重建后保持幂等；checkpoint restore 只接受 canonical snapshot，
  篡改或非 canonical 状态不产生部分成功。

### L8-4 离线评估

- 固定、版本化、无真实数据的行为用例和数据集合同。
- 按维度阈值、case 结果、失败归因和 aggregate metrics 的离线评估。
- ShadowComparator 只生成隔离报告，禁止写业务结果。
- RealModelTrialGate 默认关闭；缺少显式 enable、外部批准、预算或数据策略时返回
  `external_gate_required`，不会调用模型。

## 3. 独立审查与返工

第一次终端 Claude Code 独立审查结论为 `REWORK`，P0/P1/P2/P3=`0/3/4/6`，
原始输出保存在本机 `D:\tmp\l8-review.out`。本轮关闭了 RecoverySession
持久化和 single-use、EpisodeStore key index、BudgetLedger 重建幂等、敏感
metadata、字段匹配、fault 计数、canonical JSON、metric label 和 real-model
no-call gate 等问题。

修复后的终端 Claude Code 只读终审结论：

| 项目 | 结果 |
|---|---|
| Verdict | **ACCEPT** |
| 严重度 | P0=0、P1=0、P2=0、P3=5 |
| 原 REWORK 复核矩阵 | 8/8 PASS |
| 执行方式 | 从 `D:\tmp` 使用 Claude Code CLI safe mode；仅开放 Read，无 Bash/Edit |
| 外发边界 | 4 个 L8 模块、4 个测试、任务契约；未读取/发送 `.env`、密钥、凭据、真实业务数据、ignored data、stash 或 `.claude/` |

终审保留以下非阻塞 P3：

1. 显式复用同一 `episode_id` 且更换 `storage_key` 时，EpisodeStore 的覆盖语义可进一步收紧。
2. `RecoverySessionV1.side_effect_ledger_digest` 当前未参与运行时消费，可删除或接线。
3. EvaluationSuite 读取 InjectedModelTrial 私有延迟属性，存在轻度耦合。
4. observability canonical JSON 尚未显式设置 `allow_nan=False`；当前合同无 float 字段。
5. node-name 临床关键词测试可从 `diagnosis` 扩展为全标记参数化。

这些项目不影响当前有限 SBX 合同，不授权扩大到产品或真实数据轨道。

## 4. 验收证据

| 门禁 | 结果 |
|---|---|
| observability 专项 | `55 passed in 1.11s` |
| security 专项 | `42 passed in 2.55s` |
| faults 专项 | `57 passed in 2.59s` |
| evaluation 专项 | `9 passed in 2.40s` |
| L8 四文件组合 | `163 passed in 2.22s` |
| 非 integration 全量 | `2312 passed, 362 deselected in 129.94s` |
| Ruff check | `All checks passed!` |
| Ruff format（L8 白名单） | `8 files already formatted` |
| mypy | `Success: no issues found in 4 source files` |
| lock | `uv lock --check` 通过；84 packages resolved |
| diff | implementation staged diff 与最终管理 diff 均通过 `git diff --check` |

全量校准期间曾出现一次
`test_contrast_limits_negation_scope_and_rejects_global_explicit_none` 偶发失败：
`2311 passed, 1 failed, 362 deselected`。该 L3 用例单跑 `1 passed`、整个 L3 文件
`56 passed`，随后同一候选的全量复跑 `2312 passed`。现象与测试随机 UUID 偶发形成
手机号样式并被既有隐私扫描器拒绝一致；L8 文件未修改 L3/Runtime/隐私投影代码。
该校准事实保留，不据此宣称既有全仓测试完全无偶发风险。

格式门禁首次使用任务书原通配符时，命中了 5 个 L5～L7 既有模块和 3 个 L8
模块。只格式化允许范围内的 3 个 L8 文件，随后把格式检查校正为 4 个 L8 源模块
白名单并通过；既有模块未修改。

## 5. 范围核对

- 实现提交只包含 4 个 L8 源模块、4 个对应测试和 L8 任务书。
- 未修改产品 Runtime、HTTP/API、MainGraph、GraphRunner、数据库、Redis、Milvus、
  模型 gateway、前端、依赖或 lockfile。
- 未接入真实模型、LangSmith、生产 shadow write、外部数据或持久化。
- `.claude/` 是用户的未跟踪目录，未读取、未修改、未暂存、未提交。

## 6. 验收边界

L8-SBX 的 `accepted / engineering complete` 只证明固定合成数据上的离线
reference implementation 和工程门禁完成。L8-PROD、L9、真实模型试跑、
LangSmith、生产 shadow、真实 checkpoint、外部数据、临床/公开/商业/机构使用和
专业批准继续为 `external_gate` / NO-GO。
