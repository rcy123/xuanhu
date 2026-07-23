# L5-3/4-R10 persisted signature proof restore architecture convergence

## 1. 状态、触发与架构结论

- 状态：**已发布 / 待开发交付**。
- 触发：R9 delivery `259a5634f79b8078b3a4c714e273a42af8b48dc7` 的独立 CI 全绿，但 Reviewer P1=1；`ACC-20260723-045` 未接受 R9。
- 重复根因：R7～R9 均证明“snapshot 自洽/可重派生”不能替代 live 可达性。R9 的无密钥 digest helper 完全由 snapshot 字段计算，攻击者可同步改变 action、digest 与全部 refs 后再次通过。
- 架构结论：冻结继续增加无密钥摘要条件。恢复必须持有 live 已验证的合成 signature proof，并通过注入的离线 verifier 重验；只有外部于 snapshot 的 verifier authority 能区分“字段被重算”与“曾经通过 live 验证”。
- 范围仍仅 fixed-fictitious/synthetic、offline unit/in-memory reference state；不进入 Runtime、HTTP、DB、Gateway、容器、部署、外部服务或 L6。

## 2. 精确起点与允许文件（精确 6 个）

- 开发必须从包含本任务书、`ACC-20260723-045`、`DEC-20260723-038` 的 clean exact management release HEAD 开始。
- 唯一允许文件：
  1. `app/agent_runtime/sandbox_review.py`
  2. `app/agent_runtime/sandbox_recheck.py`
  3. `tests/test_l5_3_sandbox_reviewer_interrupt_resume.py`
  4. `tests/test_l5_4_sandbox_modify_full_recheck.py`
  5. `docs/dev-handoff/agent-refactor-l5-3-sandbox.md`
  6. `docs/dev-handoff/agent-refactor-l5-4-sandbox.md`
- PM 六台账、任务书、L5-1/L5-2、配置、依赖、锁文件与其他阶段均禁止修改；不得 reset/amend/覆盖历史。

## 3. 必须先得到的 RED

production 零 diff 时先升级/新增：

1. L5-3 两方向 `REJECT↔CONFIRM`：同步改变 attempt/event action，用 R9 helper 重算并替换两处 digest，完整重派生 attempt/event/全部 transition refs，但保留原 live signature proof；R9 必须被证明错误接受并翻转 eligibility。
2. L5-4 同一两方向 private snapshot 必须被证明错误接受并翻转 completion eligibility。
3. 非空 attempt snapshot 在没有 restore verifier 时必须 fail closed；R9 当前没有该依赖，先红。
4. 原 signature proof 任意变化、verifier 返回 false、verifier 抛异常均固定拒绝；输入 canonical bytes 不变，无 cause/context。
5. 结构断言：sealed attempt 持有唯一 bounded/repr-hidden signature proof；store restore 必须调用注入 verifier；L5-4 所有 shared store restore/copy call sites 传入同一个 verifier。
6. 正例：sealed 与 applied、CONFIRM 与 REJECT、L5-3 与 L5-4 restart 均可用相同 verifier 恢复；empty store 不需要 verifier；plaintext nonce 仍不持久化，event 仍不复制 signature。

RED 必须记录 exact parent、失败/选中数、production 零 diff；禁止使用旧 ref、删除记录、skip/xfail、条件断言或调用 signer 给篡改 snapshot 重新签名制造错误威胁模型。

## 4. 最小可信恢复设计

- `SandboxTestReviewProofV1.sandbox_test_signature` 增加固定上限；`_SealedAttemptV1` 保存同一测试 signature proof，字段同样 bounded 且 `repr=False`；attempt ref 必须覆盖它，event 不保存它。
- live stage 仍先校验 nonce、重算 signed digest、调用 `SandboxSignatureVerifier.verify`；只有成功后才能把原 proof signature 密封进 attempt。
- `SandboxInMemoryReviewStore` 对含 attempts 的 snapshot restore 必须要求 injected `SandboxSignatureVerifier`，并在接受前对每个 attempt 使用 persisted digest/scheme/key/signature 重验；missing/false/exception 全部固定拒绝。
- R9 的 persisted-challenge digest relation 保留，先证明 digest 对应 challenge/action/identity，再用 snapshot 外 verifier 证明 signature 对应该 digest；两层不能互相替代。
- `SandboxRecheckCoordinator` 已持有同一 verifier；构造 initial/current/candidate shared store 时必须显式传递，不新增第二套判断。
- 测试 fake signer 与 fake verifier 必须角色分离；verifier 不暴露签名生成方法，篡改测试不得使用 signer。只模拟离线 authority，不连接密钥服务或外部系统。
- signature proof 仅为固定合成测试证明，不是真实凭据；不得进入 repr、event、日志、异常或 plaintext resume command。

## 5. 门禁与阈值

- L5-3 不少于 `80 passed`；L5-4 不少于 `59 passed`；R10 双向、missing verifier、false/exception、signature drift、结构/正例全部命中。
- R7/R8/R9、L5-1/L5-2、32 并发、状态机、Safety、Runtime/Legacy/public、privacy、AST/离线边界全部不回退。
- Ruff 全仓、mypy 四个 L5 production、L0、`uv lock --check`、forced/calibrated 全量非 integration、diff/scope/tracked/exact/clean 全通过。
- forced full 只允许既有 `tests/test_config.py::test_load_with_defaults` 环境默认值差异；calibrated 仅移除 `APP_ENV`。
- 全程使用 Goal 固定 loopback fake env 与 `UV_OFFLINE=1`；不读取 `.env`、ignored `data/`、`.codex_tmp`，不启动或访问任何服务。

## 6. 交付、验收与停止条件

- 两份 handoff 只写“R10 已交付，申请独立验收”，保留 RED/GREEN、verifier call evidence、门禁、scope、限制与回退。
- 创建一个原子 delivery commit，精确 6 文件，parent 为本任务 release commit，结束 clean。
- 冻结后调用新的独立 Reviewer/CI；PM 复现两方向 action+digest+refs、signature drift、missing/false/exception verifier、正例 restart 与角色隔离。
- R10 单项通过后创建 shared acceptance，再从新 clean exact HEAD 执行全新的最终组合 Reviewer/CI/PM；不得复用 final R1～R4 或 R9 结果。
- 若修复必须引入真实密钥、外部 signer/verifier、Runtime、DB 或网络，立即停止并报告；当前 injected offline verifier 设计不触发该条件。
- L6 未发布、未开始；真实临床、患者服务、公开生产与商业用途继续 NO-GO。
