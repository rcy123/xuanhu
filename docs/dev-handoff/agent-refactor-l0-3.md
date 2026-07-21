# Agent 重构 L0-3 交接文件

## 任务

- 编号：L0-3
- 名称：Runtime Feature Flag 与性能基线
- 状态：已完成，验收通过
- 日期：2026-07-09

## 交付

- `app/core/config.py`：`AGENT_RUNTIME_VERSION=legacy|langgraph`
- `.env.example`：开关示例（该根文件当前被 `.gitignore` 忽略）
- `tests/test_agent_runtime_flag.py`
- `tests/golden/test_legacy_performance_baseline.py`
- `docs/01_agent部分优化/legacy-performance-baseline.md`

Feature Flag 默认严格为 `legacy`。L0 只完成配置校验，不实现 LangGraph
运行时路由，不改变任何既有会话或 Legacy 行为。

## 验收证据

- Feature Flag：`8 passed`
- 性能专项：`1 passed`
- 20 个 `/messages` 回合：40 次模型调用，P50 54.48 ms，P95 91.67 ms，
  失败率 0%。
- Token：当前 Legacy 网关丢弃 usage，明确记录为 `unavailable`，不得用字符数
  冒充；后续由 RunArtifact/Episode 指标补齐。
