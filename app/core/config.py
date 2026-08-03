"""配置模块 — 基于 pydantic-settings 的统一配置系统。

生产配置统一使用 MODEL_GATEWAY_* 口径，敏感字段通过 safe_dump() 脱敏输出。
"""

from __future__ import annotations

import functools
import re
from typing import Any, Literal
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
    milvus_collection: str = Field(default="xuanhu_knowledge_v3", description="Milvus collection 名称")
    milvus_timeout_seconds: int = Field(default=30, ge=1, description="Milvus 连接超时（秒）")

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
    gateway_health_check_timeout_seconds: int = Field(
        default=10, ge=1, description="网关健康检查超时（秒）"
    )
    model_gateway_max_retries: int = Field(
        default=2, ge=0, description="网关请求最大重试次数"
    )
    model_gateway_route_profile: str = Field(
        default="default", description="网关路由 profile"
    )
    model_gateway_structured_mode: Literal["auto", "tools", "json_object"] = Field(
        default="auto",
        description=(
            "结构化输出模式：tools=工具强制 tool_choice（内网 mimo 等兼容网关）；"
            "json_object=response_format JSON（DeepSeek 等 strict OpenAI 网关，"
            "thinking 模型不支持强制 tool_choice）；auto=按网关域名自动选择"
        ),
    )

    # ---- Embedding 网关覆盖（可选，本地联调用） ----
    # 设置后覆盖 MODEL_GATEWAY_BASE_URL / MODEL_GATEWAY_API_KEY 用于 embedding 调用
    # 未设置时回退到 MODEL_GATEWAY_* 生产口径
    embedding_gateway_base_url: str = Field(
        default="", description="Embedding 专用网关地址（可选，未设置时回退 MODEL_GATEWAY_BASE_URL）"
    )
    embedding_gateway_api_key: str = Field(
        default="", description="Embedding 专用网关 API 密钥（可选，未设置时回退 MODEL_GATEWAY_API_KEY）"
    )
    embedding_gateway_timeout_seconds: int = Field(
        default=0, ge=0, description="Embedding 网关超时（秒），0=回退 MODEL_GATEWAY_TIMEOUT_SECONDS"
    )
    embedding_gateway_max_retries: int = Field(
        default=0, ge=0, description="Embedding 网关最大重试次数，0=回退 MODEL_GATEWAY_MAX_RETRIES"
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

    # ---- RAG 推理链路接入开关 ----
    # 正式接入 RAG：编排层按 policy_version 选 *.rag.v1（见 reasoning_retrieval.stage_rag_enabled）。
    # rag_enabled 为总开关；关闭时辨证/开方维持 no-rag 契约（evidence_mode=model_knowledge_only、
    # confidence 封顶 0.65），与既有行为完全一致。
    rag_enabled: bool = Field(
        default=True,
        validation_alias="XUANHU_RAG_ENABLED",
        description="推理链路 RAG 总开关；关闭时走 no-rag 契约",
    )
    rag_syndrome_enabled: bool = Field(
        default=True,
        description="辨证阶段 RAG 开关（需 rag_enabled=true 才生效）",
    )
    rag_formula_enabled: bool = Field(
        default=True,
        description="开方阶段 RAG 开关（需 rag_enabled=true 才生效）",
    )
    rag_syndrome_top_k: int = Field(default=8, ge=1, le=20, description="辨证检索最终返回条数")
    rag_formula_top_k: int = Field(default=8, ge=1, le=20, description="开方检索最终返回条数")
    rag_query_max_chars: int = Field(default=600, ge=50, description="推理检索 query 最大长度")

    # ---- Agent ----
    agent_runtime_version: Literal["legacy", "langgraph"] = Field(
        default="legacy",
        description=(
            "新会话默认 Agent 运行时。L0-L8 默认 legacy；"
            "该开关不得改变既有会话的运行时身份"
        ),
    )
    langgraph_public_enabled: bool = Field(
        default=False,
        validation_alias="XUANHU_LANGGRAPH_PUBLIC_ENABLED",
        description="允许公共会话创建 API 新建 LangGraph 会话；默认关闭并失败封闭",
    )
    # 2a/2.5a: 采集范式灰度开关。开启后新 session 的 intake_extraction 产出
    # 槽位对象(dimension_slots),covered 判定认槽位齐;关闭则维持裸 fact_key 路径。
    # 灰度观察期稳定后切主路径(2.5a),裸键旁路仅历史 session 兼容读。
    intake_slot_path_enabled: bool = Field(
        default=False,
        validation_alias="XUANHU_INTAKE_SLOT_PATH_ENABLED",
        description="新会话走槽位采集范式(阶段 2 灰度);默认关闭,转正后置 true",
    )
    agent_runtime_rollout_phase: Literal[
        "legacy",
        "development",
        "automated_test",
        "internal",
        "canary",
        "full",
        "rollback",
    ] = Field(
        default="legacy",
        validation_alias="AGENT_RUNTIME_ROLLOUT_PHASE",
        description="L9 新会话切流阶段；full/rollback 启用额外失败封闭约束",
    )
    langgraph_product_ready: bool = Field(
        default=False,
        validation_alias="XUANHU_LANGGRAPH_PRODUCT_READY",
        description="L5-PROD～L8-PROD 与发布门禁已完成的显式授权；默认 false",
    )
    agent_max_retries: int = Field(default=2, ge=0, description="Agent 最大重试次数")
    safety_rollback_limit: int = Field(default=3, ge=1, description="安全审核回退次数上限")
    enable_streaming: bool = Field(default=False, description="是否启用 SSE 流式输出")
    sse_heartbeat_interval_seconds: float = Field(
        default=30.0,
        ge=0.1,
        description="SSE 空闲心跳间隔（秒）",
    )
    prompt_manifest_path: str = Field(
        default="app/agents/prompts/manifest.yaml", description="Prompt 清单文件路径"
    )

    # ---- Durable Outbox Publisher ----
    outbox_publisher_enabled: bool = Field(
        default=True,
        description="启动 PostgreSQL Outbox 到 Redis Stream 的后台发布器",
    )
    outbox_publisher_batch_size: int = Field(default=50, ge=1, le=500)
    outbox_publisher_lease_seconds: int = Field(default=30, ge=1, le=3600)
    outbox_publisher_max_attempts: int = Field(default=8, ge=1, le=100)
    outbox_publisher_base_retry_seconds: int = Field(default=1, ge=0, le=3600)
    outbox_publisher_max_retry_seconds: int = Field(default=300, ge=1, le=86_400)
    outbox_publisher_poll_interval_seconds: float = Field(default=0.5, gt=0, le=60)
    outbox_publisher_shutdown_grace_seconds: float = Field(default=10, gt=0, le=120)
    outbox_ready_max_oldest_age_seconds: float = Field(default=300, ge=0)
    outbox_ready_max_dead_letters: int = Field(default=0, ge=0)
    event_dedupe_ttl_seconds: int = Field(
        default=86_400,
        ge=60,
        le=2_592_000,
        description="Redis Stream 每会话 Outbox 去重窗口的滑动 TTL（秒）",
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


# Agent 单次模型调用超时必须严格大于网关单请求超时
# （``app.agent_runtime.runtime._validate_deadline_invariants`` 前置守卫强制：
# 若 ModelPolicy.timeout < MODEL_GATEWAY_TIMEOUT_SECONDS，模型调用在发出前即被判
# MODEL_GATEWAY_TIMEOUT，整体静默退回模板）。各 Agent 统一按“网关超时 + 余量”推导，
# 避免调整 MODEL_GATEWAY_TIMEOUT_SECONDS 后 agent 在 preflight 秒退。
AGENT_MODEL_TIMEOUT_MARGIN_SECONDS = 15


def agent_model_timeout_seconds() -> int:
    """Derive an Agent ModelPolicy timeout strictly above the gateway timeout."""

    return get_settings().model_gateway_timeout_seconds + AGENT_MODEL_TIMEOUT_MARGIN_SECONDS
