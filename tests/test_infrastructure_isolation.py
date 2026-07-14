"""Process-level proof that xdist workers receive independent infrastructure.

Run this module with ``--dist=each``.  Every worker deliberately writes the
same PostgreSQL primary key and Redis key; the test can pass concurrently only
when both resources have been isolated by ``tests/conftest.py``.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import select

from app.core.redis import get_redis
from app.db.session import get_session_factory
from app.models.consult import ConsultSession

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

_FIXED_SESSION_ID = uuid.UUID("b15f6b45-61cb-4c32-a5c8-c1db3cfac4f0")
_FIXED_REDIS_KEY = "xuanhu:test:xdist-isolation:fixed-key"


async def test_each_worker_can_write_identical_database_and_redis_keys() -> None:
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "main")

    factory = get_session_factory()
    async with factory() as db:
        db.add(
            ConsultSession(
                id=_FIXED_SESSION_ID,
                patient_info={"test": "xdist-isolation"},
                agent_runtime="langgraph",
            )
        )
        await db.commit()
        stored_worker = await db.scalar(
            select(ConsultSession.patient_info).where(ConsultSession.id == _FIXED_SESSION_ID)
        )
    assert stored_worker == {"test": "xdist-isolation"}

    redis = await get_redis()
    created = await redis.set(_FIXED_REDIS_KEY, worker_id, nx=True, ex=60)
    assert created is True
    assert await redis.get(_FIXED_REDIS_KEY) == worker_id
