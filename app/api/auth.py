"""认证 API — 阶段 1 加固（T1.3）。

``POST /api/v1/auth/login`` 是除健康检查外唯一不需 token 的写接口。
登录失败统一返回 401（不区分"用户不存在"与"密码错误"），连续失败
锁定账号（Redis 计数，key=``auth:fail:{doctor_id}``），并按 IP 限流。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import (
    AccountDisabledError,
    AccountLockedError,
    UnauthenticatedError,
    auth_fail_key,
    create_access_token,
)
from app.core.config import Settings, get_settings
from app.core.exceptions import RateLimitedError
from app.core.redis import get_redis
from app.db.session import get_db
from app.models.doctor import Doctor
from app.schemas.common import success_response

logger = logging.getLogger("xuanhu.auth")

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

LOGIN_RATE_LIMIT_PREFIX = "ratelimit:login:"

_password_hasher = PasswordHasher()


class LoginRequest(BaseModel):
    """登录请求体。"""

    doctor_id: str = Field(..., min_length=1, max_length=128, description="医师唯一标识（doctors.id UUID）")
    password: str = Field(..., min_length=1, max_length=256, description="登录密码")


def hash_password(password: str) -> str:
    """以 argon2id 哈希密码；禁止明文存储。"""
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """校验密码哈希；哈希格式非法按不匹配处理。"""
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


async def _ip_rate_allowed(request: Request) -> None:
    """按客户端 IP 对登录接口限流（10 次/分钟，超限 429）。"""
    settings = get_settings()
    redis = await get_redis()
    client_ip = request.client.host if request.client else "unknown"
    key = f"{LOGIN_RATE_LIMIT_PREFIX}{client_ip}"
    current = await redis.incr(key)
    if current == 1:
        await redis.expire(key, 60)
    if current > settings.login_rate_limit_per_minute:
        raise RateLimitedError(
            message="请求过于频繁，请稍后重试",
            retry_after=60,
        )


@router.post("/login")
async def login(
    request: Request,
    body: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """医师登录：校验密码、失败锁定、签发 JWT。"""
    settings = get_settings()
    trace_id = str(uuid.uuid4())

    await _ip_rate_allowed(request)

    redis = await get_redis()
    fail_key = auth_fail_key(body.doctor_id)

    # 锁定判定：失败计数达到阈值后，key 存续期内一律拒绝。
    locked_count = await redis.get(fail_key)
    if locked_count is not None and int(locked_count) >= settings.login_fail_lock_threshold:
        raise AccountLockedError()

    # 统一错误：用户不存在 / 密码错误 / UUID 非法 都返回 UNAUTHENTICATED。
    try:
        doctor_uuid = uuid.UUID(body.doctor_id)
    except ValueError:
        raise UnauthenticatedError() from None

    result = await db.execute(select(Doctor).where(Doctor.id == doctor_uuid))
    doctor = result.scalar_one_or_none()
    if doctor is None or not verify_password(doctor.password_hash, body.password):
        await _register_login_failure(redis, fail_key, settings)
        raise UnauthenticatedError()

    if not doctor.enabled:
        raise AccountDisabledError()

    # 登录成功：清除失败计数，签发 token。
    await redis.delete(fail_key)
    doctor.last_login_at = datetime.now(UTC)
    token, expires_in = create_access_token(str(doctor.id), name=doctor.name, settings=settings)
    logger.info("auth.login.success doctor_id=%s", doctor.id)
    return JSONResponse(
        status_code=200,
        content=success_response(
            data={
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": expires_in,
            },
            trace_id=trace_id,
            message="ok",
        ),
    )


async def _register_login_failure(redis: Redis, fail_key: str, settings: Settings) -> None:
    """记录一次登录失败；达到阈值后 key 存续期=锁定窗口。"""
    count = await redis.incr(fail_key)
    if count == 1:
        await redis.expire(fail_key, settings.login_fail_lock_seconds)
    logger.warning(
        "auth.login.failed doctor_id=%s attempts=%d/%d",
        fail_key.rsplit(":", 1)[-1],
        count,
        settings.login_fail_lock_threshold,
    )


# ---------------------------------------------------------------------------
# 异常处理器（认证错误统一 envelope）
# ---------------------------------------------------------------------------


async def auth_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """把认证异常转为标准 envelope，错误信息统一不区分细节。"""
    trace_id = request.headers.get("x-request-id") or request.headers.get("x-trace-id") or str(uuid.uuid4())
    assert isinstance(exc, UnauthenticatedError | AccountLockedError | AccountDisabledError)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "detail": None,
            "retryable": exc.retryable,
            "stage": None,
            "trace_id": trace_id,
        },
    )


auth_exception_handlers: dict[type[Exception], object] = {
    UnauthenticatedError: auth_exception_handler,
    AccountLockedError: auth_exception_handler,
    AccountDisabledError: auth_exception_handler,
}
