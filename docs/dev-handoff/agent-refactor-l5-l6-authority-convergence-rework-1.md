# L5/L6-AUTH-R1 交付与验收记录

> 交付日期：2026-07-27
> 代码提交：`45acf54698c3fa57a05bece88ec84d8fc294fa7f`
> L7 撤回提交：`21004b917d8f9fcfd8b46a1f8c9fd0392b7084ef`

## 交付结果

- L5：完整 bundle/run envelope 重放、显式 authorizer、immutable digest registry、历史 recognition cache、候选快照提交前复验、解析前 256 KiB 门禁和收窄后的 configured-pattern scanner。
- L6：typed/frozen v2 DTO、exact coordinator capability、单快照 pipeline、deep hidden-state/hostile-graph 拒绝、canonical serialization、authority-bound/thread-safe/idempotent store、字段 allowlist narration。
- L7：发布与实现提交已通过 revert 撤回；未提交的 L7 验收草稿保存在本地 `stash@{0}`，仅用于可恢复审计，不属于当前代码基线。

## 验证证据

| 门禁 | 结果 |
|---|---|
| L5/L6 组合专项 | `311 passed` |
| 全量非 integration | `1936 passed, 362 deselected` |
| Ruff | `All checks passed!` |
| strict mypy（4 个变更源模块） | `Success: no issues found` |
| 全仓 mypy | 979 个既有错误；不作为本次门禁，也不声称通过 |
| lock | `uv lock --check` 通过 |
| 前端 | lint 通过；typecheck 通过；`23 files / 171 tests passed` |
| DB integration | 未执行：`TEST_DATABASE_URL`、`DATABASE_URL` 均未配置 |
| L5 独立终审 | L5-SBX GO；P0/P1/P2=0；L5-PROD NO-GO |
| L6 独立终审 | L6-SBX technical GO；private/list 原攻击关闭；并发与多记录通过 |

## 设计结论

- `authorize()` 返回 True 是当前操作的授权线性化点；之后发生的撤销只影响后续调用，不追溯取消已授权的重叠操作。
- identifier scan 结果仅表示未匹配已配置模式，不是全面隐私扫描或来源证明；synthetic admission 的信任根是由可信 bootstrap 提供的 immutable bundle digest registry。
- `assembled_at` 使用唯一 CONFIRM 事件的 `applied_at`，表示 authority-confirmed assembly time，因此同一权威重试字节稳定。
- 这是离线 sandbox reference composition，不是产品 RecordSubgraph/API/DB/Doctor Review 实现。

## 验收结论

L5-SBX 与 L6-SBX 重新验收通过。允许重新发布一个以 `45acf54` 为基线、严格保持 offline/fixed-synthetic/in-memory 边界的 L7-SBX 任务；不得恢复已撤回的旧 L7 提交，也不得把本结论用于 L5-PROD/L6-PROD/L7-PROD。
