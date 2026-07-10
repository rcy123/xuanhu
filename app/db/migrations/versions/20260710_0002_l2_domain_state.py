"""Create L2 domain-state ledger and graph metadata tables.

Revision ID: 20260710_0002
Revises: 20250624_0001
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260710_0002"
down_revision: str | None = "20250624_0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _uuid_column(
    name: str, foreign_key: str | None = None, *, nullable: bool = False, ondelete: str | None = None
) -> Any:
    args: tuple[object, ...] = () if foreign_key is None else (sa.ForeignKey(foreign_key, ondelete=ondelete),)
    return sa.Column(name, postgresql.UUID(as_uuid=True), *args, nullable=nullable)  # type: ignore[arg-type]


def upgrade() -> None:
    op.create_table(
        "observations",
        _uuid_column("id", nullable=False),
        _uuid_column("session_id", "consult_sessions.id", nullable=False, ondelete="CASCADE"),
        sa.Column("fact_key", sa.String(128), nullable=False),
        sa.Column("value", postgresql.JSONB, nullable=True),
        sa.Column("normalized_value", postgresql.JSONB, nullable=True),
        _uuid_column("source_message_id", "consult_messages.id", nullable=False, ondelete="RESTRICT"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("confidence", postgresql.DOUBLE_PRECISION, nullable=True),
        _uuid_column("supersedes_observation_id", "observations.id", nullable=True, ondelete="RESTRICT"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("status IN ('active','corrected','retracted')", name="chk_observations_status"),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="chk_observations_confidence_range"
        ),
        sa.CheckConstraint(
            "(status = 'active' AND supersedes_observation_id IS NULL) OR (status IN ('corrected','retracted') AND supersedes_observation_id IS NOT NULL)",
            name="chk_observations_status_relation",
        ),
        sa.CheckConstraint(
            "supersedes_observation_id IS NULL OR supersedes_observation_id <> id",
            name="chk_observations_no_self_supersede",
        ),
    )
    op.create_index("idx_observations_session_fact_created", "observations", ["session_id", "fact_key", "created_at"])
    op.create_index("idx_observations_source_message", "observations", ["source_message_id"])
    op.create_table(
        "safety_profiles",
        _uuid_column("id", nullable=False),
        _uuid_column("session_id", "consult_sessions.id", nullable=False, ondelete="CASCADE"),
        *[
            sa.Column(name, sa.String(20), nullable=False, server_default="unknown")
            for name in (
                "allergy_collection_status",
                "pregnancy_collection_status",
                "lactation_collection_status",
                "medications_collection_status",
                "major_conditions_collection_status",
                "contraindications_collection_status",
            )
        ],
        sa.Column("allergens", postgresql.JSONB),
        sa.Column("pregnancy_value", sa.String(16)),
        sa.Column("lactation_value", sa.String(16)),
        sa.Column("medications", postgresql.JSONB),
        sa.Column("major_conditions", postgresql.JSONB),
        sa.Column("contraindications", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id"),
    )
    for field in (
        "allergy_collection_status",
        "pregnancy_collection_status",
        "lactation_collection_status",
        "medications_collection_status",
        "major_conditions_collection_status",
        "contraindications_collection_status",
    ):
        op.create_check_constraint(
            f"chk_safety_profiles_{field}", "safety_profiles", f"{field} IN ('unknown','explicitly_none','collected')"
        )
    op.create_check_constraint(
        "chk_safety_profiles_pregnancy_value",
        "safety_profiles",
        "pregnancy_value IS NULL OR pregnancy_value IN ('pregnant','not_pregnant','possible')",
    )
    op.create_check_constraint(
        "chk_safety_profiles_lactation_value",
        "safety_profiles",
        "lactation_value IS NULL OR lactation_value IN ('lactating','not_lactating')",
    )
    for status, value in (
        ("allergy_collection_status", "allergens"),
        ("medications_collection_status", "medications"),
        ("major_conditions_collection_status", "major_conditions"),
        ("contraindications_collection_status", "contraindications"),
    ):
        op.create_check_constraint(
            f"chk_safety_profiles_{value}_collection",
            "safety_profiles",
            f"({status} = 'unknown' AND {value} IS NULL) OR ({status} = 'explicitly_none' AND {value} IS NULL) OR ({status} = 'collected' AND jsonb_typeof({value}) = 'array' AND jsonb_array_length({value}) > 0)",
        )
    op.create_check_constraint(
        "chk_safety_profiles_pregnancy_collection",
        "safety_profiles",
        "(pregnancy_collection_status = 'unknown' AND pregnancy_value IS NULL) OR (pregnancy_collection_status = 'explicitly_none' AND pregnancy_value IS NULL) OR (pregnancy_collection_status = 'collected' AND pregnancy_value IS NOT NULL)",
    )
    op.create_check_constraint(
        "chk_safety_profiles_lactation_collection",
        "safety_profiles",
        "(lactation_collection_status = 'unknown' AND lactation_value IS NULL) OR (lactation_collection_status = 'explicitly_none' AND lactation_value IS NULL) OR (lactation_collection_status = 'collected' AND lactation_value IS NOT NULL)",
    )
    op.create_table(
        "graph_runs",
        _uuid_column("id", nullable=False),
        _uuid_column("session_id", "consult_sessions.id", nullable=False, ondelete="CASCADE"),
        sa.Column("graph_version", sa.String(64), nullable=False),
        sa.Column("command_id", sa.String(128), nullable=False),
        sa.Column("input_state_version", sa.Integer, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("input_state_version >= 1", name="chk_graph_runs_input_state_version"),
        sa.CheckConstraint("status IN ('running','completed','failed','cancelled')", name="chk_graph_runs_status"),
    )
    op.create_index("idx_graph_runs_session_created", "graph_runs", ["session_id", "created_at"])
    op.create_index("idx_graph_runs_session_command", "graph_runs", ["session_id", "command_id"])
    op.create_table(
        "graph_run_steps",
        _uuid_column("id", nullable=False),
        _uuid_column("graph_run_id", "graph_runs.id", nullable=False, ondelete="CASCADE"),
        sa.Column("step_index", sa.Integer, nullable=False),
        sa.Column("step_name", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("metadata", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("step_index >= 0", name="chk_graph_run_steps_index"),
        sa.CheckConstraint("status IN ('started','completed','failed','skipped')", name="chk_graph_run_steps_status"),
        sa.CheckConstraint(
            "metadata IS NULL OR jsonb_typeof(metadata) = 'object'", name="chk_graph_run_steps_metadata_object"
        ),
        sa.UniqueConstraint("graph_run_id", "step_index", name="uq_graph_run_steps_run_index"),
    )
    op.create_table(
        "artifact_revisions",
        _uuid_column("id", nullable=False),
        _uuid_column("artifact_id", nullable=False),
        sa.Column("artifact_type", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        _uuid_column("session_id", "consult_sessions.id", nullable=False, ondelete="CASCADE"),
        sa.Column("input_state_version", sa.Integer, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        _uuid_column("produced_by_run_id", "graph_runs.id", nullable=False, ondelete="RESTRICT"),
        _uuid_column("parent_revision_id", nullable=True),
        sa.Column("parent_revision", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("artifact_id", "revision", name="uq_artifact_revisions_artifact_revision"),
        sa.UniqueConstraint("id", "artifact_id", "session_id", "revision", name="uq_artifact_revisions_parent_target"),
        sa.CheckConstraint("revision >= 1", name="chk_artifact_revisions_revision"),
        sa.CheckConstraint("input_state_version >= 1", name="chk_artifact_revisions_input_state_version"),
        sa.CheckConstraint("status IN ('current','superseded','stale')", name="chk_artifact_revisions_status"),
        sa.CheckConstraint("char_length(artifact_type) > 0", name="chk_artifact_revisions_type_nonempty"),
        sa.CheckConstraint(
            "(revision = 1 AND parent_revision_id IS NULL AND parent_revision IS NULL) OR "
            "(revision > 1 AND parent_revision_id IS NOT NULL AND parent_revision = revision - 1)",
            name="chk_artifact_revisions_parent_relation",
        ),
        sa.ForeignKeyConstraint(
            ["parent_revision_id", "artifact_id", "session_id", "parent_revision"],
            [
                "artifact_revisions.id",
                "artifact_revisions.artifact_id",
                "artifact_revisions.session_id",
                "artifact_revisions.revision",
            ],
            name="fk_artifact_revisions_parent_same_artifact_session",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "idx_artifact_revisions_session_type_status", "artifact_revisions", ["session_id", "artifact_type", "status"]
    )
    op.create_index(
        "uq_artifact_revisions_one_current",
        "artifact_revisions",
        ["artifact_id"],
        unique=True,
        postgresql_where=sa.text("status = 'current'"),
    )
    op.create_table(
        "gate_results",
        _uuid_column("id", nullable=False),
        _uuid_column("session_id", "consult_sessions.id", nullable=False, ondelete="CASCADE"),
        _uuid_column("graph_run_id", "graph_runs.id", nullable=True, ondelete="SET NULL"),
        sa.Column("gate_name", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("input_state_version", sa.Integer, nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("details", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("input_state_version >= 1", name="chk_gate_results_input_state_version"),
        sa.CheckConstraint("decision IN ('passed','failed','blocked')", name="chk_gate_results_decision"),
        sa.CheckConstraint("char_length(gate_name) > 0", name="chk_gate_results_name_nonempty"),
        sa.CheckConstraint(
            "details IS NULL OR jsonb_typeof(details) = 'object'", name="chk_gate_results_details_object"
        ),
    )
    op.create_index("idx_gate_results_session_created", "gate_results", ["session_id", "created_at"])
    op.create_index("idx_gate_results_run", "gate_results", ["graph_run_id"])


def downgrade() -> None:
    op.drop_table("gate_results")
    op.drop_table("artifact_revisions")
    op.drop_table("graph_run_steps")
    op.drop_table("graph_runs")
    op.drop_table("safety_profiles")
    op.drop_table("observations")
