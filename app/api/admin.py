"""Fail-closed administrator API for clinical account lifecycle management.

Only a current, database-bound ``admin`` bearer token may call this router.
Deleting a user means disabling the clinical account: account rows are never
physically deleted because they may own historical consultations and audit
records.
"""

from __future__ import annotations

import uuid
from typing import Literal, cast

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import String, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import hash_password
from app.api.request_context import get_trace_id
from app.core.auth import (
    AdminActionForbiddenError,
    AdminUserNotFoundError,
    DoctorPrincipal,
    require_admin,
)
from app.core.ratelimit import require_admin_write_rate_limit
from app.db.session import get_db
from app.models.audit import AuditEvent
from app.models.doctor import Doctor
from app.schemas.admin import AdminDoctorCreateRequest, AdminDoctorItem, AdminDoctorListResponse
from app.schemas.common import success_response

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


def _item(doctor: Doctor) -> AdminDoctorItem:
    """Project only fields that are safe and useful to the management UI."""
    return AdminDoctorItem(
        id=str(doctor.id),
        username=doctor.username,
        name=doctor.name,
        # The database check and authentication layer constrain this stored
        # value; the cast expresses that invariant to the response schema.
        role=cast(Literal["doctor", "admin"], doctor.role),
        enabled=doctor.enabled,
        last_login_at=doctor.last_login_at,
        created_at=doctor.created_at,
    )


def _escaped_contains(value: str) -> str:
    """Treat operator search text literally rather than as a LIKE pattern."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/doctors")
async def list_doctors(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    q: str | None = Query(default=None, max_length=128, alias="q"),
    # Accept the initially documented spelling during the UI rollout, while
    # keeping ``q`` as the canonical public query parameter.
    query: str | None = Query(default=None, max_length=128, include_in_schema=False),
    _: DoctorPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List clinical and administrator accounts without credential material."""
    trace_id = get_trace_id(request)
    statement = select(Doctor)
    search_query = q if q is not None else query
    if search_query:
        pattern = f"%{_escaped_contains(search_query.strip())}%"
        statement = statement.where(
            or_(
                Doctor.name.ilike(pattern, escape="\\"),
                Doctor.username.ilike(pattern, escape="\\"),
                Doctor.id.cast(String).ilike(pattern, escape="\\"),
            )
        )

    total = await db.scalar(select(func.count()).select_from(statement.subquery()))
    records = (
        await db.scalars(
            statement.order_by(Doctor.created_at.desc(), Doctor.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    data = AdminDoctorListResponse(
        items=[_item(record) for record in records],
        total=int(total or 0),
        page=page,
        page_size=page_size,
    )
    return JSONResponse(
        status_code=200,
        content=success_response(data=data.model_dump(mode="json"), trace_id=trace_id),
    )


@router.post("/doctors", status_code=201)
async def create_doctor(
    request: Request,
    body: AdminDoctorCreateRequest,
    admin: DoctorPrincipal = Depends(require_admin),
    _rate_limit: None = Depends(require_admin_write_rate_limit),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a doctor-only account and record a credential-free audit event."""
    trace_id = get_trace_id(request)
    existing = await db.scalar(select(Doctor).where(Doctor.username == body.username))
    if existing is not None:
        raise AdminActionForbiddenError(message="登录名已被使用，请更换")
    doctor = Doctor(
        username=body.username,
        name=body.name,
        password_hash=hash_password(body.password),
        role="doctor",
        enabled=True,
        auth_version=1,
    )
    db.add(doctor)
    await db.flush()
    db.add(
        AuditEvent(
            session_id=None,
            event_type="admin.doctor.created",
            actor_type="admin",
            actor_id=admin.doctor_id,
            # Do not add name, password, password hash, or auth_version here.
            payload={"target_doctor_id": str(doctor.id), "role": "doctor"},
            trace_id=trace_id,
        )
    )
    await db.flush()
    return JSONResponse(
        status_code=201,
        content=success_response(data=_item(doctor).model_dump(mode="json"), trace_id=trace_id),
    )


@router.delete("/doctors/{doctor_id}")
async def disable_doctor(
    request: Request,
    doctor_id: str,
    admin: DoctorPrincipal = Depends(require_admin),
    _rate_limit: None = Depends(require_admin_write_rate_limit),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Soft-delete a clinical account, invalidating every issued token at once."""
    trace_id = get_trace_id(request)
    try:
        target_id = uuid.UUID(doctor_id)
    except ValueError:
        raise AdminUserNotFoundError() from None

    doctor = await db.scalar(select(Doctor).where(Doctor.id == target_id).with_for_update())
    if doctor is None:
        raise AdminUserNotFoundError()
    if str(doctor.id) == admin.doctor_id:
        raise AdminActionForbiddenError(message="不能停用当前管理员账号")
    if doctor.role == "admin":
        raise AdminActionForbiddenError(message="不能通过用户管理停用管理员账号")

    # An already-disabled row is an idempotent DELETE: do not keep incrementing
    # its version or create duplicate audit records for a state that did not
    # change.
    if doctor.enabled:
        doctor.enabled = False
        doctor.auth_version += 1
        db.add(
            AuditEvent(
                session_id=None,
                event_type="admin.doctor.disabled",
                actor_type="admin",
                actor_id=admin.doctor_id,
                # No credential material is present in the audit payload.
                payload={"target_doctor_id": str(doctor.id), "role": "doctor"},
                trace_id=trace_id,
            )
        )
        await db.flush()

    return JSONResponse(
        status_code=200,
        content=success_response(data=_item(doctor).model_dump(mode="json"), trace_id=trace_id),
    )
