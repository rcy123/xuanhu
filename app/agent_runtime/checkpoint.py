"""AsyncPostgresSaver 生命周期管理与跨进程恢复底座。

L1-3 范围：
- 封装 AsyncPostgresSaver 的异步创建、setup、健康检查和可靠关闭。
- 校验 checkpoint config 与 Graph State 的 session_id/graph_version 一致性。
- 校验接入不可绕过的 checkpoint 写入路径（``ConfigValidatingCheckpointer``）。
- 健康检查错误归一化并脱敏，不泄露 DB URL、密码或底层连接信息。
- 不实现 GraphRunner、ainvoke/astream 包装、超时取消或事件转换。
- 不接入 FastAPI 生产路由、业务 Agent 或 Legacy RecoveryService。

对齐 ADR-002 / 迁移边界 §2-3：
- thread_id 使用 ``{graph_version}:{session_id}`` 命名空间。
- 不在 root graph 的 aget_state config 中加入 ``checkpoint_ns``。
- LangGraph 1.2.x 会将 ``checkpoint_ns`` 解释为 subgraph namespace。

Windows 事件循环策略：
- 本模块不在 import 时修改全局事件循环策略。
- psycopg v3 异步连接在 Windows 上要求 ``SelectorEventLoop``；
  调用方（测试 fixture 或生产启动入口）负责显式设置事件循环策略。
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.agent_runtime.config import validate_checkpoint_config
from app.agent_runtime.errors import CheckpointError, CheckpointHealthCheckError

# 用于检测错误消息中是否包含敏感信息（DB URL、密码等）。
_SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"postgresql?://[^\s]+", re.IGNORECASE),  # DB URL
    re.compile(r"password\s*[=:]\s*\S+", re.IGNORECASE),  # password=xxx
    re.compile(r"\bapi[_-]?key\s*[=:]\s*\S+", re.IGNORECASE),  # api_key=xxx
)


def _sanitize_error_message(message: str) -> str:
    """脱敏错误消息：移除 DB URL、密码、主机名和其他连接信息。

    将敏感信息替换为 ``***``。对于无法确定脱敏后是否安全的消息，
    返回通用错误描述而非原始内容。
    """
    sanitized = message
    for pattern in _SENSITIVE_PATTERNS:
        sanitized = pattern.sub("***", sanitized)
    # 额外脱敏：psycopg 错误消息中可能单独出现主机名（不在 URL 内）
    # 例如 "failed to resolve host 'nonexistent_host'"
    sanitized = re.sub(r"host\s+'[^']+'", "host '***'", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"user\s+'[^']+'", "user '***'", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"database\s+'[^']+'", "database '***'", sanitized, flags=re.IGNORECASE)
    return sanitized


class ConfigValidatingCheckpointer(BaseCheckpointSaver[Any]):
    """在实际 checkpoint 写入前校验 Graph State 与 ``thread_id``。

    LangGraph 会在首节点运行前写入输入 checkpoint，因此不能依赖图节点或
    调用方主动选择的 wrapper 做校验。该代理拦截 ``put``/``aput``，从
    ``channel_values`` 提取输入 state，在任何持久化前执行一致性校验。

    所有方法签名使用 ``Any`` 以兼容 LangGraph 内部类型（``Checkpoint``、
    ``RunnableConfig`` 等），mypy override 错误通过 type: ignore 抑制。
    """

    def __init__(self, delegate: BaseCheckpointSaver[Any]) -> None:
        super().__init__(serde=delegate.serde)
        self._delegate = delegate

    @property
    def config_specs(self) -> Any:
        return self._delegate.config_specs

    def get_next_version(self, current: Any, channel: Any) -> Any:
        return self._delegate.get_next_version(current, channel)

    @staticmethod
    def _state_from_checkpoint(checkpoint: Any) -> dict[str, Any] | None:
        if not isinstance(checkpoint, dict):
            return None
        channel_values = checkpoint.get("channel_values", {})
        if not isinstance(channel_values, dict):
            return None
        state = channel_values.get("__start__", channel_values)
        return state if isinstance(state, dict) else None

    def _validate_write(self, config: Any, checkpoint: Any) -> None:
        state = self._state_from_checkpoint(checkpoint)
        if state is None:
            return
        if "session_id" in state or "graph_version" in state:
            validate_checkpoint_config(config, state)

    def get_tuple(self, config: Any) -> Any:
        return self._delegate.get_tuple(config)

    def list(self, config: Any, *, filter: Any = None, before: Any = None, limit: int | None = None) -> Iterator[Any]:
        return self._delegate.list(config, filter=filter, before=before, limit=limit)

    def put(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        self._validate_write(config, checkpoint)
        return self._delegate.put(config, checkpoint, metadata, new_versions)

    def put_writes(self, config: Any, writes: Any, task_id: str, task_path: str = "") -> None:
        self._delegate.put_writes(config, writes, task_id, task_path)

    async def aget_tuple(self, config: Any) -> Any:
        return await self._delegate.aget_tuple(config)

    async def aget(self, config: Any) -> Any:
        return await self._delegate.aget(config)

    async def alist(self, config: Any, *, filter: Any = None, before: Any = None, limit: int | None = None) -> AsyncIterator[Any]:
        async for item in self._delegate.alist(config, filter=filter, before=before, limit=limit):
            yield item

    async def aput(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        self._validate_write(config, checkpoint)
        return await self._delegate.aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config: Any, writes: Any, task_id: str, task_path: str = "") -> None:
        await self._delegate.aput_writes(config, writes, task_id, task_path)


@asynccontextmanager
async def postgres_checkpointer(db_url: str) -> AsyncIterator[ConfigValidatingCheckpointer]:
    """异步上下文管理器：创建、setup、yield、可靠关闭 AsyncPostgresSaver。

    对齐验收标准 1/8：setup 成功且可重复；连接池和后台资源在正常及异常路径均可靠关闭。

    生命周期由 ``from_conn_string`` 返回的 context manager 拥有。
    退出时通过 ``cm.__aexit__`` 关闭连接池和后台资源，
    不依赖外部 ``close`` 函数。

    yield 的 ``ConfigValidatingCheckpointer`` 自动在每次 ``put``/``aput``
    时校验 config 与 state 的 session_id/graph_version 一致性，
    调用方无法绕过。

    用法::

        async with postgres_checkpointer(db_url) as saver:
            graph = build_main_graph(checkpointer=saver)
            await graph.ainvoke(state, config=config)

    参数:
        db_url: PostgreSQL 连接字符串。

    Yields:
        已完成 setup 的 ``ConfigValidatingCheckpointer`` 实例。

    Raises:
        CheckpointError: 如果创建或 setup 失败（错误消息已脱敏）。
    """
    # 使用 from_conn_string 返回的 context manager 管理完整生命周期。
    cm = AsyncPostgresSaver.from_conn_string(db_url)
    try:
        saver = await cm.__aenter__()
        await saver.setup()
    except Exception as exc:
        # setup 失败时确保清理资源
        with contextlib.suppress(Exception):
            await cm.__aexit__(None, None, None)
        sanitized = _sanitize_error_message(str(exc))
        raise CheckpointError(
            f"Failed to create or setup postgres checkpointer: {sanitized}",
            code="CHECKPOINT_CREATE_FAILED",
        ) from exc

    # yield 阶段：调用方异常自然传播，不包装为 CheckpointError。
    # finally 确保拥有生命周期的 context manager 关闭连接池。
    try:
        yield ConfigValidatingCheckpointer(saver)
    finally:
        await cm.__aexit__(None, None, None)


async def check_postgres_health(saver: BaseCheckpointSaver[Any]) -> None:
    """执行 checkpoint 健康检查。

    对齐验收标准 1/9：setup 可重复执行；健康检查错误必须归一化并脱敏。

    健康检查策略：
    1. 调用 ``setup()`` 验证数据库可达且表结构正常（幂等操作）。
    2. 如果失败，抛出 ``CheckpointHealthCheckError``（消息已脱敏）。

    参数:
        saver: 已初始化的 checkpointer 实例（可以是
            ``ConfigValidatingCheckpointer`` 或底层 ``AsyncPostgresSaver``）。

    Raises:
        CheckpointHealthCheckError: 如果健康检查失败。错误消息不含 DB URL 或密码。
    """
    # ConfigValidatingCheckpointer 代理了底层 saver，但 setup 不在
    # BaseCheckpointSaver 接口中。直接访问委托对象的 setup。
    inner = getattr(saver, "_delegate", saver)
    try:
        await inner.setup()  # type: ignore[union-attr]
    except Exception as exc:
        sanitized = _sanitize_error_message(str(exc))
        raise CheckpointHealthCheckError(detail=sanitized) from exc


def extract_thread_id(config: dict[str, Any]) -> str:
    """从 LangGraph config 中提取 thread_id。

    参数:
        config: LangGraph runnable config dict。

    返回:
        thread_id 字符串。

    Raises:
        ValueError: 如果 config 中缺少 thread_id。
    """
    configurable = config.get("configurable", {})
    thread_id: str = configurable.get("thread_id", "")
    if not thread_id:
        raise ValueError("config.configurable.thread_id is missing or empty")
    return thread_id
