"""Privacy-safe request and response schemas for account administration."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.core.auth import PASSWORD_MIN_LENGTH, USERNAME_PATTERN


class AdminDoctorCreateRequest(BaseModel):
    """Create a clinical account; this API never creates administrators."""

    username: str = Field(..., min_length=3, max_length=64, description="登录名（拼音/工号，唯一）")
    name: str = Field(..., min_length=1, max_length=64, description="医师姓名")
    password: str = Field(
        ...,
        min_length=PASSWORD_MIN_LENGTH,
        max_length=256,
        description="初始密码；仅用于本次创建，绝不回显或审计",
    )

    @field_validator("username")
    @classmethod
    def username_must_be_valid(cls, value: str) -> str:
        cleaned = value.strip()
        if not USERNAME_PATTERN.fullmatch(cleaned):
            raise ValueError("username must be 3-64 letters/digits/._- and start with alphanumeric")
        return cleaned

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("name must not be blank")
        return cleaned


class AdminDoctorItem(BaseModel):
    """Management projection that deliberately omits credentials and version."""

    id: str
    username: str
    name: str
    role: Literal["doctor", "admin"]
    enabled: bool
    last_login_at: datetime | None
    created_at: datetime


class AdminDoctorListResponse(BaseModel):
    """Page of administration-safe account projections."""

    items: list[AdminDoctorItem]
    total: int = Field(..., ge=0)
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1)
