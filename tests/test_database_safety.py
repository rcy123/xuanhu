"""Regression tests for the destructive-test database guard."""

from __future__ import annotations

import os

import pytest

from tests._database_safety import (
    DESTRUCTIVE_TESTS_SENTINEL_ENV,
    TEST_DATABASE_URL_ENV,
    TEST_REDIS_URL_ENV,
    UnsafeTestDatabaseError,
    destructive_database_environment,
    make_run_database_url,
    make_worker_database_url,
    make_worker_redis_url,
    require_destructive_test_database,
    require_destructive_test_redis,
    validate_test_database_url,
    validate_test_redis_url,
)


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://user:secret@localhost/xuanhu",
        "postgresql://user:secret@localhost/xuanhu_test_shadow",
        "postgresql://user:secret@localhost/",
        "mysql://user:secret@localhost/xuanhu_test",
        "postgresql+psycopg://user:secret@localhost/xuanhu_test",
        "postgresql+asyncpg://user:secret@localhost/xuanhu_test",
        "not-a-url",
        "postgresql://user:secret@localhost/xuanhu_test?dbname=postgres",
        "postgresql://user:secret@localhost/xuanhu_test?host=prod.example.com",
        "postgresql://user:secret@localhost/xuanhu_test?service=production",
    ],
)
def test_validate_test_database_url_rejects_unsafe_targets(database_url: str) -> None:
    with pytest.raises(UnsafeTestDatabaseError) as raised:
        validate_test_database_url(database_url)
    assert "secret" not in str(raised.value)
    assert database_url not in str(raised.value)


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://user:secret@localhost/xuanhu_test",
        "postgresql://user:secret@localhost/worker_gw0_test?sslmode=disable",
        "postgres://user:secret@localhost/%78uanhu_test",
    ],
)
def test_validate_test_database_url_accepts_only_explicit_test_suffix(database_url: str) -> None:
    assert validate_test_database_url(database_url) == database_url


def test_configured_test_url_without_operator_sentinel_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TEST_DATABASE_URL_ENV, "postgresql://user:secret@localhost/xuanhu_test")
    monkeypatch.delenv(DESTRUCTIVE_TESTS_SENTINEL_ENV, raising=False)
    with pytest.raises(UnsafeTestDatabaseError, match=DESTRUCTIVE_TESTS_SENTINEL_ENV):
        require_destructive_test_database()


def test_missing_test_urls_fail_instead_of_skipping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TEST_DATABASE_URL_ENV, raising=False)
    monkeypatch.delenv(TEST_REDIS_URL_ENV, raising=False)
    with pytest.raises(UnsafeTestDatabaseError, match=TEST_DATABASE_URL_ENV):
        require_destructive_test_database()
    with pytest.raises(UnsafeTestDatabaseError, match=TEST_REDIS_URL_ENV):
        require_destructive_test_redis()


def test_destructive_environment_temporarily_routes_db_url(monkeypatch: pytest.MonkeyPatch) -> None:
    original = "postgresql://user:secret@localhost/application"
    target = "postgresql://user:secret@localhost/xuanhu_test"
    monkeypatch.setenv("DB_URL", original)
    monkeypatch.setenv(TEST_DATABASE_URL_ENV, target)
    monkeypatch.setenv(DESTRUCTIVE_TESTS_SENTINEL_ENV, "1")

    with destructive_database_environment() as selected:
        assert selected == target
        assert os.environ["DB_URL"] == target

    assert os.environ["DB_URL"] == original


def test_make_worker_database_url_keeps_test_suffix_and_connection_options() -> None:
    result = make_worker_database_url(
        "postgresql://user:secret@localhost:5432/xuanhu_test?sslmode=disable",
        "gw2",
    )
    assert result == "postgresql://user:secret@localhost:5432/xuanhu_gw2_test?sslmode=disable"
    assert validate_test_database_url(result) == result


@pytest.mark.parametrize("worker_id", ["", "../prod", "worker id", "x" * 33])
def test_make_worker_database_url_rejects_unsafe_worker_id(worker_id: str) -> None:
    with pytest.raises(UnsafeTestDatabaseError, match="worker id"):
        make_worker_database_url("postgresql://user:secret@localhost/xuanhu_test", worker_id)


def test_run_database_url_is_unique_bounded_and_guarded() -> None:
    base = f"postgresql://user:secret@localhost/{'long_name_' * 8}test_test"
    first = make_run_database_url(base, "run-a", "gw0")
    second = make_run_database_url(base, "run-b", "gw0")
    assert first != second
    assert len(first.rsplit("/", 1)[-1].encode()) <= 63
    assert first.endswith("_test")
    validate_test_database_url(first)


@pytest.mark.parametrize(
    "redis_url",
    [
        "redis://:secret@localhost/0",
        "redis://:secret@localhost/7",
        "redis://:secret@localhost/16",
        "redis://:secret@localhost/8?db=0",
        "redis://:secret@localhost/8?host=prod.example.com",
        "http://localhost/8",
        "redis://localhost/not-a-number",
    ],
)
def test_validate_test_redis_url_rejects_unsafe_targets(redis_url: str) -> None:
    with pytest.raises(UnsafeTestDatabaseError) as raised:
        validate_test_redis_url(redis_url)
    assert "secret" not in str(raised.value)


def test_redis_guard_and_worker_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    base = "redis://:secret@localhost:6379/8"
    monkeypatch.setenv(TEST_REDIS_URL_ENV, base)
    monkeypatch.setenv(DESTRUCTIVE_TESTS_SENTINEL_ENV, "1")
    assert require_destructive_test_redis() == base
    assert make_worker_redis_url(base, "main").endswith("/8")
    assert make_worker_redis_url(base, "gw3").endswith("/11")
    with pytest.raises(UnsafeTestDatabaseError, match="not enough"):
        make_worker_redis_url("redis://localhost/15", "gw1")
