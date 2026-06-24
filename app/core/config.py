"""配置模块 — 基于 pydantic-settings 的统一配置系统。

生产配置统一使用 MODEL_GATEWAY_* 口径，敏感字段通过 safe_dump() 脱敏输出。
"""

from __future__ import annotations

import functools
import re
from typing import Any
from urllib.parse import urlparse, urlunparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# 敏感字段检测
# ---------------------------------------------------------------------------

_SENSITIVE_RE = re.compile(r"(api_key|password|secret|token)", re.IGNORECASE)

# 需对 URL 内嵌密码做掩码的字段名后缀
_URL_FIELD_SUFFIXES = ("_url",)


def _is_sensitive(field_name: str) -> bool:
    """若字段名命中 api_key / password / secret / token 则视为敏感字段。"""
    return bool(_SENSITIVE_RE.search(field_name))


def _mask_url_password(value: str) -> str:
    """对 URL 中 userinfo 的 password 部分做掩码。

    例如 ``postgresql://user:supersecret@host/db``
    会被转为 ``postgresql://user:***@host/db``。

    若解析失败或 URL 不含密码则原样返回。
    """
    try:
        parsed = urlparse(value)
    except Exception:
        return value

    if parsed.password is None:
        return value

    # 分离 userinfo 和 hostpart
    netloc = parsed.netloc
    if "@" not in netloc:
        return value

    userinfo, hostpart = netloc.split("@", 1)
    if ":" in userinfo:
        user = userinfo.rsplit(":", 1)[0]
        masked_netloc = f"{user}:***@{hostpart}"
    else:
        # userinfo 不含冒号但 netloc 含 @ —— 理论上不会同时满足 password is not None，
        # 保留防御性处理。
        masked_netloc = f"{userinfo}:***@{hostpart}"

    parsed = parsed._replace(netloc=masked_netloc)
    return urlunparse(parsed)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """悬壶（Xuanhu）统一配置。

    加载优先级（由高到低）：
    1. 环境变量
    2. .env 文件
    3. 字段默认值
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- 应用运行配置 ----
    app_env: str = Field(default="local", description="运行环境: local / staging / production")
    app_name: str = Field(default="xuanhu", description="应用名称")
    app_version: str = Field(default="0.1.0", description="应用版本号")
    api_host: str = Field(default="0.0.0.0", description="API 监听地址")
    api_port: int = Field(default=8000, ge=1, le=65535, description="API 监听端口")

    # ---- 数据库 ----
    database_url: str = Field(..., alias="DB_URL", description="PostgreSQL 连接串（必填）")
    redis_url: str = Field(..., alias="REDIS_URL", description="Redis 连接串（必填）")

    # ---- Milvus ----
    milvus_host: str = Field(default="localhost", description="Milvus 服务地址")
    milvus_port: int = Field(default=19530, ge=1, le=65535, description="Milvus 端口")
    milvus_collection: str = Field(default="xuanhu_knowledge", description="Milvus collection 名称")

    # ---- 模型网关（生产统一口径） ----
    model_gateway_base_url: str = Field(
        ...,
        description="内网模型网关地址（必填）",
    )
    model_gateway_api_key: str = Field(
        ...,
        description="模型网关 API 密钥（必填，日志必须脱敏）",
    )
    model_gateway_timeout_seconds: int = Field(
        default=60, ge=1, description="网关请求超时（秒）"
    )
    model_gateway_max_retries: int = Field(
        default=2, ge=0, description="网关请求最大重试次数"
    )
    model_gateway_route_profile: str = Field(
        default="default", description="网关路由 profile"
    )

    # ---- 模型名称 ----
    chat_model: str = Field(..., description="对话模型名称（必填）")
    embedding_model: str = Field(..., description="Embedding 模型名称（必填）")
    embedding_dim: int = Field(
        ..., ge=1, description="向量维度，必须与 Milvus collection 一致（必填）"
    )

    # ---- RAG 参数 ----
    rag_top_k_vector: int = Field(default=12, ge=1, description="向量检索 top-k")
    rag_top_k_fulltext: int = Field(default=12, ge=1, description="全文检索 top-k")
    rag_top_n_final: int = Field(default=8, ge=1, description="合并去重后最终返回条数")

    # ---- Agent ----
    agent_max_retries: int = Field(default=2, ge=0, description="Agent 最大重试次数")
    safety_rollback_limit: int = Field(default=3, ge=1, description="安全审核回退次数上限")
    enable_streaming: bool = Field(default=False, description="是否启用 SSE 流式输出")
    prompt_manifest_path: str = Field(
        default="app/agents/prompts/manifest.yaml", description="Prompt 清单文件路径"
    )

    # ---- 会话锁与导出 ----
    session_lock_ttl_seconds: int = Field(default=90, ge=1, description="会话锁 TTL（秒）")
    session_lock_wait_seconds: int = Field(default=0, ge=0, description="会话锁等待超时（秒），0 表示不等待")
    export_file_ttl_seconds: int = Field(default=3600, ge=1, description="导出文件 TTL（秒）")

    # -------------------------------------------------------------------
    # 脱敏
    # -------------------------------------------------------------------

    def safe_dump(self) -> dict[str, Any]:
        """返回脱敏后的配置字典。

        两层防护：
        1. 字段名命中 api_key / password / secret / token → 值替换为 ``"***"``
        2. 字段名以 ``_url`` 结尾且值为 URL → 掩码 URL 内嵌密码（如
           ``postgresql://user:secret@host/db`` → ``postgresql://user:***@host/db``）
        其余字段原样返回。
        """
        masked: dict[str, Any] = {}
        for field_name, value in self.model_dump().items():
            if _is_sensitive(field_name):
                masked[field_name] = "***"
            elif isinstance(value, str) and field_name.endswith(_URL_FIELD_SUFFIXES):
                masked[field_name] = _mask_url_password(value)
            else:
                masked[field_name] = value
        return masked


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取缓存的 Settings 实例。

    首次调用时从环境变量和 .env 加载配置，后续调用返回同一实例。
    测试中可通过 ``get_settings.cache_clear()`` 清除缓存后重新加载。
    """
    return Settings()  # type: ignore[call-arg]  # 必填字段由环境变量/.env 提供
