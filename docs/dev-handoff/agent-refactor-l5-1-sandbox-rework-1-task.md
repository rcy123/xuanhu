# L5-1-R1 确定性 authority、synthetic manifest 与最大资源证据限定返工

## 发布信息

| 项目 | 内容 |
|---|---|
| 状态 | 已发布 / 待交付 |
| 发布人 | Codex（工程项目经理） |
| 失败交付 | `d3eb7b90611b477eb837c395215473f80fe9726f`（保留，不覆盖） |
| 失败证据 | `ACC-20260722-019`；独立 Review P0=0、P1=1、P2=2、P3=0 |
| authority 决策 | `DEC-20260722-014` |
| 执行起点 | 包含本合同与原子 rework 记录的 clean exact management release HEAD，由项目经理提交后报告 |
| 交付文件 | `docs/dev-handoff/agent-refactor-l5-1-sandbox.md` |

## 目标

只关闭第 1 次交付已证明的三个 finding：

1. 以可机械验证的 immutable/canonical evaluator authority 取代“调用黑盒两次且相等即视为确定”的有限采样；
2. 让 inline fixed-fictitious fixture 携带并绑定准入包 §5.3/§8 的完整 synthetic manifest；
3. 把 1,000 次性能/RSS 门禁固化为最大合法 `64 formula items + 256 issues`。

R1 不重写原任务，不扩展到持久化、graph、L5-2、Runtime、HTTP、数据库、Gateway、Legacy 或真实医疗语义。

## 必须先红

从 clean exact R1 release HEAD 开始，先只修改专项测试并记录 RED。至少新增并先红：

- `test_l5_1_pairwise_state_drift_cannot_change_identical_request_result`
- `test_l5_1_arbitrary_stateful_callable_is_not_a_deterministic_authority`
- `test_l5_1_inline_fixture_manifest_is_complete_strict_and_digest_bound`
- `test_l5_1_thousand_true_maximum_results_are_resource_bounded`

RED 必须证明原 `d3eb7b9` 行为可复现；不得 skip、xfail、条件跳过、只改测试期望或先写生产代码。

## 必须实现

### 1. 单一确定性 authority

- 裁决必须由 frozen、strict、canonical、digest-bound 的 evaluator authority 机械地产生；authority 的全部规则/参数/输出映射必须包含在 bundle/subject 决策绑定中。
- 不得接受任意带隐藏可变状态的 callable 作为已证明 deterministic authority。
- 相同 canonical subject + bundle + authority 在同一 adapter、多次请求、新 adapter 和不同 `PYTHONHASHSEED` 下必须产生逐字节相同 decision/issues/result digest。
- 必须覆盖 calls 1～2 为 `block`、3～4 为 `allow` 的 pair-wise drift 对手；结果只能固定或在首次进入 authority 边界前 fail closed，不能前后翻转。
- 禁止以 3 次、N 次或随机抽样替代结构性纯度/绑定；禁止第二套 decision authority。
- issue ID、rule ID、severity、execution order 与 decision 仍只能来自唯一 evaluator authority，adapter 只做 canonical 验证、排序、digest 与边界控制。

可采用由 adapter 解释的 immutable declarative plan，或从 canonical frozen plan 每次构造隔离 evaluator；无论方案为何，必须从代码结构证明可变黑盒状态不进入裁决。

### 2. 完整 admitted synthetic manifest

新增 frozen/strict manifest DTO 并绑定到 subject/dataset digest，至少包含：

- `schema_version`、`dataset_name`、`dataset_version`；
- `admission_scope=personal_learning_synthetic_only`；
- allowlisted `provenance_type=constructed_fixture`；
- 固定 source statement，明确不来自真实病历、个人记录、生产日志、聊天记录或外部数据集；
- fixed fixture 的 generator path/version/digest/seed 均为 `not_applicable`，并带人工构造证据；
- `case_count`、`content_sha256` 与 canonical fixture 内容一致；
- 固定、非临床的 `created_at` 和 `created_by_test_role`；
- prohibited identifier scan 的 tool/version/ruleset/time/result，不记录命中原值；
- `label_status=not_clinically_adjudicated`。

缺字段、extra、未知 provenance、manifest/content digest 不一致、case count 不一致或 scan 非通过均须在 evaluator authority 前固定 fail closed、调用/解释/下游为 0。

### 3. 真实最大资源门禁

- 1,000 次正式样本必须同时使用 64 formula items 与 256 unique issues；预热至少 20 次。
- 每次结果逐字节相同；p95 `<50 ms`、p99 `<100 ms`、RSS 增长 `<64 MiB`。
- 输出 Python、CPU、fixture size、samples、warmup、计时/RSS 方法；不得把独立 Reviewer 的临时探针替代为 committed test。

## 允许修改范围

- `app/agent_runtime/sandbox_safety.py`
- `tests/test_l5_1_sandbox_safety_adapter.py`
- `docs/dev-handoff/agent-refactor-l5-1-sandbox.md`

除此之外全部禁止。执行者不修改本任务书或 PM 台账。

## 回归与受控环境

继续使用原 L5-1 任务书的全部 loopback fake 环境，不读取/显示 `.env`，不读取 ignored `data/`/`.codex_tmp`，不启动应用、网络、容器、数据库或 Gateway。

至少运行：

```powershell
uv run pytest tests/test_l5_1_sandbox_safety_adapter.py -q -rs
uv run pytest tests/test_safety_rule_engine.py -q -rs
uv run pytest tests/test_l4_5_11_1_intake_privacy_projection.py tests/test_l4_5_11_2_runtime_privacy_guard.py -q -rs
uv run ruff check app/agent_runtime/sandbox_safety.py tests/test_l5_1_sandbox_safety_adapter.py
uv run mypy app/agent_runtime/sandbox_safety.py
uv run pytest tests/test_l0_1_contract.py -q -rs
uv run pytest -q -rs
uv lock --check
git diff --check
```

精确全量在 `APP_ENV=sandbox-test` 下的既有 `test_load_with_defaults` 冲突必须原样记录；另以只移除 `APP_ENV`、保留所有 fake 外部终端的校准全量证明实际回归。不得修改 `tests/test_config.py` 或公共配置制造通过。

## 停止条件

- 需要修改允许列表外文件或增加依赖/配置/feature flag；
- 只能通过增加 evaluator 黑盒采样次数、进程级无界 cache、时间/随机性或第二套裁决逻辑关闭 P1；
- manifest 需要读取真实/外部/ignored 数据或复制标识命中原值；
- 最大资源门槛无法在原阈值内满足；
- 需要 Runtime、HTTP、DB、Gateway、Legacy、review/record/export、L5-2 或临床/生产语义；
- 出现无法归属 diff、真实/可关联个人数据、有效凭据或范围外 P0/P1。

普通测试/静态失败属于合同内返工，不构成暂停理由。

## 交付要求

在原 handoff 追加 R1 章节：记录 R1 release/delivery exact HEAD、真实 RED、方案不变量、全部 GREEN/回归/资源、失败历史、实际 diff、scope/tracked/clean 和未决限制。创建单一 R1 开发提交，只能写“已交付，申请验收”。
