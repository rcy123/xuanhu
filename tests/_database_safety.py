"""Fail-closed helpers for tests that mutate a real PostgreSQL database.

Destructive tests must never infer their target from the application's ``DB_URL``.
They require both an explicitly supplied ``TEST_DATABASE_URL`` and a separate
operator acknowledgement.  Keeping the guard in one module prevents individual
fixtures from gradually drifting back to the development database.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

TEST_DATABASE_URL_ENV = "TEST_DATABASE_URL"
TEST_REDIS_URL_ENV = "TEST_REDIS_URL"
DESTRUCTIVE_TESTS_SENTINEL_ENV = "XUANHU_ALLOW_DESTRUCTIVE_TESTS"
DESTRUCTIVE_TESTS_SENTINEL_VALUE = "1"
_POSTGRES_SCHEMES = frozenset({"postgres", "postgresql"})
_WORKER_ID_PATTERN = re.compile(r"[a-zA-Z0-9_-]{1,32}\Z")
_TARGET_OVERRIDE_PARAMETERS = frozenset(
    {"dbname", "database", "host", "hostaddr", "port", "user", "service", "servicefile"}
)
_REDIS_SCHEMES = frozenset({"redis", "rediss"})


class UnsafeTestDatabaseError(RuntimeError):
    """Raised before a destructive fixture can touch an unsafe database."""


def validate_test_database_url(database_url: str) -> str:
    """Return *database_url* only when it names an explicit ``*_test`` DB.

    The check intentionally operates on the decoded final path component and
    rejects query-only or server-level URLs.  Credentials are never included in
    error messages.
    """

    try:
        parsed = urlsplit(database_url)
    except ValueError as exc:
        raise UnsafeTestDatabaseError("TEST_DATABASE_URL is not a valid PostgreSQL URL") from exc

    database_name = unquote(parsed.path.rsplit("/", 1)[-1]).strip()
    if parsed.scheme not in _POSTGRES_SCHEMES or not parsed.hostname or not database_name:
        raise UnsafeTestDatabaseError("TEST_DATABASE_URL must identify a PostgreSQL database")
    query_keys = {key.casefold() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    if query_keys & _TARGET_OVERRIDE_PARAMETERS:
        raise UnsafeTestDatabaseError("TEST_DATABASE_URL must not override connection target parameters")

    # libpq query parameters can change the effective destination.  Parse with
    # psycopg as a second, driver-equivalent check after rejecting target keys.
    from psycopg.conninfo import conninfo_to_dict

    libpq_url = urlunsplit(parsed._replace(scheme="postgresql"))
    try:
        effective = conninfo_to_dict(libpq_url)
    except Exception as exc:
        raise UnsafeTestDatabaseError("TEST_DATABASE_URL is not a valid PostgreSQL URL") from exc
    effective_database_name = str(effective.get("dbname") or "")
    if effective_database_name.casefold() != database_name.casefold():
        raise UnsafeTestDatabaseError("TEST_DATABASE_URL effective database does not match its path")
    if not database_name.casefold().endswith("_test"):
        raise UnsafeTestDatabaseError("destructive tests require a database name ending in '_test'")
    return database_url


def require_destructive_test_database() -> str:
    """Resolve the only database URL destructive tests are allowed to use.

    Missing and unsafe configuration are both hard errors so an integration CI
    job cannot report success after silently skipping its real-service tests.
    """

    database_url = os.environ.get(TEST_DATABASE_URL_ENV, "").strip()
    if not database_url:
        raise UnsafeTestDatabaseError(f"{TEST_DATABASE_URL_ENV} is required for integration tests")
    validate_test_database_url(database_url)
    if os.environ.get(DESTRUCTIVE_TESTS_SENTINEL_ENV) != DESTRUCTIVE_TESTS_SENTINEL_VALUE:
        raise UnsafeTestDatabaseError(
            f"set {DESTRUCTIVE_TESTS_SENTINEL_ENV}={DESTRUCTIVE_TESTS_SENTINEL_VALUE} "
            "to acknowledge destructive test execution"
        )
    return database_url


def validate_test_redis_url(redis_url: str) -> str:
    """Allow destructive Redis tests only on logical databases 8 through 15."""

    try:
        parsed = urlsplit(redis_url)
    except ValueError as exc:
        raise UnsafeTestDatabaseError("TEST_REDIS_URL is not a valid Redis URL") from exc
    query_keys = {key.casefold() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    if query_keys & {"db", "database", "host", "port", "username", "password"}:
        raise UnsafeTestDatabaseError("TEST_REDIS_URL must not override connection target parameters")
    raw_database = parsed.path.removeprefix("/")
    if parsed.scheme not in _REDIS_SCHEMES or not parsed.hostname or not raw_database.isdigit():
        raise UnsafeTestDatabaseError("TEST_REDIS_URL must identify a Redis logical database")
    database = int(raw_database)
    if not 8 <= database <= 15:
        raise UnsafeTestDatabaseError("destructive tests require Redis logical database 8 through 15")
    return redis_url


def require_destructive_test_redis() -> str:
    redis_url = os.environ.get(TEST_REDIS_URL_ENV, "").strip()
    if not redis_url:
        raise UnsafeTestDatabaseError(f"{TEST_REDIS_URL_ENV} is required for integration tests")
    validate_test_redis_url(redis_url)
    if os.environ.get(DESTRUCTIVE_TESTS_SENTINEL_ENV) != DESTRUCTIVE_TESTS_SENTINEL_VALUE:
        raise UnsafeTestDatabaseError(
            f"set {DESTRUCTIVE_TESTS_SENTINEL_ENV}={DESTRUCTIVE_TESTS_SENTINEL_VALUE} "
            "to acknowledge destructive test execution"
        )
    return redis_url


def make_worker_database_url(database_url: str, worker_id: str) -> str:
    """Derive a distinct guarded database name for one pytest-xdist worker."""

    validate_test_database_url(database_url)
    if not _WORKER_ID_PATTERN.fullmatch(worker_id):
        raise UnsafeTestDatabaseError("pytest worker id contains unsupported characters")
    parsed = urlsplit(database_url)
    database_name = unquote(parsed.path.rsplit("/", 1)[-1])
    stem = database_name[: -len("_test")]
    worker_database = f"{stem}_{worker_id}_test"
    parent_path = parsed.path.rsplit("/", 1)[0]
    return urlunsplit(parsed._replace(path=f"{parent_path}/{quote(worker_database)}"))


def make_run_database_url(database_url: str, run_id: str, worker_id: str) -> str:
    """Derive a <=63-byte database name unique to one run and worker."""

    validate_test_database_url(database_url)
    if not run_id or len(run_id) > 256:
        raise UnsafeTestDatabaseError("test run id is missing or too long")
    if not _WORKER_ID_PATTERN.fullmatch(worker_id):
        raise UnsafeTestDatabaseError("pytest worker id contains unsupported characters")
    parsed = urlsplit(database_url)
    base_name = unquote(parsed.path.rsplit("/", 1)[-1])[: -len("_test")]
    safe_stem = re.sub(r"[^a-zA-Z0-9_]+", "_", base_name).strip("_") or "xuanhu"
    digest = hashlib.sha256(f"{run_id}:{worker_id}".encode()).hexdigest()[:16]
    database_name = f"{safe_stem[:38]}_{digest}_test"
    parent_path = parsed.path.rsplit("/", 1)[0]
    return urlunsplit(parsed._replace(path=f"{parent_path}/{database_name}"))


def make_worker_redis_url(redis_url: str, worker_id: str) -> str:
    """Assign one guarded logical Redis DB to an xdist worker (max 8)."""

    validate_test_redis_url(redis_url)
    if worker_id == "main":
        offset = 0
    else:
        match = re.fullmatch(r"gw(\d+)", worker_id)
        if match is None:
            raise UnsafeTestDatabaseError("pytest worker id cannot be mapped to a Redis database")
        offset = int(match.group(1))
    parsed = urlsplit(redis_url)
    selected = int(parsed.path.removeprefix("/")) + offset
    if selected > 15:
        raise UnsafeTestDatabaseError("not enough isolated Redis logical databases for pytest workers")
    return urlunsplit(parsed._replace(path=f"/{selected}"))


@contextmanager
def destructive_database_environment() -> Iterator[str]:
    """Temporarily route application/Alembic configuration to the guarded DB."""

    database_url = require_destructive_test_database()
    previous = os.environ.get("DB_URL")
    os.environ["DB_URL"] = database_url

    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        yield database_url
    finally:
        if previous is None:
            os.environ.pop("DB_URL", None)
        else:
            os.environ["DB_URL"] = previous
        get_settings.cache_clear()
