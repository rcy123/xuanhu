# Agent 重构 L0-2 交接文件

## 任务

- 编号：L0-2
- 名称：Golden E2E 行为基线
- 状态：已完成，验收通过
- 日期：2026-07-09

## 交付

- `tests/golden/test_legacy_golden.py`
- `tests/golden/conftest.py`
- `docs/01_agent部分优化/legacy-behavior-baseline.md`

覆盖正常问诊、信息不足、过敏、妊娠/可能妊娠、红旗、安全失败与修改、
医师确认/修改/拒绝、病历生成。测试使用 fake Agent，不调用真实模型。

## 验收证据

- Golden 专项：`9 passed, 1 xfailed`
- strict xfail：Legacy 缺少确定性 red-flag gate；L3 必须修复，禁止作为
  LangGraph 兼容目标。
- 全量后端：`954 passed, 1 xfailed, 2 warnings`
- Legacy API、数据库、Redis、SSE、前端和测试分类已记录在行为基线。
