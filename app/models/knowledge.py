"""知识库表 — knowledge_sources, formulas, herbs, dosage_units, acupoints, theory_cases, knowledge_chunks。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class KnowledgeSource(Base, UUIDPrimaryKeyMixin):
    """知识来源表 — knowledge_sources。"""

    __tablename__ = "knowledge_sources"

    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    source_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    license_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict)

    # -- relationships --
    formulas: Mapped[list[Formula]] = relationship("Formula", back_populates="source_rel", lazy="raise")
    herbs: Mapped[list[Herb]] = relationship("Herb", back_populates="source_rel", lazy="raise")
    acupoints: Mapped[list[Acupoint]] = relationship("Acupoint", back_populates="source_rel", lazy="raise")
    theory_cases: Mapped[list[TheoryCase]] = relationship("TheoryCase", back_populates="source_rel", lazy="raise")

    __table_args__ = (
        CheckConstraint(
            "source_type IN ('formula','herb','acupoint','theory','case')",
            name="chk_knowledge_sources_source_type",
        ),
        Index("idx_knowledge_sources_type", "source_type"),
        Index("idx_knowledge_sources_title", "title"),
    )


class Formula(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """方剂表 — formulas。"""

    __tablename__ = "formulas"

    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_sources.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    aliases: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    composition: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    effect: Mapped[str | None] = mapped_column(Text, nullable=True)
    indications: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    modification_rules: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    doc_text: Mapped[str] = mapped_column(Text, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # -- relationships --
    source_rel: Mapped[KnowledgeSource | None] = relationship("KnowledgeSource", back_populates="formulas")
    chunks: Mapped[list[KnowledgeChunk]] = relationship(
        "KnowledgeChunk",
        primaryjoin="and_(Formula.id == foreign(KnowledgeChunk.source_id), "
        "KnowledgeChunk.source_type == 'formula')",
        viewonly=True,
        lazy="raise",
    )

    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(composition) = 'array'",
            name="chk_formulas_composition_array",
        ),
        # 部分唯一索引：active（未软删除）的方剂名唯一
        Index(
            "uniq_formulas_name_active",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "idx_formulas_name_trgm",
            text("name gin_trgm_ops"),
            postgresql_using="gin",
        ),
        Index("idx_formulas_source_id", "source_id"),
        Index(
            "idx_formulas_doc_text_fts",
            text("to_tsvector('simple', doc_text)"),
            postgresql_using="gin",
        ),
    )


class Herb(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """中药表 — herbs。"""

    __tablename__ = "herbs"

    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_sources.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    aliases: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    properties: Mapped[str | None] = mapped_column(String(128), nullable=True)
    meridians: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    effects: Mapped[str | None] = mapped_column(Text, nullable=True)
    indications: Mapped[str | None] = mapped_column(Text, nullable=True)
    dosage: Mapped[str | None] = mapped_column(String(128), nullable=True)
    max_dose: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    contraindications: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    eighteen_incompatibilities: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    nineteen_fears: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    pregnancy_contraindication: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    incompatibilities: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    doc_text: Mapped[str] = mapped_column(Text, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # -- relationships --
    source_rel: Mapped[KnowledgeSource | None] = relationship("KnowledgeSource", back_populates="herbs")
    chunks: Mapped[list[KnowledgeChunk]] = relationship(
        "KnowledgeChunk",
        primaryjoin="and_(Herb.id == foreign(KnowledgeChunk.source_id), "
        "KnowledgeChunk.source_type == 'herb')",
        viewonly=True,
        lazy="raise",
    )

    __table_args__ = (
        CheckConstraint(
            "max_dose IS NULL OR max_dose > 0",
            name="chk_herbs_max_dose_positive",
        ),
        CheckConstraint(
            "pregnancy_contraindication IN ('forbidden','caution','none')",
            name="chk_herbs_pregnancy_contraindication",
        ),
        Index(
            "uniq_herbs_name_active",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "idx_herbs_name_trgm",
            text("name gin_trgm_ops"),
            postgresql_using="gin",
        ),
        Index(
            "idx_herbs_aliases_gin",
            text("aliases"),
            postgresql_using="gin",
            postgresql_ops={"aliases": "jsonb_path_ops"},
        ),
        Index("idx_herbs_pregnancy", "pregnancy_contraindication"),
        Index(
            "idx_herbs_doc_text_fts",
            text("to_tsvector('simple', doc_text)"),
            postgresql_using="gin",
        ),
    )


class DosageUnit(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """剂量单位表 — dosage_units。

    字段口径（P0 已确认）：
    - unit_name / aliases / to_grams / conversion_type
    - precision_note / is_standard / enabled
    """

    __tablename__ = "dosage_units"

    unit_name: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    aliases: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    to_grams: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    conversion_type: Mapped[str] = mapped_column(String(32), nullable=False)
    precision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_standard: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        CheckConstraint(
            "conversion_type IN ('standard','fixed','herb_specific','unsupported')",
            name="chk_dosage_units_conversion_type",
        ),
        CheckConstraint(
            "conversion_type NOT IN ('standard','fixed') OR "
            "(to_grams IS NOT NULL AND to_grams > 0)",
            name="chk_dosage_units_to_grams_required",
        ),
        Index("idx_dosage_units_enabled", "enabled"),
    )


class Acupoint(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """穴位表 — acupoints。"""

    __tablename__ = "acupoints"

    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_sources.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    aliases: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    meridian: Mapped[str | None] = mapped_column(String(128), nullable=True)
    location: Mapped[str | None] = mapped_column(Text, nullable=True)
    indications: Mapped[str | None] = mapped_column(Text, nullable=True)
    operation: Mapped[str | None] = mapped_column(Text, nullable=True)
    contraindications: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    doc_text: Mapped[str] = mapped_column(Text, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # -- relationships --
    source_rel: Mapped[KnowledgeSource | None] = relationship("KnowledgeSource", back_populates="acupoints")
    chunks: Mapped[list[KnowledgeChunk]] = relationship(
        "KnowledgeChunk",
        primaryjoin="and_(Acupoint.id == foreign(KnowledgeChunk.source_id), "
        "KnowledgeChunk.source_type == 'acupoint')",
        viewonly=True,
        lazy="raise",
    )

    __table_args__ = (
        Index(
            "uniq_acupoints_name_active",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "idx_acupoints_name_trgm",
            text("name gin_trgm_ops"),
            postgresql_using="gin",
        ),
        Index("idx_acupoints_meridian", "meridian"),
        Index(
            "idx_acupoints_doc_text_fts",
            text("to_tsvector('simple', doc_text)"),
            postgresql_using="gin",
        ),
    )


class TheoryCase(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """理论与医案表 — theory_cases。"""

    __tablename__ = "theory_cases"

    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_sources.id", ondelete="SET NULL"), nullable=True
    )
    entry_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    disease_category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    syndrome: Mapped[str | None] = mapped_column(String(128), nullable=True)
    treatment_principle: Mapped[str | None] = mapped_column(Text, nullable=True)
    formula_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    doc_text: Mapped[str] = mapped_column(Text, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # -- relationships --
    source_rel: Mapped[KnowledgeSource | None] = relationship("KnowledgeSource", back_populates="theory_cases")
    chunks: Mapped[list[KnowledgeChunk]] = relationship(
        "KnowledgeChunk",
        primaryjoin="and_(TheoryCase.id == foreign(KnowledgeChunk.source_id), "
        "KnowledgeChunk.source_type.in_(['theory', 'case']))",
        viewonly=True,
        lazy="raise",
    )

    __table_args__ = (
        CheckConstraint(
            "entry_type IN ('theory','case')",
            name="chk_theory_cases_entry_type",
        ),
        Index("idx_theory_cases_type", "entry_type"),
        Index("idx_theory_cases_syndrome", "syndrome"),
        Index("idx_theory_cases_disease", "disease_category"),
        Index(
            "idx_theory_cases_doc_text_fts",
            text("to_tsvector('simple', doc_text)"),
            postgresql_using="gin",
        ),
    )


class KnowledgeChunk(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """知识 chunk 表 — knowledge_chunks。

    RAG 检索的最小文本单元，记录向量同步状态和 Milvus 关联 ID。
    MVP 不实现 Milvus 写入，但保留 vector_id 和 embedding_status 字段。
    """

    __tablename__ = "knowledge_chunks"

    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    extra_meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    embedding_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    embedding_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    vector_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    embedded_at: Mapped[datetime | None] = mapped_column(nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        CheckConstraint(
            "source_type IN ('formula','herb','acupoint','theory','case')",
            name="chk_knowledge_chunks_source_type",
        ),
        CheckConstraint(
            "embedding_status IN ('pending','done','failed')",
            name="chk_knowledge_chunks_embedding_status",
        ),
        Index("idx_knowledge_chunks_source", "source_type", "source_id"),
        Index("idx_knowledge_chunks_embedding_status", "embedding_status", "updated_at"),
        Index("idx_knowledge_chunks_content_hash", "content_hash"),
        Index("idx_knowledge_chunks_vector_id", "vector_id"),
        Index(
            "idx_knowledge_chunks_content_fts",
            text("to_tsvector('simple', content)"),
            postgresql_using="gin",
        ),
        Index(
            "uniq_knowledge_chunks_active_hash",
            "source_type",
            "source_id",
            "content_hash",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )
