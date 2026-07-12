"""XuanhuGraphState 定义。

对齐实施计划 §6.2 和 ADR-002 Graph State 边界：
Graph State 只保存最小可序列化执行数据和引用，不是临床事实真源。

字段说明：
- ``session_id``：会话标识，对应 ``thread_id``。
- ``domain_state_version``：Domain State 版本指针（整型引用，不是完整 Domain State）。
- ``command``：当前待执行命令类型（message/advance/review/recover），不含患者输入或临床载荷。
- ``command_id``：命令幂等键。
- ``graph_version``：图版本标识（用于版本化 checkpoint namespace）。
- ``run_id``：本次图执行运行的唯一标识。
- ``route``：当前路由目标（节点名）。
- ``gate_results``：Policy Gate 结果的标识符引用，不保存完整 Gate 输出或临床字段。
- ``artifact_refs``：Domain artifact 引用集合（UUID 字符串引用，如 doctor_review_ref）。
- ``pending_interrupt``：当前挂起中断的类型、ID 和恢复令牌引用，不保存医师决定或患者数据。
- ``budget``：执行预算追踪（剩余步数/token/deadline 引用）。
- ``last_error``：脱敏错误码和 trace 引用，不保存异常堆栈、Prompt 或模型输出。

禁止放入 Graph State 的内容（ADR-002 §明确禁止）：
- SQLAlchemy Session 或任何 ORM 对象
- 模型客户端
- Python 函数、方法或可调用对象
- 完整 Prompt 文本
- 完整原始模型输出
- 结构化临床模型输出（SyndromeResult、FormulaDraft 等）
- 患者身份信息（PII）
"""

from __future__ import annotations

from typing import Any

from typing_extensions import TypedDict

# ---------------------------------------------------------------------------
# 子结构 TypedDict（全部 JSON-safe 类型）
# ---------------------------------------------------------------------------


class GateResultRef(TypedDict, total=False):
    """Policy Gate 结果引用。

    只保存 gate 名称、判定和版本引用，不保存完整 Gate 输出或临床字段。
    """

    gate_name: str
    decision: str  # "passed" | "failed" | "blocked"
    policy_version: str


class ArtifactRef(TypedDict, total=False):
    """Domain artifact 引用。

    通过 UUID 字符串引用 Domain State 中的临床产物（如 doctor_review、
    safety_rule_result、syndrome_result），不保存完整临床结构化模型输出。
    """

    kind: str  # "doctor_review" | "safety_rule_result" | "syndrome_result" | ...
    artifact_id: str  # UUID 字符串
    revision: int


class PendingInterrupt(TypedDict, total=False):
    """挂起中断信息。

    只保存中断类型、ID 和恢复令牌引用，不保存医师决定、处方或患者数据。
    """

    kind: str  # "doctor_review" | "human_input" | ...
    interrupt_id: str
    resume_token_ref: str


class Budget(TypedDict, total=False):
    """执行预算追踪。

    追踪剩余步数、token 和 deadline 引用，不保存完整调用日志。
    """

    remaining_steps: int
    remaining_tokens: int
    deadline_ref: str  # ISO 8601 时间戳字符串或 trace 引用


class LastError(TypedDict, total=False):
    """最近错误信息（脱敏）。

    只保存错误码和 trace 引用，不保存异常堆栈、Prompt、模型输出或患者数据。
    """

    code: str
    trace_id: str
    detail: str  # 脱敏简短描述，不含敏感数据


# ---------------------------------------------------------------------------
# XuanhuGraphState（主 State）
# ---------------------------------------------------------------------------


class XuanhuGraphState(TypedDict, total=False):
    """悬壶 MainGraph 执行游标状态。

    使用 ``TypedDict(total=False)`` 以兼容 LangGraph 的增量 state 更新模式：
    节点可以只返回部分字段，LangGraph 会将其合并到完整 state 中。

    所有字段类型均为 JSON-safe（str、int、bool、list、dict、None），
    确保可被 ``json.dumps`` 和 LangGraph 默认序列化器处理。
    """

    session_id: str
    domain_state_version: int
    command: str  # XuanhuCommand 值
    command_id: str
    graph_version: str
    run_id: str
    route: str
    intake_route: str
    reasoning_route: str
    gate_results: list[GateResultRef]
    artifact_refs: list[ArtifactRef]
    pending_interrupt: PendingInterrupt | None
    budget: Budget
    last_error: LastError | None


# ---------------------------------------------------------------------------
# 序列化校验工具
# ---------------------------------------------------------------------------


def validate_state_json_safe(state: dict[str, Any]) -> None:
    """验证 state dict 只包含 JSON 可序列化类型。

    对齐 ADR-002：Graph State 必须 JSON/标准序列化友好。
    使用 ``json.dumps`` 作为权威校验，拒绝不可序列化值（如函数、ORM 对象）。

    参数:
        state: 待校验的 state dict。

    Raises:
        TypeError: 如果 state 包含不可 JSON 序列化的值。
    """
    import json

    json.dumps(state, ensure_ascii=False)


def default_state(
    *,
    session_id: str = "",
    command: str = "",
    command_id: str = "",
    graph_version: str = "v1",
    run_id: str = "",
) -> XuanhuGraphState:
    """构造一个最小默认 Graph State（所有字段有安全初始值）。

    用于测试和图初始化。不包含任何临床数据。
    """
    return XuanhuGraphState(
        session_id=session_id,
        domain_state_version=0,
        command=command,
        command_id=command_id,
        graph_version=graph_version,
        run_id=run_id,
        route="",
        intake_route="",
        reasoning_route="",
        gate_results=[],
        artifact_refs=[],
        pending_interrupt=None,
        budget=Budget(remaining_steps=0, remaining_tokens=0, deadline_ref=""),
        last_error=None,
    )
