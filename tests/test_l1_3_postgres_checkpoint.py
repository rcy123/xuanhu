"""L1-3 专项测试：AsyncPostgresSaver 与跨进程恢复。

测试范围：
1. AsyncPostgresSaver setup 和健康检查成功且 setup 可重复执行。
2. MainGraph 状态可写入并从 PG checkpoint 读取。
3. 新建 graph/checkpointer 实例后仍能读取相同状态。
4. 两个独立 OS 进程可完成暂停、退出、重建和恢复。
5. 不同 session/thread 互相隔离。
6. 相同 session 的 v1/v2 checkpoint 互相隔离。
7. config/state session_id 或 graph_version 错配被确定性拒绝，且错误脱敏。
   - 纯函数校验（不需 PG）。
   - 真实 graph.ainvoke + PG 回归：错配必须失败且不产生新 checkpoint。
8. 连接池和后台资源在正常及异常路径均可靠关闭。
   - 测试证明活动资源实际关闭（操作失败），不能只断言"不抛异常"。
9. 不调用真实模型，不改变 Legacy 行为。
10. 子进程不通过 argv 接收 DB_URL；输出不含敏感信息。

所有 PG 测试标记 ``pytest.mark.integration``（需要真实 PostgreSQL）。

Windows 事件循环：
- psycopg v3 异步连接在 Windows 上要求 ``SelectorEventLoop``。
- 通过 ``event_loop_policy`` fixture 在模块级别切换策略，
  不在生产代码中隐式修改全局事件循环策略。

无真实模型、Redis 或患者数据依赖。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import StateSnapshot

from app.agent_runtime.checkpoint import (
    check_postgres_health,
    extract_thread_id,
    postgres_checkpointer,
)
from app.agent_runtime.commands import NODE_REASONING_SUBGRAPH_V1, XuanhuCommand
from app.agent_runtime.config import (
    GRAPH_VERSION_V1,
    make_run_config,
    make_thread_id,
    parse_thread_id,
    validate_checkpoint_config,
)
from app.agent_runtime.errors import (
    CheckpointConfigMismatchError,
    CheckpointError,
    CheckpointHealthCheckError,
)
from app.agent_runtime.graph import build_main_graph
from app.agent_runtime.state import XuanhuGraphState, default_state
from tests._database_safety import require_destructive_test_database

# ---------------------------------------------------------------------------
# Windows 事件循环：psycopg v3 异步连接要求 SelectorEventLoop
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pg_url() -> str:
    """从显式、受保护的测试数据库配置获取 PostgreSQL URL。"""
    return require_destructive_test_database()


def _random_session_id() -> str:
    """生成随机 session_id，避免跨测试 checkpoint 污染。"""
    return f"session-{uuid.uuid4().hex[:12]}"


def _make_initial_state(
    *,
    command: str = "",
    session_id: str | None = None,
    graph_version: str = GRAPH_VERSION_V1,
) -> XuanhuGraphState:
    """构造测试用初始 Graph State。"""
    return default_state(
        session_id=session_id or _random_session_id(),
        command=command,
        command_id=f"cmd-{uuid.uuid4().hex[:8]}" if command else "",
        graph_version=graph_version,
        run_id=f"run-{uuid.uuid4().hex[:8]}" if command else "",
    )


async def _reasoning_executor(_: XuanhuGraphState) -> dict[str, object]:
    return {"route": NODE_REASONING_SUBGRAPH_V1}


# ---------------------------------------------------------------------------
# 1. Setup 和健康检查
# ---------------------------------------------------------------------------


class TestSetupAndHealthCheck:
    """AsyncPostgresSaver setup 和健康检查。"""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_setup_succeeds_via_context_manager(self) -> None:
        """postgres_checkpointer 上下文管理器成功创建并 setup。"""
        async with postgres_checkpointer(_pg_url()) as saver:
            assert saver is not None

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_setup_is_idempotent(self) -> None:
        """setup() 多次调用不报错。"""
        async with postgres_checkpointer(_pg_url()) as saver:
            inner = saver._delegate
            await inner.setup()
            await inner.setup()
            await inner.setup()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_health_check_passes_for_healthy_saver(self) -> None:
        """健康检查对已初始化的 saver 返回成功。"""
        async with postgres_checkpointer(_pg_url()) as saver:
            await check_postgres_health(saver)

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_health_check_raises_for_closed_saver(self) -> None:
        """健康检查对已关闭的 saver 抛出 CheckpointHealthCheckError（脱敏）。"""
        async with AsyncPostgresSaver.from_conn_string(_pg_url()) as saver:
            await saver.setup()
        # 上下文管理器退出后 saver 已关闭

        with pytest.raises(CheckpointHealthCheckError) as exc_info:
            await check_postgres_health(saver)

        # 错误消息不得包含 DB URL 或密码
        error_msg = str(exc_info.value)
        assert "postgresql://" not in error_msg
        assert "xuanhu_dev" not in error_msg


# ---------------------------------------------------------------------------
# 2. MainGraph 写入并从 PG checkpoint 读取
# ---------------------------------------------------------------------------


class TestWriteAndRead:
    """MainGraph 状态可写入并从 PG checkpoint 读取。"""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_main_graph_writes_and_reads_checkpoint(self) -> None:
        """MainGraph 使用 AsyncPostgresSaver 写入并可读取 checkpoint。"""
        session_id = _random_session_id()
        config = make_run_config(session_id)
        state = _make_initial_state(
            command=XuanhuCommand.MESSAGE.value,
            session_id=session_id,
        )

        async with postgres_checkpointer(_pg_url()) as saver:
            graph = build_main_graph(checkpointer=saver)
            result = await graph.ainvoke(state, config=config)
            assert result["route"] == "intake_subgraph_v1"

            snapshot: StateSnapshot = await graph.aget_state(config)
            assert snapshot is not None
            assert snapshot.values.get("route") == "intake_subgraph_v1"
            assert snapshot.values.get("command") == XuanhuCommand.MESSAGE.value
            assert snapshot.values.get("session_id") == session_id

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_main_graph_preserves_domain_state_version(self) -> None:
        """checkpoint 保留 domain_state_version 引用。"""
        session_id = _random_session_id()
        config = make_run_config(session_id)
        state = _make_initial_state(
            command=XuanhuCommand.ADVANCE.value,
            session_id=session_id,
        )
        state["domain_state_version"] = 99

        async with postgres_checkpointer(_pg_url()) as saver:
            graph = build_main_graph(checkpointer=saver, reasoning_executor=_reasoning_executor)
            await graph.ainvoke(state, config=config)
            snapshot = await graph.aget_state(config)
            assert snapshot.values.get("domain_state_version") == 99


# ---------------------------------------------------------------------------
# 3. 新建 graph/checkpointer 实例后读取相同状态
# ---------------------------------------------------------------------------


class TestRecreateAndRead:
    """新建 graph/checkpointer 实例后仍能读取相同状态。"""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_recreate_saver_and_read_same_state(self) -> None:
        """重新创建 saver 和 graph 后可读取之前写入的 checkpoint。"""
        session_id = _random_session_id()
        config = make_run_config(session_id)
        state = _make_initial_state(
            command=XuanhuCommand.REVIEW.value,
            session_id=session_id,
        )

        # 写入
        async with postgres_checkpointer(_pg_url()) as saver:
            graph = build_main_graph(checkpointer=saver)
            await graph.ainvoke(state, config=config)

        # 重新创建读取
        async with postgres_checkpointer(_pg_url()) as saver:
            graph = build_main_graph(checkpointer=saver)
            snapshot = await graph.aget_state(config)
            assert snapshot is not None
            assert snapshot.values.get("route") == "review_placeholder"
            assert snapshot.values.get("session_id") == session_id
            assert snapshot.values.get("command") == XuanhuCommand.REVIEW.value

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_recreate_saver_preserves_run_id(self) -> None:
        """重新创建 saver 后保留 run_id。"""
        session_id = _random_session_id()
        config = make_run_config(session_id)
        state = _make_initial_state(
            command=XuanhuCommand.RECOVER.value,
            session_id=session_id,
        )

        async with postgres_checkpointer(_pg_url()) as saver:
            graph = build_main_graph(checkpointer=saver)
            await graph.ainvoke(state, config=config)

        async with postgres_checkpointer(_pg_url()) as saver:
            graph = build_main_graph(checkpointer=saver)
            snapshot = await graph.aget_state(config)
            assert snapshot.values.get("run_id") == state["run_id"]


# ---------------------------------------------------------------------------
# 4. 跨进程恢复
# ---------------------------------------------------------------------------


class TestCrossProcessRecovery:
    """两个独立 OS 进程可完成暂停、退出、重建和恢复。"""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_cross_process_write_and_read(self) -> None:
        """进程 A 写入 checkpoint 后退出，进程 B 重建并读取。

        进程 A/B 行为说明：
        - 进程 A：启动 → 从 DB_URL 环境变量获取连接串 → 创建 AsyncPostgresSaver
          → setup → invoke 最小图（counter=42）→ checkpoint 持久化到 PG → 进程退出
        - 进程 B：启动（新 OS 进程）→ 从 DB_URL 环境变量获取连接串 → 创建新
          AsyncPostgresSaver → setup → aget_state 读取同一 thread_id → 验证数据完整
        """
        session_id = _random_session_id()
        thread_id = make_thread_id(session_id)
        helper = os.path.join(os.path.dirname(__file__), "_l1_3_subprocess.py")
        python_exe = sys.executable

        env = {**os.environ}

        # 进程 A：写入
        result_a = subprocess.run(
            [python_exe, helper, "write", thread_id, XuanhuCommand.MESSAGE.value],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert result_a.returncode == 0, f"Process A failed: {result_a.stderr}"
        output_a = json.loads(result_a.stdout)
        assert output_a["status"] == "ok", f"Process A error: {output_a}"
        assert output_a["data"]["counter"] == 43

        # 进程 B：读取
        result_b = subprocess.run(
            [python_exe, helper, "read", thread_id],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert result_b.returncode == 0, f"Process B failed: {result_b.stderr}"
        output_b = json.loads(result_b.stdout)
        assert output_b["status"] == "ok", f"Process B error: {output_b}"
        assert output_b["data"]["counter"] == 43
        assert output_b["data"]["last_node"] == "finalize"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_cross_process_interrupt_and_resume(self) -> None:
        """进程 A 运行到 interrupt 后退出，进程 B 恢复并完成。

        进程 A/B 行为说明：
        - 进程 A：启动 → invoke interrupt graph → 图暂停在 step_b → checkpoint
          持久化 → 进程退出
        - 进程 B：启动 → Command(resume=10) → 图从 step_b 恢复并执行到 step_c
          → 验证完成
        """
        session_id = _random_session_id()
        thread_id = make_thread_id(session_id)
        helper = os.path.join(os.path.dirname(__file__), "_l1_3_subprocess.py")
        python_exe = sys.executable

        env = {**os.environ}

        # 进程 A：运行到 interrupt
        result_a = subprocess.run(
            [python_exe, helper, "interrupt", thread_id],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert result_a.returncode == 0, f"Process A failed: {result_a.stderr}"
        output_a = json.loads(result_a.stdout)
        assert output_a["status"] == "ok", f"Process A error: {output_a}"
        assert "step_b" in output_a["data"]["next"], f"Expected paused at step_b: {output_a}"
        assert output_a["data"]["counter"] == 1

        # 进程 B：恢复并完成
        result_b = subprocess.run(
            [python_exe, helper, "resume", thread_id, "10"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert result_b.returncode == 0, f"Process B failed: {result_b.stderr}"
        output_b = json.loads(result_b.stdout)
        assert output_b["status"] == "ok", f"Process B error: {output_b}"
        assert output_b["data"]["counter"] == 11
        assert output_b["data"]["last_node"] == "step_c"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_subprocess_connection_failure_has_no_sensitive_data(self) -> None:
        """真实连接失败时，子进程 args/stdout/stderr 均不泄露连接详情。"""
        thread_id = make_thread_id(_random_session_id())
        helper = os.path.join(os.path.dirname(__file__), "_l1_3_subprocess.py")
        python_exe = sys.executable

        # 使用含凭据且格式非法的 URI，驱动在真实 from_conn_string 路径中快速失败，
        # 避免网络超时让 CI 等待连接池重试。
        db_url = "postgresql://secret_user:secret_pass@%ZZ"
        env = {**os.environ, "DB_URL": db_url}

        # 覆盖真实驱动连接串失败路径，验证异常文本不会跨子进程边界泄露。
        result = subprocess.run(
            [python_exe, helper, "read", thread_id],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert result.returncode == 1

        for arg in result.args:
            assert db_url not in arg, f"DB URL found in subprocess args: {arg}"
        combined_output = "\n".join([*result.args, result.stdout, result.stderr]).lower()
        for sensitive_value in (
            "postgresql://",
            "secret_user",
            "secret_pass",
            "%zz",
            "connection failed",
        ):
            assert sensitive_value not in combined_output

        output = json.loads(result.stdout)
        assert output["status"] == "error"
        assert output["data"].get("code") == "CHECKPOINT_SUBPROCESS_FAILED"
        assert set(output["data"]) == {"error_type", "code"}


# ---------------------------------------------------------------------------
# 5. Thread 隔离
# ---------------------------------------------------------------------------


class TestThreadIsolation:
    """不同 session/thread 互相隔离。"""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_different_sessions_are_isolated(self) -> None:
        """不同 session_id 的 checkpoint 互相隔离。"""
        session_a = _random_session_id()
        session_b = _random_session_id()
        config_a = make_run_config(session_a)
        config_b = make_run_config(session_b)

        state_a = _make_initial_state(command=XuanhuCommand.MESSAGE.value, session_id=session_a)
        state_b = _make_initial_state(command=XuanhuCommand.ADVANCE.value, session_id=session_b)

        async with postgres_checkpointer(_pg_url()) as saver:
            graph = build_main_graph(checkpointer=saver, reasoning_executor=_reasoning_executor)
            await graph.ainvoke(state_a, config=config_a)
            await graph.ainvoke(state_b, config=config_b)

            snap_a = await graph.aget_state(config_a)
            snap_b = await graph.aget_state(config_b)

            assert snap_a.values.get("route") == "intake_subgraph_v1"
            assert snap_b.values.get("route") == NODE_REASONING_SUBGRAPH_V1
            assert snap_a.values.get("session_id") == session_a
            assert snap_b.values.get("session_id") == session_b


# ---------------------------------------------------------------------------
# 6. Graph version 隔离
# ---------------------------------------------------------------------------


class TestGraphVersionIsolation:
    """相同 session 的 v1/v2 checkpoint 互相隔离。"""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_v1_v2_checkpoints_are_isolated(self) -> None:
        """相同 session_id 但不同 graph_version 的 checkpoint 互相隔离。"""
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

        async with postgres_checkpointer(_pg_url()) as saver:
            graph = build_main_graph(checkpointer=saver, reasoning_executor=_reasoning_executor)
            await graph.ainvoke(state_v1, config=config_v1)
            await graph.ainvoke(state_v2, config=config_v2)

            snap_v1 = await graph.aget_state(config_v1)
            snap_v2 = await graph.aget_state(config_v2)

            assert snap_v1.values.get("route") == "intake_subgraph_v1"
            assert snap_v2.values.get("route") == NODE_REASONING_SUBGRAPH_V1
            assert snap_v1.values.get("graph_version") == "v1"
            assert snap_v2.values.get("graph_version") == "v2"


# ---------------------------------------------------------------------------
# 7. Config/State 错配拒绝
# ---------------------------------------------------------------------------


class TestConfigStateMismatch:
    """config/state session_id 或 graph_version 错配被确定性拒绝。

    分为两层测试：
    1. 纯函数校验（不需 PG）：validate_checkpoint_config 直接调用。
    2. 真实 graph.ainvoke + PG 回归：公开图入口在真实 PG 环境中
       拒绝错配，且不产生新 checkpoint。
    """

    # ---- 纯函数校验 ----

    def test_session_id_mismatch_raises(self) -> None:
        """config 的 session_id 与 state 的 session_id 不一致时拒绝。"""
        config = make_run_config("session-a")
        state = default_state(session_id="session-b", command="message", graph_version="v1")

        with pytest.raises(CheckpointConfigMismatchError) as exc_info:
            validate_checkpoint_config(config, state)

        assert exc_info.value.field == "session_id"

    def test_graph_version_mismatch_raises(self) -> None:
        """config 的 graph_version 与 state 的 graph_version 不一致时拒绝。"""
        config = make_run_config("session-001", graph_version="v1")
        state = default_state(session_id="session-001", command="message", graph_version="v2")

        with pytest.raises(CheckpointConfigMismatchError) as exc_info:
            validate_checkpoint_config(config, state)

        assert exc_info.value.field == "graph_version"

    def test_matching_config_state_passes(self) -> None:
        """config 和 state 一致时通过。"""
        session_id = "session-001"
        config = make_run_config(session_id, graph_version="v1")
        state = default_state(session_id=session_id, command="message", graph_version="v1")

        validate_checkpoint_config(config, state)

    def test_missing_thread_id_raises(self) -> None:
        """config 缺少 thread_id 时拒绝。"""
        config: dict = {"configurable": {}}
        state = default_state(session_id="session-001", command="message", graph_version="v1")

        with pytest.raises(CheckpointConfigMismatchError):
            validate_checkpoint_config(config, state)

    def test_invalid_thread_id_format_raises(self) -> None:
        """thread_id 格式无效时拒绝。"""
        config: dict = {"configurable": {"thread_id": "no-colon"}}
        state = default_state(session_id="session-001", command="message", graph_version="v1")

        with pytest.raises(CheckpointConfigMismatchError):
            validate_checkpoint_config(config, state)

    def test_empty_state_fields_passes(self) -> None:
        """state 字段为空时跳过校验。"""
        config = make_run_config("session-001")
        state = default_state(session_id="", command="message", graph_version="")

        validate_checkpoint_config(config, state)

    def test_error_message_does_not_leak_db_url(self) -> None:
        """错配错误消息不含 DB URL 或密码。"""
        config = make_run_config("session-a")
        state = default_state(session_id="session-b", command="message", graph_version="v1")

        with pytest.raises(CheckpointConfigMismatchError) as exc_info:
            validate_checkpoint_config(config, state)

        error_msg = str(exc_info.value)
        assert "postgresql://" not in error_msg
        assert "xuanhu_dev" not in error_msg
        assert "password" not in error_msg.lower()

    # ---- 真实 graph.ainvoke + PG 回归 ----

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_session_id_mismatch_blocks_ainvoke_and_writes_no_checkpoint(self) -> None:
        """session_id 错配时公开图入口拒绝执行且不产生 checkpoint。

        对齐 AR-B-005 要求 2：错配必须失败；失败后目标 thread 不得产生新 checkpoint。
        """
        session_id_config = _random_session_id()
        session_id_state = _random_session_id()
        config = make_run_config(session_id_config)  # config 指向 session_id_config
        state = _make_initial_state(
            command=XuanhuCommand.MESSAGE.value,
            session_id=session_id_state,  # state 指向不同 session_id
        )

        async with postgres_checkpointer(_pg_url()) as saver:
            graph = build_main_graph(checkpointer=saver)

            # ConfigValidatingCheckpointer 必须在首次写入前拒绝
            with pytest.raises(CheckpointConfigMismatchError):
                await graph.ainvoke(state, config=config)

            # 验证目标 thread 没有产生 checkpoint
            snapshot = await graph.aget_state(config)
            # 新 thread 的 snapshot 应为空或无值
            assert snapshot.values.get("route") is None
            assert snapshot.values.get("session_id") is None

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_graph_version_mismatch_blocks_ainvoke_and_writes_no_checkpoint(self) -> None:
        """graph_version 错配时公开图入口拒绝且不产生 checkpoint。"""
        session_id = _random_session_id()
        config = make_run_config(session_id, graph_version="v1")
        state = _make_initial_state(
            command=XuanhuCommand.MESSAGE.value,
            session_id=session_id,
            graph_version="v2",  # 与 config 的 v1 不一致
        )

        async with postgres_checkpointer(_pg_url()) as saver:
            graph = build_main_graph(checkpointer=saver)

            with pytest.raises(CheckpointConfigMismatchError):
                await graph.ainvoke(state, config=config)

            # 验证目标 thread 没有产生 checkpoint
            snapshot = await graph.aget_state(config)
            assert snapshot.values.get("route") is None
            assert snapshot.values.get("session_id") is None

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_valid_invoke_then_mismatch_does_not_corrupt_existing(self) -> None:
        """成功写入后，错配请求不破坏已有 checkpoint。"""
        session_id = _random_session_id()
        config = make_run_config(session_id, graph_version="v1")
        state = _make_initial_state(
            command=XuanhuCommand.MESSAGE.value,
            session_id=session_id,
            graph_version="v1",
        )

        async with postgres_checkpointer(_pg_url()) as saver:
            graph = build_main_graph(checkpointer=saver)

            # 第一次：正常写入
            await graph.ainvoke(state, config=config)
            snap1 = await graph.aget_state(config)
            assert snap1.values.get("route") == "intake_subgraph_v1"

            # 第二次：错配请求被拒绝
            mismatch_state = _make_initial_state(
                command=XuanhuCommand.ADVANCE.value,
                session_id=_random_session_id(),  # 不同 session_id
                graph_version="v1",
            )
            with pytest.raises(CheckpointConfigMismatchError):
                await graph.ainvoke(mismatch_state, config=config)

            # 原有 checkpoint 未被破坏
            snap2 = await graph.aget_state(config)
            assert snap2.values.get("route") == "intake_subgraph_v1"
            assert snap2.values.get("session_id") == session_id


# ---------------------------------------------------------------------------
# 8. 资源关闭
# ---------------------------------------------------------------------------


class TestResourceCleanup:
    """连接池和后台资源在正常及异常路径均可靠关闭。

    测试必须证明活动资源实际关闭（操作失败），不能只断言"不抛异常"。
    """

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_normal_path_closes_connection_pool(self) -> None:
        """正常退出后连接池已关闭：setup 再次调用必然失败。"""
        async with postgres_checkpointer(_pg_url()) as saver:
            inner = saver._delegate
            await inner.setup()
            closed_inner = inner

        # 连接池关闭后，setup 必然抛异常（不只是"不抛异常"）
        with pytest.raises(Exception) as exc_info:
            await closed_inner.setup()
        # 确认是连接/池相关的错误，不是其他意外异常
        error_msg = str(exc_info.value).lower()
        assert any(kw in error_msg for kw in ("closed", "pool", "connection", "operational", "interface"))

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_exception_path_closes_connection_pool(self) -> None:
        """异常退出后连接池也已关闭。"""
        closed_inner: AsyncPostgresSaver | None = None
        with pytest.raises(RuntimeError, match="simulated error"):
            async with postgres_checkpointer(_pg_url()) as saver:
                closed_inner = saver._delegate
                raise RuntimeError("simulated error")

        assert closed_inner is not None
        # 连接池关闭后，setup 必然抛异常
        with pytest.raises(Exception) as exc_info:
            await closed_inner.setup()
        error_msg = str(exc_info.value).lower()
        assert any(kw in error_msg for kw in ("closed", "pool", "connection", "operational", "interface"))

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_invalid_db_url_raises_checkpoint_error_sanitized(self) -> None:
        """无效 DB URL 抛出 CheckpointError（消息已脱敏）。"""
        bad_url = "postgresql://bad_user:bad_pass@nonexistent_host:5432/bad_db"

        with pytest.raises(CheckpointError) as exc_info:
            async with postgres_checkpointer(bad_url):
                pass

        # 错误消息不含原始 URL 中的用户名、密码或主机名
        error_msg = str(exc_info.value)
        assert "bad_user" not in error_msg
        assert "bad_pass" not in error_msg
        assert "nonexistent_host" not in error_msg
        assert "bad_db" not in error_msg


# ---------------------------------------------------------------------------
# 9. 工具函数测试
# ---------------------------------------------------------------------------


class TestUtilityFunctions:
    """checkpoint 工具函数测试。"""

    def test_extract_thread_id_valid(self) -> None:
        """extract_thread_id 从 config 中提取 thread_id。"""
        config = make_run_config("session-001", graph_version="v1")
        thread_id = extract_thread_id(config)
        assert thread_id == "v1:session-001"

    def test_extract_thread_id_missing_raises(self) -> None:
        """extract_thread_id 缺少 thread_id 时抛出 ValueError。"""
        config: dict = {"configurable": {}}
        with pytest.raises(ValueError):
            extract_thread_id(config)

    def test_parse_thread_id_valid(self) -> None:
        """parse_thread_id 正确解析版本化 thread_id。"""
        graph_version, session_id = parse_thread_id("v1:session-abc")
        assert graph_version == "v1"
        assert session_id == "session-abc"

    def test_parse_thread_id_with_colon_in_session_id(self) -> None:
        """parse_thread_id 正确处理 session_id 中含 ``:`` 的情况。"""
        graph_version, session_id = parse_thread_id("v1:session:with:colons")
        assert graph_version == "v1"
        assert session_id == "session:with:colons"

    def test_parse_thread_id_invalid_raises(self) -> None:
        """parse_thread_id 格式无效时抛出 ValueError。"""
        with pytest.raises(ValueError):
            parse_thread_id("no-colon")


# ---------------------------------------------------------------------------
# 10. InMemorySaver 兼容性（不破坏 L1-2 单测）
# ---------------------------------------------------------------------------


class TestInMemorySaverCompatibility:
    """MainGraph 仍支持 InMemorySaver（不破坏 L1-2 单测）。"""

    @pytest.mark.asyncio
    async def test_main_graph_works_with_inmemory_saver(self) -> None:
        """MainGraph 使用 InMemorySaver 仍可正常路由。"""
        checkpointer = InMemorySaver()
        graph = build_main_graph(checkpointer=checkpointer)
        session_id = _random_session_id()
        config = make_run_config(session_id)
        state = _make_initial_state(command=XuanhuCommand.MESSAGE.value, session_id=session_id)

        result = await graph.ainvoke(state, config=config)
        assert result["route"] == "intake_subgraph_v1"

        snapshot = await graph.aget_state(config)
        assert snapshot.values.get("route") == "intake_subgraph_v1"

    @pytest.mark.asyncio
    async def test_main_graph_works_without_checkpointer(self) -> None:
        """MainGraph 不传 checkpointer 仍可正常路由。"""
        graph = build_main_graph(reasoning_executor=_reasoning_executor)
        session_id = _random_session_id()
        state = _make_initial_state(command=XuanhuCommand.ADVANCE.value, session_id=session_id)

        # 无 checkpointer 时 config 不是必需的
        result = await graph.ainvoke(state)
        assert result["route"] == NODE_REASONING_SUBGRAPH_V1
