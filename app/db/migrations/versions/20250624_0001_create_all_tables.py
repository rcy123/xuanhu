"""P2-1 初始迁移：创建全部业务表与知识库表 + dosage_units 种子数据

Revision ID: 20250624_0001
Revises:
Create Date: 2026-06-24
"""
from collections.abc import Sequence
from datetime import UTC

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20250624_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建所有表、索引、约束，写入 dosage_units 种子数据。"""
    # -- 1. PostgreSQL 扩展 --
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # -- 2. consult_sessions --
    op.create_table(
        "consult_sessions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.func.gen_random_uuid(),
        ),
        sa.Column("patient_ref", sa.String(128), nullable=True),
        sa.Column("patient_info", postgresql.JSONB, nullable=False),
        sa.Column("chief_complaint", sa.Text, nullable=True),
        sa.Column("current_stage", sa.String(32), nullable=False, server_default="inquiry"),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("pending_review", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column(
            "rollback_counts",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("state_snapshot", postgresql.JSONB, nullable=True),
        sa.Column("state_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("last_checkpoint_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recovery_status", sa.String(32), nullable=False, server_default="normal"),
        sa.Column("blocked_reason", sa.String(256), nullable=True),
        sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.execute(
        "ALTER TABLE consult_sessions "
        "ADD CONSTRAINT chk_consult_sessions_patient_info_object "
        "CHECK (jsonb_typeof(patient_info) = 'object')"
    )
    op.execute("ALTER TABLE consult_sessions ADD CONSTRAINT chk_consult_sessions_state_version_positive CHECK (state_version >= 1)")
    op.execute(
        "ALTER TABLE consult_sessions ADD CONSTRAINT chk_consult_sessions_current_stage "
        "CHECK (current_stage IN ('inquiry','sufficiency','syndrome','prescription',"
        "'modification','safety','review','record','done','blocked'))"
    )
    op.execute(
        "ALTER TABLE consult_sessions ADD CONSTRAINT chk_consult_sessions_status "
        "CHECK (status IN ('active','pending_review','done','blocked','terminated'))"
    )
    op.execute(
        "ALTER TABLE consult_sessions ADD CONSTRAINT chk_consult_sessions_recovery_status "
        "CHECK (recovery_status IN ('normal','recovering','manual_required'))"
    )
    op.create_index(
        "idx_consult_sessions_status_updated_at",
        "consult_sessions",
        ["status", sa.text("updated_at DESC")],
    )
    op.create_index("idx_consult_sessions_patient_ref", "consult_sessions", ["patient_ref"])
    op.create_index("idx_consult_sessions_current_stage", "consult_sessions", ["current_stage"])
    op.create_index("idx_consult_sessions_recovery_status", "consult_sessions", ["recovery_status"])
    op.create_index("idx_consult_sessions_blocked", "consult_sessions", ["blocked_reason", "blocked_at"])

    # -- 3. agent_runs --
    op.create_table(
        "agent_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.func.gen_random_uuid(),
        ),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("consult_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_name", sa.String(32), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("input_snapshot", postgresql.JSONB, nullable=True),
        sa.Column("output_snapshot", postgresql.JSONB, nullable=True),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("latency_ms", sa.Integer, nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.execute(
        "ALTER TABLE agent_runs ADD CONSTRAINT chk_agent_runs_status "
        "CHECK (status IN ('success','failed','blocked'))"
    )
    op.create_index(
        "idx_agent_runs_session_created",
        "agent_runs",
        ["session_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_agent_runs_agent_status",
        "agent_runs",
        ["agent_name", "status", sa.text("created_at DESC")],
    )
    op.create_index("idx_agent_runs_trace_id", "agent_runs", ["trace_id"])

    # -- 4. consult_messages --
    op.create_table(
        "consult_messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.func.gen_random_uuid(),
        ),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("consult_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("agent_name", sa.String(32), nullable=True),
        sa.Column("agent_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("structured_delta", postgresql.JSONB, nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.execute(
        "ALTER TABLE consult_messages ADD CONSTRAINT chk_consult_messages_role "
        "CHECK (role IN ('doctor','patient_proxy','agent','system'))"
    )
    op.create_index("idx_consult_messages_session_created", "consult_messages", ["session_id", "created_at"])
    op.create_index("idx_consult_messages_agent_run", "consult_messages", ["agent_run_id"])
    op.create_index("idx_consult_messages_trace_id", "consult_messages", ["trace_id"])

    # -- 5. agent_evidences --
    op.create_table(
        "agent_evidences",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.func.gen_random_uuid(),
        ),
        sa.Column("agent_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("consult_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("evidence_id", sa.String(128), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.String(255), nullable=True),
        sa.Column("content_snippet", sa.Text, nullable=True),
        sa.Column("score", sa.Float(precision=53), nullable=True),  # double precision
        sa.Column("rank", sa.Integer, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.execute(
        "ALTER TABLE agent_evidences ADD CONSTRAINT chk_agent_evidences_source_type "
        "CHECK (source_type IN ('formula','herb','acupoint','theory','case'))"
    )
    op.create_index("idx_agent_evidences_agent_run", "agent_evidences", ["agent_run_id"])
    op.create_index("idx_agent_evidences_session", "agent_evidences", ["session_id", sa.text("created_at DESC")])
    op.create_index("idx_agent_evidences_source", "agent_evidences", ["source_type", "source_id"])
    op.create_index("idx_agent_evidences_chunk", "agent_evidences", ["chunk_id"])

    # -- 6. safety_rule_runs --
    op.create_table(
        "safety_rule_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.func.gen_random_uuid(),
        ),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("consult_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("formula_source", sa.String(32), nullable=False),
        sa.Column("passed", sa.Boolean, nullable=False),
        sa.Column("issues", postgresql.JSONB, nullable=False),
        sa.Column("formula_snapshot", postgresql.JSONB, nullable=False),
        sa.Column("normalized_formula", postgresql.JSONB, nullable=True),
        sa.Column("patient_snapshot", postgresql.JSONB, nullable=False),
        sa.Column("rule_version", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.execute(
        "ALTER TABLE safety_rule_runs ADD CONSTRAINT chk_safety_rule_runs_formula_source "
        "CHECK (formula_source IN ('agent_output','doctor_override'))"
    )
    op.execute(
        "ALTER TABLE safety_rule_runs ADD CONSTRAINT chk_safety_rule_runs_issues_array "
        "CHECK (jsonb_typeof(issues) = 'array')"
    )
    op.execute(
        "ALTER TABLE safety_rule_runs ADD CONSTRAINT chk_safety_rule_runs_formula_snapshot_object "
        "CHECK (jsonb_typeof(formula_snapshot) = 'object')"
    )
    op.execute(
        "ALTER TABLE safety_rule_runs ADD CONSTRAINT chk_safety_rule_runs_patient_snapshot_object "
        "CHECK (jsonb_typeof(patient_snapshot) = 'object')"
    )
    op.create_index(
        "idx_safety_rule_runs_session_created",
        "safety_rule_runs",
        ["session_id", sa.text("created_at DESC")],
    )
    op.create_index("idx_safety_rule_runs_agent_run", "safety_rule_runs", ["agent_run_id"])
    op.create_index(
        "idx_safety_rule_runs_passed",
        "safety_rule_runs",
        ["passed", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_safety_rule_runs_rule_version",
        "safety_rule_runs",
        ["rule_version", sa.text("created_at DESC")],
    )

    # -- 7. doctor_reviews --
    op.create_table(
        "doctor_reviews",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.func.gen_random_uuid(),
        ),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("consult_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("safety_rule_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("safety_rule_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("original_formula", postgresql.JSONB, nullable=True),
        sa.Column("formula_override", postgresql.JSONB, nullable=True),
        sa.Column("feedback", sa.Text, nullable=True),
        sa.Column("reviewed_by", sa.String(128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.execute(
        "ALTER TABLE doctor_reviews ADD CONSTRAINT chk_doctor_reviews_action "
        "CHECK (action IN ('confirm','modify','reject'))"
    )
    op.create_index(
        "idx_doctor_reviews_session_created",
        "doctor_reviews",
        ["session_id", sa.text("created_at DESC")],
    )
    op.create_index("idx_doctor_reviews_agent_run", "doctor_reviews", ["agent_run_id"])
    op.create_index("idx_doctor_reviews_safety_rule_run", "doctor_reviews", ["safety_rule_run_id"])
    op.create_index(
        "idx_doctor_reviews_reviewed_by",
        "doctor_reviews",
        ["reviewed_by", sa.text("created_at DESC")],
    )

    # -- 8. medical_records --
    op.create_table(
        "medical_records",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.func.gen_random_uuid(),
        ),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("consult_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("record_text", sa.Text, nullable=False),
        sa.Column("record_json", postgresql.JSONB, nullable=False),
        sa.Column("diff_from_previous", postgresql.JSONB, nullable=True),
        sa.Column("doctor_review_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("doctor_reviews.id", ondelete="SET NULL"), nullable=True),
        sa.Column("disclaimer", sa.Text, nullable=False),
        sa.Column("edited_by_doctor", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.execute("ALTER TABLE medical_records ADD CONSTRAINT chk_medical_records_version_positive CHECK (version >= 1)")
    op.execute(
        "ALTER TABLE medical_records ADD CONSTRAINT chk_medical_records_record_json_object "
        "CHECK (jsonb_typeof(record_json) = 'object')"
    )
    op.create_index(
        "uniq_medical_records_session_version",
        "medical_records",
        ["session_id", "version"],
        unique=True,
    )
    op.create_index(
        "idx_medical_records_session_version",
        "medical_records",
        ["session_id", sa.text("version DESC")],
    )
    op.create_index("idx_medical_records_doctor_review", "medical_records", ["doctor_review_id"])

    # -- 9. audit_events --
    op.create_table(
        "audit_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.func.gen_random_uuid(),
        ),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("consult_sessions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=True),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.execute(
        "ALTER TABLE audit_events ADD CONSTRAINT chk_audit_events_actor_type "
        "CHECK (actor_type IN ('doctor','agent','system'))"
    )
    op.create_index(
        "idx_audit_events_session_created",
        "audit_events",
        ["session_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_audit_events_type_created",
        "audit_events",
        ["event_type", sa.text("created_at DESC")],
    )
    op.create_index("idx_audit_events_trace_id", "audit_events", ["trace_id"])

    # -- 10. knowledge_sources --
    op.create_table(
        "knowledge_sources",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.func.gen_random_uuid(),
        ),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("source_name", sa.String(255), nullable=True),
        sa.Column("source_version", sa.String(64), nullable=True),
        sa.Column("license_note", sa.Text, nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.execute(
        "ALTER TABLE knowledge_sources ADD CONSTRAINT chk_knowledge_sources_source_type "
        "CHECK (source_type IN ('formula','herb','acupoint','theory','case'))"
    )
    op.create_index("idx_knowledge_sources_type", "knowledge_sources", ["source_type"])
    op.create_index("idx_knowledge_sources_title", "knowledge_sources", ["title"])

    # -- 11. formulas --
    op.create_table(
        "formulas",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.func.gen_random_uuid(),
        ),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("knowledge_sources.id", ondelete="SET NULL"), nullable=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("aliases", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("composition", postgresql.JSONB, nullable=False),
        sa.Column("effect", sa.Text, nullable=True),
        sa.Column("indications", sa.Text, nullable=True),
        sa.Column("usage", sa.Text, nullable=True),
        sa.Column("source", sa.Text, nullable=True),
        sa.Column("modification_rules", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("doc_text", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "ALTER TABLE formulas ADD CONSTRAINT chk_formulas_composition_array "
        "CHECK (jsonb_typeof(composition) = 'array')"
    )
    op.execute("CREATE UNIQUE INDEX uniq_formulas_name_active ON formulas (name) WHERE deleted_at IS NULL")
    op.execute("CREATE INDEX idx_formulas_name_trgm ON formulas USING gin (name gin_trgm_ops)")
    op.create_index("idx_formulas_source_id", "formulas", ["source_id"])
    op.execute(
        "CREATE INDEX idx_formulas_doc_text_fts ON formulas "
        "USING gin (to_tsvector('simple', doc_text))"
    )

    # -- 12. herbs --
    op.create_table(
        "herbs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.func.gen_random_uuid(),
        ),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("knowledge_sources.id", ondelete="SET NULL"), nullable=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("aliases", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("properties", sa.String(128), nullable=True),
        sa.Column("meridians", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("effects", sa.Text, nullable=True),
        sa.Column("indications", sa.Text, nullable=True),
        sa.Column("dosage", sa.String(128), nullable=True),
        sa.Column("max_dose", sa.Numeric, nullable=True),
        sa.Column("contraindications", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("eighteen_incompatibilities", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("nineteen_fears", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("pregnancy_contraindication", sa.String(32), nullable=False, server_default="none"),
        sa.Column("incompatibilities", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("doc_text", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("ALTER TABLE herbs ADD CONSTRAINT chk_herbs_max_dose_positive CHECK (max_dose IS NULL OR max_dose > 0)")
    op.execute(
        "ALTER TABLE herbs ADD CONSTRAINT chk_herbs_pregnancy_contraindication "
        "CHECK (pregnancy_contraindication IN ('forbidden','caution','none'))"
    )
    op.execute("CREATE UNIQUE INDEX uniq_herbs_name_active ON herbs (name) WHERE deleted_at IS NULL")
    op.execute("CREATE INDEX idx_herbs_name_trgm ON herbs USING gin (name gin_trgm_ops)")
    op.execute("CREATE INDEX idx_herbs_aliases_gin ON herbs USING gin (aliases jsonb_path_ops)")
    op.create_index("idx_herbs_pregnancy", "herbs", ["pregnancy_contraindication"])
    op.execute(
        "CREATE INDEX idx_herbs_doc_text_fts ON herbs "
        "USING gin (to_tsvector('simple', doc_text))"
    )

    # -- 13. dosage_units --
    op.create_table(
        "dosage_units",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.func.gen_random_uuid(),
        ),
        sa.Column("unit_name", sa.String(32), nullable=False, unique=True),
        sa.Column("aliases", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("to_grams", sa.Numeric, nullable=True),
        sa.Column("conversion_type", sa.String(32), nullable=False),
        sa.Column("precision_note", sa.Text, nullable=True),
        sa.Column("is_standard", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.execute(
        "ALTER TABLE dosage_units ADD CONSTRAINT chk_dosage_units_conversion_type "
        "CHECK (conversion_type IN ('standard','fixed','herb_specific','unsupported'))"
    )
    op.execute(
        "ALTER TABLE dosage_units ADD CONSTRAINT chk_dosage_units_to_grams_required "
        "CHECK (conversion_type NOT IN ('standard','fixed') OR "
        "(to_grams IS NOT NULL AND to_grams > 0))"
    )
    op.create_index("idx_dosage_units_enabled", "dosage_units", ["enabled"])

    # -- 13b. dosage_units 种子数据 --
    _seed_dosage_units()

    # -- 14. acupoints --
    op.create_table(
        "acupoints",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.func.gen_random_uuid(),
        ),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("knowledge_sources.id", ondelete="SET NULL"), nullable=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("aliases", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("meridian", sa.String(128), nullable=True),
        sa.Column("location", sa.Text, nullable=True),
        sa.Column("indications", sa.Text, nullable=True),
        sa.Column("operation", sa.Text, nullable=True),
        sa.Column("contraindications", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("source", sa.Text, nullable=True),
        sa.Column("doc_text", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("CREATE UNIQUE INDEX uniq_acupoints_name_active ON acupoints (name) WHERE deleted_at IS NULL")
    op.execute("CREATE INDEX idx_acupoints_name_trgm ON acupoints USING gin (name gin_trgm_ops)")
    op.create_index("idx_acupoints_meridian", "acupoints", ["meridian"])
    op.execute(
        "CREATE INDEX idx_acupoints_doc_text_fts ON acupoints "
        "USING gin (to_tsvector('simple', doc_text))"
    )

    # -- 15. theory_cases --
    op.create_table(
        "theory_cases",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.func.gen_random_uuid(),
        ),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("knowledge_sources.id", ondelete="SET NULL"), nullable=True),
        sa.Column("entry_type", sa.String(32), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("disease_category", sa.String(128), nullable=True),
        sa.Column("syndrome", sa.String(128), nullable=True),
        sa.Column("treatment_principle", sa.Text, nullable=True),
        sa.Column("formula_summary", sa.Text, nullable=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("source", sa.Text, nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("doc_text", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "ALTER TABLE theory_cases ADD CONSTRAINT chk_theory_cases_entry_type "
        "CHECK (entry_type IN ('theory','case'))"
    )
    op.create_index("idx_theory_cases_type", "theory_cases", ["entry_type"])
    op.create_index("idx_theory_cases_syndrome", "theory_cases", ["syndrome"])
    op.create_index("idx_theory_cases_disease", "theory_cases", ["disease_category"])
    op.execute(
        "CREATE INDEX idx_theory_cases_doc_text_fts ON theory_cases "
        "USING gin (to_tsvector('simple', doc_text))"
    )

    # -- 16. knowledge_chunks --
    op.create_table(
        "knowledge_chunks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.func.gen_random_uuid(),
        ),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("content_hash", sa.String(128), nullable=False),
        sa.Column("embedding_model", sa.String(128), nullable=True),
        sa.Column("embedding_status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("vector_id", sa.String(128), nullable=True),
        sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.execute(
        "ALTER TABLE knowledge_chunks ADD CONSTRAINT chk_knowledge_chunks_source_type "
        "CHECK (source_type IN ('formula','herb','acupoint','theory','case'))"
    )
    op.execute(
        "ALTER TABLE knowledge_chunks ADD CONSTRAINT chk_knowledge_chunks_embedding_status "
        "CHECK (embedding_status IN ('pending','done','failed'))"
    )
    op.create_index("idx_knowledge_chunks_source", "knowledge_chunks", ["source_type", "source_id"])
    op.create_index("idx_knowledge_chunks_embedding_status", "knowledge_chunks", ["embedding_status", "updated_at"])
    op.create_index("idx_knowledge_chunks_content_hash", "knowledge_chunks", ["content_hash"])
    op.create_index("idx_knowledge_chunks_vector_id", "knowledge_chunks", ["vector_id"])
    op.execute(
        "CREATE INDEX idx_knowledge_chunks_content_fts ON knowledge_chunks "
        "USING gin (to_tsvector('simple', content))"
    )
    op.execute(
        "CREATE UNIQUE INDEX uniq_knowledge_chunks_active_hash "
        "ON knowledge_chunks (source_type, source_id, content_hash) "
        "WHERE deleted_at IS NULL"
    )

def _seed_dosage_units() -> None:
    """写入 dosage_units 种子数据，保证两=30g、钱=3g。"""
    import json
    from datetime import datetime

    now = datetime.now(UTC).isoformat()

    seed_data = [
        {
            "id": "10000000-0000-0000-0000-000000000001",
            "unit_name": "g",
            "aliases": json.dumps(["克", "公克"]),
            "to_grams": 1.0,
            "conversion_type": "standard",
            "precision_note": "标准克重单位。",
            "is_standard": True,
            "enabled": True,
            "created_at": now,
            "updated_at": now,
        },
        {
            "id": "10000000-0000-0000-0000-000000000002",
            "unit_name": "两",
            "aliases": json.dumps(["市两"]),
            "to_grams": 30.0,
            "conversion_type": "fixed",
            "precision_note": "MVP 安全审核采用现代标准保守换算：1 两 = 30g；古籍剂量不可直接套用。",
            "is_standard": False,
            "enabled": True,
            "created_at": now,
            "updated_at": now,
        },
        {
            "id": "10000000-0000-0000-0000-000000000003",
            "unit_name": "钱",
            "aliases": json.dumps(["市钱"]),
            "to_grams": 3.0,
            "conversion_type": "fixed",
            "precision_note": "MVP 安全审核采用现代标准保守换算：1 钱 = 3g。",
            "is_standard": False,
            "enabled": True,
            "created_at": now,
            "updated_at": now,
        },
        {
            "id": "10000000-0000-0000-0000-000000000004",
            "unit_name": "枚",
            "aliases": json.dumps(["个"]),
            "to_grams": None,
            "conversion_type": "herb_specific",
            "precision_note": "需按药材和炮制规格单独判断。",
            "is_standard": False,
            "enabled": True,
            "created_at": now,
            "updated_at": now,
        },
        {
            "id": "10000000-0000-0000-0000-000000000005",
            "unit_name": "适量",
            "aliases": json.dumps(["少许"]),
            "to_grams": None,
            "conversion_type": "unsupported",
            "precision_note": "不支持自动换算，需人工确认。",
            "is_standard": False,
            "enabled": True,
            "created_at": now,
            "updated_at": now,
        },
    ]

    # 使用 raw SQL INSERT 以便精确控制字段值
    dosage_units_table = sa.table(
        "dosage_units",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("unit_name", sa.String),
        sa.column("aliases", postgresql.JSONB),
        sa.column("to_grams", sa.Numeric),
        sa.column("conversion_type", sa.String),
        sa.column("precision_note", sa.Text),
        sa.column("is_standard", sa.Boolean),
        sa.column("enabled", sa.Boolean),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )

    op.bulk_insert(dosage_units_table, seed_data)  # type: ignore[arg-type]


def downgrade() -> None:
    """回滚本次迁移：删除所有表（按外键依赖顺序反向删除）。"""
    op.execute("DROP TABLE IF EXISTS knowledge_chunks CASCADE")
    op.execute("DROP TABLE IF EXISTS theory_cases CASCADE")
    op.execute("DROP TABLE IF EXISTS acupoints CASCADE")
    op.execute("DROP TABLE IF EXISTS dosage_units CASCADE")
    op.execute("DROP TABLE IF EXISTS herbs CASCADE")
    op.execute("DROP TABLE IF EXISTS formulas CASCADE")
    op.execute("DROP TABLE IF EXISTS knowledge_sources CASCADE")
    op.execute("DROP TABLE IF EXISTS audit_events CASCADE")
    op.execute("DROP TABLE IF EXISTS medical_records CASCADE")
    op.execute("DROP TABLE IF EXISTS doctor_reviews CASCADE")
    op.execute("DROP TABLE IF EXISTS safety_rule_runs CASCADE")
    op.execute("DROP TABLE IF EXISTS agent_evidences CASCADE")
    op.execute("DROP TABLE IF EXISTS consult_messages CASCADE")
    op.execute("DROP TABLE IF EXISTS agent_runs CASCADE")
    op.execute("DROP TABLE IF EXISTS consult_sessions CASCADE")
