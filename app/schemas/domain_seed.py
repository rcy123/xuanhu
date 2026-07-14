"""Clinical-only contract for seeding a new LangGraph session."""

from __future__ import annotations

from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.domain import SafetyProfileSchema

INITIAL_DOMAIN_SEED_VERSION = "initial-domain-seed.v1"


class SeedObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    observation_id: UUID
    fact_key: str
    value: str | int
    normalized_value: str | int


class InitialDomainSeed(BaseModel):
    """Identity-free initial facts derived from the structured create form."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: ClassVar[str] = INITIAL_DOMAIN_SEED_VERSION

    source_message_id: UUID
    observations: tuple[SeedObservation, ...] = ()
    safety_profile: SafetyProfileSchema
    payload_digest: str
