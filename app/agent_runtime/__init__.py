"""悬壶 LangGraph Agent Runtime 骨架（L1）。

本包是 LangGraph v2 执行体系的最小运行底座，不包含临床逻辑。

L1-2 范围：
- ``XuanhuGraphState``：对齐实施计划 §6.2 的最小可序列化执行游标状态。
- ``MainGraph``：START -> command router -> 占位节点 -> END/blocked/manual terminal。
- 命令路由：message/advance/review/recover/unknown。

禁止事项（L1-2 边界）：
- 不接入业务 Agent。
- 不接入真实模型、Redis、RAG 或患者数据。
- 不接入 FastAPI 生产路由。
- 不接入 AsyncPostgresSaver 生产 checkpointer（留给 L1-3）。
- 不实现 GraphRunner/stream（留给 L1-4）。
"""

from __future__ import annotations

__all__: list[str] = []
