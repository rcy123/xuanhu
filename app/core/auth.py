"""JWT 认证核心 — 阶段 1 生产环境加固（T1.2 / T1.5）。

方案见 ``docs/04_生产环境加固/01-认证授权与密钥轮换.md``。

职责：
- 签发 / 校验 HS256 JWT（``sub``=doctor_id、``name``、``roles``、``jti``）。
- ``get_current_doctor``：普通 HTTP 路由的 ``Depends`` 注入点，从
  ``Authorization: Bearer <token>`` 解析可信医师主体。
- ``get_current_doctor_from_query``：SSE 专用（浏览器 EventSource 无法改
  header，token 走 query string，仅此一处例外）。
- 三态开关 ``XUANHU_AUTH_ENABLED``：off / audit / on（见附录「开关灰度」）。
  ``off`` 与 ``audit`` 均不阻断，回退读取 ``X-Doctor-Id`` 头**仅作显示用途**，
  绝不作为授权依据；``on`` 为正式阻断态。

健康检查与 Prometheus 探针端点豁免认证，豁免清单以显式常量维护，
禁止前缀匹配豁免（避免 ``/health/../`` 类绕过）。
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import jwt as pyjwt
from fastapi import Depends, Header, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.exceptions import XuanhuError
from app.db.session import get_db, get_session_factory
from app.models.doctor import Doctor

logger = logging.getLogger("xuanhu.auth")

# ---------------------------------------------------------------------------
# 健康检查 / 监控探针豁免清单 —— 显式常量，禁止前缀匹配。
# K8s probe 与 Prometheus 抓取不携带 token 是硬约束。
# ---------------------------------------------------------------------------
AUTH_EXEMPT_PATHS: frozenset[str] = frozenset(
    {
        "/api/v1/health",
        "/api/v1/health/llm",
        "/api/v1/health/ready",
        "/api/v1/health/rag",
        "/api/v1/health/outbox",
        "/metrics",
        "/metrics/outbox",
        "/health",
        "/health/llm",
    }
)

JWT_ALGORITHM = "HS256"
ACCOUNT_ROLES: frozenset[str] = frozenset({"doctor", "admin"})
# New account-creation channels use this floor.  Login keeps accepting older
# hashes so a migration does not lock out pre-existing clinician accounts.
PASSWORD_MIN_LENGTH = 12
# 登录名：简短、唯一、好记的 ASCII 标识（拼音/工号），替代难记的 UUID。
USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,63}$")
DatabaseSession = Annotated[AsyncSession, Depends(get_db)]


# ---------------------------------------------------------------------------
# 认证异常（错误码与接口设计文档 envelope 对齐）
# ---------------------------------------------------------------------------


class UnauthenticatedError(XuanhuError):
    """未携带 token 或 token 无法解析。"""

    code = "UNAUTHENTICATED"
    message = "未认证，请先登录"
    status_code = 401
    retryable = False


class InvalidTokenError(XuanhuError):
    """token 签名无效或格式非法（含伪造）。"""

    code = "INVALID_TOKEN"
    message = "令牌无效"
    status_code = 401
    retryable = False


class TokenExpiredError(XuanhuError):
    """token 已过期。"""

    code = "TOKEN_EXPIRED"
    message = "令牌已过期，请重新登录"
    status_code = 401
    retryable = False


class AccountLockedError(XuanhuError):
    """登录失败次数超限，账号暂时锁定。"""

    code = "ACCOUNT_LOCKED"
    message = "登录失败次数过多，账号已锁定，请稍后再试"
    status_code = 401
    retryable = False


class AccountDisabledError(XuanhuError):
    """账号已停用。"""

    code = "ACCOUNT_DISABLED"
    message = "账号已停用，请联系管理员"
    status_code = 403
    retryable = False


class TokenRevokedError(XuanhuError):
    """The token no longer matches the authoritative account record."""

    code = "TOKEN_REVOKED"
    message = "登录状态已失效，请重新登录"
    status_code = 401
    retryable = False


class ClinicalRoleRequiredError(XuanhuError):
    """An administrator token was presented to a clinical endpoint."""

    code = "CLINICAL_ROLE_REQUIRED"
    message = "该账号无医师问诊权限"
    status_code = 403
    retryable = False


class AdminRequiredError(XuanhuError):
    """A non-administrator attempted to access the administration API."""

    code = "ADMIN_REQUIRED"
    message = "需要管理员权限"
    status_code = 403
    retryable = False


class AdminActionForbiddenError(XuanhuError):
    """An otherwise authenticated administrator attempted a forbidden action."""

    code = "ADMIN_ACTION_FORBIDDEN"
    message = "不允许执行该管理员操作"
    status_code = 409
    retryable = False


class AdminUserNotFoundError(XuanhuError):
    """A requested account does not exist in the administration view."""

    code = "ADMIN_USER_NOT_FOUND"
    message = "用户不存在"
    status_code = 404
    retryable = False


# ---------------------------------------------------------------------------
# 主体模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DoctorPrincipal:
    """可信医师主体。

    仅含身份展示字段，**绝不携带原始 token 或密钥**。``doctor_id`` 来自
    JWT claim（``sub``），不可由客户端自报。
    """

    doctor_id: str | None
    name: str | None = None
    role: str | None = None
    roles: tuple[str, ...] = ("doctor",)
    auth_version: int | None = None


# ---------------------------------------------------------------------------
# 签发 / 校验
# ---------------------------------------------------------------------------


def create_access_token(
    doctor_id: str,
    *,
    name: str | None = None,
    role: str = "doctor",
    auth_version: int = 1,
    settings: Settings | None = None,
) -> tuple[str, int]:
    """签发 access token，返回 ``(token, expires_in_seconds)``。

    颁发永远只用新 key（双密钥灰度期亦然）。
    """
    if role not in ACCOUNT_ROLES:
        raise ValueError(f"unsupported account role: {role!r}")
    if type(auth_version) is not int or auth_version < 1:
        raise ValueError("auth_version must be a positive integer")

    effective = settings or get_settings()
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=effective.jwt_access_token_ttl_seconds)
    payload = {
        "sub": doctor_id,
        "name": name,
        # ``role`` is the authoritative JWT claim.  ``roles`` is retained for
        # older consumers that already render this claim, but authorization
        # never trusts it independently of the database-bound single role.
        "role": role,
        "roles": [role],
        "auth_version": auth_version,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        "jti": uuid.uuid4().hex,
    }
    token = pyjwt.encode(payload, effective.jwt_signing_key, algorithm=JWT_ALGORITHM)
    return token, effective.jwt_access_token_ttl_seconds


def _decode_token_payload(token: str, settings: Settings) -> dict[str, Any]:
    """按新 key → 旧 key（双密钥灰度）顺序验签。失败抛认证异常。"""
    keys = [settings.jwt_signing_key]
    if settings.jwt_signing_key_previous:
        keys.append(settings.jwt_signing_key_previous)
    for key in keys:
        try:
            return dict(
                pyjwt.decode(
                    token,
                    key,
                    algorithms=[JWT_ALGORITHM],
                    options={"require": ["sub", "exp"]},
                )
            )
        except pyjwt.ExpiredSignatureError:
            raise TokenExpiredError() from None
        except pyjwt.InvalidTokenError:
            continue
    raise InvalidTokenError()


def _principal_from_payload(payload: dict[str, Any]) -> DoctorPrincipal:
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        raise InvalidTokenError()
    role = payload.get("role")
    if not isinstance(role, str) or role not in ACCOUNT_ROLES:
        raise InvalidTokenError()
    auth_version = payload.get("auth_version")
    # bool is a subclass of int, so a strict type check is required here.
    if type(auth_version) is not int or auth_version < 1:
        raise InvalidTokenError()
    name = payload.get("name")
    return DoctorPrincipal(
        doctor_id=sub,
        name=name if isinstance(name, str) else None,
        role=role,
        roles=(role,),
        auth_version=auth_version,
    )


def _compat_principal_from_payload(payload: dict[str, Any]) -> DoctorPrincipal:
    """Read old clinical JWTs in the non-blocking rollout modes only.

    Old tokens contain ``sub`` and may contain ``roles=["doctor"]`` but lack
    the later ``role`` and ``auth_version`` claims.  They can never become
    administrator credentials: the strict parser is mandatory for ``on`` and
    for all admin routes.
    """
    try:
        return _principal_from_payload(payload)
    except InvalidTokenError:
        pass

    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        raise InvalidTokenError()
    roles_raw = payload.get("roles")
    if roles_raw is not None:
        if not isinstance(roles_raw, list) or any(not isinstance(item, str) for item in roles_raw):
            raise InvalidTokenError()
        if any(item != "doctor" for item in roles_raw):
            raise InvalidTokenError()
    name = payload.get("name")
    return DoctorPrincipal(
        doctor_id=sub,
        name=name if isinstance(name, str) else None,
        role="doctor",
        roles=("doctor",),
        auth_version=None,
    )


def _nonblocking_clinical_principal_or_anonymous(
    principal: DoctorPrincipal,
    fallback_doctor_id: str | None,
) -> DoctorPrincipal:
    """Keep non-clinical JWTs out of the clinical compatibility path.

    ``off`` and ``audit`` deliberately remain non-blocking during rollout, but
    an administrator credential still must not become a clinical principal.
    A valid doctor JWT can retain its legacy compatibility behaviour; every
    other parsed role falls back to the same anonymous display-only principal
    as an absent or invalid token.
    """
    if principal.role == "doctor":
        return principal
    logger.warning(
        "auth.denied_simulated: non-clinical token role=%s cannot enter clinical path",
        principal.role,
    )
    return _anonymous_principal(fallback_doctor_id)


def _parse_bearer(authorization: str | None) -> str | None:
    """从 ``Authorization`` 头提取 Bearer token；非法格式返回 None。"""
    if not authorization:
        return None
    scheme, _, rest = authorization.partition(" ")
    if scheme.lower() != "bearer" or not rest:
        return None
    return rest.strip()


def _anonymous_principal(x_doctor_id: str | None) -> DoctorPrincipal:
    """回退主体：X-Doctor-Id 仅作显示用途，不作为授权依据。"""
    return DoctorPrincipal(doctor_id=x_doctor_id or None, name=x_doctor_id or None)


def auth_fail_key(doctor_id: str) -> str:
    """登录失败计数的 Redis key（锁定窗口=key 的 TTL）。"""
    return f"auth:fail:{doctor_id}"


# ---------------------------------------------------------------------------
# FastAPI 依赖
# ---------------------------------------------------------------------------


async def _bind_principal_to_account(
    principal: DoctorPrincipal,
    db: AsyncSession,
) -> DoctorPrincipal:
    """Bind a signed JWT to the current, authoritative account row.

    Signature verification alone cannot revoke a credential early.  This
    lookup enforces account disablement, role changes, and ``auth_version`` on
    every bearer-token request.  Account absence deliberately uses the same
    token-revoked response as a version mismatch, avoiding account discovery.
    """
    if principal.doctor_id is None or principal.role is None or principal.auth_version is None:
        raise InvalidTokenError()
    try:
        doctor_id = uuid.UUID(principal.doctor_id)
    except ValueError:
        raise InvalidTokenError() from None

    doctor = await db.scalar(select(Doctor).where(Doctor.id == doctor_id))
    if doctor is None:
        raise TokenRevokedError()
    # A disabled account may not continue using an already-issued token.  Keep
    # ``ACCOUNT_DISABLED`` for a *new login* attempt, but make bearer callers
    # clear their session through the standard 401 credential-revoked path.
    if not doctor.enabled:
        raise TokenRevokedError()
    if doctor.auth_version != principal.auth_version or doctor.role != principal.role:
        raise TokenRevokedError()
    return DoctorPrincipal(
        doctor_id=str(doctor.id),
        name=doctor.name,
        role=doctor.role,
        roles=(doctor.role,),
        auth_version=doctor.auth_version,
    )


async def _strict_principal_from_authorization(
    authorization: str | None,
    db: AsyncSession,
) -> DoctorPrincipal:
    """Decode, validate, and database-bind a bearer token without fallbacks."""
    token = _parse_bearer(authorization)
    if token is None:
        raise UnauthenticatedError()
    payload = _decode_token_payload(token, get_settings())
    return await _bind_principal_to_account(_principal_from_payload(payload), db)


async def _strict_principal_from_authorization_lazily(
    authorization: str | None,
) -> DoctorPrincipal:
    """Strictly bind a clinical bearer only when auth enforcement is enabled.

    The clinical dependencies must stay DB-free in ``off`` and ``audit`` so
    their documented rollout fallback still works in degraded environments.
    ``require_admin`` intentionally does not use this helper: admin routes
    always receive a mandatory database dependency and fail closed.
    """
    factory = get_session_factory()
    async with factory() as db:
        return await _strict_principal_from_authorization(authorization, db)


async def get_current_doctor(
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_doctor_id: str | None = Header(default=None, alias="X-Doctor-Id"),
) -> DoctorPrincipal:
    """从 Bearer token 解析出可信医师主体；失败抛认证异常。

    三态开关语义：
    - ``on``   ：无有效 token → 401。``X-Doctor-Id`` 完全失效。
    - ``audit``：校验但仅记日志不阻断；无有效 token 时退回
      ``X-Doctor-Id`` 作显示用，并打 ``auth.denied_simulated`` 日志。
    - ``off``  ：不校验，回退 ``X-Doctor-Id``（灰度回退态）。
    """
    settings = get_settings()
    token = _parse_bearer(authorization)
    if token:
        try:
            if settings.xuanhu_auth_enabled == "on":
                principal = await _strict_principal_from_authorization_lazily(authorization)
                if principal.role != "doctor":
                    raise ClinicalRoleRequiredError()
                return principal
            principal = _compat_principal_from_payload(_decode_token_payload(token, settings))
            return _nonblocking_clinical_principal_or_anonymous(principal, x_doctor_id)
        except XuanhuError as exc:
            if settings.xuanhu_auth_enabled == "on":
                raise
            logger.warning(
                "auth.denied_simulated: token 无效 code=%s（%s 模式不阻断）",
                exc.code,
                settings.xuanhu_auth_enabled,
            )
            return _anonymous_principal(x_doctor_id)

    if settings.xuanhu_auth_enabled == "on":
        raise UnauthenticatedError()
    if settings.xuanhu_auth_enabled == "audit":
        logger.warning(
            "auth.denied_simulated: 未携带 token（%s 模式不阻断）",
            settings.xuanhu_auth_enabled,
        )
    return _anonymous_principal(x_doctor_id)


async def get_current_doctor_from_query(
    token: str | None = Query(default=None),
) -> DoctorPrincipal:
    """SSE 专用：从 query string 的 ``?token=<jwt>`` 校验医师身份。

    SSE（EventSource）无法自定义 header，token 走 query string 是唯一
    例外。无效/缺失 token 在 ``on`` 模式直接抛认证异常（建连即断）。
    反向代理层（阶段 3）access log 不记 query string，缓解 token 泄露窗口。
    """
    settings = get_settings()
    if token:
        try:
            if settings.xuanhu_auth_enabled == "on":
                principal = await _strict_principal_from_authorization_lazily(f"Bearer {token}")
                if principal.role != "doctor":
                    raise ClinicalRoleRequiredError()
                return principal
            principal = _compat_principal_from_payload(_decode_token_payload(token, settings))
            return _nonblocking_clinical_principal_or_anonymous(principal, None)
        except XuanhuError as exc:
            if settings.xuanhu_auth_enabled == "on":
                raise
            logger.warning(
                "auth.denied_simulated: SSE token 无效 code=%s（%s 模式不阻断）",
                exc.code,
                settings.xuanhu_auth_enabled,
            )
            return _anonymous_principal(None)

    if settings.xuanhu_auth_enabled == "on":
        raise UnauthenticatedError()
    if settings.xuanhu_auth_enabled == "audit":
        logger.warning(
            "auth.denied_simulated: SSE 未携带 token（%s 模式不阻断）",
            settings.xuanhu_auth_enabled,
        )
    return _anonymous_principal(None)


async def require_admin(
    db: DatabaseSession,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> DoctorPrincipal:
    """Require a current active administrator, regardless of auth rollout mode.

    Administrative account management is never eligible for the clinical
    ``XUANHU_AUTH_ENABLED=off/audit`` compatibility path.  It requires a
    properly signed bearer token and an enabled, version-matching ``admin``
    row on every request.
    """
    principal = await _strict_principal_from_authorization(authorization, db)
    if principal.role != "admin":
        raise AdminRequiredError()
    return principal
