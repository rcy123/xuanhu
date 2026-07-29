"""公开恢复接口的运行时分流。

此模块是 ``POST /recover`` 与 Legacy ``RecoveryService`` 之间的安全边界：
只读查询会话的 ``agent_runtime``，仅 Legacy 会话能够进入原恢复服务。
LangGraph 恢复实现完成前必须 fail closed，不能触碰 Legacy Redis checkpoint、
会话锁或任何恢复写路径。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.lifecycle import SharedLangGraphRuntime
from app.core.exceptions import (
    SessionNotFoundError,
    StateRecoveryRequiredError,
)
from app.models.consult import ConsultSession
from app.schemas.recovery import RecoveryRequest, RecoveryResponse
from app.services.langgraph_recovery import LangGraphRecoveryService
from app.services.recovery import RecoveryService


class RecoveryDispatcher:
    """按会话持久化的运行时选择恢复实现。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._resolved_runtime: tuple[str, str] | None = None

    async def recover(
        self,
        session_id: str,
        request: RecoveryRequest,
        *,
        doctor_id: str | None,
        trace_id: str,
        idempotency_key: str = "",
        shared_runtime: SharedLangGraphRuntime | None = None,
        allow_request_local_runtime: bool = False,
    ) -> RecoveryResponse:
        """只读解析运行时后分流到完全隔离的恢复实现。"""
        runtime = (
            self._resolved_runtime[1]
            if self._resolved_runtime is not None and self._resolved_runtime[0] == session_id
            else await self.get_runtime(session_id)
        )
        if runtime == "langgraph":
            return await LangGraphRecoveryService(self._db).recover(
                session_id,
                request,
                doctor_id=doctor_id,
                trace_id=trace_id,
                idempotency_key=idempotency_key,
                shared_runtime=shared_runtime,
                allow_request_local_runtime=allow_request_local_runtime,
            )
        if runtime != "legacy":
            # 防御数据库约束失效或历史脏数据：任何未显式支持的 runtime
            # 都不能越过分流边界进入 Legacy 恢复链路。
            raise StateRecoveryRequiredError(
                message="会话运行时不支持恢复",
                detail=f"session_id={session_id} agent_runtime={runtime}",
                retryable=False,
            )

        # 只有明确的 legacy 值才会抵达这里并进入会获取 Redis 锁的旧恢复链路。
        return await RecoveryService(self._db).recover(
            session_id,
            request,
            doctor_id=doctor_id,
            trace_id=trace_id,
        )

    async def get_runtime(self, session_id: str) -> str:
        """仅从 PostgreSQL 读取权威 runtime，不获取锁、不修改会话。"""
        try:
            sid = uuid.UUID(session_id)
        except ValueError as exc:
            raise SessionNotFoundError(
                detail=f"session_id={session_id} 格式非法",
                retryable=False,
            ) from exc

        result = await self._db.execute(
            select(ConsultSession.agent_runtime).where(ConsultSession.id == sid)
        )
        runtime = result.scalar_one_or_none()
        if runtime is None:
            raise SessionNotFoundError(
                detail=f"session_id={session_id} 在数据库中未找到",
                retryable=False,
            )
        self._resolved_runtime = (session_id, runtime)
        return runtime
