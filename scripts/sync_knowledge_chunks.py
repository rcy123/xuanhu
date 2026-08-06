"""Knowledge Chunks 生成与 Milvus 向量同步脚本 — P2-3.

用法::

    uv run python -m scripts.sync_knowledge_chunks --build-chunks
    uv run python -m scripts.sync_knowledge_chunks --sync-vectors
    uv run python -m scripts.sync_knowledge_chunks --all --reindex-all --target-collection xuanhu_knowledge_v2
    uv run python -m scripts.sync_knowledge_chunks --all
    uv run python -m scripts.sync_knowledge_chunks --all --source-type herbs --limit 5
    uv run python -m scripts.sync_knowledge_chunks --all --dry-run
    uv run python -m scripts.sync_knowledge_chunks --check-consistency

幂等保证：
- 同一 source_type + source_id + content 的 content_hash 稳定复算
- active chunk 唯一约束 (source_type, source_id, content_hash) WHERE deleted_at IS NULL
- 重复 build-chunks 不产生重复 active chunk
- vector_id = knowledge_chunks.id::text，PG 与 Milvus 主键一致

Milvus payload：vector_id / chunk_id / source_type / source_id / title / content_hash / content（T3.5/M1）
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import logging
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import String, cast, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embedding_gateway import build_embedding_gateway_settings
from app.db.session import get_session_factory

# ---------------------------------------------------------------------------
# 项目根路径
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
CHUNK_SIZE_MIN = 500
CHUNK_SIZE_MAX = 800
CHUNK_OVERLAP_MIN = 50
CHUNK_OVERLAP_MAX = 100
# text-embedding-v4 的同步 API 单次最多接受 10 条文本。
EMBED_BATCH_SIZE = 10

# 一致性检查重试配置
CONSISTENCY_RETRY_MAX = 3
CONSISTENCY_RETRY_DELAY_SECONDS = 3.0

# 同步报告中脱敏的错误摘要最大长度
MAX_ERROR_SUMMARY_LENGTH = 200

logger = logging.getLogger("xuanhu.sync_chunks")


# ===================================================================
# 报告数据结构
# ===================================================================


@dataclass
class ChunkBuildStats:
    """单个类型的 chunk 生成统计。"""

    source_type: str
    sources_processed: int = 0
    chunks_created: int = 0
    chunks_skipped: int = 0  # content_hash 未变
    chunks_deleted: int = 0  # 内容变化后软删除的旧 chunk
    errors: int = 0


@dataclass
class VectorSyncStats:
    """向量同步统计。"""

    source_type: str
    chunks_pending: int = 0
    chunks_embedded: int = 0
    chunks_failed: int = 0
    vectors_inserted: int = 0
    vectors_deleted: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class ConsistencyResult:
    """一致性检查结果。"""

    pg_done_chunks: int = 0
    milvus_vectors: int = 0
    matched: int = 0
    pg_missing_in_milvus: list[str] = field(default_factory=list)
    milvus_orphans: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def is_consistent(self) -> bool:
        return len(self.pg_missing_in_milvus) == 0 and len(self.milvus_orphans) == 0 and len(self.errors) == 0


# ===================================================================
# 工具函数
# ===================================================================


def compute_content_hash(source_type: str, source_id: str, content: str) -> str:
    """计算 content_hash = SHA-256(source_type:source_id:content)。

    对同一三元组稳定复算，重复执行不产生重复 active chunk。
    """
    payload = f"{source_type}:{source_id}:{content}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def split_chinese_text(
    text: str,
    chunk_size: int = 600,
    overlap: int = 80,
) -> list[str]:
    """将中文长文本按句子边界切分为重叠 chunk。

    Args:
        text: 待切分的中文文本。
        chunk_size: 目标 chunk 大小（字符数），应在 500–800 范围。
        overlap: 相邻 chunk 重叠字符数，应在 50–100 范围。

    Returns:
        切分后的文本片段列表。若原文短于 chunk_size 则返回单元素列表。
    """
    if len(text) <= chunk_size:
        return [text]

    chunks: list[str] = []
    start = 0
    sentence_breaks = set('。！？；\n）」》》"”')

    while start < len(text):
        end = start + chunk_size
        if end >= len(text):
            chunks.append(text[start:])
            break

        # 尝试在 chunk 后半段找句子边界
        window = text[start:end]
        search_start = max(0, len(window) - 200)
        found_break = False
        for i in range(len(window) - 1, search_start - 1, -1):
            if window[i] in sentence_breaks:
                end = start + i + 1
                found_break = True
                break

        if not found_break:
            # 回退：找任意标点
            for i in range(len(window) - 1, search_start - 1, -1):
                if window[i] in "，、；：·,;: ":
                    end = start + i + 1
                    break

        chunks.append(text[start:end])
        start = end - overlap

    return chunks


def _sanitize_error(exc: Exception) -> str:
    """脱敏错误摘要：截断长度，不泄露 API key 或完整响应。"""
    msg = f"{type(exc).__name__}"
    exc_str = str(exc)
    if exc_str:
        msg += f": {exc_str[:MAX_ERROR_SUMMARY_LENGTH]}"
    return msg


# ===================================================================
# Chunk 生成
# ===================================================================


class ChunkBuilder:
    """从 PostgreSQL 主数据生成 knowledge_chunks。"""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------

    async def build_all(
        self,
        *,
        source_type: str | None = None,
        limit: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, ChunkBuildStats]:
        """对所有（或指定）来源类型生成 chunk。

        Returns:
            dict[source_type, ChunkBuildStats]
        """
        types = self._resolve_types(source_type)
        stats: dict[str, ChunkBuildStats] = {}

        for st in types:
            stats[st] = await self._build_for_type(st, limit=limit, dry_run=dry_run)

        return stats

    async def build_for_source(
        self,
        source_type: str,
        source_id: uuid.UUID,
        title: str,
        doc_text: str,
        extra_meta: dict[str, Any] | None = None,
        *,
        dry_run: bool = False,
    ) -> ChunkBuildStats:
        """为单个来源记录生成 chunk（供外部调用和测试使用）。

        Args:
            source_type: 来源类型 (herb/formula/acupoint/theory/case)。
            source_id: 原始记录 UUID。
            title: chunk 标题。
            doc_text: 原始文档文本。
            extra_meta: 附加元数据。
            dry_run: 仅统计不写入。

        Returns:
            ChunkBuildStats 单条记录的统计。
        """
        stats = ChunkBuildStats(source_type=source_type, sources_processed=1)
        chunks = self._generate_chunks(source_type, str(source_id), title, doc_text, extra_meta)

        async with self._session_factory() as session:
            try:
                for chunk_content, chunk_title in chunks:
                    content_hash = compute_content_hash(source_type, str(source_id), chunk_content)

                    # 检查是否已存在 active chunk
                    existing = await self._find_active_chunk(session, source_type, source_id, content_hash)
                    if existing:
                        stats.chunks_skipped += 1
                        continue

                    if not dry_run:
                        await self._insert_chunk(
                            session,
                            source_type=source_type,
                            source_id=source_id,
                            title=chunk_title,
                            content=chunk_content,
                            content_hash=content_hash,
                            extra_meta=extra_meta or {},
                        )
                    stats.chunks_created += 1

                # 软删除不再匹配的旧 chunk（内容变化导致 hash 不同）
                if not dry_run and stats.chunks_created > 0:
                    deleted = await self._soft_delete_stale_chunks(session, source_type, source_id, chunks)
                    stats.chunks_deleted = deleted

                if not dry_run:
                    await session.commit()
            except Exception:
                if not dry_run:
                    await session.rollback()
                stats.errors += 1
                raise

        return stats

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _resolve_types(self, source_type: str | None) -> list[str]:
        if source_type:
            if source_type == "theory_cases":
                return ["theory", "case"]
            if source_type in ("theory", "case"):
                return [source_type]
            return [source_type]
        return ["herb", "formula", "acupoint", "theory", "case"]

    async def _build_for_type(
        self,
        source_type: str,
        *,
        limit: int | None = None,
        dry_run: bool = False,
    ) -> ChunkBuildStats:
        """对单个来源类型全量生成 chunk。"""
        stats = ChunkBuildStats(source_type=source_type)

        async with self._session_factory() as session:
            records = await self._fetch_source_records(session, source_type, limit)
            for record in records:
                stats.sources_processed += 1
                try:
                    single_stats = await self.build_for_source(
                        source_type=record["source_type"],
                        source_id=record["id"],
                        title=record["title"],
                        doc_text=record["doc_text"],
                        extra_meta=record.get("extra_meta"),
                        dry_run=dry_run,
                    )
                    stats.chunks_created += single_stats.chunks_created
                    stats.chunks_skipped += single_stats.chunks_skipped
                    stats.chunks_deleted += single_stats.chunks_deleted
                except Exception:
                    stats.errors += 1
                    logger.exception(
                        "chunk 生成失败: source_type=%s, source_id=%s",
                        source_type,
                        record["id"],
                    )

        return stats

    async def _fetch_source_records(
        self, session: AsyncSession, source_type: str, limit: int | None
    ) -> list[dict[str, Any]]:
        """查询来源表获取待生成 chunk 的记录。"""
        from app.models.knowledge import Acupoint, Formula, Herb, TheoryCase

        records: list[dict[str, Any]] = []
        if source_type in ("theory", "case"):
            stmt = (
                select(
                    TheoryCase.id,
                    TheoryCase.entry_type,
                    TheoryCase.title,
                    TheoryCase.doc_text,
                    TheoryCase.extra_meta,
                )
                .where(TheoryCase.entry_type == source_type)
                .where(TheoryCase.deleted_at.is_(None))
                .order_by(TheoryCase.title)
            )
            if limit is not None:
                stmt = stmt.limit(limit)
            result = await session.execute(stmt)
            for row in result.all():
                records.append(
                    {
                        "source_type": row.entry_type,
                        "id": row.id,
                        "title": row.title,
                        "doc_text": row.doc_text,
                        "extra_meta": row.extra_meta,
                    }
                )
            return records

        model_cls: type[Herb] | type[Formula] | type[Acupoint]
        if source_type == "herb":
            model_cls = Herb
        elif source_type == "formula":
            model_cls = Formula
        elif source_type == "acupoint":
            model_cls = Acupoint
        else:
            raise ValueError(f"unsupported source_type: {source_type}")

        stmt_by_name = (
            select(
                model_cls.id,
                model_cls.name.label("title"),
                model_cls.doc_text,
            )
            .where(model_cls.deleted_at.is_(None))
            .order_by(model_cls.name)
        )
        if limit is not None:
            stmt_by_name = stmt_by_name.limit(limit)
        result = await session.execute(stmt_by_name)
        for row in result.all():
            records.append(
                {
                    "source_type": source_type,
                    "id": row.id,
                    "title": row.title,
                    "doc_text": row.doc_text,
                    "extra_meta": {},
                }
            )

        return records

    def _generate_chunks(
        self,
        source_type: str,
        source_id: str,
        title: str,
        doc_text: str,
        extra_meta: dict[str, Any] | None = None,
    ) -> list[tuple[str, str]]:
        """从 doc_text 生成 (content, title) 列表。

        theory/case 长文本按中文句子边界切分；其余类型至少 1 条 chunk。
        """
        if not doc_text or not doc_text.strip():
            return []

        if source_type in ("theory", "case"):
            chunks_text = split_chinese_text(
                doc_text.strip(),
                chunk_size=CHUNK_SIZE_MAX,
                overlap=CHUNK_OVERLAP_MAX,
            )
        else:
            chunks_text = [doc_text.strip()]

        results: list[tuple[str, str]] = []
        for i, chunk_text in enumerate(chunks_text):
            chunk_title = title if len(chunks_text) == 1 else f"{title} (第{i + 1}段)"
            results.append((chunk_text, chunk_title))

        return results

    async def _find_active_chunk(
        self, session: Any, source_type: str, source_id: uuid.UUID, content_hash: str
    ) -> Any | None:
        """查找 active（未软删除）chunk。"""
        from app.models.knowledge import KnowledgeChunk

        stmt = (
            select(KnowledgeChunk.id)
            .where(KnowledgeChunk.source_type == source_type)
            .where(KnowledgeChunk.source_id == source_id)
            .where(KnowledgeChunk.content_hash == content_hash)
            .where(KnowledgeChunk.deleted_at.is_(None))
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _insert_chunk(
        self,
        session: Any,
        *,
        source_type: str,
        source_id: uuid.UUID,
        title: str,
        content: str,
        content_hash: str,
        extra_meta: dict[str, Any],
    ) -> Any:
        """插入新 chunk，状态为 pending。"""
        from app.models.knowledge import KnowledgeChunk

        chunk = KnowledgeChunk(
            source_type=source_type,
            source_id=source_id,
            title=title,
            content=content,
            extra_meta=extra_meta,
            content_hash=content_hash,
            embedding_status="pending",
        )
        session.add(chunk)
        await session.flush()
        return chunk

    async def _soft_delete_stale_chunks(
        self,
        session: Any,
        source_type: str,
        source_id: uuid.UUID,
        new_chunks: list[tuple[str, str]],
    ) -> int:
        """软删除同一 source 下不再出现在新 chunk 集合中的旧 active chunk。"""
        from app.models.knowledge import KnowledgeChunk

        new_hashes = {compute_content_hash(source_type, str(source_id), content) for content, _title in new_chunks}

        # 获取所有 active chunk 的 hash
        stmt = (
            select(KnowledgeChunk.id, KnowledgeChunk.content_hash)
            .where(KnowledgeChunk.source_type == source_type)
            .where(KnowledgeChunk.source_id == source_id)
            .where(KnowledgeChunk.deleted_at.is_(None))
        )
        result = await session.execute(stmt)
        existing = result.all()

        stale_ids = []
        for chunk_id, existing_hash in existing:
            if existing_hash not in new_hashes:
                stale_ids.append(chunk_id)

        if stale_ids:
            now = datetime.now(UTC).replace(tzinfo=None)
            stmt_update = update(KnowledgeChunk).where(KnowledgeChunk.id.in_(stale_ids)).values(deleted_at=now)
            await session.execute(stmt_update)

        return len(stale_ids)


# ===================================================================
# 向量同步
# ===================================================================


class VectorSyncer:
    """将 chunk 向量化并写入指定 Milvus collection。"""

    def __init__(
        self,
        session_factory: Any,
        milvus_client: Any,
        *,
        embedding_model: str | None = None,
        embedding_dim: int | None = None,
        collection_name: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._milvus = milvus_client
        self._settings = _get_settings_safe()
        self._embedding_model = embedding_model or self._settings.embedding_model
        self._embedding_dim = embedding_dim or self._settings.embedding_dim
        self._collection_name = collection_name or self._settings.milvus_collection

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------

    async def ensure_collection(self, *, require_empty: bool = False) -> bool:
        """创建或验证 Milvus collection。

        若 collection 已存在则验证维度一致；否则按 EMBEDDING_DIM 创建。
        ``require_empty=True`` 时对已有 collection 额外执行 fail-closed 空集检查，
        用于安全的全量重建，避免覆盖已有向量。
        返回 True 表示 collection 可用。
        """
        collection_name = self._collection_name
        dim = self._embedding_dim

        if self._milvus.has_collection(collection_name):
            if require_empty and not self._existing_collection_is_empty():
                return False

            # 验证已有 collection 维度
            try:
                desc = self._milvus.describe_collection(collection_name)
                existing_dim = None
                for field in desc.get("fields", []):
                    if field.get("name") == "embedding":
                        existing_dim = field.get("params", {}).get("dim")
                        break
                if existing_dim is not None and existing_dim != dim:
                    logger.error(
                        "Milvus collection 维度不一致: collection=%s, 已有=%d, 配置 EMBEDDING_DIM=%d",
                        collection_name,
                        existing_dim,
                        dim,
                    )
                    return False
            except Exception:
                logger.exception("无法获取 Milvus collection 描述: %s", collection_name)
                return False
            logger.info("Milvus collection 已存在: %s (dim=%d)", collection_name, dim)
            return True

        # 创建 collection
        try:
            from pymilvus import DataType

            schema = self._milvus.create_schema()
            schema.add_field(
                field_name="vector_id",
                datatype=DataType.VARCHAR,
                is_primary=True,
                max_length=128,
            )
            schema.add_field(
                field_name="chunk_id",
                datatype=DataType.VARCHAR,
                max_length=128,
            )
            schema.add_field(
                field_name="source_type",
                datatype=DataType.VARCHAR,
                max_length=32,
            )
            schema.add_field(
                field_name="source_id",
                datatype=DataType.VARCHAR,
                max_length=128,
            )
            schema.add_field(
                field_name="title",
                datatype=DataType.VARCHAR,
                max_length=512,
            )
            schema.add_field(
                field_name="content_hash",
                datatype=DataType.VARCHAR,
                max_length=128,
            )
            # T3.5/M1: 把 chunk 原文存进 Milvus，检索时 output_fields 直返 content，
            # 省去 PG 回填往返。chunks p99=783 / max=800 chars → max_length=4096 余量充足。
            # 旧 collection 无该字段时运行时 _collection_has_content 探测自动回退。
            schema.add_field(
                field_name="content",
                datatype=DataType.VARCHAR,
                max_length=4096,
            )
            schema.add_field(
                field_name="embedding",
                datatype=DataType.FLOAT_VECTOR,
                dim=dim,
            )

            self._milvus.create_collection(
                collection_name=collection_name,
                schema=schema,
                index_params=self._milvus.prepare_index_params()
                if hasattr(self._milvus, "prepare_index_params")
                else None,
            )

            # 创建向量索引
            index_params = (
                self._milvus.prepare_index_params() if hasattr(self._milvus, "prepare_index_params") else None
            )
            if index_params is not None:
                index_params.add_index(
                    field_name="embedding",
                    index_type="HNSW",
                    metric_type="COSINE",
                    params={"M": 16, "efConstruction": 200},
                )
            else:
                # Fallback for older pymilvus
                index_params = {
                    "field_name": "embedding",
                    "index_type": "HNSW",
                    "metric_type": "COSINE",
                    "params": {"M": 16, "efConstruction": 200},
                }

            self._milvus.create_index(
                collection_name=collection_name,
                index_params=index_params,
            )
            self._milvus.load_collection(collection_name)
            logger.info(
                "Milvus collection 已创建: %s (dim=%d, metric=COSINE)",
                collection_name,
                dim,
            )
            return True
        except Exception:
            logger.exception("创建 Milvus collection 失败: %s", collection_name)
            return False

    def _existing_collection_is_empty(self) -> bool:
        """Fail closed：仅在能够确认目标 collection 行数为零时返回 True。"""
        collection_name = self._collection_name
        try:
            stats = self._milvus.get_collection_stats(collection_name=collection_name)
            row_count = int(stats["row_count"])
        except (KeyError, TypeError, ValueError):
            logger.exception("无法确认全量重建目标 collection 是否为空: %s", collection_name)
            return False
        except Exception:
            logger.exception("读取全量重建目标 collection 统计失败: %s", collection_name)
            return False

        if row_count != 0:
            logger.error(
                "拒绝全量重建到非空 collection: collection=%s, row_count=%d",
                collection_name,
                row_count,
            )
            return False
        return True

    async def sync_all(
        self,
        *,
        source_type: str | None = None,
        limit: int | None = None,
        dry_run: bool = False,
        reindex_all: bool = False,
    ) -> dict[str, VectorSyncStats]:
        """同步 chunk 到 Milvus。

        默认只同步 active pending chunk；``reindex_all=True`` 时选择 active
        pending + done + failed chunk，且在 collection 一致性通过前不更新
        PostgreSQL embedding 状态。

        Returns:
            dict[source_type, VectorSyncStats]
        """
        types = self._resolve_types(source_type)
        stats: dict[str, VectorSyncStats] = {}

        for st in types:
            stats[st] = await self._sync_for_type(
                st,
                limit=limit,
                dry_run=dry_run,
                reindex_all=reindex_all,
            )

        return stats

    async def sync_pending_batch(
        self,
        chunks: list[dict[str, Any]],
        *,
        dry_run: bool = False,
        update_pg_status: bool = True,
    ) -> VectorSyncStats:
        """同步一批 chunk（供测试和批量处理使用）。

        按 source_type + source_id 分组，每组先删旧向量再插入新向量。

        Args:
            chunks: 包含 id, source_type, source_id, title, content, content_hash 的 chunk 列表。
            dry_run: 仅统计不写入。
            update_pg_status: 是否按同步结果更新 PostgreSQL embedding 状态。

        Returns:
            VectorSyncStats
        """
        stats = VectorSyncStats(source_type="batch")
        stats.chunks_pending = len(chunks)

        if not chunks:
            return stats

        # 分组
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for c in chunks:
            key = (c["source_type"], c["source_id"])
            groups.setdefault(key, []).append(c)

        # 收集所有文本用于批量 embedding
        texts = [c["content"] for c in chunks]

        try:
            embeddings = await self._embed_batch(texts, dry_run=dry_run)
        except Exception as exc:
            stats.errors.append(_sanitize_error(exc))
            if not dry_run and update_pg_status:
                await self._mark_chunks_failed([c["id"] for c in chunks], _sanitize_error(exc))
            stats.chunks_failed = len(chunks)
            return stats

        # 逐个分组处理
        for (_st, _sid), group_chunks in groups.items():
            try:
                group_stats = await self._sync_group(
                    group_chunks,
                    embeddings,
                    texts,
                    dry_run=dry_run,
                    update_pg_status=update_pg_status,
                )
                stats.chunks_embedded += group_stats.chunks_embedded
                stats.chunks_failed += group_stats.chunks_failed
                stats.vectors_inserted += group_stats.vectors_inserted
                stats.vectors_deleted += group_stats.vectors_deleted
                stats.errors.extend(group_stats.errors)
            except Exception as exc:
                stats.errors.append(_sanitize_error(exc))
                stats.chunks_failed += len(group_chunks)

        return stats

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _resolve_types(self, source_type: str | None) -> list[str]:
        if source_type:
            if source_type == "theory_cases":
                return ["theory", "case"]
            if source_type in ("theory", "case"):
                return [source_type]
            return [source_type]
        return ["herb", "formula", "acupoint", "theory", "case"]

    async def _sync_for_type(
        self,
        source_type: str,
        *,
        limit: int | None = None,
        dry_run: bool = False,
        reindex_all: bool = False,
    ) -> VectorSyncStats:
        """读取选定状态的 active chunk 并同步到 Milvus。"""
        from app.models.knowledge import KnowledgeChunk

        stats = VectorSyncStats(source_type=source_type)

        async with self._session_factory() as session:
            stmt = select(KnowledgeChunk).where(KnowledgeChunk.source_type == source_type)
            if reindex_all:
                stmt = stmt.where(
                    KnowledgeChunk.embedding_status.in_(("pending", "done", "failed"))
                )
            else:
                stmt = stmt.where(KnowledgeChunk.embedding_status == "pending")
            stmt = stmt.where(KnowledgeChunk.deleted_at.is_(None)).order_by(KnowledgeChunk.created_at)
            if limit is not None:
                stmt = stmt.limit(limit)

            result = await session.execute(stmt)
            selected_chunks = result.scalars().all()

            if not selected_chunks:
                logger.info(
                    "source_type=%s: 无%s chunk",
                    source_type,
                    " active pending/done/failed" if reindex_all else " pending",
                )
                return stats

            chunk_dicts = [
                {
                    "id": c.id,
                    "source_type": c.source_type,
                    "source_id": str(c.source_id),
                    "title": c.title,
                    "content": c.content,
                    "content_hash": c.content_hash,
                }
                for c in selected_chunks
            ]

            batch_stats = await self.sync_pending_batch(
                chunk_dicts,
                dry_run=dry_run,
                update_pg_status=not reindex_all,
            )
            stats.chunks_pending = batch_stats.chunks_pending
            stats.chunks_embedded = batch_stats.chunks_embedded
            stats.chunks_failed = batch_stats.chunks_failed
            stats.vectors_inserted = batch_stats.vectors_inserted
            stats.vectors_deleted = batch_stats.vectors_deleted
            stats.errors = batch_stats.errors

        return stats

    async def _sync_group(
        self,
        chunks: list[dict[str, Any]],
        all_embeddings: list[list[float]],
        all_texts: list[str],
        *,
        dry_run: bool = False,
        update_pg_status: bool = True,
    ) -> VectorSyncStats:
        """同步一组（同一 source_type + source_id）chunk。

        策略：先删除 Milvus 中该 source 的全部旧向量，再插入新向量。
        """
        stats = VectorSyncStats(source_type="group")

        if not chunks:
            return stats

        collection_name = self._collection_name

        # 建立 content → embedding 映射
        content_to_embedding: dict[str, list[float]] = {}
        for t_idx, text in enumerate(all_texts):
            if text not in content_to_embedding:
                content_to_embedding[text] = all_embeddings[t_idx]

        # 1. 删除旧向量（仅删除当前批次涉及的 vector_id，不影响同 source 已 done 的其他 chunk）
        if not dry_run:
            try:
                vector_ids = [str(c["id"]) for c in chunks]
                # Milvus filter: vector_id in ["id1", "id2", ...]
                ids_json = ", ".join(f'"{vid}"' for vid in vector_ids)
                delete_result = self._milvus.delete(
                    collection_name=collection_name,
                    filter=f"vector_id in [{ids_json}]",
                )
                # delete_result may be a dict or DeleteResult; handle both
                if isinstance(delete_result, dict):
                    deleted_count = delete_result.get("delete_count", 0)
                elif hasattr(delete_result, "delete_count"):
                    deleted_count = delete_result.delete_count
                else:
                    deleted_count = 0
                stats.vectors_deleted += deleted_count
            except Exception as exc:
                logger.warning(
                    "Milvus 删除旧向量失败（可能因为首次同步无旧向量）: %s",
                    _sanitize_error(exc),
                )

        # 2. 插入新向量
        rows_to_insert: list[dict[str, Any]] = []
        chunk_ids_ok: list[uuid.UUID] = []
        chunk_ids_fail: list[uuid.UUID] = []

        for chunk in chunks:
            content = chunk["content"]
            embedding = content_to_embedding.get(content)
            if embedding is None:
                chunk_ids_fail.append(chunk["id"])
                stats.errors.append(f"chunk {chunk['id']} 无对应 embedding")
                continue

            rows_to_insert.append(
                {
                    "vector_id": str(chunk["id"]),
                    "chunk_id": str(chunk["id"]),
                    "source_type": chunk["source_type"],
                    "source_id": chunk["source_id"],
                    "title": chunk["title"][:512],
                    "content_hash": chunk["content_hash"],
                    "content": content[:4096],
                    "embedding": embedding,
                }
            )
            chunk_ids_ok.append(chunk["id"])

        if rows_to_insert and not dry_run:
            try:
                self._milvus.insert(
                    collection_name=collection_name,
                    data=rows_to_insert,
                )
                stats.vectors_inserted = len(rows_to_insert)
            except Exception as exc:
                stats.errors.append(_sanitize_error(exc))
                chunk_ids_fail.extend(chunk_ids_ok)
                chunk_ids_ok = []

        # 3. 更新 PG 状态
        if chunk_ids_ok and not dry_run and update_pg_status:
            await self._mark_chunks_done(chunk_ids_ok)
        stats.chunks_embedded = len(chunk_ids_ok)

        if chunk_ids_fail and not dry_run and update_pg_status:
            await self._mark_chunks_failed(
                chunk_ids_fail,
                "; ".join(stats.errors[-3:]) if stats.errors else "Milvus 写入失败",
            )
        stats.chunks_failed = len(chunk_ids_fail)

        return stats

    async def _embed_batch(
        self,
        texts: list[str],
        *,
        dry_run: bool = False,
    ) -> list[list[float]]:
        """调用模型网关批量 embedding。

        若 dry_run，返回与 EMBEDDING_DIM 一致的伪向量。

        配置读取优先级（高→低）：
        1. shell 环境变量 EMBEDDING_GATEWAY_BASE_URL / EMBEDDING_GATEWAY_API_KEY
        2. .env 文件中的 EMBEDDING_GATEWAY_*
        3. MODEL_GATEWAY_* 生产口径（回退）
        """
        if dry_run:
            return [[0.0] * self._embedding_dim for _ in texts]

        from app.core.gateway import ModelGatewayClient

        # 读取 embedding 专用网关配置（Settings 已处理 .env + shell 环境变量优先级）
        settings = self._settings
        gateway_settings = build_embedding_gateway_settings(settings)

        if gateway_settings is not settings:
            # 使用 embedding 专用网关 — 轻量 settings 代理不会修改全局单例
            client = ModelGatewayClient(settings=gateway_settings)
        else:
            client = ModelGatewayClient()

        all_embeddings: list[list[float]] = []

        for i in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[i : i + EMBED_BATCH_SIZE]
            try:
                embeddings = await client.embed(
                    batch,
                    model=self._embedding_model,
                    trace_id=f"sync-chunks-{datetime.now(UTC).timestamp()}",
                )
                # 校验维度
                for emb in embeddings:
                    if len(emb) != self._embedding_dim:
                        raise ValueError(f"Embedding 维度不一致: 期望 {self._embedding_dim}, 实际 {len(emb)}")
                all_embeddings.extend(embeddings)
            except Exception:
                logger.exception("embedding 批处理失败: batch_size=%d", len(batch))
                raise

        return all_embeddings

    async def _mark_chunks_done(self, chunk_ids: list[uuid.UUID]) -> None:
        """标记 chunk 为 done，逐条更新确保 vector_id 字符串类型正确。"""
        from app.models.knowledge import KnowledgeChunk

        async with self._session_factory() as session:
            now = datetime.now(UTC).replace(tzinfo=None)
            for cid in chunk_ids:
                stmt = (
                    update(KnowledgeChunk)
                    .where(KnowledgeChunk.id == cid)
                    .values(
                        embedding_status="done",
                        embedding_model=self._embedding_model,
                        vector_id=str(cid),  # vector_id = id::text (字符串)
                        embedded_at=now,
                    )
                )
                await session.execute(stmt)
            await session.commit()

    async def _mark_chunks_failed(self, chunk_ids: list[uuid.UUID], error_summary: str) -> None:
        """标记 chunk 为 failed，错误摘要写入 extra_meta。"""
        from app.models.knowledge import KnowledgeChunk

        async with self._session_factory() as session:
            for cid in chunk_ids:
                stmt = (
                    update(KnowledgeChunk)
                    .where(KnowledgeChunk.id == cid)
                    .values(
                        embedding_status="failed",
                        extra_meta={"embedding_error": error_summary[:MAX_ERROR_SUMMARY_LENGTH]},
                    )
                )
                await session.execute(stmt)
            await session.commit()

    async def finalize_reindex_statuses(self) -> int:
        """Promote active pending/failed chunks only after reindex consistency passes.

        A full reindex writes to a new collection without touching PostgreSQL
        statuses.  This final transaction makes newly indexed chunks visible to
        the PostgreSQL full-text fallback and records the exact embedding model.
        """
        from app.models.knowledge import KnowledgeChunk

        async with self._session_factory() as session:
            now = datetime.now(UTC).replace(tzinfo=None)
            stmt = (
                update(KnowledgeChunk)
                .where(KnowledgeChunk.deleted_at.is_(None))
                .where(KnowledgeChunk.embedding_status.in_(("pending", "failed")))
                .values(
                    embedding_status="done",
                    embedding_model=self._embedding_model,
                    vector_id=cast(KnowledgeChunk.id, String),
                    embedded_at=now,
                )
            )
            result = await session.execute(stmt)
            await session.commit()
            return int(result.rowcount or 0)


# ===================================================================
# 一致性检查
# ===================================================================


async def check_consistency(
    session_factory: Any,
    milvus_client: Any,
    *,
    collection_name: str | None = None,
    reindex_all: bool = False,
) -> ConsistencyResult:
    """检查 PG knowledge_chunks 与 Milvus 的一致性。

    - 默认要求 PG active done chunk 与 Milvus 对应。
    - 全量重建时要求 PG 所有 active chunk 与目标 collection 对应。
    - Milvus 中不应存在不在对应 PG 选择集内的 orphan 向量。

    Returns:
        ConsistencyResult
    """
    from app.models.knowledge import KnowledgeChunk

    settings = _get_settings_safe()
    selected_collection = collection_name or settings.milvus_collection
    result = ConsistencyResult()

    # 1. 获取 PG 中所有目标 chunk
    async with session_factory() as session:
        stmt = select(KnowledgeChunk.vector_id, KnowledgeChunk.id, KnowledgeChunk.source_type)
        if reindex_all:
            stmt = stmt.where(
                KnowledgeChunk.embedding_status.in_(("pending", "done", "failed"))
            )
        else:
            stmt = stmt.where(KnowledgeChunk.embedding_status == "done")
        stmt = stmt.where(KnowledgeChunk.deleted_at.is_(None))
        rows = await session.execute(stmt)
        pg_done: dict[str, dict[str, Any]] = {}
        for row in rows.all():
            vid = str(row.id)  # vector_id = id::text
            pg_done[vid] = {
                "chunk_id": str(row.id),
                "source_type": row.source_type,
                "vector_id": vid,
            }
        result.pg_done_chunks = len(pg_done)

    # 2. 查询 Milvus 中所有向量
    if not milvus_client.has_collection(selected_collection):
        result.errors.append(f"Milvus collection 不存在: {selected_collection}")
        return result

    try:
        milvus_vectors: dict[str, dict[str, Any]] = {}
        # 分批查询
        offset = 0
        page_size = 1000
        while True:
            query_result = milvus_client.query(
                collection_name=selected_collection,
                filter="vector_id != ''",
                output_fields=["vector_id", "chunk_id", "source_type"],
                offset=offset,
                limit=page_size,
            )
            if not query_result:
                break
            for row in query_result:
                milvus_vectors[row["vector_id"]] = row
            if len(query_result) < page_size:
                break
            offset += page_size

        result.milvus_vectors = len(milvus_vectors)
    except Exception as exc:
        result.errors.append(_sanitize_error(exc))
        return result

    # 3. 对比
    for vid in pg_done:
        if vid not in milvus_vectors:
            result.pg_missing_in_milvus.append(vid)

    for vid in milvus_vectors:
        if vid not in pg_done:
            result.milvus_orphans.append(vid)

    result.matched = result.pg_done_chunks - len(result.pg_missing_in_milvus)

    return result


async def check_consistency_with_retry(
    session_factory: Any,
    milvus_client: Any,
    *,
    max_retries: int = CONSISTENCY_RETRY_MAX,
    retry_delay: float = CONSISTENCY_RETRY_DELAY_SECONDS,
    collection_name: str | None = None,
    reindex_all: bool = False,
) -> ConsistencyResult:
    """带重试的 PG ↔ Milvus 一致性检查。

    解决 Milvus 写入后短暂可见性延迟造成的误报 mismatch。
    每次重试前执行 flush 确保数据可见。

    Args:
        session_factory: PG 会话工厂。
        milvus_client: Milvus 客户端。
        max_retries: 最大重试次数（首次尝试 + N 次重试）。
        retry_delay: 重试间隔（秒）。
        collection_name: 显式目标 collection；默认使用 Settings。
        reindex_all: 是否按 active pending + done 口径检查。

    Returns:
        ConsistencyResult — 首次一致或最终重试结果。
    """
    settings = _get_settings_safe()
    selected_collection = collection_name or settings.milvus_collection

    for attempt in range(max_retries + 1):
        if attempt > 0:
            # 重试前 flush collection 确保数据可见
            with contextlib.suppress(Exception):
                milvus_client.flush(collection_name=selected_collection)
            logger.info(
                "一致性检查重试 %d/%d（等待 %.1f 秒）...",
                attempt,
                max_retries,
                retry_delay,
            )
            await asyncio.sleep(retry_delay)

        if collection_name is None and not reindex_all:
            result = await check_consistency(session_factory, milvus_client)
        else:
            result = await check_consistency(
                session_factory,
                milvus_client,
                collection_name=selected_collection,
                reindex_all=reindex_all,
            )

        if result.is_consistent:
            if attempt > 0:
                logger.info("一致性检查重试后通过（第 %d 次尝试）", attempt + 1)
            return result

        if attempt < max_retries:
            logger.warning(
                "一致性检查不一致（第 %d 次尝试），PG done=%d, Milvus=%d, matched=%d, missing=%d, orphan=%d",
                attempt + 1,
                result.pg_done_chunks,
                result.milvus_vectors,
                result.matched,
                len(result.pg_missing_in_milvus),
                len(result.milvus_orphans),
            )

    logger.error("一致性检查在 %d 次重试后仍未通过", max_retries + 1)
    return result


# ===================================================================
# 工具函数
# ===================================================================


def _get_settings_safe() -> Any:
    """获取 Settings，若环境变量缺失则给出明确提示。"""
    from app.core.config import get_settings

    try:
        return get_settings()
    except Exception as exc:
        print(f"[ERROR] 配置加载失败: {exc}")
        print("请确保 .env 或环境变量中已配置必填字段：")
        print("  DB_URL, REDIS_URL, MODEL_GATEWAY_BASE_URL, MODEL_GATEWAY_API_KEY,")
        print("  CHAT_MODEL, EMBEDDING_MODEL, EMBEDDING_DIM")
        sys.exit(1)


def _create_milvus_client() -> Any:
    """创建 Milvus 客户端。"""
    settings = _get_settings_safe()
    try:
        from pymilvus import MilvusClient

        return MilvusClient(
            uri=f"http://{settings.milvus_host}:{settings.milvus_port}",
            timeout=30,
        )
    except ImportError:
        print("[ERROR] 请安装 pymilvus: uv add pymilvus")
        sys.exit(1)
    except Exception as exc:
        print(f"[ERROR] 无法连接 Milvus ({settings.milvus_host}:{settings.milvus_port}): {exc}")
        sys.exit(1)


# ===================================================================
# CLI
# ===================================================================


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Knowledge Chunks 生成与 Milvus 向量同步",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--build-chunks",
        action="store_true",
        help="从 PG 主数据生成 knowledge_chunks",
    )
    group.add_argument(
        "--sync-vectors",
        action="store_true",
        help="将 pending chunk 向量化并写入 Milvus",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="等价于 --build-chunks + --sync-vectors + --check-consistency",
    )
    group.add_argument(
        "--check-consistency",
        action="store_true",
        help="检查 PG knowledge_chunks 与 Milvus 的一致性",
    )

    parser.add_argument(
        "--source-type",
        type=str,
        default=None,
        choices=["herbs", "formulas", "acupoints", "theory_cases", "herb", "formula", "acupoint", "theory", "case"],
        help="仅处理指定类型",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="限制处理的来源记录数（用于 smoke test）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅统计不写入",
    )
    parser.add_argument(
        "--reindex-all",
        action="store_true",
        help="将所有 active chunk 重建到显式的新/空 collection，通过一致性检查后再更新 PG 状态",
    )
    parser.add_argument(
        "--target-collection",
        type=str,
        default=None,
        help="--reindex-all 的显式目标 Milvus collection",
    )
    return parser


def _validate_reindex_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """校验全量重建参数，避免隐式选择或覆盖 collection。"""
    if not args.reindex_all:
        if args.target_collection is not None:
            parser.error("--target-collection 只能与 --reindex-all 一起使用")
        return

    if not args.all:
        parser.error("--reindex-all 必须与 --all 一起使用，以强制执行一致性检查和状态收口")
    if args.target_collection is None or not args.target_collection.strip():
        parser.error("--reindex-all 必须显式指定 --target-collection")
    if args.source_type is not None or args.limit is not None:
        parser.error("--reindex-all 必须处理全部 active chunk，不能与 --source-type 或 --limit 一起使用")
    if args.dry_run:
        parser.error("--reindex-all 不支持 --dry-run；dry-run 不得隐式创建 collection")

    args.target_collection = args.target_collection.strip()


def _normalize_source_type(raw: str | None) -> str | None:
    """统一 source_type 参数：herbs→herb, formulas→formula, acupoints→acupoint。"""
    if raw is None:
        return None
    mapping = {"herbs": "herb", "formulas": "formula", "acupoints": "acupoint", "theory_cases": "theory_cases"}
    return mapping.get(raw, raw)


def _print_build_stats(all_stats: dict[str, ChunkBuildStats]) -> None:
    print("\n========== Chunk 生成统计 ==========")
    total_created = 0
    total_skipped = 0
    total_deleted = 0
    total_errors = 0
    for st, s in sorted(all_stats.items()):
        print(
            f"  {st:12s}: {s.sources_processed:3d} sources | "
            f"{s.chunks_created:3d} created | {s.chunks_skipped:3d} skipped | "
            f"{s.chunks_deleted:3d} deleted | {s.errors:3d} errors"
        )
        total_created += s.chunks_created
        total_skipped += s.chunks_skipped
        total_deleted += s.chunks_deleted
        total_errors += s.errors
    print(
        f"  {'TOTAL':12s}:           | "
        f"{total_created:3d} created | {total_skipped:3d} skipped | "
        f"{total_deleted:3d} deleted | {total_errors:3d} errors"
    )
    print("====================================\n")


def _print_sync_stats(all_stats: dict[str, VectorSyncStats]) -> None:
    print("\n========== 向量同步统计 ==========")
    total_pending = 0
    total_embedded = 0
    total_failed = 0
    total_inserted = 0
    total_deleted = 0
    for st, s in sorted(all_stats.items()):
        print(
            f"  {st:12s}: {s.chunks_pending:3d} pending | "
            f"{s.chunks_embedded:3d} embedded | {s.chunks_failed:3d} failed | "
            f"{s.vectors_inserted:3d} inserted | {s.vectors_deleted:3d} deleted"
        )
        total_pending += s.chunks_pending
        total_embedded += s.chunks_embedded
        total_failed += s.chunks_failed
        total_inserted += s.vectors_inserted
        total_deleted += s.vectors_deleted
        for err in s.errors[:3]:  # 只展示前 3 条错误摘要
            print(f"           error: {err[:120]}")
        if len(s.errors) > 3:
            print(f"           ... 另有 {len(s.errors) - 3} 条错误")
    print(
        f"  {'TOTAL':12s}: {total_pending:3d} pending | "
        f"{total_embedded:3d} embedded | {total_failed:3d} failed | "
        f"{total_inserted:3d} inserted | {total_deleted:3d} deleted"
    )
    print("==================================\n")


def _print_consistency(result: ConsistencyResult) -> None:
    print("\n========== PG <-> Milvus 一致性检查 ==========")
    print(f"  PG done chunks:     {result.pg_done_chunks}")
    print(f"  Milvus vectors:     {result.milvus_vectors}")
    print(f"  Matched:            {result.matched}")
    if result.pg_missing_in_milvus:
        print(f"  PG→Milvus 缺失:     {len(result.pg_missing_in_milvus)} 条")
        for vid in result.pg_missing_in_milvus[:10]:
            print(f"    - {vid}")
        if len(result.pg_missing_in_milvus) > 10:
            print(f"    ... 另有 {len(result.pg_missing_in_milvus) - 10} 条")
    if result.milvus_orphans:
        print(f"  Milvus orphan:      {len(result.milvus_orphans)} 条")
        for vid in result.milvus_orphans[:10]:
            print(f"    - {vid}")
        if len(result.milvus_orphans) > 10:
            print(f"    ... 另有 {len(result.milvus_orphans) - 10} 条")
    if result.errors:
        print(f"  检查错误:           {len(result.errors)} 条")
        for err in result.errors[:5]:
            print(f"    - {err[:150]}")
    if result.is_consistent:
        print("  结论: [OK] 一致")
    else:
        print("  结论: [MISMATCH] 不一致，需重建或排查")
    print("==============================================\n")


async def _main() -> int:
    """主流程，返回退出码（0=成功, 1=失败）。"""
    parser = _build_parser()
    args = parser.parse_args()
    _validate_reindex_args(parser, args)

    # 默认执行 --all
    if not any([args.build_chunks, args.sync_vectors, args.all, args.check_consistency]):
        args.all = True

    source_type = _normalize_source_type(args.source_type)

    settings = _get_settings_safe()

    # 连接 PG
    session_factory = get_session_factory()
    logger.info("PostgreSQL 连接工厂已就绪")

    # 连接 Milvus（build-chunks 不需要）
    milvus_client = None
    need_milvus = args.sync_vectors or args.all or args.check_consistency
    if need_milvus:
        milvus_client = _create_milvus_client()
        logger.info("Milvus 客户端已连接: %s:%d", settings.milvus_host, settings.milvus_port)

    exit_code = 0

    try:
        # --build-chunks
        if args.build_chunks or args.all:
            print(
                f"\n[build-chunks] 开始生成 chunks (source_type={source_type}, limit={args.limit}, dry_run={args.dry_run})"
            )
            builder = ChunkBuilder(session_factory)
            build_stats = await builder.build_all(
                source_type=source_type,
                limit=args.limit,
                dry_run=args.dry_run,
            )
            _print_build_stats(build_stats)

        # --sync-vectors
        if args.sync_vectors or args.all:
            if milvus_client is None:
                milvus_client = _create_milvus_client()

            print(
                "\n[sync-vectors] 开始向量同步 "
                f"(source_type={source_type}, limit={args.limit}, dry_run={args.dry_run}, "
                f"reindex_all={args.reindex_all}, target_collection={args.target_collection})"
            )
            if args.reindex_all:
                syncer = VectorSyncer(
                    session_factory,
                    milvus_client,
                    collection_name=args.target_collection,
                )
            else:
                syncer = VectorSyncer(session_factory, milvus_client)

            # 确保 collection 存在
            if args.reindex_all:
                ok = await syncer.ensure_collection(require_empty=True)
            else:
                ok = await syncer.ensure_collection()
            if not ok:
                print("[ERROR] 无法创建或安全验证 Milvus collection，同步中止")
                return 1

            if args.reindex_all:
                sync_stats = await syncer.sync_all(
                    source_type=source_type,
                    limit=args.limit,
                    dry_run=args.dry_run,
                    reindex_all=True,
                )
            else:
                sync_stats = await syncer.sync_all(
                    source_type=source_type,
                    limit=args.limit,
                    dry_run=args.dry_run,
                )
            _print_sync_stats(sync_stats)

            # 检查同步是否有失败
            total_failed = sum(s.chunks_failed for s in sync_stats.values())
            if total_failed > 0:
                print(f"[ERROR] 向量同步失败: {total_failed} chunks failed")
                exit_code = 1

        # --check-consistency
        if args.check_consistency or args.all:
            if milvus_client is None:
                milvus_client = _create_milvus_client()

            print("\n[check-consistency] 开始一致性检查")
            # --all 模式下使用重试机制，避免 Milvus 可见性延迟误报
            if args.all:
                if args.reindex_all:
                    result = await check_consistency_with_retry(
                        session_factory,
                        milvus_client,
                        collection_name=args.target_collection,
                        reindex_all=True,
                    )
                else:
                    result = await check_consistency_with_retry(session_factory, milvus_client)
            else:
                result = await check_consistency(session_factory, milvus_client)
            _print_consistency(result)

            if not result.is_consistent:
                print("[ERROR] PG <-> Milvus 一致性检查不通过")
                exit_code = 1
            elif args.reindex_all and exit_code == 0:
                # 只有新 collection 已覆盖全部 active chunk 后，才把新建或
                # 先前失败的 chunk 晋升为 done，避免向量失败时破坏旧库可用性。
                finalized = await syncer.finalize_reindex_statuses()
                print(f"[reindex-finalize] PostgreSQL 状态收口完成: {finalized} chunks")

                final_result = await check_consistency_with_retry(
                    session_factory,
                    milvus_client,
                    collection_name=args.target_collection,
                )
                _print_consistency(final_result)
                if not final_result.is_consistent:
                    print("[ERROR] reindex 状态收口后一致性检查不通过")
                    exit_code = 1

    finally:
        # 清理 Milvus 连接
        if milvus_client is not None:
            with contextlib.suppress(Exception):
                milvus_client.close()

    return exit_code


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    rc = asyncio.run(_main())
    sys.exit(rc)
