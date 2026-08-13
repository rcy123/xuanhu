"""配置模块 — 基于 pydantic-settings 的统一配置系统。

生产配置统一使用 MODEL_GATEWAY_* 口径，敏感字段通过 safe_dump() 脱敏输出。
"""

from __future__ import annotations

import functools
import os
import re
from typing import Annotated, Any, Literal
from urllib.parse import urlparse, urlunparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.metrics import observe_production_secrets_validity

# ---------------------------------------------------------------------------
# 敏感字段检测
# ---------------------------------------------------------------------------

# 命中 api_key / password / secret / token / signing_key 的字段在 safe_dump 中一律掩码。
# 注意：Settings 中所有含 "key" 的字段均为密钥类（api_key / signing_key），
# 不存在需明文展示的业务 key 字段。
_SENSITIVE_RE = re.compile(r"(api_key|password|secret|token|key)", re.IGNORECASE)

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
    api_workers: int = Field(
        default=1,
        ge=1,
        le=16,
        validation_alias="API_WORKERS",
        description=(
            "阶段2 横向扩展：uvicorn worker 进程数；1=单进程，>1=多进程。"
            "命令 worker/outbox publisher 靠 SKIP LOCKED 多进程安全 claim，会话锁靠 PG advisory lock。"
        ),
    )
    db_pool_size: int = Field(
        default=10,
        ge=1,
        le=100,
        description="SQLAlchemy 连接池大小（每进程）；多 worker 时需保证 api_workers×(pool_size+max_overflow) ≤ PG max_connections",
    )
    db_pool_max_overflow: int = Field(
        default=20,
        ge=0,
        le=200,
        description="SQLAlchemy 连接池溢出上限（每进程）",
    )

    # ---- 数据库 ----
    database_url: str = Field(..., alias="DB_URL", description="PostgreSQL 连接串（必填）")
    redis_url: str = Field(..., alias="REDIS_URL", description="Redis 连接串（必填）")

    # ---- Milvus ----
    milvus_host: str = Field(default="localhost", description="Milvus 服务地址")
    milvus_port: int = Field(default=19530, ge=1, le=65535, description="Milvus 端口")
    milvus_collection: str = Field(
        default="xuanhu_knowledge_v4", description="Milvus collection 名称（v4=含 content 字段 M1 直返）"
    )
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
    model_gateway_timeout_seconds: int = Field(default=60, ge=1, description="网关请求超时（秒）")
    gateway_health_check_timeout_seconds: int = Field(default=10, ge=1, description="网关健康检查超时（秒）")
    model_gateway_max_retries: int = Field(default=2, ge=0, description="网关请求最大重试次数")
    model_gateway_route_profile: str = Field(default="default", description="网关路由 profile")
    reasoning_retry_backoff_base_seconds: float = Field(
        default=10.0,
        ge=0,
        description=(
            "reasoning 节点级重试的指数退避基数（秒）：第 n 次重试前等待 "
            "base*n 秒。第三方中转网关对短时高频调用有风控（403），退避可跨过"
            "风控窗口；设 0 关闭（测试用）。"
        ),
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

    # ---- Embedding 缓存 ----
    embedding_cache_ttl_seconds: int = Field(default=3600, ge=0, description="Embedding 向量缓存 TTL（秒），0=不缓存")
    embedding_cache_prewarm_on_startup: bool = Field(
        default=False, description="API 启动时后台预热 embedding 缓存（L1+L2），不阻塞就绪"
    )

    # ---- Reranker 网关覆盖（可选，Cross-Encoder 模式专用） ----
    # 设置后覆盖 MODEL_GATEWAY_BASE_URL / MODEL_GATEWAY_API_KEY 用于 reranker 调用
    # 未设置时回退到 MODEL_GATEWAY_* 生产口径
    reranker_gateway_base_url: str = Field(
        default="", description="Reranker 专用网关地址（可选，未设置时回退 MODEL_GATEWAY_BASE_URL）"
    )
    reranker_gateway_api_key: str = Field(
        default="", description="Reranker 专用网关 API 密钥（可选，未设置时回退 MODEL_GATEWAY_API_KEY）"
    )
    reranker_gateway_timeout_seconds: int = Field(
        default=0, ge=0, description="Reranker 网关超时（秒），0=回退 MODEL_GATEWAY_TIMEOUT_SECONDS"
    )
    reranker_gateway_max_retries: int = Field(
        default=0, ge=0, description="Reranker 网关最大重试次数，0=回退 MODEL_GATEWAY_MAX_RETRIES"
    )

    # ---- 模型名称 ----
    chat_model: str = Field(..., description="对话模型名称（必填）")
    embedding_model: str = Field(..., description="Embedding 模型名称（必填）")
    embedding_dim: int = Field(..., ge=1, description="向量维度，必须与 Milvus collection 一致（必填）")

    # ---- RAG 参数 ----
    rag_top_k_vector: int = Field(default=12, ge=1, description="向量检索 top-k")
    rag_top_k_fulltext: int = Field(default=12, ge=1, description="全文检索 top-k")
    rag_fulltext_lexical_enabled: bool = Field(
        default=True,
        description="是否将中文 Query 拆为短词进行全文候选召回；关闭时保留原整句 FTS/ILIKE 行为",
    )
    rag_fulltext_lexical_max_terms: int = Field(
        default=12,
        ge=1,
        le=32,
        description="中文全文候选召回最多使用的短词数量",
    )
    rag_dual_query_enabled: bool = Field(
        default=False,
        description="是否对辨证阶段的问诊键 Query 与医案改写 Query 做候选级 RRF 融合；默认关闭",
    )
    rag_dual_query_rrf_k: int = Field(
        default=60,
        ge=1,
        le=200,
        description="双视图候选融合的 Reciprocal Rank Fusion 常数",
    )
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

    # ═══════════════════════════════════════════════════════════════
    # P1: 基础方多方案筛选
    # ═══════════════════════════════════════════════════════════════
    base_formula_confidence_threshold: float = Field(
        default=0.45,
        ge=0.0,
        le=1.0,
        description="基础方候选方案的置信度阈值——低于此值的方案被丢弃，不进入 modification",
    )
    base_formula_min_alternatives: int = Field(
        default=1, ge=1, le=4, description="至少保留的基础方候选数（即使低于阈值也保留 top-N）"
    )
    base_formula_max_alternatives: int = Field(default=3, ge=2, le=4, description="最多保留的基础方候选数")

    # ═══════════════════════════════════════════════════════════════
    # P2: Query 改写模型
    # ═══════════════════════════════════════════════════════════════
    rag_query_rewrite_enabled: bool = Field(
        default=False, description="是否启用辨证 query LLM 改写（默认关闭，需主动开启）"
    )
    rag_query_rewrite_model: str = Field(
        default="",
        description=(
            "Query 改写专用模型名称。为空时复用 chat_model。"
            "改写任务不需要医学推理能力，建议使用轻量快速模型（如 qwen3.7-flash 或更小模型）以控制延迟。"
        ),
    )
    rag_query_rewrite_model_temperature: float = Field(
        default=0.1, ge=0.0, le=1.0, description="改写模型 temperature（低温度保证输出稳定、可复现）"
    )
    rag_query_rewrite_model_max_tokens: int = Field(
        default=400, ge=100, le=1000, description="改写模型最大输出 token 数"
    )
    rag_query_rewrite_timeout_seconds: float = Field(
        default=3.0, ge=0.5, le=10.0, description="改写调用超时秒数（超时降级为原始 query）"
    )

    # ---- Query 改写网关覆盖（可选，轻量模型专用） ----
    # 设置后覆盖 MODEL_GATEWAY_BASE_URL / MODEL_GATEWAY_API_KEY 用于 query 改写调用
    # 未设置时回退到 runtime.gateway（主推理网关）
    rag_query_rewrite_gateway_base_url: str = Field(
        default="", description="Query 改写专用网关地址（可选，未设置时回退 runtime.gateway）"
    )
    rag_query_rewrite_gateway_api_key: str = Field(
        default="", description="Query 改写专用网关 API 密钥（可选，未设置时回退 runtime.gateway）"
    )
    rag_query_rewrite_gateway_timeout_seconds: float = Field(
        default=0, ge=0, description="改写网关超时（秒），0=回退 rag_query_rewrite_timeout_seconds"
    )
    rag_query_rewrite_gateway_max_retries: int = Field(
        default=0, ge=0, description="改写网关最大重试次数，0=回退 model_gateway_max_retries"
    )

    # ═══════════════════════════════════════════════════════════════
    # Reranker: Cross-Encoder / LLM Reranker
    # ═══════════════════════════════════════════════════════════════
    rag_reranker_enabled: bool = Field(
        default=False, description="是否启用 Cross-Encoder / LLM Reranker（默认关闭，关闭时使用 MVP 加权求和）"
    )
    rag_reranker_provider: str = Field(
        default="cross_encoder",
        pattern="^(cross_encoder|llm)$",
        description="Reranker 类型：cross_encoder（专用模型）或 llm（LLM 评判）",
    )
    rag_reranker_model: str = Field(
        default="",
        description=(
            "Reranker 模型名称。cross_encoder 模式下为 reranker 模型名（如 BAAI/bge-reranker-v2-m3）；"
            "llm 模式下为 LLM 模型名（为空时复用 chat_model）。"
        ),
    )
    rag_reranker_top_k: int = Field(
        default=20, ge=10, le=50, description="送入 reranker 的候选 chunk 数量（从 ANN 召回中取 top-K）"
    )
    rag_reranker_fulltext_quota: int = Field(
        default=0,
        ge=0,
        le=50,
        description="Cross-Encoder 候选池中保留的全文独有候选数，0=保持原有合并顺序",
    )
    rag_reranker_max_chunks_per_source: int = Field(
        default=0,
        ge=0,
        le=20,
        description="Cross-Encoder 候选池内同一来源最多保留的 chunk 数，0=不做来源多样化",
    )
    rag_reranker_final_top_k: int = Field(default=8, ge=3, le=20, description="Reranker 重排后最终返回的 chunk 数量")
    rag_reranker_timeout_seconds: float = Field(
        default=5.0, ge=1.0, le=15.0, description="Reranker 调用超时秒数（超时降级为 MVP 加权求和）"
    )

    # ---- Agent ----
    agent_runtime_version: Literal["legacy", "langgraph"] = Field(
        default="legacy",
        description=("新会话默认 Agent 运行时。L0-L8 默认 legacy；该开关不得改变既有会话的运行时身份"),
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
    # R9 问题契约编排开关：开启后接线点 A-E 生效（契约创建/绑定/覆盖事件/完整性
    # 投影/残余追问），关闭则全部跳过、与现网行为完全一致。灰度观察期稳定后转正。
    question_contract_enabled: bool = Field(
        default=False,
        validation_alias="XUANHU_QUESTION_CONTRACT_ENABLED",
        description="R9 问题契约编排开关;默认关闭=现网行为不变",
    )
    question_contract_max_followups: int = Field(
        default=2,
        ge=1,
        le=4,
        validation_alias="XUANHU_QUESTION_CONTRACT_MAX_FOLLOWUPS",
        description="R9 根契约允许的残余追问次数上限(契约 revision 上限 = 该值+1)",
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
    prompt_manifest_path: str = Field(default="app/agents/prompts/manifest.yaml", description="Prompt 清单文件路径")

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

    # ---- Durable Async Command Worker (R6-A / R6-B / R7) ----
    # R7 default ON: the three POST endpoints prefer the durable async 202 path
    # and fall back to synchronous only when the worker/runtime/registry is
    # unavailable. This is the operator kill switch: set
    # XUANHU_ASYNC_COMMAND_ENABLED=false to fall back to the synchronous R1-R5
    # path (worker not started => admission never ready => sync fallback).
    async_command_enabled: bool = Field(
        default=True,
        validation_alias="XUANHU_ASYNC_COMMAND_ENABLED",
        description="启动后台 AsyncCommandWorker（R7 默认开启；设 false 为回滚/熔断回同步路径）",
    )
    async_command_batch_size: int = Field(default=10, ge=1, le=200)
    async_command_max_concurrency: int = Field(
        default=8,
        ge=1,
        le=32,
        description="阶段1 并发消费：同一批命令中不同会话可并行处理的上限；同会话命令仍串行（见 async_command_worker.run_once）",
    )
    async_command_lease_seconds: int = Field(default=60, ge=5, le=3600)
    async_command_heartbeat_seconds: float = Field(
        default=20,
        ge=1,
        description="Worker 心跳间隔，必须严格小于 lease（构造时校验）",
    )
    async_command_max_attempts: int = Field(default=8, ge=1, le=100)
    async_command_retry_base_seconds: int = Field(default=1, ge=0, le=3600)
    async_command_retry_max_seconds: int = Field(default=300, ge=1, le=86_400)
    async_command_poll_interval_seconds: float = Field(default=0.5, gt=0, le=60)
    async_command_shutdown_grace_seconds: float = Field(default=10, gt=0, le=120)

    # ---- LangGraph 图执行总超时 ----
    # 一次 advance 串行执行 辨证(≤网关超时+15s) + 基础方(≤网关超时+15s)，
    # 最坏 ≈ 2×(MODEL_GATEWAY_TIMEOUT_SECONDS+15)。默认 300s 覆盖 90s 网关下的
    # 最坏 210s 并留余量；所有 GraphRunner 调用（advance/recovery/review 的
    # shared 与 request-local 路径）必须统一读此配置，避免单点 120s 硬编码
    # 在慢模型下把方剂生成拦腰截断（会话停留在 syndrome 且方剂缺失）。
    graph_runner_timeout_seconds: int = Field(
        default=300,
        ge=60,
        le=3600,
        validation_alias="XUANHU_GRAPH_RUNNER_TIMEOUT_SECONDS",
        description="LangGraph GraphRunner 单次执行总超时（秒）；默认 300",
    )

    # ---- 会话锁与导出 ----
    session_lock_ttl_seconds: int = Field(default=90, ge=1, description="会话锁 TTL（秒）")
    session_lock_wait_seconds: int = Field(default=0, ge=0, description="会话锁等待超时（秒），0 表示不等待")
    export_file_ttl_seconds: int = Field(default=3600, ge=1, description="导出文件 TTL（秒）")

    # ═══════════════════════════════════════════════════════════════
    # 阶段 1：认证授权（docs/04_生产环境加固/01-认证授权与密钥轮换.md）
    # ═══════════════════════════════════════════════════════════════
    jwt_signing_key: str = Field(
        default="",
        description="JWT 签名密钥（HS256 对称密钥；生产环境缺失或占位符即 fail-fast）",
    )
    jwt_signing_key_previous: str = Field(
        default="",
        description="双密钥灰度期的旧签名密钥（可选；校验失败时回退尝试，颁发永远只用新 key）",
    )
    jwt_access_token_ttl_seconds: int = Field(
        default=28_800,
        ge=60,
        le=86_400,
        description="JWT access token 有效期（秒）；默认 8 小时，过期重新登录",
    )
    xuanhu_auth_enabled: Literal["off", "audit", "on"] = Field(
        default="on",
        validation_alias="XUANHU_AUTH_ENABLED",
        description=(
            "阶段 1 认证三态开关：off=不生效（灰度回退态）/ audit=校验但仅记审计不阻断"
            "（观察期）/ on=生效且阻断（正式态）。生产稳定后必须移除 off/audit 分支"
        ),
    )
    login_fail_lock_threshold: int = Field(
        default=5,
        ge=1,
        le=20,
        description="同一医师连续登录失败锁定阈值（计数存 Redis，key=auth:fail:{doctor_id}）",
    )
    login_fail_lock_seconds: int = Field(
        default=900,
        ge=60,
        le=86_400,
        description="登录失败锁定时长（秒）；默认 15 分钟",
    )

    # ═══════════════════════════════════════════════════════════════
    # 阶段 2：PHI 访问控制（docs/04_生产环境加固/02-PHI访问控制与日志脱敏.md）
    # ═══════════════════════════════════════════════════════════════
    xuanhu_access_enabled: Literal["off", "audit", "on"] = Field(
        default="on",
        validation_alias="XUANHU_ACCESS_ENABLED",
        description=(
            "阶段 2 会话所有权校验三态开关：off=不生效 / audit=校验但仅记审计不阻断 / "
            "on=生效且阻断（403/404）。生产稳定后必须移除 off/audit 分支"
        ),
    )

    # ═══════════════════════════════════════════════════════════════
    # 阶段 3：基础设施（docs/04_生产环境加固/03-基础设施与网络边界加固.md）
    # ═══════════════════════════════════════════════════════════════
    xuanhu_prod_secret_guard: bool = Field(
        default=True,
        validation_alias="XUANHU_PROD_SECRET_GUARD",
        description="生产环境密钥占位符 fail-fast 守卫；紧急时可关闭但需事后审计",
    )
    milvus_use_tls: bool = Field(
        default=False,
        description="Milvus 使用 TLS（内网明文可接受，跨机部署再开启；默认 false）",
    )

    # ═══════════════════════════════════════════════════════════════
    # 阶段 4：运行态安全（docs/04_生产环境加固/04-运行态安全加固.md）
    # ═══════════════════════════════════════════════════════════════
    xuanhu_ratelimit_enabled: bool = Field(
        default=True,
        validation_alias="XUANHU_RATELIMIT_ENABLED",
        description="阶段 4 限流总开关；紧急时可关闭但需事后审计",
    )
    login_rate_limit_per_minute: int = Field(
        default=10,
        ge=1,
        le=600,
        description="登录接口按 IP 限流（次/分钟）",
    )
    advance_rate_limit_per_minute: int = Field(
        default=20,
        ge=1,
        le=600,
        description="advance 接口按医师限流（次/分钟）",
    )
    write_rate_limit_per_minute: int = Field(
        default=60,
        ge=1,
        le=600,
        description="其余写接口按医师限流（次/分钟）",
    )
    stream_concurrent_limit: int = Field(
        default=5,
        ge=1,
        le=100,
        description="SSE 按医师并发连接上限",
    )
    trust_proxy_headers: bool = Field(
        default=True,
        validation_alias="TRUST_PROXY_HEADERS",
        description=(
            "登录限流是否信任反向代理的 X-Forwarded-For 头（阶段3 nginx 注入）；"
            "关闭后回退直连 IP，避免攻击者伪造头绕过限流"
        ),
    )
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"],
        validation_alias="CORS_ALLOWED_ORIGINS",
        description="CORS 允许来源（逗号分隔）；生产禁止通配符 *（allow_credentials=True 组合被浏览器规范禁止）",
    )
    model_whitelist: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        validation_alias="MODEL_WHITELIST",
        description="允许的模型名白名单（逗号分隔）；生产为空即 fail-fast，防止模型名被篡改指向任意端点",
    )

    # -------------------------------------------------------------------
    # 校验
    # -------------------------------------------------------------------

    @field_validator("cors_allowed_origins", "model_whitelist", mode="before")
    @classmethod
    def _split_csv_lists(cls, value: Any) -> Any:
        """把逗号分隔的 env 字符串解析为 list[str]。

        pydantic-settings 对复杂类型默认尝试 JSON 解码；当 env 值是纯逗号
        分隔串（如 ``a,b,c``）时 JSON 解码失败，这里在 before 阶段拆分为
        list，两种形态（``'["a","b"]'`` 与 ``'a,b'``）都兼容。
        """
        if isinstance(value, str):
            parts = [item.strip() for item in value.split(",") if item.strip()]
            return parts or []
        return value

    @model_validator(mode="after")
    def async_command_invariants(self) -> Settings:
        """Heartbeat must stay strictly below the lease for the R6-A worker."""
        if self.async_command_enabled and self.async_command_heartbeat_seconds >= self.async_command_lease_seconds:
            raise ValueError("async_command_heartbeat_seconds must be strictly less than async_command_lease_seconds")
        return self

    @model_validator(mode="after")
    def cors_no_wildcard_origin(self) -> Settings:
        """M2：禁止通配来源 ``*``。

        ``Access-Control-Allow-Origin: *`` 与 ``allow_credentials=True`` 的
        组合被浏览器规范直接拒绝；即便未来改为不携带凭据，通配来源也会让
        任何站点都能读取响应，破坏同源边界。因此无论是否携带凭据都禁止
        ``*``——显式白名单是唯一合法形态。
        """
        if "*" in self.cors_allowed_origins:
            raise ValueError("CORS_ALLOWED_ORIGINS must not contain '*' (wildcard origin is forbidden)")
        return self

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
# 生产环境密钥守卫（T1.6 / T3.3）
# ---------------------------------------------------------------------------

# 占位符 / 已知默认值：命中即视为不合规。生产环境命中任一即启动失败。
_PLACEHOLDER_SECRET_MARKERS = re.compile(
    r"(change-me|changeme|placeholder|your[-_]?(password|key|secret)|example|"
    r"minioadmin|xuanhu_dev|sk-test|sk-ci|sk-change|^test$|^dev$|^secret$|^password$)",
    re.IGNORECASE,
)

# Settings 中的密钥字段（safe_dump 会脱敏；守卫校验其值）
_SETTINGS_SECRET_FIELDS = (
    "model_gateway_api_key",
    "embedding_gateway_api_key",
    "reranker_gateway_api_key",
    "rag_query_rewrite_gateway_api_key",
    "jwt_signing_key",
)

# Compose 中间件凭据（不在 Settings 中，读取环境变量）
_ENV_SECRET_FIELDS = (
    "POSTGRES_PASSWORD",
    "REDIS_PASSWORD",
    "MINIO_ACCESS_KEY",
    "MINIO_SECRET_KEY",
)


def _looks_like_placeholder(value: str) -> bool:
    """判定密钥值是否为占位符 / 已知默认值。"""
    if not value:
        return True
    return bool(_PLACEHOLDER_SECRET_MARKERS.search(value))


def validate_production_secrets(settings: Settings) -> list[str]:
    """检查生产环境密钥合规性，返回不合规项列表（空列表=通过）。

    覆盖 Settings 密钥字段（*_api_key / jwt_signing_key）、Compose 中间件
    凭据（POSTGRES_PASSWORD / REDIS_PASSWORD / MINIO_*）以及 M5 模型白名单
    （MODEL_WHITELIST）。生产环境任一为空、占位符或已知默认值即启动失败；
    本地/测试环境不触发。
    """
    violations: list[str] = []
    for field_name in _SETTINGS_SECRET_FIELDS:
        value = getattr(settings, field_name) or ""
        if _looks_like_placeholder(str(value)):
            violations.append(field_name.upper())
    # HS256 签名密钥最小强度：RFC 7518 建议 ≥32 字节；文档 01 §2.1 要求
    # openssl rand -base64 48（48 字节）。短密钥虽非占位符，但可被暴力
    # 枚举，生产环境视为不合规。
    jwt_key = str(getattr(settings, "jwt_signing_key", "") or "")
    if jwt_key and len(jwt_key.encode("utf-8")) < 32:
        violations.append("JWT_SIGNING_KEY_TOO_SHORT")
    for env_name in _ENV_SECRET_FIELDS:
        value = os.environ.get(env_name, "")
        if _looks_like_placeholder(value):
            violations.append(env_name)
    if settings.app_env == "production" and not settings.model_whitelist:
        # M5：生产禁止空白名单——空列表等于「任何模型名都放行」，无法兜底
        # 模型名被篡改指向任意端点。fail-fast 强制运维显式配置白名单。
        violations.append("MODEL_WHITELIST")
    return violations


def ensure_production_secrets_ready(settings: Settings | None = None) -> None:
    """生产环境 fail-fast 守卫：任一密钥不合规即抛 RuntimeError。

    仅当 ``APP_ENV=production`` 且 ``XUANHU_PROD_SECRET_GUARD != false``
    时生效；其余环境为 no-op。守卫失败时先把 ``production_secrets_invalid``
    指标置 1（Prometheus 立即告警），再抛异常终止启动。
    """
    effective = settings or get_settings()
    if effective.app_env != "production":
        observe_production_secrets_validity(True)
        return
    if not effective.xuanhu_prod_secret_guard:
        observe_production_secrets_validity(True)
        return
    violations = validate_production_secrets(effective)
    if violations:
        observe_production_secrets_validity(False)
        raise RuntimeError("生产环境密钥守卫失败：以下密钥缺失/为占位符/为默认值/强度不足，禁止启动 —— " + ", ".join(violations))
    observe_production_secrets_validity(True)


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
