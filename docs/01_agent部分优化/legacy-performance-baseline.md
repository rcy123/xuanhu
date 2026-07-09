# Legacy Agent 性能基线

> 任务：L0-3
> 日期：2026-07-09
> 环境：Windows 本地开发环境，PostgreSQL + Redis，本地 ASGI，fake Agent
> 命令：`uv run pytest tests/golden/test_legacy_performance_baseline.py -q -rs --log-cli-level=INFO`

## 1. 问诊回合基线

样本为同一 Legacy 会话连续 20 次 `POST /messages`。fake Agent 消除真实模型
和网络波动，但保留 FastAPI、数据库事务、会话锁、Redis 事件及 Legacy
Inquiry/Sufficiency 编排。

| 指标 | 结果 |
|---|---:|
| 样本数 | 20 |
| 每回合模型调用数 | 2（InquiryAgent + SufficiencyAgent） |
| 总模型调用数 | 40 |
| P50 | 54.48 ms |
| P95 | 91.67 ms |
| 失败数 | 0 |
| 失败率 | 0% |

这些延迟只用于比较 Harness/编排开销，不是生产真实模型 SLA。后续对比必须使用
相同 fake-model 方法；真实模型基线属于 L8-4。

## 2. 完整 Legacy 链路调用数

典型单轮信息充分并最终生成病历的 Legacy 调用上限基线为 9 次：

1. `/messages`：Inquiry + Sufficiency，共 2 次；
2. `/advance` 的 inquiry/sufficiency：重复调用，共 2 次；
3. Syndrome、Prescription、Modification：3 次；
4. SafetyExplanation：1 次；
5. Record：1 次。

SafetyRuleEngine 是确定性调用，不计入模型调用数。L3 的目标是每条用户消息最多
一次 Intake 模型调用，简单下一问不调用模型；该指标必须相对本基线下降。

## 3. Token 基线

当前 Legacy `ModelGatewayClient` 会丢弃网关响应中的 `usage`，`agent_runs` 也没有
Token 字段，因此无法从现有生产可观测数据得到可信的 prompt/completion/total
Token。L0 将这一事实记录为基线：`token_usage=unavailable`，不使用字符数冒充
Token，也不调用真实模型补测。

Token 采集必须在 Harness RunArtifact/Episode 可观测性中实现，并且不得保存完整
Prompt 或原始模型输出。对应实施任务为 L2 Runtime 数据契约与 L8 Episode/指标；
在完成前，任何 Token 对比均应标为不可用。

## 4. 回归阈值

- L0 自动测试使用宽松防卡死门禁：fake-model `/messages` P95 < 5000 ms。
- 该阈值不是性能目标；性能回归判断应对比相同环境、相同样本的历史 P95。
- 失败率必须为 0%，每回合调用数必须精确为 2，防止隐式新增模型调用。
