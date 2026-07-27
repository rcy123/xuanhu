# L5/L6-AUTH-R1 权限与记录边界收敛任务书

> 发布/执行日期：2026-07-27
> 撤回后基线：`21004b917d8f9fcfd8b46a1f8c9fd0392b7084ef`
> 交付提交：`45acf54698c3fa57a05bece88ec84d8fc294fa7f`

## 目标

撤回全部 L7 工作后，对 L6 及其上游 L5 权限链做一次对抗式收敛。任务只关闭离线 fixed-synthetic / in-memory sandbox reference composition，不接入应用 Runtime、HTTP、DB、真实数据或临床工作流。

## 必须关闭的边界

1. L5 Safety 结果必须由完整 subject + rule bundle + run envelope 机械重放，不接受调用方自洽伪造的 ALLOW。
2. 规则包必须经显式 authorizer；提供 immutable digest registry 作为可信 bootstrap 组合件。`recognize` 只建立历史登记，`authorize` 是当前操作的线性化授权点。
3. Review/Recheck 必须在 restore、stage、resume、eligibility 和 revision transition 上校验同一规则权限；历史 recognition 只确认一次，后续 transition 不得被外部旧登记漂移毒化。
4. configured identifier scanner 只拦直接号码/邮箱和整字段身份 alias；不得把普通 narrative、`id`、`mobile_threshold` 等技术字段误判为身份数据，也不得宣称全面 PII/来源认证。
5. 所有大 wire 输入必须在 Pydantic parse/model dump 前执行资源门禁。
6. L6 只接受 exact `SandboxRecheckCoordinator` capability，不接受 caller-supplied snapshot instance/dict/bytes/str。
7. L6 DTO 必须深冻结且拒绝 nested extra/private/错误容器；超限/hostile graph 在 canonical serialization 前拒绝。
8. Store 写入必须与当前 L5 authority 绑定，使用 canonical bytes、record_id/key 双向绑定、锁和幂等语义；不能保留旧 Store-only 无验证写入合同。
9. Pipeline 必须只读取一次 authority snapshot，并按 assemble → verify → store → serialize → narrate 顺序执行；narration 只输出 allowlist 字段并转义控制字符。

## 明确 supersede 的旧合同

- `ACC-20260724-050R1` 中“raw snapshot instance/dict/bytes/str 可作为 assembler/verifier authority”的合同被 supersede。那些值是可复制数据，不是权限能力。
- `DEC-20260724-047` 中“Store 只做持久化、不做 authority consistency”的合同被 supersede。首次写入和幂等重放都必须绑定当前 L5 authority。
- 旧记录保留为历史，不回写或删除；新 wire schema 为 `sandbox-medical-record.v2`。

## 验收门禁

- L5/L6 专项全部通过；伪造 ALLOW、未登记 bundle、历史 recognition 漂移、private state、list/超大图、非规范 JSON、错 key、并发和单快照探针必须覆盖。
- 全量非 integration pytest、全仓 Ruff、变更模块 strict mypy、lock、前端 lint/typecheck/tests 通过。
- 独立只读 L5、L6 终审均无剩余 sandbox P0/P1/P2。
- 产品/临床边界继续 NO-GO；未配置 `TEST_DATABASE_URL` 时不得伪称 DB integration 已执行。
