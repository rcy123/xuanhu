"""LangGraph event 到版本化业务事件的纯转换层。

L1-4 范围：
- 将 LangGraph ``astream(stream_mode='updates')`` 的内部 chunk 转换为
  稳定、可序列化的 ``XuanhuRunEvent``。
- 事件不泄露内部 config、checkpoint、完整 state、prompt、模型原始输出、
  密钥或患者身份。
- 只暴露执行流程元数据（节点名、路由、run_id）。

对齐实施计划 §6.2 工作项 8 和 ADR-002：
- 外部只暴露版本化业务事件，不暴露 LangGraph 内部事件格式。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from typing_extensions import TypedDict

# 事件 Schema 版本。当事件结构发生不兼容变更时递增。
EVENT_SCHEMA_VERSION: str = "1"

# 允许从 state_delta 中提取的安全字段名。
# 这些字段是执行流程元数据，不含临床数据、prompt 或模型输出。
_SAFE_STATE_FIELDS: frozenset[str] = frozenset(
    {
        "route",
        "run_id",
        "command",
    }
)


class XuanhuRunEvent(TypedDict, total=False):
    """版本化业务事件。

    所有字段均为 JSON-safe（str），可被 ``json.dumps`` 序列化。
    不含完整 state、config、checkpoint、prompt、模型输出、密钥或患者身份。

    字段说明：
    - ``event_version``：事件 Schema 版本（当前为 ``"1"``）。
    - ``event_type``：事件类型（``graph_started`` / ``node_completed`` /
      ``graph_completed`` / ``graph_failed``）。
    - ``node_name``：产生该事件的节点名（仅 ``node_completed`` 有）。
    - ``route``：当前路由目标（从 state_delta 的 ``route`` 字段提取）。
    - ``run_id``：本次运行的唯一标识（从 state_delta 的 ``run_id`` 字段提取）。
    - ``command``：当前命令类型（从 state_delta 的 ``command`` 字段提取）。
    - ``timestamp``：ISO 8601 UTC 时间戳。
    - ``error_code``：错误码（仅 ``graph_failed`` 有，已脱敏）。
    """

    event_version: str
    event_type: str
    node_name: str
    route: str
    run_id: str
    command: str
    timestamp: str
    error_code: str


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。"""
    return datetime.now(UTC).isoformat()


def _extract_safe_fields(state_delta: dict[str, Any]) -> dict[str, str]:
    """从 state_delta 中提取安全字段。

    只提取 ``_SAFE_STATE_FIELDS`` 中的字段，忽略其余所有字段。
    确保不泄露完整 state、临床数据或敏感信息。
    """
    result: dict[str, str] = {}
    for field in _SAFE_STATE_FIELDS:
        value = state_delta.get(field)
        if isinstance(value, str) and value:
            result[field] = value
    return result


def make_graph_started_event(*, run_id: str = "") -> XuanhuRunEvent:
    """构造 ``graph_started`` 事件。"""
    event: XuanhuRunEvent = {
        "event_version": EVENT_SCHEMA_VERSION,
        "event_type": "graph_started",
        "timestamp": _now_iso(),
    }
    if run_id:
        event["run_id"] = run_id
    return event


def make_node_completed_event(
    node_name: str,
    state_delta: dict[str, Any],
) -> XuanhuRunEvent:
    """从 LangGraph updates chunk 构造 ``node_completed`` 事件。

    LangGraph ``stream_mode='updates'`` 产出形如 ``{node_name: state_delta}``
    的 dict。本函数提取安全字段并构造版本化事件。

    参数:
        node_name: 节点名（从 chunk key 提取）。
        state_delta: 节点输出的 state 增量（从 chunk value 提取）。

    返回:
        ``XuanhuRunEvent``，``event_type="node_completed"``。
    """
    safe = _extract_safe_fields(state_delta)
    event: XuanhuRunEvent = {
        "event_version": EVENT_SCHEMA_VERSION,
        "event_type": "node_completed",
        "node_name": node_name,
        "timestamp": _now_iso(),
    }
    event.update(safe)  # type: ignore[typeddict-item]
    return event


def make_graph_completed_event(*, run_id: str = "") -> XuanhuRunEvent:
    """构造 ``graph_completed`` 事件。"""
    event: XuanhuRunEvent = {
        "event_version": EVENT_SCHEMA_VERSION,
        "event_type": "graph_completed",
        "timestamp": _now_iso(),
    }
    if run_id:
        event["run_id"] = run_id
    return event


def make_graph_failed_event(*, error_code: str, run_id: str = "") -> XuanhuRunEvent:
    """构造 ``graph_failed`` 事件。

    参数:
        error_code: 脱敏错误码（不含堆栈、prompt 或敏感数据）。
        run_id: 运行标识（可选）。
    """
    event: XuanhuRunEvent = {
        "event_version": EVENT_SCHEMA_VERSION,
        "event_type": "graph_failed",
        "timestamp": _now_iso(),
        "error_code": error_code,
    }
    if run_id:
        event["run_id"] = run_id
    return event


def convert_updates_chunk(chunk: dict[str, Any]) -> XuanhuRunEvent | None:
    """将 LangGraph ``stream_mode='updates'`` chunk 转换为 ``XuanhuRunEvent``。

    LangGraph updates 模式产出形如 ``{node_name: state_delta}`` 的 dict。
    本函数提取节点名和安全字段，返回 ``node_completed`` 事件。
    如果 chunk 格式不符合预期，返回 ``None``。

    参数:
        chunk: LangGraph astream(stream_mode='updates') 产出的 chunk。

    返回:
        ``XuanhuRunEvent`` 或 ``None``（如果 chunk 格式无效）。
    """
    if not isinstance(chunk, dict) or len(chunk) == 0:
        return None

    # updates chunk: {node_name: state_delta}
    # 取第一个（也是唯一的）key-value 对
    node_name = next(iter(chunk.keys()))
    state_delta = chunk[node_name]

    if not isinstance(state_delta, dict):
        state_delta = {}

    return make_node_completed_event(node_name, state_delta)
