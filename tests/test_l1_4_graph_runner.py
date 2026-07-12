"""L1-4 专项测试：GraphRunner、超时取消与事件转换。

测试范围：
1. 正常 invoke：GraphRunner.ainvoke 返回正确结果。
2. 正常 stream：astream_events 产出有序事件。
3. 事件 schema：事件可序列化、版本稳定、不含敏感字段。
4. 超时：总超时正确触发 GraphRunnerTimeoutError。
5. 取消：外部取消正确传播（不吞掉 CancelledError）。
6. 错误归一化：执行异常归一化为 GraphRunnerError（消息脱敏）。
7. 错配拒绝：config/state session_id 或 graph_version 错配在运行前被拒绝。
8. 事件顺序：graph_started → node_completed* → graph_completed。
9. 无 checkpointer 兼容性。

无真实模型、Redis、RAG 或患者数据依赖。
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agent_runtime.commands import NODE_REASONING_SUBGRAPH_V1, XuanhuCommand
from app.agent_runtime.config import make_run_config
from app.agent_runtime.errors import (
    CheckpointConfigMismatchError,
    GraphRunnerError,
    GraphRunnerTimeoutError,
)
from app.agent_runtime.events import (
    EVENT_SCHEMA_VERSION,
    convert_updates_chunk,
    make_graph_completed_event,
    make_graph_failed_event,
    make_graph_started_event,
)
from app.agent_runtime.graph import build_main_graph
from app.agent_runtime.runner import GraphRunner
from app.agent_runtime.state import default_state

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _random_session_id() -> str:
    return f"session-{uuid.uuid4().hex[:12]}"


def _make_state(
    *,
    command: str = XuanhuCommand.MESSAGE.value,
    session_id: str | None = None,
    graph_version: str = "v1",
) -> dict:
    return default_state(
        session_id=session_id or _random_session_id(),
        command=command,
        command_id=f"cmd-{uuid.uuid4().hex[:8]}",
        graph_version=graph_version,
        run_id=f"run-{uuid.uuid4().hex[:8]}",
    )


# ---------------------------------------------------------------------------
# 1. 正常 invoke
# ---------------------------------------------------------------------------


class TestNormalInvoke:
    """GraphRunner.ainvoke 正常执行。"""

    @pytest.mark.asyncio
    async def test_ainvoke_returns_correct_result(self) -> None:
        """ainvoke 返回正确的路由结果。"""
        checkpointer = InMemorySaver()
        graph = build_main_graph(checkpointer=checkpointer)
        runner = GraphRunner(graph, timeout_seconds=10)

        session_id = _random_session_id()
        config = make_run_config(session_id)
        state = _make_state(command=XuanhuCommand.MESSAGE.value, session_id=session_id)

        result = await runner.ainvoke(state, config)
        assert result["route"] == "intake_subgraph_v1"

    @pytest.mark.asyncio
    async def test_ainvoke_preserves_session_id(self) -> None:
        """ainvoke 保留 session_id。"""
        checkpointer = InMemorySaver()
        graph = build_main_graph(checkpointer=checkpointer)
        runner = GraphRunner(graph)

        session_id = _random_session_id()
        config = make_run_config(session_id)
        state = _make_state(command=XuanhuCommand.ADVANCE.value, session_id=session_id)

        result = await runner.ainvoke(state, config)
        assert result["route"] == NODE_REASONING_SUBGRAPH_V1
        assert result["session_id"] == session_id

    @pytest.mark.asyncio
    async def test_ainvoke_without_timeout(self) -> None:
        """timeout_seconds=None 不限制执行。"""
        checkpointer = InMemorySaver()
        graph = build_main_graph(checkpointer=checkpointer)
        runner = GraphRunner(graph, timeout_seconds=None)

        session_id = _random_session_id()
        config = make_run_config(session_id)
        state = _make_state(command=XuanhuCommand.REVIEW.value, session_id=session_id)

        result = await runner.ainvoke(state, config)
        assert result["route"] == "review_placeholder"

    @pytest.mark.asyncio
    async def test_ainvoke_with_zero_timeout(self) -> None:
        """timeout_seconds=0 不限制执行。"""
        checkpointer = InMemorySaver()
        graph = build_main_graph(checkpointer=checkpointer)
        runner = GraphRunner(graph, timeout_seconds=0)

        session_id = _random_session_id()
        config = make_run_config(session_id)
        state = _make_state(command=XuanhuCommand.RECOVER.value, session_id=session_id)

        result = await runner.ainvoke(state, config)
        assert result["route"] == "recovery_placeholder"


# ---------------------------------------------------------------------------
# 2. 正常 stream
# ---------------------------------------------------------------------------


class TestNormalStream:
    """astream_events 产出有序事件。"""

    @pytest.mark.asyncio
    async def test_stream_produces_events(self) -> None:
        """astream_events 产出事件。"""
        checkpointer = InMemorySaver()
        graph = build_main_graph(checkpointer=checkpointer)
        runner = GraphRunner(graph, timeout_seconds=10)

        session_id = _random_session_id()
        config = make_run_config(session_id)
        state = _make_state(command=XuanhuCommand.MESSAGE.value, session_id=session_id)

        events = []
        async for event in runner.astream_events(state, config):
            events.append(event)

        # 至少有 graph_started + node_completed + graph_completed
        assert len(events) >= 3

    @pytest.mark.asyncio
    async def test_stream_first_event_is_graph_started(self) -> None:
        """第一个事件是 graph_started。"""
        checkpointer = InMemorySaver()
        graph = build_main_graph(checkpointer=checkpointer)
        runner = GraphRunner(graph)

        session_id = _random_session_id()
        config = make_run_config(session_id)
        state = _make_state(session_id=session_id)

        events = []
        async for event in runner.astream_events(state, config):
            events.append(event)

        assert events[0]["event_type"] == "graph_started"

    @pytest.mark.asyncio
    async def test_stream_last_event_is_graph_completed(self) -> None:
        """最后一个事件是 graph_completed。"""
        checkpointer = InMemorySaver()
        graph = build_main_graph(checkpointer=checkpointer)
        runner = GraphRunner(graph)

        session_id = _random_session_id()
        config = make_run_config(session_id)
        state = _make_state(session_id=session_id)

        events = []
        async for event in runner.astream_events(state, config):
            events.append(event)

        assert events[-1]["event_type"] == "graph_completed"


# ---------------------------------------------------------------------------
# 3. 事件 schema
# ---------------------------------------------------------------------------


class TestEventSchema:
    """事件可序列化、版本稳定、不含敏感字段。"""

    @pytest.mark.asyncio
    async def test_events_are_json_serializable(self) -> None:
        """所有事件可被 json.dumps 序列化。"""
        checkpointer = InMemorySaver()
        graph = build_main_graph(checkpointer=checkpointer)
        runner = GraphRunner(graph)

        session_id = _random_session_id()
        config = make_run_config(session_id)
        state = _make_state(session_id=session_id)

        async for event in runner.astream_events(state, config):
            serialized = json.dumps(event, ensure_ascii=False)
            deserialized = json.loads(serialized)
            assert deserialized["event_version"] == EVENT_SCHEMA_VERSION

    @pytest.mark.asyncio
    async def test_all_events_have_version(self) -> None:
        """所有事件都有 event_version 字段。"""
        checkpointer = InMemorySaver()
        graph = build_main_graph(checkpointer=checkpointer)
        runner = GraphRunner(graph)

        session_id = _random_session_id()
        config = make_run_config(session_id)
        state = _make_state(session_id=session_id)

        async for event in runner.astream_events(state, config):
            assert "event_version" in event
            assert event["event_version"] == EVENT_SCHEMA_VERSION

    @pytest.mark.asyncio
    async def test_all_events_have_timestamp(self) -> None:
        """所有事件都有 timestamp 字段。"""
        checkpointer = InMemorySaver()
        graph = build_main_graph(checkpointer=checkpointer)
        runner = GraphRunner(graph)

        session_id = _random_session_id()
        config = make_run_config(session_id)
        state = _make_state(session_id=session_id)

        async for event in runner.astream_events(state, config):
            assert "timestamp" in event
            assert isinstance(event["timestamp"], str)

    @pytest.mark.asyncio
    async def test_events_do_not_contain_sensitive_fields(self) -> None:
        """事件不含 config、checkpoint、完整 state、prompt 或模型输出字段。"""
        checkpointer = InMemorySaver()
        graph = build_main_graph(checkpointer=checkpointer)
        runner = GraphRunner(graph)

        session_id = _random_session_id()
        config = make_run_config(session_id)
        state = _make_state(session_id=session_id)

        forbidden_fields = {
            "config",
            "configurable",
            "checkpoint",
            "checkpoint_ns",
            "thread_id",
            "prompt",
            "model_output",
            "raw_output",
            "api_key",
            "db_url",
            "password",
            "patient_info",
            "domain_state_version",
            "gate_results",
            "artifact_refs",
            "pending_interrupt",
            "budget",
            "last_error",
        }

        async for event in runner.astream_events(state, config):
            for field in forbidden_fields:
                assert field not in event, f"Event contains forbidden field: {field}"

    @pytest.mark.asyncio
    async def test_node_completed_events_have_node_name(self) -> None:
        """node_completed 事件有 node_name 字段。"""
        checkpointer = InMemorySaver()
        graph = build_main_graph(checkpointer=checkpointer)
        runner = GraphRunner(graph)

        session_id = _random_session_id()
        config = make_run_config(session_id)
        state = _make_state(session_id=session_id)

        async for event in runner.astream_events(state, config):
            if event["event_type"] == "node_completed":
                assert "node_name" in event
                assert isinstance(event["node_name"], str)
                assert event["node_name"]  # non-empty


# ---------------------------------------------------------------------------
# 4. 超时
# ---------------------------------------------------------------------------


class TestTimeout:
    """总超时正确触发 GraphRunnerTimeoutError。"""

    @pytest.mark.asyncio
    async def test_ainvoke_timeout_raises_timeout_error(self) -> None:
        """ainvoke 超时抛出 GraphRunnerTimeoutError。"""
        from langgraph.graph import END, START, StateGraph
        from typing_extensions import TypedDict

        class SlowState(TypedDict, total=False):
            step: int

        graph_def = StateGraph(SlowState)

        async def _slow_node(state: SlowState) -> dict:
            await asyncio.sleep(10)
            return {"step": 1}

        graph_def.add_node("slow", _slow_node)
        graph_def.add_edge(START, "slow")
        graph_def.add_edge("slow", END)
        graph = graph_def.compile(checkpointer=InMemorySaver())

        runner = GraphRunner(graph, timeout_seconds=0.05)

        with pytest.raises(GraphRunnerTimeoutError) as exc_info:
            await runner.ainvoke({"step": 0}, config={"configurable": {"thread_id": "v1:timeout-test"}})

        assert exc_info.value.code == "RUNNER_TIMEOUT"

    @pytest.mark.asyncio
    async def test_stream_timeout_emits_failed_event_then_raises(self) -> None:
        """astream_events 超时时产出 graph_failed 事件后抛出异常。"""
        from langgraph.graph import END, START, StateGraph
        from typing_extensions import TypedDict

        class SlowState(TypedDict, total=False):
            step: int

        graph_def = StateGraph(SlowState)

        async def _slow_node(state: SlowState) -> dict:
            await asyncio.sleep(10)
            return {"step": 1}

        graph_def.add_node("slow", _slow_node)
        graph_def.add_edge(START, "slow")
        graph_def.add_edge("slow", END)
        graph = graph_def.compile(checkpointer=InMemorySaver())

        runner = GraphRunner(graph, timeout_seconds=0.05)

        events = []
        with pytest.raises(GraphRunnerTimeoutError):
            async for event in runner.astream_events(
                {"step": 0},
                config={"configurable": {"thread_id": "v1:stream-timeout-test"}},
            ):
                events.append(event)

        # 至少有 graph_started 和 graph_failed
        event_types = [e["event_type"] for e in events]
        assert "graph_started" in event_types
        assert "graph_failed" in event_types


# ---------------------------------------------------------------------------
# 5. 取消
# ---------------------------------------------------------------------------


class TestCancellation:
    """外部取消正确传播。"""

    @pytest.mark.asyncio
    async def test_ainvoke_cancellation_propagates(self) -> None:
        """ainvoke 被外部取消时 CancelledError 被传播。"""
        from langgraph.graph import END, START, StateGraph
        from typing_extensions import TypedDict

        class SlowState(TypedDict, total=False):
            step: int

        graph_def = StateGraph(SlowState)

        async def _slow_node(state: SlowState) -> dict:
            await asyncio.sleep(10)
            return {"step": 1}

        graph_def.add_node("slow", _slow_node)
        graph_def.add_edge(START, "slow")
        graph_def.add_edge("slow", END)
        graph = graph_def.compile(checkpointer=InMemorySaver())

        runner = GraphRunner(graph, timeout_seconds=None)

        task = asyncio.create_task(
            runner.ainvoke({"step": 0}, config={"configurable": {"thread_id": "v1:cancel-test"}})
        )
        await asyncio.sleep(0.05)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_stream_cancellation_propagates(self) -> None:
        """astream_events 被外部取消时 CancelledError 被传播。"""
        from langgraph.graph import END, START, StateGraph
        from typing_extensions import TypedDict

        class SlowState(TypedDict, total=False):
            step: int

        graph_def = StateGraph(SlowState)

        async def _slow_node(state: SlowState) -> dict:
            await asyncio.sleep(10)
            return {"step": 1}

        graph_def.add_node("slow", _slow_node)
        graph_def.add_edge(START, "slow")
        graph_def.add_edge("slow", END)
        graph = graph_def.compile(checkpointer=InMemorySaver())

        runner = GraphRunner(graph, timeout_seconds=None)

        async def _consume() -> list:
            events = []
            async for event in runner.astream_events(
                {"step": 0},
                config={"configurable": {"thread_id": "v1:stream-cancel-test"}},
            ):
                events.append(event)
            return events

        task = asyncio.create_task(_consume())
        await asyncio.sleep(0.05)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# 6. 错误归一化
# ---------------------------------------------------------------------------


class TestErrorNormalization:
    """执行异常归一化为 GraphRunnerError（消息脱敏）。"""

    @pytest.mark.asyncio
    async def test_ainvoke_error_normalized(self) -> None:
        """ainvoke 执行异常归一化为 GraphRunnerError。"""
        from langgraph.graph import END, START, StateGraph
        from typing_extensions import TypedDict

        class ErrState(TypedDict, total=False):
            step: int

        graph_def = StateGraph(ErrState)

        async def _error_node(state: ErrState) -> dict:
            raise RuntimeError("internal error with sensitive: postgresql://user:pass@host/db")

        graph_def.add_node("error", _error_node)
        graph_def.add_edge(START, "error")
        graph_def.add_edge("error", END)
        graph = graph_def.compile(checkpointer=InMemorySaver())

        runner = GraphRunner(graph, timeout_seconds=10)

        with pytest.raises(GraphRunnerError) as exc_info:
            await runner.ainvoke({"step": 0}, config={"configurable": {"thread_id": "v1:error-test"}})

        assert exc_info.value.code == "RUNNER_EXECUTION_FAILED"
        error_msg = str(exc_info.value)
        # 敏感信息被脱敏
        assert "postgresql://user:pass@host/db" not in error_msg
        assert "pass" not in error_msg.split("RuntimeError:")[-1]  # password scrubbed

    @pytest.mark.asyncio
    async def test_stream_error_emits_failed_event(self) -> None:
        """astream_events 执行异常时产出 graph_failed 事件。"""
        from langgraph.graph import END, START, StateGraph
        from typing_extensions import TypedDict

        class ErrState(TypedDict, total=False):
            step: int

        graph_def = StateGraph(ErrState)

        async def _error_node(state: ErrState) -> dict:
            raise ValueError("something went wrong")

        graph_def.add_node("error", _error_node)
        graph_def.add_edge(START, "error")
        graph_def.add_edge("error", END)
        graph = graph_def.compile(checkpointer=InMemorySaver())

        runner = GraphRunner(graph, timeout_seconds=10)

        events = []
        with pytest.raises(GraphRunnerError):
            async for event in runner.astream_events(
                {"step": 0},
                config={"configurable": {"thread_id": "v1:stream-error-test"}},
            ):
                events.append(event)

        event_types = [e["event_type"] for e in events]
        assert "graph_started" in event_types
        assert "graph_failed" in event_types
        # graph_failed 事件有 error_code
        failed_events = [e for e in events if e["event_type"] == "graph_failed"]
        assert failed_events
        assert "error_code" in failed_events[0]

    @pytest.mark.asyncio
    async def test_ainvoke_error_hides_sensitive_text_and_exception_chain(self) -> None:
        """ainvoke 错误和异常链均不保留底层敏感文本。"""
        from langgraph.graph import END, START, StateGraph
        from typing_extensions import TypedDict

        class ErrState(TypedDict, total=False):
            step: int

        sensitive_values = (
            "demo-api-key-123",
            "Bearer demo-token-456",
            "prompt=private-prompt-789",
            "patient=demo-patient-000",
            "postgresql://demo-user:demo-password@localhost/demo-db",
        )

        graph_def = StateGraph(ErrState)

        async def _error_node(state: ErrState) -> dict:
            raise RuntimeError(" | ".join(sensitive_values))

        graph_def.add_node("error", _error_node)
        graph_def.add_edge(START, "error")
        graph_def.add_edge("error", END)
        runner = GraphRunner(graph_def.compile(checkpointer=InMemorySaver()))

        with pytest.raises(GraphRunnerError) as exc_info:
            await runner.ainvoke({"step": 0}, config={"configurable": {"thread_id": "v1:privacy"}})

        error = exc_info.value
        assert error.code == "RUNNER_EXECUTION_FAILED"
        assert str(error) == "Graph execution failed"
        assert error.__cause__ is None
        assert error.__context__ is None
        rendered_error = f"{error!s} {error!r} {error.__cause__!s} {error.__context__!s}"
        assert all(value not in rendered_error for value in sensitive_values)

    @pytest.mark.asyncio
    async def test_stream_error_hides_sensitive_text_in_event_and_exception_chain(self) -> None:
        """stream 的失败事件和异常链均不保留底层敏感文本。"""
        from langgraph.graph import END, START, StateGraph
        from typing_extensions import TypedDict

        class ErrState(TypedDict, total=False):
            step: int

        sensitive_values = (
            "demo-api-key-123",
            "Bearer demo-token-456",
            "prompt=private-prompt-789",
            "patient=demo-patient-000",
            "postgresql://demo-user:demo-password@localhost/demo-db",
        )

        graph_def = StateGraph(ErrState)

        async def _error_node(state: ErrState) -> dict:
            raise RuntimeError(" | ".join(sensitive_values))

        graph_def.add_node("error", _error_node)
        graph_def.add_edge(START, "error")
        graph_def.add_edge("error", END)
        runner = GraphRunner(graph_def.compile(checkpointer=InMemorySaver()))

        events = []
        with pytest.raises(GraphRunnerError) as exc_info:
            async for event in runner.astream_events(
                {"step": 0},
                config={"configurable": {"thread_id": "v1:privacy-stream"}},
            ):
                events.append(event)

        error = exc_info.value
        assert error.code == "RUNNER_EXECUTION_FAILED"
        assert str(error) == "Graph execution failed"
        assert error.__cause__ is None
        assert error.__context__ is None
        assert any(event["event_type"] == "graph_failed" for event in events)
        rendered_output = f"{error!s} {error!r} {error.__cause__!s} {error.__context__!s} {json.dumps(events)}"
        assert all(value not in rendered_output for value in sensitive_values)


# ---------------------------------------------------------------------------
# 7. 错配拒绝
# ---------------------------------------------------------------------------


class TestConfigMismatch:
    """config/state 错配在运行前被拒绝。"""

    @pytest.mark.asyncio
    async def test_ainvoke_session_id_mismatch_rejected(self) -> None:
        """ainvoke session_id 错配被拒绝。"""
        checkpointer = InMemorySaver()
        graph = build_main_graph(checkpointer=checkpointer)
        runner = GraphRunner(graph)

        config = make_run_config("session-a")
        state = _make_state(session_id="session-b")

        with pytest.raises(CheckpointConfigMismatchError):
            await runner.ainvoke(state, config)

    @pytest.mark.asyncio
    async def test_ainvoke_graph_version_mismatch_rejected(self) -> None:
        """ainvoke graph_version 错配被拒绝。"""
        checkpointer = InMemorySaver()
        graph = build_main_graph(checkpointer=checkpointer)
        runner = GraphRunner(graph)

        session_id = _random_session_id()
        config = make_run_config(session_id, graph_version="v1")
        state = _make_state(session_id=session_id, graph_version="v2")

        with pytest.raises(CheckpointConfigMismatchError):
            await runner.ainvoke(state, config)

    @pytest.mark.asyncio
    async def test_stream_session_id_mismatch_rejected(self) -> None:
        """astream_events session_id 错配被拒绝（不产出任何事件）。"""
        checkpointer = InMemorySaver()
        graph = build_main_graph(checkpointer=checkpointer)
        runner = GraphRunner(graph)

        config = make_run_config("session-a")
        state = _make_state(session_id="session-b")

        event_count = 0
        with pytest.raises(CheckpointConfigMismatchError):
            async for _event in runner.astream_events(state, config):
                event_count += 1

        assert event_count == 0  # 不产出任何事件

    @pytest.mark.asyncio
    async def test_mismatch_does_not_invoke_graph(self) -> None:
        """错配时不调用 graph.ainvoke（不产生 checkpoint）。"""
        checkpointer = InMemorySaver()
        graph = build_main_graph(checkpointer=checkpointer)
        runner = GraphRunner(graph)

        session_id = _random_session_id()
        config = make_run_config(session_id)
        mismatch_state = _make_state(session_id="different-session")

        with pytest.raises(CheckpointConfigMismatchError):
            await runner.ainvoke(mismatch_state, config)

        # 验证没有 checkpoint 被写入
        snapshot = await graph.aget_state(config)
        assert snapshot.values.get("route") is None
        assert snapshot.values.get("session_id") is None


# ---------------------------------------------------------------------------
# 8. 事件顺序
# ---------------------------------------------------------------------------


class TestEventOrdering:
    """事件顺序：graph_started → node_completed* → graph_completed。"""

    @pytest.mark.asyncio
    async def test_event_order_is_correct(self) -> None:
        """事件顺序正确。"""
        checkpointer = InMemorySaver()
        graph = build_main_graph(checkpointer=checkpointer)
        runner = GraphRunner(graph)

        session_id = _random_session_id()
        config = make_run_config(session_id)
        state = _make_state(command=XuanhuCommand.MESSAGE.value, session_id=session_id)

        events = []
        async for event in runner.astream_events(state, config):
            events.append(event)

        event_types = [e["event_type"] for e in events]

        # 第一个是 graph_started
        assert event_types[0] == "graph_started"

        # 最后一个是 graph_completed
        assert event_types[-1] == "graph_completed"

        # 中间都是 node_completed
        for et in event_types[1:-1]:
            assert et == "node_completed"

    @pytest.mark.asyncio
    async def test_node_completed_has_route_from_state(self) -> None:
        """node_completed 事件包含 route 字段（从 state_delta 提取）。"""
        checkpointer = InMemorySaver()
        graph = build_main_graph(checkpointer=checkpointer)
        runner = GraphRunner(graph)

        session_id = _random_session_id()
        config = make_run_config(session_id)
        state = _make_state(command=XuanhuCommand.MESSAGE.value, session_id=session_id)

        events = []
        async for event in runner.astream_events(state, config):
            events.append(event)

        node_events = [e for e in events if e["event_type"] == "node_completed"]
        # 至少有一个 node_completed 包含 route
        routes = [e.get("route") for e in node_events if "route" in e]
        assert len(routes) > 0
        assert all(isinstance(r, str) for r in routes)

    @pytest.mark.asyncio
    async def test_events_have_run_id(self) -> None:
        """事件包含 run_id 字段（从 state 提取）。"""
        checkpointer = InMemorySaver()
        graph = build_main_graph(checkpointer=checkpointer)
        runner = GraphRunner(graph)

        session_id = _random_session_id()
        config = make_run_config(session_id)
        state = _make_state(session_id=session_id)
        expected_run_id = state["run_id"]

        events = []
        async for event in runner.astream_events(state, config):
            events.append(event)

        # graph_started 和 graph_completed 应该有 run_id
        start_event = events[0]
        assert start_event.get("run_id") == expected_run_id

        end_event = events[-1]
        assert end_event.get("run_id") == expected_run_id


# ---------------------------------------------------------------------------
# 9. 事件转换工具函数测试
# ---------------------------------------------------------------------------


class TestEventConversion:
    """事件转换工具函数测试。"""

    def test_convert_updates_chunk_valid(self) -> None:
        """convert_updates_chunk 正确转换有效 chunk。"""
        chunk = {"command_router": {"route": "intake_subgraph_v1"}}
        event = convert_updates_chunk(chunk)
        assert event is not None
        assert event["event_type"] == "node_completed"
        assert event["node_name"] == "command_router"
        assert event["route"] == "intake_subgraph_v1"
        assert event["event_version"] == EVENT_SCHEMA_VERSION

    def test_convert_updates_chunk_empty(self) -> None:
        """convert_updates_chunk 对空 dict 返回 None。"""
        event = convert_updates_chunk({})
        assert event is None

    def test_convert_updates_chunk_non_dict(self) -> None:
        """convert_updates_chunk 对非 dict 返回 None。"""
        event = convert_updates_chunk("not a dict")  # type: ignore[arg-type]
        assert event is None

    def test_make_graph_started_event(self) -> None:
        """make_graph_started_event 构造正确。"""
        event = make_graph_started_event(run_id="run-001")
        assert event["event_type"] == "graph_started"
        assert event["run_id"] == "run-001"
        assert event["event_version"] == EVENT_SCHEMA_VERSION
        assert "timestamp" in event

    def test_make_graph_completed_event(self) -> None:
        """make_graph_completed_event 构造正确。"""
        event = make_graph_completed_event(run_id="run-001")
        assert event["event_type"] == "graph_completed"
        assert event["run_id"] == "run-001"

    def test_make_graph_failed_event(self) -> None:
        """make_graph_failed_event 构造正确。"""
        event = make_graph_failed_event(error_code="RUNNER_TIMEOUT", run_id="run-001")
        assert event["event_type"] == "graph_failed"
        assert event["error_code"] == "RUNNER_TIMEOUT"
        assert event["run_id"] == "run-001"

    def test_events_are_json_serializable(self) -> None:
        """所有事件构造函数产出的事件可被 json.dumps 序列化。"""
        events = [
            make_graph_started_event(run_id="r1"),
            make_graph_completed_event(run_id="r1"),
            make_graph_failed_event(error_code="ERR", run_id="r1"),
        ]
        for event in events:
            serialized = json.dumps(event, ensure_ascii=False)
            deserialized = json.loads(serialized)
            assert deserialized["event_version"] == EVENT_SCHEMA_VERSION
