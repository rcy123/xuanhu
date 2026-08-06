"""P1-2 配置系统测试。

覆盖：
- 从环境变量加载必填配置
- 缺失必填配置时报明确错误
- safe_dump 脱敏
- 数值字段类型正确
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings, _is_sensitive, get_settings

# ---------------------------------------------------------------------------
# 辅助：在隔离环境下创建 Settings（不读 .env）
# ---------------------------------------------------------------------------


def _make_settings(**overrides: str) -> Settings:
    """在未污染的环境变量基础上创建 Settings 实例。

    显式传入 ``_env_file=None`` 避免读取本机 .env。
    """
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# _is_sensitive 单元测试
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field_name,expected",
    [
        ("api_key", True),
        ("model_gateway_api_key", True),
        ("password", True),
        ("db_password", True),
        ("secret", True),
        ("some_secret_value", True),
        ("token", True),
        ("access_token", True),
        ("database_url", False),
        ("app_name", False),
        ("embedding_dim", False),
        ("chat_model", False),
    ],
)
def test_is_sensitive_detection(field_name: str, expected: bool) -> None:
    """验证敏感字段关键词检测。"""
    assert _is_sensitive(field_name) is expected


# ---------------------------------------------------------------------------
# 必填配置加载
# ---------------------------------------------------------------------------


REQUIRED_ENV = {
    "DB_URL": "postgresql://user:pass@host/db",
    "REDIS_URL": "redis://host:6379/0",
    "MODEL_GATEWAY_BASE_URL": "http://gw:8080/v1",
    "MODEL_GATEWAY_API_KEY": "sk-abc123",
    "CHAT_MODEL": "gpt-4o",
    "EMBEDDING_MODEL": "text-embed-3",
    "EMBEDDING_DIM": "1536",
}


def test_load_all_required_from_env(monkeypatch) -> None:
    """可从环境变量加载全部必填配置项。"""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.database_url == "postgresql://user:pass@host/db"
    assert settings.redis_url == "redis://host:6379/0"
    assert settings.model_gateway_base_url == "http://gw:8080/v1"
    assert settings.model_gateway_api_key == "sk-abc123"
    assert settings.chat_model == "gpt-4o"
    assert settings.embedding_model == "text-embed-3"
    assert settings.embedding_dim == 1536


def test_load_with_defaults(monkeypatch) -> None:
    """未设置的可选字段应使用默认值。"""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("MILVUS_COLLECTION", raising=False)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.app_env == "local"
    assert settings.app_name == "xuanhu"
    assert settings.api_host == "0.0.0.0"
    assert settings.api_port == 8000
    assert settings.milvus_host == "localhost"
    assert settings.milvus_port == 19530
    assert settings.milvus_collection == "xuanhu_knowledge_v4"
    assert settings.model_gateway_timeout_seconds == 60
    assert settings.model_gateway_max_retries == 2
    assert settings.model_gateway_route_profile == "default"
    assert settings.rag_top_k_vector == 12
    assert settings.agent_max_retries == 2
    assert settings.event_dedupe_ttl_seconds == 86_400


def test_milvus_collection_environment_override(monkeypatch) -> None:
    """显式运行时配置应覆盖代码默认 collection。"""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("MILVUS_COLLECTION", "runtime_override")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.milvus_collection == "runtime_override"


# ---------------------------------------------------------------------------
# 缺失必填配置
# ---------------------------------------------------------------------------


def test_missing_database_url_raises(monkeypatch) -> None:
    """缺少 DB_URL 时应抛出 ValidationError。"""
    # 设置所有必填项，再显式删除目标字段（覆盖 conftest 默认值）
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("DB_URL", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)  # type: ignore[call-arg]

    errors_text = str(exc_info.value).lower()
    assert "database_url" in errors_text or "db_url" in errors_text


def test_missing_model_gateway_api_key_raises(monkeypatch) -> None:
    """缺少 MODEL_GATEWAY_API_KEY 时应抛出 ValidationError。"""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("MODEL_GATEWAY_API_KEY", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)  # type: ignore[call-arg]

    errors_text = str(exc_info.value).lower()
    assert "model_gateway_api_key" in errors_text


def test_missing_embedding_dim_raises(monkeypatch) -> None:
    """缺少 EMBEDDING_DIM 时应抛出 ValidationError。"""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("EMBEDDING_DIM", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)  # type: ignore[call-arg]

    errors_text = str(exc_info.value).lower()
    assert "embedding_dim" in errors_text


# ---------------------------------------------------------------------------
# safe_dump 脱敏
# ---------------------------------------------------------------------------

# 含真实密码的 URL，用于验证脱敏不会泄露
DB_URL_WITH_PASSWORD = "postgresql://app_user:supersecret@dbhost:5432/xuanhu"
REDIS_URL_WITH_PASSWORD = "redis://:redissecret@redishost:6379/0"


def test_safe_dump_masks_api_key_field(monkeypatch) -> None:
    """safe_dump 不得泄露 api_key 明文。"""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    dump = settings.safe_dump()

    assert dump["model_gateway_api_key"] == "***"
    assert dump["chat_model"] == "gpt-4o"
    assert dump["embedding_dim"] == 1536


def test_safe_dump_masks_url_passwords(monkeypatch) -> None:
    """database_url / redis_url 中的密码不得明文出现在 safe_dump 输出中。"""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    # 覆盖为包含可识别密码的 URL
    monkeypatch.setenv("DB_URL", DB_URL_WITH_PASSWORD)
    monkeypatch.setenv("REDIS_URL", REDIS_URL_WITH_PASSWORD)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    dump = settings.safe_dump()

    # 转成字符串便于一次性搜索
    dump_str = str(dump)

    # 密码本身不得出现
    assert "supersecret" not in dump_str, f"DB_URL 密码泄露: {dump_str}"
    assert "redissecret" not in dump_str, f"REDIS_URL 密码泄露: {dump_str}"

    # 掩码后的 URL 应保留结构和 *** 占位
    db_val = dump["database_url"]
    assert isinstance(db_val, str)
    assert "***" in db_val, f"database_url 未掩码: {db_val}"
    assert db_val.startswith("postgresql://"), f"database_url scheme 丢失: {db_val}"
    assert "supersecret" not in db_val

    redis_val = dump["redis_url"]
    assert isinstance(redis_val, str)
    assert "***" in redis_val, f"redis_url 未掩码: {redis_val}"
    assert redis_val.startswith("redis://"), f"redis_url scheme 丢失: {redis_val}"
    assert "redissecret" not in redis_val


def test_safe_dump_url_without_password_unchanged(monkeypatch) -> None:
    """不含密码的 URL 应保持原样。"""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("MODEL_GATEWAY_BASE_URL", "http://gateway:8080/v1")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    dump = settings.safe_dump()

    assert dump["model_gateway_base_url"] == "http://gateway:8080/v1"


def test_safe_dump_masks_all_sensitive_field_names(monkeypatch) -> None:
    """所有字段名包含 api_key/password/secret/token 的值应被完全掩码。"""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    dump = settings.safe_dump()

    for field_name, value in dump.items():
        if _is_sensitive(field_name):
            assert value == "***", f"字段 {field_name} 未脱敏"


def test_safe_dump_does_not_mask_non_sensitive(monkeypatch) -> None:
    """safe_dump 不应误伤非敏感字段。"""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    dump = settings.safe_dump()

    # 以下字段应为明文
    assert dump["app_name"] == "xuanhu"
    assert dump["api_port"] == 8000
    assert dump["milvus_host"] == "localhost"
    assert dump["chat_model"] == "gpt-4o"
    assert dump["embedding_model"] == "text-embed-3"
    assert dump["embedding_dim"] == 1536
    assert isinstance(dump["rag_top_k_vector"], int)


# ---------------------------------------------------------------------------
# 数值字段类型
# ---------------------------------------------------------------------------


def test_numeric_fields_have_correct_types(monkeypatch) -> None:
    """EMBEDDING_DIM / MILVUS_PORT 等字段应为 int 类型。"""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert isinstance(settings.embedding_dim, int)
    assert isinstance(settings.milvus_port, int)
    assert isinstance(settings.api_port, int)
    assert isinstance(settings.model_gateway_timeout_seconds, int)
    assert isinstance(settings.model_gateway_max_retries, int)
    assert isinstance(settings.event_dedupe_ttl_seconds, int)


def test_event_dedupe_ttl_is_configurable_and_bounded(monkeypatch) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("EVENT_DEDUPE_TTL_SECONDS", "120")

    assert Settings(_env_file=None).event_dedupe_ttl_seconds == 120  # type: ignore[call-arg]

    monkeypatch.setenv("EVENT_DEDUPE_TTL_SECONDS", "59")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_numeric_fields_reject_invalid_values(monkeypatch) -> None:
    """数值字段拒绝非数值字符串。"""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    # 将 EMBEDDING_DIM 覆盖为非法字符串
    monkeypatch.setenv("EMBEDDING_DIM", "not-a-number")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# get_settings 缓存
# ---------------------------------------------------------------------------


def test_get_settings_returns_same_instance() -> None:
    """get_settings() 应返回缓存的同一实例。"""
    get_settings.cache_clear()
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2


def test_get_settings_cache_clear(monkeypatch) -> None:
    """cache_clear() 后应返回新实例。"""
    get_settings.cache_clear()
    s1 = get_settings()
    get_settings.cache_clear()
    s2 = get_settings()
    assert s1 is not s2
