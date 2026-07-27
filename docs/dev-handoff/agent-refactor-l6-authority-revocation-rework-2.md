# L6-AUTH-R2 实时权限撤销与状态完整性交付记录

> 交付日期：2026-07-27
> 重新打开基线：`95ec7a6d743ac37d91586233257589e6ab8c5e4a`
> 代码提交：`e8e0973e527abc238466b0b0d0734ca4c3a35083`

## 交付结果

- Recheck 新增窄化、冻结的 active authorized record projection；每次记录操作都在 sealed state capture 后调用当前 rule-bundle authorizer，并把该调用作为授权线性化点。
- authority capture 在 signature verifier/authorizer 前后绑定 exact coordinator、review coordinator、review store、RLock、内部 list 容器及完整模型图；拒绝实例遮蔽、回调重入换引用、可执行容器、hidden/private/extra 状态和标量子类漂移。
- Review、Recheck、Record 的 public scope 边界统一要求 exact `str`；Assembler、Verifier、Store 使用 fresh projection，Pipeline 使用单一 projection 贯穿一次事务。
- 本次是运行时代码与能力设计修复，不是只增加规则：修改了 `sandbox_recheck.py`、`sandbox_review.py`、`sandbox_record.py` 三个源模块，并新增/扩展撤权、并发、重入与模型完整性回归。

## RED / GREEN 证据

| 探针族 | RED | GREEN |
|---|---|---|
| 当前 bundle 撤权 | 撤权后 Assembler 仍可成功 | Assembler、Verifier、Store、Pipeline 四边界全部拒绝 |
| nested/instance override | completion authority、review eligibility 与 outer projection 可被实例路径替换 | exact capability + unbound sealed capture，全部拒绝 |
| 外部回调重入 | authorizer/verifier 可在校验期间替换 current ref、锁或持久状态 | 回调前后身份与完整状态复核，全部拒绝 |
| 可执行容器与隐藏图 | revision/receipt 容器、hidden state、scalar subclass 可在读取或规范化时漂移 | exact built-in container/leaf 与 type-aware graph match，全部拒绝 |
| hostile public scope | `str` 子类可借值比较触发副作用 | Review/Recheck/Record 公开入口 exact-str fail-closed |

## 最终门禁

| 门禁 | 结果 |
|---|---|
| L5/L6 组合专项 | `338 passed in 79.07s` |
| 全量非 integration | `1963 passed, 362 deselected in 116.07s` |
| Ruff | `All checks passed!` |
| 生产源码 mypy | `uv run mypy app scripts --no-incremental --show-error-codes`：159 个源码文件，0 错误 |
| 全仓 mypy 基线 | `uv run mypy app tests ...`：917 个错误 / 65 个文件；均在测试目录，作为既有注解债务保留，不声称 full mypy 通过 |
| lock / diff | `uv lock --check`、`git diff --check` 通过 |
| 前端（未改前端） | lint、typecheck 通过；23 个 test files / 171 tests 通过 |
| DB integration | 未执行：`TEST_DATABASE_URL`、`DATABASE_URL` 均未配置 |
| 独立只读架构复核 | L7-SBX GO；P0=0、P1=0、P2=0、P3=0 |

## 设计边界

- `authorize()` 成功返回是操作的实时授权线性化点。撤权影响尚未越过该点和之后发起的操作，不追溯撤销已经越过该点的重叠操作。
- active projection 只携带组装/验证/存储所需的窄字段；可复制 snapshot 仍只是数据，不是 capability。
- exact-type 与对象图检查用于保护明确的同进程公共能力边界，不宣称 Python 对象能替代进程隔离或可信执行环境。
- 结论仅覆盖 fixed-synthetic、offline unit/in-memory reference composition；产品 RecordSubgraph/API/DB、Runtime、HTTP、部署、Doctor Review、真实数据和临床/公开用途继续 NO-GO。

## 验收结论

L5-SBX 与 L6-SBX 基于 `e8e0973` 再次验收通过。可以从该 clean 代码基线重新发布一个严格 bounded 的 L7-SBX 任务；旧 L7 发布/实现仍禁止恢复或 cherry-pick，L7-PROD 不获授权。
