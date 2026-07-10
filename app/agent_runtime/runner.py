"""GraphRunner：封装已编译 MainGraph 的 ainvoke/astream。

L1-4 范围：
- 封装 ``CompiledStateGraph.ainvoke`` 和 ``astream``，添加可配置总超时、
  外部取消正确传播和脱敏错误归一化。
- 运行前校验 config 的 thread_id 与 state 的 session_id/graph_version 一致，
  复用 L1-3 的 ``validate_checkpoint_config``，不削弱 checkpoint 写入边界。
- ``astream_events`` 产出版本化业务事件（通过 ``events`` 模块转换），
  不泄露内部 config、checkpoint、完整 state、prompt、模型输出、密钥或患者身份。

禁止事项：
- 不接入 FastAPI、SSE、WebSocket 或生产 API。
- 不实现业务 Agent、模型调用或 Legacy 改造。
- 不吞掉 ``asyncio.CancelledError``。

对齐实施计划 §6.2 工作项 7 和 §2.4：
- 总超时通过 ``asyncio.timeout`` 实现。
- ``CancelledError`` 被重新抛出，不被捕获或吞掉。
- 错误归一化为 ``GraphRunnerError``，消息脱敏。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from langgraph.graph.state import CompiledStateGraph

from app.agent_runtime.config import validate_checkpoint_config
from app.agent_runtime.errors import (
    CheckpointConfigMismatchError,
    GraphRunnerError,
    GraphRunnerTimeoutError,
)
from app.agent_runtime.events import (
    XuanhuRunEvent,
    convert_updates_chunk,
    make_graph_completed_event,
    make_graph_failed_event,
    make_graph_started_event,
)

# 默认总超时（秒）。设为 0 表示不限制。
DEFAULT_TIMEOUT_SECONDS: float = 30.0


class GraphRunner:
    """封装已编译 MainGraph 的执行入口。

    提供 ``ainvoke`` 和 ``astream_events`` 两个方法，添加：
    - 可配置总超时（``asyncio.timeout``）
    - 外部取消正确传播（不吞掉 ``CancelledError``）
    - 脱敏错误归一化（``GraphRunnerError``）
    - 运行前 config/state 一致性校验

    用法::

        runner = GraphRunner(graph, timeout_seconds=10)
        result = await runner.ainvoke(state, config=config)

        async for event in runner.astream_events(state, config=config):
            print(event)

    参数:
        graph: 已编译的 LangGraph 图（``CompiledStateGraph``）。
        timeout_seconds: 总超时秒数。设为 ``None`` 或 ``0`` 表示不限制。
    """

    def __init__(
        self,
        graph: CompiledStateGraph[Any, Any, Any, Any],
        *,
        timeout_seconds: float | None = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._graph = graph
        self._timeout_seconds = timeout_seconds

    @property
    def timeout_seconds(self) -> float | None:
        """配置的总超时秒数，``None`` 表示不限制。"""
        return self._timeout_seconds

    async def ainvoke(
        self,
        state: dict[str, Any],
        config: dict[str, Any],
    ) -> dict[str, Any]:
        """执行 ``graph.ainvoke``，添加超时、取消传播和错误归一化。

        运行前校验 config 与 state 的 session_id/graph_version 一致性。
        校验失败时抛出 ``CheckpointConfigMismatchError``（不调用 ainvoke）。
        超时抛出 ``GraphRunnerTimeoutError``。
        其他异常归一化为 ``GraphRunnerError``（消息脱敏）。
        ``asyncio.CancelledError`` 被重新抛出，不被吞掉。

        参数:
            state: Graph State。
            config: LangGraph runnable config。

        Returns:
            graph.ainvoke 的返回值（dict）。

        Raises:
            CheckpointConfigMismatchError: config 与 state 不一致。
            GraphRunnerTimeoutError: 总超时。
            GraphRunnerError: 其他执行错误（消息脱敏）。
        """
        # 运行前校验（复用 L1-3 逻辑，不削弱 checkpoint 写入边界）
        validate_checkpoint_config(config, state)

        timeout_ctx = (
            asyncio.timeout(self._timeout_seconds)
            if self._timeout_seconds and self._timeout_seconds > 0
            else _NullTimeout()
        )

        execution_error: GraphRunnerError | None = None

        try:
            async with timeout_ctx:
                result = await self._graph.ainvoke(state, config=config)  # type: ignore[call-overload]
                return dict(result) if result is not None else {}
        except TimeoutError as exc:
            raise GraphRunnerTimeoutError(
                timeout_seconds=self._timeout_seconds or 0,
            ) from exc
        except asyncio.CancelledError:
            # 不吞掉 CancelledError，直接重新抛出
            raise
        except CheckpointConfigMismatchError:
            # 校验错误直接传播，不包装
            raise
        except Exception:
            # 不读取或链式保留底层异常；异常文本可能包含密钥、prompt 或患者信息。
            execution_error = GraphRunnerError(
                "Graph execution failed",
                code="RUNNER_EXECUTION_FAILED",
            )

        if execution_error is not None:
            # 在 except 块外抛出，避免 __cause__/__context__ 持有底层异常文本。
            raise execution_error
        raise AssertionError("GraphRunner.ainvoke reached an unreachable state")

    async def astream_events(
        self,
        state: dict[str, Any],
        config: dict[str, Any],
    ) -> AsyncIterator[XuanhuRunEvent]:
        """执行 ``graph.astream`` 并产出版本化业务事件。

        事件流顺序：
        1. ``graph_started`` — 图执行开始
        2. ``node_completed`` — 每个节点完成（从 updates chunk 转换）
        3. ``graph_completed`` — 图执行完成
        或
        3. ``graph_failed`` — 图执行失败（error_code 已脱敏）

        运行前校验 config 与 state 的 session_id/graph_version 一致性。
        ``asyncio.CancelledError`` 被重新抛出，不被吞掉。

        参数:
            state: Graph State。
            config: LangGraph runnable config。

        Yields:
            ``XuanhuRunEvent`` 事件。

        Raises:
            CheckpointConfigMismatchError: config 与 state 不一致（在产出任何事件前）。
            GraphRunnerTimeoutError: 总超时。
            GraphRunnerError: 其他执行错误（消息脱敏）。
        """
        # 运行前校验
        validate_checkpoint_config(config, state)

        run_id = state.get("run_id", "")
        if isinstance(run_id, str) and run_id:
            pass
        else:
            run_id = ""

        timeout_ctx = (
            asyncio.timeout(self._timeout_seconds)
            if self._timeout_seconds and self._timeout_seconds > 0
            else _NullTimeout()
        )

        execution_error: GraphRunnerError | None = None

        try:
            async with timeout_ctx:
                yield make_graph_started_event(run_id=run_id)

                async for chunk in self._graph.astream(
                    state,
                    config=config,
                    stream_mode="updates",
                ):  # type: ignore[call-overload]
                    event = convert_updates_chunk(chunk)
                    if event is not None:
                        yield event

                yield make_graph_completed_event(run_id=run_id)
        except TimeoutError as exc:
            yield make_graph_failed_event(
                error_code="RUNNER_TIMEOUT",
                run_id=run_id,
            )
            raise GraphRunnerTimeoutError(
                timeout_seconds=self._timeout_seconds or 0,
            ) from exc
        except asyncio.CancelledError:
            # 不吞掉 CancelledError，直接重新抛出
            raise
        except CheckpointConfigMismatchError:
            # 校验错误直接传播
            raise
        except Exception:
            # 不读取或链式保留底层异常；事件和对外错误只暴露固定错误码。
            execution_error = GraphRunnerError(
                "Graph execution failed",
                code="RUNNER_EXECUTION_FAILED",
            )

        if execution_error is not None:
            yield make_graph_failed_event(
                error_code="RUNNER_EXECUTION_FAILED",
                run_id=run_id,
            )
            # 在 except 块外抛出，避免 __cause__/__context__ 持有底层异常文本。
            raise execution_error


class _NullTimeout:
    """无超时上下文管理器（当 timeout_seconds 为 0 或 None 时使用）。

    实现与 ``asyncio.timeout`` 相同的 ``async with`` 接口但不限制时间。
    """

    async def __aenter__(self) -> None:
        pass

    async def __aexit__(self, *_: object) -> None:
        pass
