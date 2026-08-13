"""Role-boundary tests for the non-blocking clinical auth rollout modes."""

from __future__ import annotations

import pytest

from app.core.auth import (
    create_access_token,
    get_current_doctor,
    get_current_doctor_from_query,
)


@pytest.mark.parametrize("mode", ("off", "audit"))
@pytest.mark.asyncio
async def test_admin_token_is_anonymous_on_clinical_http_in_nonblocking_modes(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """Compatibility must never turn an admin JWT into a clinical identity."""
    from app.core.config import get_settings

    monkeypatch.setenv("XUANHU_AUTH_ENABLED", mode)
    get_settings.cache_clear()
    try:
        token, _ = create_access_token(
            "00000000-0000-0000-0000-000000000001",
            role="admin",
            auth_version=1,
        )
        principal = await get_current_doctor(
            authorization=f"Bearer {token}",
            x_doctor_id="display-only-fallback",
        )
        assert principal.doctor_id == "display-only-fallback"
        assert principal.name == "display-only-fallback"
        assert principal.role is None
        assert principal.auth_version is None
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("mode", ("off", "audit"))
@pytest.mark.asyncio
async def test_admin_token_is_anonymous_on_clinical_sse_in_nonblocking_modes(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """The query-token SSE exception has the same role boundary as HTTP."""
    from app.core.config import get_settings

    monkeypatch.setenv("XUANHU_AUTH_ENABLED", mode)
    get_settings.cache_clear()
    try:
        token, _ = create_access_token(
            "00000000-0000-0000-0000-000000000001",
            role="admin",
            auth_version=1,
        )
        principal = await get_current_doctor_from_query(token=token)
        assert principal.doctor_id is None
        assert principal.name is None
        assert principal.role is None
        assert principal.auth_version is None
    finally:
        get_settings.cache_clear()
