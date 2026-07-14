"""The browser verification seeder must never target developer services."""

from __future__ import annotations

import runpy
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

_SCRIPT = Path(__file__).parents[1] / "scripts" / "p9-2-verify.py"


def _load_guard(monkeypatch: pytest.MonkeyPatch, database_url: str, redis_url: str) -> Callable[[], None]:
    monkeypatch.setenv("P9_2_DATABASE_URL", database_url)
    monkeypatch.setenv("P9_2_REDIS_URL", redis_url)
    namespace = runpy.run_path(str(_SCRIPT))
    return cast(Callable[[], None], namespace["_require_safe_seed_targets"])


def test_seed_guard_requires_explicit_destructive_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XUANHU_ALLOW_DESTRUCTIVE_TESTS", raising=False)
    guard = _load_guard(
        monkeypatch,
        "postgresql://tester:secret@localhost/xuanhu_test",
        "redis://localhost/8",
    )
    with pytest.raises(RuntimeError, match="XUANHU_ALLOW_DESTRUCTIVE_TESTS"):
        guard()


@pytest.mark.parametrize(
    "database_url,redis_url",
    [
        ("postgresql://tester:secret@localhost/xuanhu", "redis://localhost/8"),
        ("postgresql://tester:secret@localhost/xuanhu_test", "redis://localhost/0"),
        ("postgresql://tester:secret@localhost/xuanhu_test?dbname=xuanhu_test", "redis://localhost/8"),
    ],
)
def test_seed_guard_rejects_unsafe_targets(
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
    redis_url: str,
) -> None:
    monkeypatch.setenv("XUANHU_ALLOW_DESTRUCTIVE_TESTS", "1")
    guard = _load_guard(monkeypatch, database_url, redis_url)
    with pytest.raises(RuntimeError):
        guard()


def test_seed_guard_accepts_explicit_isolated_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XUANHU_ALLOW_DESTRUCTIVE_TESTS", "1")
    guard = _load_guard(
        monkeypatch,
        "postgresql://tester:secret@localhost/xuanhu_test",
        "redis://localhost/8",
    )
    guard()
