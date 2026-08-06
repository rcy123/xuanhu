"""pytest 会话级配置。

在测试收集阶段注入必需的默认环境变量，避免模块级 Settings()
导入时因缺少必填配置而抛出 ValidationError。

各测试文件如需验证"缺失必填配置"路径，可在用例内通过
monkeypatch.delenv 清除默认值后创建临时 Settings 实例。
"""

import asyncio
import os
import sys
import uuid
from collections.abc import Awaitable, Callable, Iterator
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

import pytest

from tests._database_safety import (
    TEST_DATABASE_URL_ENV,
    TEST_REDIS_URL_ENV,
    make_run_database_url,
    make_worker_redis_url,
    require_destructive_test_database,
    require_destructive_test_redis,
)


def _set_test_defaults() -> None:
    """设置绝不会命中开发服务的单元测试占位配置。"""
    # Unit tests need syntactically valid application settings during import,
    # but the defaults must never point at a developer service.  Real service
    # tests are marked ``integration`` and use explicit guarded test URLs.
    # Never inherit DB_URL/REDIS_URL from the developer shell.  Integration
    # services are selected exclusively via guarded TEST_* variables below.
    os.environ["DB_URL"] = "postgresql://unit:unit@127.0.0.1:1/xuanhu_unit_test"
    os.environ["REDIS_URL"] = "redis://:unit@127.0.0.1:1/15"
    os.environ["OUTBOX_PUBLISHER_ENABLED"] = "false"
    # 3d: 统一后端后测试会话一律 langgraph(legacy 不再创建);
    # 本地 shell/.env 不得把测试环境切回 legacy 路径。
    os.environ["AGENT_RUNTIME_VERSION"] = "langgraph"
    os.environ["AGENT_RUNTIME_ROLLOUT_PHASE"] = "legacy"
    # 3d: 统一后端后 langgraph 是唯一会话路径,测试默认开启公共创建。
    os.environ["XUANHU_LANGGRAPH_PUBLIC_ENABLED"] = "true"
    os.environ["XUANHU_LANGGRAPH_PRODUCT_READY"] = "false"
    # 2a/2.5: 测试环境默认关闭槽位灰度(除非用例显式 monkeypatch),
    # 避免 .env 的 XUANHU_INTAKE_SLOT_PATH_ENABLED 污染单元测试。
    os.environ["XUANHU_INTAKE_SLOT_PATH_ENABLED"] = "false"
    os.environ.setdefault("MODEL_GATEWAY_BASE_URL", "http://localhost:8080/v1")
    os.environ.setdefault("MODEL_GATEWAY_API_KEY", "sk-test-placeholder")
    os.environ.setdefault("CHAT_MODEL", "test-chat-model")
    os.environ.setdefault("EMBEDDING_MODEL", "test-embedding-model")
    os.environ.setdefault("EMBEDDING_DIM", "768")
    # 测试环境默认关闭 RAG（测试 fakes 按 no-rag 契约编写）。
    os.environ["XUANHU_RAG_ENABLED"] = "false"
    # 测试环境关闭 reasoning 重试退避（避免失败路径测试等待 10s/20s）。
    os.environ["REASONING_RETRY_BACKOFF_BASE_SECONDS"] = "0"


_set_test_defaults()


@pytest.fixture(autouse=True)
def _allow_request_local_langgraph_test_runtime() -> Iterator[None]:
    """Opt the shared test ASGI app into its explicit no-lifespan fallback."""

    from app.main import app

    attribute = "allow_request_local_langgraph_test_runtime"
    had_previous = hasattr(app.state, attribute)
    previous = getattr(app.state, attribute, None)
    setattr(app.state, attribute, True)
    try:
        yield
    finally:
        if had_previous:
            setattr(app.state, attribute, previous)
        else:
            delattr(app.state, attribute)


@pytest.fixture
def enable_public_langgraph(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Explicitly opt API tests into the controlled LangGraph rollout."""
    from app.core.config import get_settings

    monkeypatch.setenv("XUANHU_LANGGRAPH_PUBLIC_ENABLED", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def pytest_asyncio_loop_factories(
    config: pytest.Config,
    item: pytest.Item,
) -> dict[str, Callable[[], asyncio.AbstractEventLoop]]:
    """Create psycopg-compatible selector loops without overriding fixtures."""
    del config, item
    if sys.platform == "win32":
        return {"windows-selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}


def _maintenance_database_url(database_url: str) -> str:
    parsed = urlsplit(database_url)
    scheme = parsed.scheme.split("+", 1)[0]
    return urlunsplit(parsed._replace(scheme=scheme, path="/postgres"))


def _database_name(database_url: str) -> str:
    return unquote(urlsplit(database_url).path.rsplit("/", 1)[-1])


def _drop_worker_database(connection: object, database_name: str) -> None:
    from psycopg import sql

    # ``connection`` is deliberately kept structurally typed here so conftest
    # does not initialise a database driver during ordinary unit collection.
    cursor = connection.cursor()  # type: ignore[attr-defined]
    cursor.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
        (database_name,),
    )
    cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))


def _assert_database_at_head(database_url: str) -> None:
    import psycopg
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    expected = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    with psycopg.connect(database_url) as connection:
        actual = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    if expected is None or actual is None or actual[0] != expected:
        raise AssertionError("integration database did not finish at the Alembic head revision")


def _run_cleanup_awaitable(awaitable: Awaitable[Any]) -> Any:
    """Run loop-agnostic cache cleanup outside pytest-asyncio's fixture graph."""

    loop = asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
    try:
        return loop.run_until_complete(awaitable)
    finally:
        loop.close()


@pytest.fixture(scope="session", autouse=True)
def isolate_integration_services(request: pytest.FixtureRequest) -> Iterator[None]:
    """Provision isolated PG/Redis resources for a pure integration run."""

    integration_items = [item for item in request.session.items if item.get_closest_marker("integration")]
    unit_items = [item for item in request.session.items if item.get_closest_marker("integration") is None]
    if integration_items and unit_items:
        raise pytest.UsageError("unit and integration tests must run in separate pytest processes")
    if not integration_items:
        yield
        return

    base_database_url = require_destructive_test_database()
    base_redis_url = require_destructive_test_redis()
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "main")
    run_id = os.environ.get("PYTEST_XDIST_TESTRUNUID") or os.environ.get("CI_RUN_ID") or uuid.uuid4().hex
    selected_database_url = make_run_database_url(base_database_url, run_id, worker_id)
    selected_redis_url = make_worker_redis_url(base_redis_url, worker_id)
    database_name = _database_name(selected_database_url)

    previous = {
        TEST_DATABASE_URL_ENV: os.environ.get(TEST_DATABASE_URL_ENV),
        TEST_REDIS_URL_ENV: os.environ.get(TEST_REDIS_URL_ENV),
        "DB_URL": os.environ.get("DB_URL"),
        "REDIS_URL": os.environ.get("REDIS_URL"),
    }
    database_created = False
    environment_applied = False
    redis_ready = False

    import psycopg
    from alembic import command
    from alembic.config import Config
    from psycopg import sql
    from redis import Redis

    from app.core.config import get_settings
    from app.core.redis import reset_redis
    from app.db.session import reset_session_factory

    migration_config: Config | None = None

    try:
        with psycopg.connect(_maintenance_database_url(base_database_url), autocommit=True) as connection:
            _drop_worker_database(connection, database_name)
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
        database_created = True

        os.environ[TEST_DATABASE_URL_ENV] = selected_database_url
        os.environ[TEST_REDIS_URL_ENV] = selected_redis_url
        os.environ["DB_URL"] = selected_database_url
        os.environ["REDIS_URL"] = selected_redis_url
        environment_applied = True
        get_settings.cache_clear()
        _run_cleanup_awaitable(reset_session_factory())
        _run_cleanup_awaitable(reset_redis())

        migration_config = Config("alembic.ini")
        migration_config.set_main_option("sqlalchemy.url", selected_database_url.replace("%", "%%"))
        command.upgrade(migration_config, "head")
        _assert_database_at_head(selected_database_url)
        redis = Redis.from_url(selected_redis_url, decode_responses=True, socket_connect_timeout=3)
        try:
            redis.ping()
            redis.flushdb()
            redis_ready = True
        finally:
            redis.close()

        yield
    finally:
        try:
            if environment_applied:
                _run_cleanup_awaitable(reset_redis())
                _run_cleanup_awaitable(reset_session_factory())
            if database_created and migration_config is not None:
                command.upgrade(migration_config, "head")
                _assert_database_at_head(selected_database_url)
            if redis_ready:
                redis = Redis.from_url(selected_redis_url, decode_responses=True, socket_connect_timeout=3)
                try:
                    redis.ping()
                    redis.flushdb()
                finally:
                    redis.close()
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            get_settings.cache_clear()
            if database_created:
                with psycopg.connect(_maintenance_database_url(base_database_url), autocommit=True) as connection:
                    _drop_worker_database(connection, database_name)
