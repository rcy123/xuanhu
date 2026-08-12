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
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt as pyjwt
from fastapi import Header, Query

from app.core.config import Settings, get_settings
from app.core.exceptions import XuanhuError

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
    roles: tuple[str, ...] = ("doctor",)


# ---------------------------------------------------------------------------
# 签发 / 校验
# ---------------------------------------------------------------------------


def create_access_token(
    doctor_id: str,
    *,
    name: str | None = None,
    settings: Settings | None = None,
) -> tuple[str, int]:
    """签发 access token，返回 ``(token, expires_in_seconds)``。

    颁发永远只用新 key（双密钥灰度期亦然）。
    """
    effective = settings or get_settings()
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=effective.jwt_access_token_ttl_seconds)
    payload = {
        "sub": doctor_id,
        "name": name,
        "roles": ["doctor"],
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
    roles_raw = payload.get("roles")
    roles = tuple(str(r) for r in roles_raw) if isinstance(roles_raw, list) else ("doctor",)
    name = payload.get("name")
    return DoctorPrincipal(
        doctor_id=sub,
        name=name if isinstance(name, str) else None,
        roles=roles or ("doctor",),
    )


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
            payload = _decode_token_payload(token, settings)
            return _principal_from_payload(payload)
        except XuanhuError as exc:
            if settings.xuanhu_auth_enabled == "on":
                raise
            logger.warning(
                "auth.denied_simulated: token 无效 code=%s（%s 模式不阻断）",
                exc.code,
                settings.xuanhu_auth_enabled,
            )
    else:
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
            payload = _decode_token_payload(token, settings)
            return _principal_from_payload(payload)
        except XuanhuError as exc:
            if settings.xuanhu_auth_enabled == "on":
                raise
            logger.warning(
                "auth.denied_simulated: SSE token 无效 code=%s（%s 模式不阻断）",
                exc.code,
                settings.xuanhu_auth_enabled,
            )
    else:
        if settings.xuanhu_auth_enabled == "on":
            raise UnauthenticatedError()
        if settings.xuanhu_auth_enabled == "audit":
            logger.warning(
                "auth.denied_simulated: SSE 未携带 token（%s 模式不阻断）",
                settings.xuanhu_auth_enabled,
            )
    return _anonymous_principal(None)
