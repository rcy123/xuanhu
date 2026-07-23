# L5-3/4-R9 signed action authority restore convergence

## 1. 状态与目标

- 状态：**已发布 / 待开发交付**。
- 触发：final R4 在 exact `14c0496093e3054db2b4771463f5d018d639efc1` 发现 P1=1；CI 与 PM 全绿不能覆盖该 finding。
- 目标：让 live stage 与 snapshot restore 共用唯一、可从持久化 challenge authority 重新计算的 signed payload digest 规则，确保 attempt/event 的 action 与已验证摘要不可协调漂移。
- 范围继续是 fixed-fictitious/synthetic、offline unit/in-memory reference state；不进入 Runtime、HTTP、DB、Gateway、容器、部署、外部服务或 L6。

## 2. 精确起点与唯一 writer

- 开发必须从包含本任务书、`ACC-20260723-044` 与 `DEC-20260723-037` 的 clean exact management release HEAD 开始；开始时报告 actual HEAD/parent/status。
- 开发子 Agent 是本轮唯一 writer；先 RED、后最小 production、再完整门禁和单一 delivery commit。
- 不得 reset、amend、覆盖 final R4、R8 或更早失败/验收历史。

## 3. 允许文件（精确 5 个）

1. `app/agent_runtime/sandbox_review.py`
2. `tests/test_l5_3_sandbox_reviewer_interrupt_resume.py`
3. `tests/test_l5_4_sandbox_modify_full_recheck.py`
4. `docs/dev-handoff/agent-refactor-l5-3-sandbox.md`
5. `docs/dev-handoff/agent-refactor-l5-4-sandbox.md`

`app/agent_runtime/sandbox_recheck.py` production 必须零 diff；PM 六台账、任务书、配置、依赖、锁文件和其他阶段文件均禁止修改。

## 4. 必须先得到的 RED

production 零 diff 时先新增并运行：

1. L5-3 从真实 applied snapshot 分别覆盖 `REJECT→CONFIRM` 与 `CONFIRM→REJECT`：同步修改 attempt/event action，重新派生 attempt ref、event ref、全部引用该 attempt 的 transition refs，保持原 signed payload digest；旧 store 必须被证明错误接受且 eligibility 翻转。
2. 现有 coordinated-action 回归不得只重算 event ref；必须升级为完整引用链重派生，避免靠 stale attempt ref 得到假阳性。
3. 结构断言证明 live `review_signed_payload_digest` 与 restore 关系校验委托给同一个具名私有 authority helper；不得维护第二套字段清单。
4. L5-4 至少覆盖上述两个方向的完整 private snapshot；旧 outer restore 必须被证明错误接受且 completion eligibility 翻转；不得修改 outer production 制造拒绝。
5. 正例保持：正常 CONFIRM/REJECT live→restart round-trip；32-byte plaintext nonce 仍只返回一次，不进入 snapshot、repr、日志或异常；签名原文仍不持久化。

RED 证据必须记录 exact parent、选中/失败数、失败断言及 production 零 diff；禁止删除旧用例、skip、xfail、条件断言或保留过时引用制造通过。

## 5. 最小 production 收敛

- 提取一个私有、具名的 signed authority digest helper；输入只由持久化 challenge authority（含 `nonce_digest`）、action 与既有测试身份字段组成。
- public `review_signed_payload_digest` 继续接收一次性 plaintext nonce，但只把其固定 256-bit digest交给同一个 helper；live stage 仍先证明 plaintext nonce digest 精确匹配 challenge。
- restore 必须从 challenge 的 persisted `nonce_digest`、attempt action/identity 重新计算同一 digest，并与 attempt 中已保存的 digest 比较；event 继续精确匹配已验证 attempt。
- 该关系 guard 必须位于任何 applied eligibility 解释之前；L5-4 只通过 shared `SandboxInMemoryReviewStore` 继承。
- 不保存 plaintext nonce 或 signature，不新增 secret/key，不调用 restore-time verifier，不增加 migration/registry/多版本兼容，不改变 decision authority、状态机动作集合或外部接口。
- 任一不一致继续归一化为固定、chainless、无 payload 的 store/outer 错误，输入 canonical bytes 不变。

## 6. 验收阈值

- L5-3 专项不少于 `75 passed`；L5-4 不少于 `56 passed`；新增两方向与 shared-helper 结构用例全部命中。
- L5-1/L5-2、R7/R8、32 并发、状态机、Safety、Runtime/Legacy/public、privacy、AST/离线边界全部不回退。
- Ruff 全仓、mypy 四个 L5 production、L0、`uv lock --check`、forced/calibrated 全量非 integration、diff/scope/tracked/exact/clean 全通过。
- forced full 只允许既有 `tests/test_config.py::test_load_with_defaults` 的 `local` / 强制 `sandbox-test` 差异；calibrated 仅移除 `APP_ENV`。
- 全程使用 Goal 固定 loopback fake env 与 `UV_OFFLINE=1`；不读取 `.env`、ignored `data/`、`.codex_tmp`，不启动或访问任何服务。

## 7. 交付与独立验收

- 更新两份 handoff，保留 RED/GREEN、全部命令统计、scope、限制和回退；只能写“R9 已交付，申请独立验收”。
- 创建一个原子开发 delivery commit，精确 5 文件，parent 为本任务 release commit，结束时 clean。
- 冻结 delivery 后调用新的独立 Reviewer 与 CI；PM 独立复现两方向 L5-3/L5-4 composition、shared helper ownership 与正常 round-trip。
- R9 通过后创建 shared acceptance；随后必须从新的 clean exact HEAD 再执行全新的最终组合 Reviewer/CI/PM，不复用 final R1～R4。
- L6 未发布、未开始；真实临床、患者服务、公开生产与商业用途继续 NO-GO。
