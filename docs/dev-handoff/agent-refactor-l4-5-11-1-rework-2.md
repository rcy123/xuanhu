# L4.5-11-1-R2 matcher 线性性能返工交付记录

> 日期：2026-07-22
> 执行角色：开发者（唯一 writer）
> 状态：已交付 / 待独立验收
> 发布提交：`b8c553af9361c97683dac94c2d5a34dc5aca5dd2`
> 发布输入：`bdf155fb0ed0c0ce37ea0e6c75b7afac6f1aa6ef`
> 任务书：`docs/dev-handoff/agent-refactor-l4-5-11-1-rework-2-task.md`

## 1. 交付结论

本轮只收敛 accepted scanner/projector 共用的 `_find_matches()` 工作量，并新增正式 digit-dense 性能回归。4,000 位连续数字的正确性保持不变：scanner 返回 `False`，projector 原样返回；单条与 8-message batch 的 scanner/projector 中位数均低于任务阈值。

本交付不自称 accepted，不关闭 `AR-B-031`，不表示 L4.5-11 或 L4.5 阶段完成。未调用真实外部服务。

## 2. clean start 与先红证据

开发开始前：

- `git rev-parse HEAD` 为 `b8c553af9361c97683dac94c2d5a34dc5aca5dd2`；
- `git status --short` 无输出；
- 生产文件尚未修改，仅先在 `tests/test_l4_5_11_1_intake_privacy_projection.py` 加入正式性能回归；
- 协议固定为 `time.perf_counter_ns()` 单调时钟、10 次预热、25 次采样并取中位数；单条阈值 `< 10.0 ms`，8 条 batch 阈值 `< 80.0 ms`。

RED 命令：

```powershell
uv run pytest -q tests/test_l4_5_11_1_intake_privacy_projection.py `
  -k 'digit_dense_single_message_has_bounded_matcher_work or digit_dense_eight_message_batch_has_bounded_matcher_work'
```

发布实现的真实性能 RED：

| 输入与函数 | 发布实现中位数 | 阈值 | 结果 |
|---|---:|---:|---|
| 单条 4,000 位 / scanner | `23.385 ms` | `< 10.0 ms` | FAIL |
| 单条 4,000 位 / projector | `22.281 ms` | `< 10.0 ms` | FAIL |
| 8 条 × 4,000 位 / scanner | `179.682 ms` | `< 80.0 ms` | FAIL |
| 8 条 × 4,000 位 / projector | `185.065 ms` | `< 80.0 ms` | FAIL |

结果为 `4 failed, 53 deselected`。每个 case 的 scanner `False` / projector 原样返回断言均先通过，四项只因实际中位数越过合同阈值而失败；没有 collection、import、fixture 或人为异常替代 RED。

## 3. 实现与复杂度

旧实现从每个数字 token 起点重复跳过 B 并构造最多 17 个 digit index 的候选列表，再集中排序全部候选。新实现仍保留唯一 `_find_matches()`，但改为三次有界线性遍历：

1. 一次遍历折叠只用于 grammar 的 message boundary B，同时保留每个显著 token 的原 token index；HARD token 仍保留在显著流中并打断 grammar；
2. 一次反向遍历预计算每个显著位置开始的连续 digit-run 长度；
3. 一次前向遍历，以 O(1) 字段访问判定三种固定 grammar、最近非 B 前后边界和同起点择优，并直接做从左到右的不重叠选择。

因此时间与 token 数为 O(n)，辅助空间为 O(n)，不再为每个数字起点分配固定长候选列表或对候选集合排序。scanner/projector 仍共同调用这一个 matcher，没有跨请求缓存、输入截断、跳过扫描、回溯正则、新依赖或阈值放宽。

raw mask span 继续由保存的原 token index 生成；B 不写入字符，HARD 不被折叠。候选顺序仍为起点从左到右、同起点最长优先、同 end 时身份证最后参与择优；输出 range 在同 end 时相同。公共函数签名、有限 grammar、ASCII/全角一对一规范化、逐 message 等长、幂等和固定 fail-closed 错误均未改变。

## 4. GREEN 性能证据

正式四项性能测试转绿：`4 passed, 53 deselected`；完整专项为 `57 passed`。使用同一 10 次预热、25 次采样协议另行打印中位数：

| 输入与函数 | GREEN 中位数 | 阈值 | 结果 |
|---|---:|---:|---|
| 单条 4,000 位 / scanner | `3.981 ms` | `< 10.0 ms` | PASS |
| 单条 4,000 位 / projector | `3.762 ms` | `< 10.0 ms` | PASS |
| 8 条 × 4,000 位 / scanner | `33.088 ms` | `< 80.0 ms` | PASS |
| 8 条 × 4,000 位 / projector | `32.809 ms` | `< 80.0 ms` | PASS |

正式测试同时继续断言 dense digit 输入不是 18 位身份证候选，scanner 为 `False`、projector 对单条和 8 条输入逐条原样返回。

## 5. 语义等价补充探针

除正式测试外，以固定 seed `45112` 生成 10,000 组、每组 1～5 条消息的确定性差分样本；字符集合覆盖 ASCII/全角 digit、X/x、三种 separator、HARD 字符和空消息。直接加载发布提交 `b8c553a` 的 matcher 作为 reference，与当前 scanner/projector 逐例比较，结果为：

```text
10000 deterministic baseline differential cases passed (seed=45112)
```

该只读探针没有修改文件，不替代正式合同测试。

## 6. 最终门禁

| 门禁 | 结果 |
|---|---|
| L4.5-11-1-R2 完整专项 | `57 passed in 4.51s` |
| L4.5-11-2 Runtime 隐私 guard 专项 | `19 passed in 2.31s` |
| 相关回归现存路径超集 | `313 passed, 48 deselected in 5.55s` |
| Ruff | `All checks passed!` |
| mypy | `Success: no issues found in 1 source file` |
| L0 文档契约 | `131 passed in 2.66s` |
| 全量非 integration | `1625 passed, 362 deselected in 113.30s` |
| 10,000 组 baseline 差分探针 | 通过 |

任务书 §8 所列 `tests/test_agent_runtime.py`、`tests/test_intake_extraction.py`、`tests/test_agent_runtime_gap_closure.py`、`tests/test_reasoning_subgraph.py` 和 `tests/test_formula_agent.py` 在仓库中不存在。项目经理确认这是只读验收命令路径校准，并授权使用现存规范映射的覆盖超集，实际运行：

```powershell
uv run pytest --override-ini addopts= -q -m "not integration" `
  tests/test_l2_2_agent_runtime.py `
  tests/test_l3_1_intake_extraction.py `
  tests/test_l3_4_gap_question.py `
  tests/test_l3_5_intake_subgraph.py `
  tests/test_l4_4_reasoning_subgraph.py `
  tests/test_l4_1_syndrome_draft.py `
  tests/test_syndrome_agent.py `
  tests/test_l4_2_formula_draft.py `
  tests/test_l4_3_formula_consistency.py
```

该校准没有修改任务书、生产范围或额外测试文件。

## 7. 范围、提交与回退

唯一开发提交只包含：

1. `app/agent_runtime/context.py`；
2. `tests/test_l4_5_11_1_intake_privacy_projection.py`；
3. 新增 `docs/dev-handoff/agent-refactor-l4-5-11-1-rework-2.md`。

没有修改 Runtime、Intake 集成、task2 测试、Gateway、recorder、DTO、grounding、持久化、Legacy、临床逻辑、配置、依赖、任务书或项目管理台账。exact parent 必须为发布提交 `b8c553af9361c97683dac94c2d5a34dc5aca5dd2`；delivery SHA 在提交冻结后由 Git 生成并随交付消息报告，本文不能在不改变自身 SHA 的前提下自引用。

若独立验收失败，应由项目经理以单一交付提交执行 `git revert <delivery-sha>`，保留 RED 和失败历史；不得 reset 或覆盖历史。

## 8. 残余威胁模型限制

本轮没有扩大 v2.2 保护集合：自由文本姓名、15 位身份证、其他证件、任意编码或 Unicode 同形字符、跨请求拼接仍为明确非目标。性能门槛是当前 Python 3.12 / `uv run` 本机证据，独立 CI 仍需在冻结 exact commit 复跑协议；极端宿主争用属于运行环境风险，不能通过放宽阈值处理。`AR-B-031` 保持打开，等待独立 Reviewer、CI、项目经理跨层探针与组合最终复审。

---

**已交付，申请独立验收。**

## 9. 项目经理验收结论

| 项目 | 结果 |
|---|---|
| 验收日期 | 2026-07-22 |
| 发布提交 | `b8c553af9361c97683dac94c2d5a34dc5aca5dd2` |
| 交付提交 | `ada23c77cedd4a3e98db5d4e4a5c11328ee4c0e4` |
| 父提交核对 | `ada23c77^ == b8c553a`，通过 |
| 范围与工作区 | 精确 3 个合同允许文件；新 handoff tracked；diff check、exact HEAD、初末 clean 全部通过 |
| 独立 Code Review | **No findings**（P0/P1/P2/P3 均无） |
| 独立 CI | 全部通过 |
| 结论 | **通过 / accepted** |

### 9.1 性能 finding 关闭证据

未参与实现的 R2 Reviewer 在 Python 3.12.12 上以 10 次预热、25 次采样独立复测：

| 输入与函数 | 独立 GREEN 中位数 | 阈值 |
|---|---:|---:|
| 单条 4,000 位 / scanner | `4.127 ms` | `<10 ms` |
| 单条 4,000 位 / projector | `3.954 ms` | `<10 ms` |
| 8 条 × 4,000 位 / scanner | `32.639 ms` | `<80 ms` |
| 8 条 × 4,000 位 / projector | `35.650 ms` | `<80 ms` |

Reviewer 直接加载 parent 复现真实 RED：单条 `27.226/26.329 ms`，batch `214.342/236.760 ms`，四项只因性能越线失败。它确认新 matcher 是三次有界 O(n) 遍历，无回溯正则、截断、跳过扫描、跨请求缓存或 grammar 缩减；40,000 组公共 API、2,168 组定向样本和 20,000 组 raw-range 差分均与 parent 等价。

最初报告 R-PERF-001 的最终组合 Reviewer 随后在同一冻结提交复审，得到单条 `4.127/3.627 ms`、batch `33.672/32.479 ms`，明确裁决原 P2 已 resolved，P0～P3 均无新 finding。

### 9.2 独立 CI 与项目经理探针

独立 CI 在 `ada23c77` 上只读运行：

| 门禁 | 结果 |
|---|---|
| L4.5-11-1 专项（含 R2） | `57 passed` |
| L4.5-11-2 专项 | `19 passed` |
| 现存相关路径超集 | `313 passed, 48 deselected` |
| Ruff / mypy | 通过 |
| L0 文档契约 | `131 passed` |
| 全量非 integration | `1625 passed, 362 deselected` |
| parent-source RED | `23.065/23.479 ms`；batch `200.383/217.482 ms`，四项按预期失败 |
| diff / scope / tracked / exact / clean | 全部通过 |

任务书 §8 中五个旧测试路径在仓库不存在；开发者按项目经理授权使用九个现存语义对应文件的覆盖超集，handoff 保留实际命令。该校准不改变生产范围、测试所有权或验收门槛，不构成 finding。

项目经理以相同协议复测得到单条 `4.118/3.825 ms`、batch `33.136/31.304 ms`；跨层假网关探针证明原始 DTO 不变、跨 message 等长、clinical offset 不变、安全 Intake 请求 1 且 `max_requests=1`、直接 unsafe Intake 请求 0、非 Intake 请求 1。

### 9.3 组合最终裁决

- `L4.5-11-1-R2`：已交付 → **accepted / 已关闭**；
- `L4.5-11-1`：性能验收重新打开 → **accepted / 已完成**；
- `L4.5-11-2`：继续 **accepted / 已完成**；
- `R-PERF-001`、`R-GROUND-001`：关闭；
- `L4.5-11`：组合验收第 1 轮未通过 → **accepted / 已完成**；
- `AR-B-031`：以 `140262d`、`5ada262`、`ada23c77`、两轮组合 Review、独立 CI 与项目经理探针作为完整证据，**关闭**；
- 自由文本姓名、15 位身份证、其他证件/编码/Unicode 同形字符、跨请求重组、非 Intake/Legacy/direct Gateway 和完整隐私/法律/临床合规仍是明确非目标；
- L4.5-11 完成不代表 L4.5/L5 可进入，EXT-001、EXT-002 继续保持 NO-GO。
