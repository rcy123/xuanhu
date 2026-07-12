"""L1-2 专项测试：GraphState、MainGraph 与命令路由。

测试范围：
1. MainGraph 可按 command 路由到正确占位节点（正向测试）。
2. unknown/invalid/empty command 有确定性 blocked/manual 结果（负向测试）。
3. Graph State 只包含可序列化执行数据和引用（JSON-safe 校验）。
4. 相同 thread_id 可用 InMemorySaver 读取状态（checkpoint 持久化）。
5. 不同 thread_id 互相隔离。
6. graph_version 命名空间不互相读取。
7. 所有 conditional edge 至少有正向和负向测试。

边界：
- 不接入业务 Agent、生产 API 路由或 PG checkpointer。
- 不引入真实模型依赖。
- 不改变 Legacy 生产行为。

无真实模型、Redis 或患者数据依赖。
"""

from __future__ import annotations

import json
import uuid

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import StateSnapshot

from app.agent_runtime.commands import (
    NODE_BLOCKED_TERMINAL,
    NODE_INTAKE_PLACEHOLDER,
    NODE_MANUAL_TERMINAL,
    NODE_REASONING_SUBGRAPH_V1,
    NODE_RECOVERY_PLACEHOLDER,
    NODE_REVIEW_PLACEHOLDER,
    VALID_COMMANDS,
    XuanhuCommand,
)
from app.agent_runtime.config import (
    DEFAULT_GRAPH_VERSION,
    GRAPH_VERSION_V1,
    make_run_config,
    make_thread_id,
)
from app.agent_runtime.errors import CommandRoutingError
from app.agent_runtime.graph import build_main_graph
from app.agent_runtime.routing import resolve_command_route, route_after_router
from app.agent_runtime.state import (
    ArtifactRef,
    Budget,
    GateResultRef,
    LastError,
    PendingInterrupt,
    XuanhuGraphState,
    default_state,
    validate_state_json_safe,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _random_session_id() -> str:
    """生成随机 session_id，避免跨测试 checkpoint 污染。"""
    return f"session-{uuid.uuid4().hex[:12]}"


def _make_initial_state(
    *,
    command: str = "",
    session_id: str | None = None,
    graph_version: str = GRAPH_VERSION_V1,
) -> XuanhuGraphState:
    """构造测试用初始 Graph State。

    不包含任何临床数据或患者信息。
    """
    return default_state(
        session_id=session_id or _random_session_id(),
        command=command,
        command_id=f"cmd-{uuid.uuid4().hex[:8]}" if command else "",
        graph_version=graph_version,
        run_id=f"run-{uuid.uuid4().hex[:8]}" if command else "",
    )


# ---------------------------------------------------------------------------
# 1. 命令路由正向测试
# ---------------------------------------------------------------------------


class TestCommandRoutingPositive:
    """正向路由测试：合法 command 路由到正确占位节点。"""

    @pytest.mark.asyncio
    async def test_message_routes_to_intake_placeholder(self) -> None:
        """command=message -> intake_placeholder。"""
        graph = build_main_graph()
        state = _make_initial_state(command=XuanhuCommand.MESSAGE.value)
        result = await graph.ainvoke(state, config=make_run_config(state["session_id"]))
        assert result["route"] == NODE_INTAKE_PLACEHOLDER

    @pytest.mark.asyncio
    async def test_advance_routes_to_reasoning_subgraph(self) -> None:
        """command=advance -> reasoning_subgraph_v1。"""
        graph = build_main_graph()
        state = _make_initial_state(command=XuanhuCommand.ADVANCE.value)
        result = await graph.ainvoke(state, config=make_run_config(state["session_id"]))
        assert result["route"] == NODE_REASONING_SUBGRAPH_V1

    @pytest.mark.asyncio
    async def test_review_routes_to_review_placeholder(self) -> None:
        """command=review -> review_placeholder。"""
        graph = build_main_graph()
        state = _make_initial_state(command=XuanhuCommand.REVIEW.value)
        result = await graph.ainvoke(state, config=make_run_config(state["session_id"]))
        assert result["route"] == NODE_REVIEW_PLACEHOLDER

    @pytest.mark.asyncio
    async def test_recover_routes_to_recovery_placeholder(self) -> None:
        """command=recover -> recovery_placeholder。"""
        graph = build_main_graph()
        state = _make_initial_state(command=XuanhuCommand.RECOVER.value)
        result = await graph.ainvoke(state, config=make_run_config(state["session_id"]))
        assert result["route"] == NODE_RECOVERY_PLACEHOLDER

    @pytest.mark.parametrize("command_value", list(VALID_COMMANDS))
    def test_resolve_command_route_for_all_valid_commands(self, command_value: str) -> None:
        """resolve_command_route 对所有合法 command 返回非空路由目标。"""
        route = resolve_command_route(command_value)
        assert route != ""
        assert route != NODE_BLOCKED_TERMINAL
        assert route != NODE_MANUAL_TERMINAL


# ---------------------------------------------------------------------------
# 2. 命令路由负向测试
# ---------------------------------------------------------------------------


class TestCommandRoutingNegative:
    """负向路由测试：unknown/invalid/empty command 有确定性结果。"""

    @pytest.mark.asyncio
    async def test_empty_command_routes_to_blocked_terminal(self) -> None:
        """空 command -> blocked_terminal（缺少必需输入）。"""
        graph = build_main_graph()
        state = _make_initial_state(command="")
        result = await graph.ainvoke(state, config=make_run_config(state["session_id"]))
        assert result["route"] == NODE_BLOCKED_TERMINAL

    @pytest.mark.asyncio
    async def test_unknown_command_routes_to_manual_terminal(self) -> None:
        """未知 command -> manual_terminal（需人工介入）。"""
        graph = build_main_graph()
        state = _make_initial_state(command="totally_unknown_command")
        result = await graph.ainvoke(state, config=make_run_config(state["session_id"]))
        assert result["route"] == NODE_MANUAL_TERMINAL

    @pytest.mark.asyncio
    async def test_none_command_routes_to_blocked_terminal(self) -> None:
        """command 为 None -> blocked_terminal。"""
        graph = build_main_graph()
        state = _make_initial_state(command="")
        # 模拟 command 缺失
        state["command"] = ""  # type: ignore[assignment]
        result = await graph.ainvoke(state, config=make_run_config(state["session_id"]))
        assert result["route"] == NODE_BLOCKED_TERMINAL

    def test_resolve_command_route_none_returns_blocked(self) -> None:
        """resolve_command_route(None) -> blocked_terminal。"""
        assert resolve_command_route(None) == NODE_BLOCKED_TERMINAL

    def test_resolve_command_route_empty_returns_blocked(self) -> None:
        """resolve_command_route('') -> blocked_terminal。"""
        assert resolve_command_route("") == NODE_BLOCKED_TERMINAL

    def test_resolve_command_route_unknown_returns_manual(self) -> None:
        """resolve_command_route('unknown_xyz') -> manual_terminal。"""
        assert resolve_command_route("unknown_xyz") == NODE_MANUAL_TERMINAL

    def test_command_routing_error_carries_command(self) -> None:
        """CommandRoutingError 携带原始 command 值用于审计。"""
        err = CommandRoutingError("bad_cmd")
        assert err.command == "bad_cmd"
        assert err.code == "COMMAND_ROUTING_UNKNOWN"


# ---------------------------------------------------------------------------
# 3. Conditional edge 正反向测试
# ---------------------------------------------------------------------------


class TestConditionalEdges:
    """每个 conditional edge 至少有正向和负向测试。"""

    def test_route_after_router_returns_state_route(self) -> None:
        """route_after_router 正向：返回 state 中已设置的 route。"""
        state: XuanhuGraphState = {"route": NODE_INTAKE_PLACEHOLDER}
        assert route_after_router(state) == NODE_INTAKE_PLACEHOLDER

    def test_route_after_router_fallback_on_empty_route(self) -> None:
        """route_after_router 负向：route 为空时回退到 blocked_terminal。"""
        state: XuanhuGraphState = {}
        assert route_after_router(state) == NODE_BLOCKED_TERMINAL

    def test_route_after_router_fallback_on_missing_route(self) -> None:
        """route_after_router 负向：route 字段缺失时回退到 blocked_terminal。"""
        state: XuanhuGraphState = {"session_id": "test"}
        assert route_after_router(state) == NODE_BLOCKED_TERMINAL

    @pytest.mark.parametrize(
        "command,expected_node",
        [
            (XuanhuCommand.MESSAGE.value, NODE_INTAKE_PLACEHOLDER),
            (XuanhuCommand.ADVANCE.value, NODE_REASONING_SUBGRAPH_V1),
            (XuanhuCommand.REVIEW.value, NODE_REVIEW_PLACEHOLDER),
            (XuanhuCommand.RECOVER.value, NODE_RECOVERY_PLACEHOLDER),
            ("", NODE_BLOCKED_TERMINAL),
            ("invalid_cmd", NODE_MANUAL_TERMINAL),
        ],
    )
    def test_conditional_edge_all_paths(self, command: str, expected_node: str) -> None:
        """conditional edge 的所有路径（4 正向 + 2 负向）都有确定性映射。"""
        assert resolve_command_route(command) == expected_node


# ---------------------------------------------------------------------------
# 4. Graph State JSON 序列化友好测试
# ---------------------------------------------------------------------------


class TestStateSerialization:
    """Graph State 只包含可序列化执行数据和引用。"""

    def test_default_state_is_json_serializable(self) -> None:
        """默认 state 可被 json.dumps 序列化。"""
        state = default_state()
        serialized = json.dumps(state, ensure_ascii=False)
        deserialized = json.loads(serialized)
        assert deserialized["session_id"] == state["session_id"]

    def test_state_with_all_fields_is_json_serializable(self) -> None:
        """包含所有字段的 state 可被 json.dumps 序列化。"""
        gate: GateResultRef = {
            "gate_name": "completeness",
            "decision": "passed",
            "policy_version": "1.0",
        }
        artifact: ArtifactRef = {
            "kind": "doctor_review",
            "artifact_id": str(uuid.uuid4()),
            "revision": 1,
        }
        interrupt: PendingInterrupt = {
            "kind": "doctor_review",
            "interrupt_id": "int-001",
            "resume_token_ref": "token-ref-001",
        }
        budget: Budget = {
            "remaining_steps": 10,
            "remaining_tokens": 5000,
            "deadline_ref": "2026-07-10T12:00:00Z",
        }
        error: LastError = {
            "code": "GATEWAY_TIMEOUT",
            "trace_id": "trace-001",
            "detail": "model gateway timeout",
        }
        state: XuanhuGraphState = {
            "session_id": "session-001",
            "domain_state_version": 3,
            "command": "message",
            "command_id": "cmd-001",
            "graph_version": "v1",
            "run_id": "run-001",
            "route": "intake_placeholder",
            "gate_results": [gate],
            "artifact_refs": [artifact],
            "pending_interrupt": interrupt,
            "budget": budget,
            "last_error": error,
        }
        serialized = json.dumps(state, ensure_ascii=False)
        deserialized = json.loads(serialized)
        assert deserialized["gate_results"][0]["decision"] == "passed"
        assert deserialized["artifact_refs"][0]["artifact_id"] == artifact["artifact_id"]
        assert deserialized["pending_interrupt"]["kind"] == "doctor_review"
        assert deserialized["budget"]["remaining_steps"] == 10
        assert deserialized["last_error"]["code"] == "GATEWAY_TIMEOUT"

    def test_validate_state_json_safe_accepts_valid_state(self) -> None:
        """validate_state_json_safe 接受合法 state。"""
        state = default_state(session_id="test-001", command="message")
        validate_state_json_safe(state)  # 不抛异常即通过

    def test_validate_state_json_safe_rejects_non_serializable(self) -> None:
        """validate_state_json_safe 拒绝不可序列化的值（如函数）。"""
        state: dict = {"bad_field": lambda x: x}
        with pytest.raises(TypeError):
            validate_state_json_safe(state)

    def test_state_does_not_contain_pii_fields(self) -> None:
        """Graph State 不包含患者身份信息字段。"""
        state = default_state()
        # 确保不存在常见 PII 字段名
        pii_fields = {"name", "patient_name", "patient_info", "phone", "id_card"}
        for field in pii_fields:
            assert field not in state, f"PII field {field!r} should not be in Graph State"

    def test_state_does_not_contain_runtime_objects(self) -> None:
        """Graph State 不包含 SQLAlchemy session、模型客户端或函数。"""
        state = default_state()
        # 确保不存在常见运行时对象字段名
        runtime_fields = {
            "db_session",
            "sqlalchemy_session",
            "model_client",
            "openai_client",
            "prompt",
            "raw_output",
            "raw_model_output",
        }
        for field in runtime_fields:
            assert field not in state, f"Runtime object field {field!r} should not be in Graph State"


# ---------------------------------------------------------------------------
# 5. InMemorySaver checkpoint 持久化测试
# ---------------------------------------------------------------------------


class TestInMemorySaverCheckpoint:
    """相同 thread_id 可用 InMemorySaver 读取状态。"""

    @pytest.mark.asyncio
    async def test_checkpoint_persists_state_after_invoke(self) -> None:
        """图执行后可通过 aget_state 读取 checkpoint 快照。"""
        checkpointer = InMemorySaver()
        graph = build_main_graph(checkpointer=checkpointer)
        session_id = _random_session_id()
        config = make_run_config(session_id)

        state = _make_initial_state(
            command=XuanhuCommand.MESSAGE.value,
            session_id=session_id,
        )
        await graph.ainvoke(state, config=config)

        snapshot: StateSnapshot = await graph.aget_state(config)
        assert snapshot is not None
        assert snapshot.values.get("route") == NODE_INTAKE_PLACEHOLDER
        assert snapshot.values.get("command") == XuanhuCommand.MESSAGE.value

    @pytest.mark.asyncio
    async def test_checkpoint_preserves_session_id(self) -> None:
        """checkpoint 保留 session_id 用于 Domain State 关联。"""
        checkpointer = InMemorySaver()
        graph = build_main_graph(checkpointer=checkpointer)
        session_id = _random_session_id()
        config = make_run_config(session_id)

        state = _make_initial_state(
            command=XuanhuCommand.ADVANCE.value,
            session_id=session_id,
        )
        await graph.ainvoke(state, config=config)

        snapshot = await graph.aget_state(config)
        assert snapshot.values.get("session_id") == session_id

    @pytest.mark.asyncio
    async def test_checkpoint_preserves_domain_state_version(self) -> None:
        """checkpoint 保留 domain_state_version 引用。"""
        checkpointer = InMemorySaver()
        graph = build_main_graph(checkpointer=checkpointer)
        session_id = _random_session_id()
        config = make_run_config(session_id)

        state = _make_initial_state(
            command=XuanhuCommand.REVIEW.value,
            session_id=session_id,
        )
        state["domain_state_version"] = 42
        await graph.ainvoke(state, config=config)

        snapshot = await graph.aget_state(config)
        assert snapshot.values.get("domain_state_version") == 42


# ---------------------------------------------------------------------------
# 6. thread_id 隔离测试
# ---------------------------------------------------------------------------


class TestThreadIdIsolation:
    """不同 thread_id 的 checkpoint 互相隔离。"""

    @pytest.mark.asyncio
    async def test_different_threads_are_isolated(self) -> None:
        """不同 session_id（不同 thread_id）的 checkpoint 互相隔离。"""
        checkpointer = InMemorySaver()
        graph = build_main_graph(checkpointer=checkpointer)

        session_a = _random_session_id()
        session_b = _random_session_id()
        config_a = make_run_config(session_a)
        config_b = make_run_config(session_b)

        state_a = _make_initial_state(
            command=XuanhuCommand.MESSAGE.value,
            session_id=session_a,
        )
        state_b = _make_initial_state(
            command=XuanhuCommand.ADVANCE.value,
            session_id=session_b,
        )

        await graph.ainvoke(state_a, config=config_a)
        await graph.ainvoke(state_b, config=config_b)

        snap_a = await graph.aget_state(config_a)
        snap_b = await graph.aget_state(config_b)

        # thread_a 路由到 intake，thread_b 路由到 reasoning
        assert snap_a.values.get("route") == NODE_INTAKE_PLACEHOLDER
        assert snap_b.values.get("route") == NODE_REASONING_SUBGRAPH_V1

        # session_id 隔离
        assert snap_a.values.get("session_id") == session_a
        assert snap_b.values.get("session_id") == session_b

    @pytest.mark.asyncio
    async def test_same_thread_overwrites_state(self) -> None:
        """相同 thread_id 二次 invoke 会更新 checkpoint。"""
        checkpointer = InMemorySaver()
        graph = build_main_graph(checkpointer=checkpointer)

        session_id = _random_session_id()
        config = make_run_config(session_id)

        # 第一次：message
        state_1 = _make_initial_state(
            command=XuanhuCommand.MESSAGE.value,
            session_id=session_id,
        )
        await graph.ainvoke(state_1, config=config)
        snap_1 = await graph.aget_state(config)
        assert snap_1.values.get("route") == NODE_INTAKE_PLACEHOLDER

        # 第二次：advance（同 thread_id）
        state_2 = _make_initial_state(
            command=XuanhuCommand.ADVANCE.value,
            session_id=session_id,
        )
        await graph.ainvoke(state_2, config=config)
        snap_2 = await graph.aget_state(config)
        assert snap_2.values.get("route") == NODE_REASONING_SUBGRAPH_V1


# ---------------------------------------------------------------------------
# 7. graph_version 命名空间隔离测试
# ---------------------------------------------------------------------------


class TestGraphVersionNamespaceIsolation:
    """graph_version 命名空间不互相读取。"""

    @pytest.mark.asyncio
    async def test_different_graph_versions_are_isolated(self) -> None:
        """相同 session_id 但不同 graph_version 的 checkpoint 互相隔离。"""
        checkpointer = InMemorySaver()
        graph = build_main_graph(checkpointer=checkpointer)

        session_id = _random_session_id()
        config_v1 = make_run_config(session_id, graph_version="v1")
        config_v2 = make_run_config(session_id, graph_version="v2")

        state_v1 = _make_initial_state(
            command=XuanhuCommand.MESSAGE.value,
            session_id=session_id,
            graph_version="v1",
        )
        state_v2 = _make_initial_state(
            command=XuanhuCommand.ADVANCE.value,
            session_id=session_id,
            graph_version="v2",
        )

        await graph.ainvoke(state_v1, config=config_v1)
        await graph.ainvoke(state_v2, config=config_v2)

        snap_v1 = await graph.aget_state(config_v1)
        snap_v2 = await graph.aget_state(config_v2)

        # v1 路由到 intake，v2 路由到 reasoning
        assert snap_v1.values.get("route") == NODE_INTAKE_PLACEHOLDER
        assert snap_v2.values.get("route") == NODE_REASONING_SUBGRAPH_V1

        # graph_version 隔离
        assert snap_v1.values.get("graph_version") == "v1"
        assert snap_v2.values.get("graph_version") == "v2"

    def test_make_thread_id_includes_graph_version(self) -> None:
        """make_thread_id 将 graph_version 编码进 thread_id。"""
        thread_id = make_thread_id("session-001", "v1")
        assert thread_id == "v1:session-001"

    def test_make_thread_id_default_graph_version(self) -> None:
        """make_thread_id 默认使用 DEFAULT_GRAPH_VERSION。"""
        thread_id = make_thread_id("session-001")
        assert thread_id == f"{DEFAULT_GRAPH_VERSION}:session-001"

    def test_make_run_config_produces_correct_structure(self) -> None:
        """make_run_config 产出正确的 LangGraph config 结构。"""
        config = make_run_config("session-001", graph_version="v1")
        assert "configurable" in config
        assert config["configurable"]["thread_id"] == "v1:session-001"


# ---------------------------------------------------------------------------
# 8. MainGraph 结构与编译测试
# ---------------------------------------------------------------------------


class TestMainGraphStructure:
    """MainGraph 结构完整性测试。"""

    def test_build_main_graph_without_checkpointer(self) -> None:
        """不传 checkpointer 也能编译图。"""
        graph = build_main_graph()
        assert isinstance(graph, CompiledStateGraph)

    def test_build_main_graph_with_inmemory_saver(self) -> None:
        """传入 InMemorySaver 可编译图。"""
        checkpointer = InMemorySaver()
        graph = build_main_graph(checkpointer=checkpointer)
        assert isinstance(graph, CompiledStateGraph)

    @pytest.mark.asyncio
    async def test_graph_executes_all_placeholder_nodes(self) -> None:
        """图可执行所有占位节点路径而不报错。"""
        graph = build_main_graph()
        for command, expected_route in [
            (XuanhuCommand.MESSAGE.value, NODE_INTAKE_PLACEHOLDER),
            (XuanhuCommand.ADVANCE.value, NODE_REASONING_SUBGRAPH_V1),
            (XuanhuCommand.REVIEW.value, NODE_REVIEW_PLACEHOLDER),
            (XuanhuCommand.RECOVER.value, NODE_RECOVERY_PLACEHOLDER),
            ("", NODE_BLOCKED_TERMINAL),
            ("unknown", NODE_MANUAL_TERMINAL),
        ]:
            state = _make_initial_state(command=command)
            result = await graph.ainvoke(state, config=make_run_config(state["session_id"]))
            assert result["route"] == expected_route, (
                f"command={command!r} expected route={expected_route!r}, got {result['route']!r}"
            )

    @pytest.mark.asyncio
    async def test_graph_preserves_command_id(self) -> None:
        """图执行后保留 command_id（幂等键）。"""
        graph = build_main_graph()
        state = _make_initial_state(command=XuanhuCommand.MESSAGE.value)
        result = await graph.ainvoke(state, config=make_run_config(state["session_id"]))
        assert result["command_id"] == state["command_id"]

    @pytest.mark.asyncio
    async def test_graph_preserves_run_id(self) -> None:
        """图执行后保留 run_id。"""
        graph = build_main_graph()
        state = _make_initial_state(command=XuanhuCommand.ADVANCE.value)
        result = await graph.ainvoke(state, config=make_run_config(state["session_id"]))
        assert result["run_id"] == state["run_id"]
