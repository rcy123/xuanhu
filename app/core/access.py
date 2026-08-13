"""会话所有权校验 — 阶段 2 PHI 访问控制（T2.2 / T2.3 / T2.4）。

方案见 ``docs/04_生产环境加固/02-PHI访问控制与日志脱敏.md``。

- 写接口越权 → ``403 ACCESS_FORBIDDEN``；读接口命中他人会话 → ``404
  SESSION_NOT_FOUND``（不暴露"会话存在但不属于你"）。
- 越权写入 ``audit.access_denied`` 事件（``path`` 只记路径模板，不记真实
  session_id；session_id 单独成列）。
- 三态开关 ``XUANHU_ACCESS_ENABLED``：off=不校验（灰度回退态）/ audit=
  校验并记审计但不阻断 / on=生效并阻断。
- 历史无主会话（``doctor_id IS NULL``）过渡期已结束，``on`` 模式下一律
  fail-closed：不匹配当前医师身份的会话（含无主）读写均拒绝。

审计写入 **fail-open**：审计基础设施抖动时记一条 logger.error 但放行请求，
避免连累临床业务（文档 02 §8 权衡）。
"""

from __future__ import annotations

import logging
import uuid

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import DoctorPrincipal, get_current_doctor, get_current_doctor_from_query
from app.core.config import get_settings
from app.core.exceptions import SessionNotFoundError, XuanhuError
from app.db.session import get_db
from app.models.audit import AuditEvent
from app.models.consult import ConsultSession

logger = logging.getLogger("xuanhu.access")

ACCESS_DENIED_EVENT_TYPE = "access.denied"


class AccessForbiddenError(XuanhuError):
    """写接口越权访问。"""

    code = "ACCESS_FORBIDDEN"
    message = "无权访问该会话"
    status_code = 403
    retryable = False


def _coerce_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _actor_uuid(doctor_id: str | None) -> uuid.UUID | None:
    if not doctor_id:
        return None
    try:
        return uuid.UUID(doctor_id)
    except ValueError:
        return None


async def _write_access_denied_audit(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    attempted_doctor_id: str | None,
    path_template: str,
    reason: str,
    trace_id: str,
) -> None:
    """写 access.denied 审计事件（fail-open：失败仅记日志，不阻断请求）。"""
    try:
        db.add(
            AuditEvent(
                session_id=session_id,
                event_type=ACCESS_DENIED_EVENT_TYPE,
                actor_type="doctor",
                actor_id=attempted_doctor_id,
                payload={
                    "attempted_doctor_id": attempted_doctor_id,
                    "path": path_template,
                    "reason": reason,
                },
                trace_id=trace_id,
            )
        )
        await db.commit()
    except Exception:  # pragma: no cover - defensive
        logger.error("access.denied 审计写入失败（fail-open 放行）: trace=%s", trace_id)


async def _enforce_session_access(
    *,
    request: Request,
    session_id: str,
    doctor: DoctorPrincipal,
    db: AsyncSession,
    not_found_on_violation: bool,
) -> None:
    """所有权校验核心；``not_found_on_violation`` 控制越权时 404（读）还是 403（写）。"""
    settings = get_settings()
    mode = settings.xuanhu_access_enabled
    if mode == "off":
        return

    trace_id = (
        request.headers.get("x-request-id")
        or request.headers.get("x-trace-id")
        or uuid.uuid4().hex
    )
    route = request.scope.get("route")
    path_template = route.path if route is not None else "unknown"

    sid = _coerce_uuid(session_id)
    if sid is None:
        if mode == "on":
            raise SessionNotFoundError(detail="session_id 格式非法", retryable=False)
        return

    session = await db.get(ConsultSession, sid)
    if session is None:
        if mode == "on":
            raise SessionNotFoundError(detail="session_id not found", retryable=False)
        return

    owner = session.doctor_id
    actor = _actor_uuid(doctor.doctor_id)
    if actor is not None and owner == actor:
        # owner 匹配，放行。
        return
    # owner 为 NULL（历史无主会话，过渡期已结束）或 actor 与 owner 不匹配
    # → 越权，走下面的审计 + 403/404（fail-closed）。

    await _write_access_denied_audit(
        db,
        session_id=sid,
        attempted_doctor_id=doctor.doctor_id,
        path_template=path_template,
        reason="not_owner",
        trace_id=trace_id,
    )
    if mode == "on":
        if not_found_on_violation:
            raise SessionNotFoundError(detail="session_id not found", retryable=False)
        raise AccessForbiddenError()
    logger.warning(
        "access.denied_simulated: doctor=%s path=%s session=%s（audit 观察期不阻断）",
        doctor.doctor_id,
        path_template,
        sid,
    )


def _coerce_dep(dependency: object) -> object:
    """接受裸函数或已包装的 Depends 对象，统一返回可用的 Depends 参数。"""
    from fastapi import Depends as _Depends

    if callable(dependency):
        return _Depends(dependency)
    return dependency


async def require_session_owner(
    request: Request,
    session_id: str,
    doctor: DoctorPrincipal = Depends(get_current_doctor),
    db: AsyncSession = Depends(get_db),
) -> None:
    """写接口所有权校验：越权 → 403 ACCESS_FORBIDDEN + access.denied 审计。"""
    await _enforce_session_access(
        request=request,
        session_id=session_id,
        doctor=doctor,
        db=db,
        not_found_on_violation=False,
    )


async def require_session_reader(
    request: Request,
    session_id: str,
    doctor: DoctorPrincipal = Depends(get_current_doctor),
    db: AsyncSession = Depends(get_db),
) -> None:
    """读接口所有权校验：他人/不存在会话一律 404 SESSION_NOT_FOUND。"""
    await _enforce_session_access(
        request=request,
        session_id=session_id,
        doctor=doctor,
        db=db,
        not_found_on_violation=True,
    )


async def require_stream_session_reader(
    request: Request,
    session_id: str,
    doctor: DoctorPrincipal = Depends(get_current_doctor_from_query),
    db: AsyncSession = Depends(get_db),
) -> None:
    """SSE 读接口所有权校验：doctor 来自 query string token。"""
    await _enforce_session_access(
        request=request,
        session_id=session_id,
        doctor=doctor,
        db=db,
        not_found_on_violation=True,
    )
