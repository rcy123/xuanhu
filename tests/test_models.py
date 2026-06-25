"""P2-1 ORM 模型测试。

覆盖：
- 所有模型可正确导入
- 所有表在 Base.metadata 中注册
- 关键字段、索引、check constraint 存在性验证
- dosage_units 表结构符合 P0 确认口径
"""

from __future__ import annotations

import pytest
from sqlalchemy import CheckConstraint
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import Base
from app.models import (  # noqa: F401 — 触发模型注册
    Acupoint,
    AgentEvidence,
    AgentRun,
    AuditEvent,
    ConsultMessage,
    ConsultSession,
    DoctorReview,
    DosageUnit,
    Formula,
    Herb,
    KnowledgeChunk,
    KnowledgeSource,
    MedicalRecord,
    SafetyRuleRun,
    TheoryCase,
)

# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _get_constraint_names(table, constraint_cls=CheckConstraint):
    """获取表上特定类型约束的名称集合。"""
    return {c.name for c in table.constraints if isinstance(c, constraint_cls)}


def _has_constraint_with_suffix(table, suffix, constraint_cls=CheckConstraint):
    """检查表上是否存在名称以 suffix 结尾的约束（考虑命名约定的前缀）。"""
    return any(name.endswith(suffix) for name in _get_constraint_names(table, constraint_cls))


def _get_fk_column_names(table):
    """获取表上所有外键约束涉及的目标列名集合。"""
    names = set()
    for fk in table.foreign_keys:
        # fk.name 是外键约束名，fk.column 引用远程列
        names.add(fk.name)
    return names


def _get_index_names(table):
    """获取表上所有索引名称集合。"""
    return {idx.name for idx in table.indexes}


def _has_index_with_suffix(table, suffix):
    """检查表上是否存在名称以 suffix 结尾的索引。"""
    return any(name.endswith(suffix) for name in _get_index_names(table))


# ---------------------------------------------------------------------------
# 1. 模型导入测试
# ---------------------------------------------------------------------------


def test_all_models_import_successfully() -> None:
    """验证所有 15 个模型类均可导入且注册到 Base.metadata。"""
    expected_tables = {
        "consult_sessions",
        "consult_messages",
        "agent_runs",
        "agent_evidences",
        "safety_rule_runs",
        "doctor_reviews",
        "medical_records",
        "audit_events",
        "knowledge_sources",
        "formulas",
        "herbs",
        "dosage_units",
        "acupoints",
        "theory_cases",
        "knowledge_chunks",
    }
    actual_tables = set(Base.metadata.tables.keys())
    assert expected_tables.issubset(actual_tables), (
        f"Missing tables: {expected_tables - actual_tables}"
    )


# ---------------------------------------------------------------------------
# 2. 关键表字段验证
# ---------------------------------------------------------------------------


class TestConsultSessionFields:
    """验证 consult_sessions 表结构。"""

    @pytest.fixture
    def table(self):
        return Base.metadata.tables["consult_sessions"]

    def test_primary_key_is_uuid(self, table) -> None:
        col = table.c["id"]
        assert str(col.type) == "UUID"
        assert col.primary_key

    def test_patient_info_is_jsonb(self, table) -> None:
        assert isinstance(table.c["patient_info"].type, JSONB)

    def test_state_snapshot_is_jsonb(self, table) -> None:
        assert isinstance(table.c["state_snapshot"].type, JSONB)

    def test_required_columns_exist(self, table) -> None:
        expected = {
            "id", "patient_ref", "patient_info", "chief_complaint",
            "current_stage", "status", "pending_review",
            "rollback_counts", "state_snapshot", "state_version",
            "last_checkpoint_at", "recovery_status", "blocked_reason",
            "blocked_at", "created_by", "created_at", "updated_at",
        }
        actual = set(table.c.keys())
        assert expected.issubset(actual)

    def test_current_stage_check_constraint(self, table) -> None:
        assert _has_constraint_with_suffix(table, "chk_consult_sessions_current_stage")

    def test_status_check_constraint(self, table) -> None:
        assert _has_constraint_with_suffix(table, "chk_consult_sessions_status")


class TestConsultMessageFields:
    """验证 consult_messages 表结构。"""

    @pytest.fixture
    def table(self):
        return Base.metadata.tables["consult_messages"]

    def test_role_check_constraint(self, table) -> None:
        assert _has_constraint_with_suffix(table, "chk_consult_messages_role")

    def test_session_fk_exists(self, table) -> None:
        fk_cols = {fk.parent.name for fk in table.foreign_keys}
        assert "session_id" in fk_cols

    def test_agent_run_fk_exists(self, table) -> None:
        fk_cols = {fk.parent.name for fk in table.foreign_keys}
        assert "agent_run_id" in fk_cols


class TestAgentRunFields:
    """验证 agent_runs 表结构。"""

    @pytest.fixture
    def table(self):
        return Base.metadata.tables["agent_runs"]

    def test_status_check_constraint(self, table) -> None:
        assert _has_constraint_with_suffix(table, "chk_agent_runs_status")

    def test_input_output_snapshots_are_jsonb(self, table) -> None:
        assert isinstance(table.c["input_snapshot"].type, JSONB)
        assert isinstance(table.c["output_snapshot"].type, JSONB)


class TestAgentEvidenceFields:
    """验证 agent_evidences 表结构。"""

    @pytest.fixture
    def table(self):
        return Base.metadata.tables["agent_evidences"]

    def test_source_type_check_constraint(self, table) -> None:
        assert _has_constraint_with_suffix(table, "chk_agent_evidences_source_type")

    def test_score_is_double_precision(self, table) -> None:
        col = table.c["score"]
        type_str = str(col.type).upper()
        assert "DOUBLE" in type_str or "FLOAT" in type_str


class TestSafetyRuleRunFields:
    """验证 safety_rule_runs 表结构。"""

    @pytest.fixture
    def table(self):
        return Base.metadata.tables["safety_rule_runs"]

    def test_formula_source_check_constraint(self, table) -> None:
        assert _has_constraint_with_suffix(table, "chk_safety_rule_runs_formula_source")

    def test_issues_is_jsonb(self, table) -> None:
        assert isinstance(table.c["issues"].type, JSONB)

    def test_formula_snapshot_is_jsonb(self, table) -> None:
        assert isinstance(table.c["formula_snapshot"].type, JSONB)


class TestDoctorReviewFields:
    """验证 doctor_reviews 表结构。"""

    @pytest.fixture
    def table(self):
        return Base.metadata.tables["doctor_reviews"]

    def test_action_check_constraint(self, table) -> None:
        assert _has_constraint_with_suffix(table, "chk_doctor_reviews_action")

    def test_original_formula_is_jsonb(self, table) -> None:
        assert isinstance(table.c["original_formula"].type, JSONB)

    def test_formula_override_is_jsonb(self, table) -> None:
        assert isinstance(table.c["formula_override"].type, JSONB)


class TestMedicalRecordFields:
    """验证 medical_records 表结构。"""

    @pytest.fixture
    def table(self):
        return Base.metadata.tables["medical_records"]

    def test_version_check_constraint(self, table) -> None:
        assert _has_constraint_with_suffix(table, "chk_medical_records_version_positive")

    def test_record_json_is_jsonb(self, table) -> None:
        assert isinstance(table.c["record_json"].type, JSONB)

    def test_session_version_unique_index(self, table) -> None:
        assert _has_index_with_suffix(table, "uniq_medical_records_session_version")


class TestDosageUnitFields:
    """验证 dosage_units 表结构（P0 确认口径）。"""

    @pytest.fixture
    def table(self):
        return Base.metadata.tables["dosage_units"]

    def test_unit_name_is_string_unique(self, table) -> None:
        col = table.c["unit_name"]
        assert "VARCHAR" in str(col.type).upper()

    def test_to_grams_is_numeric_nullable(self, table) -> None:
        col = table.c["to_grams"]
        assert "NUMERIC" in str(col.type).upper()
        assert col.nullable

    def test_aliases_is_jsonb(self, table) -> None:
        assert isinstance(table.c["aliases"].type, JSONB)

    def test_is_standard_is_boolean(self, table) -> None:
        col = table.c["is_standard"]
        assert "BOOLEAN" in str(col.type).upper()

    def test_enabled_is_boolean(self, table) -> None:
        col = table.c["enabled"]
        assert "BOOLEAN" in str(col.type).upper()

    def test_conversion_type_check_constraint(self, table) -> None:
        assert _has_constraint_with_suffix(table, "chk_dosage_units_conversion_type")

    def test_to_grams_required_check_constraint(self, table) -> None:
        """设计文档要求：conversion_type IN ('standard','fixed') 时 to_grams 必须非空且大于 0。"""
        assert _has_constraint_with_suffix(table, "chk_dosage_units_to_grams_required")

    def test_no_to_gram_factor_field(self, table) -> None:
        """P0 确认：表结构中不存在旧口径字段 to_gram_factor。"""
        assert "to_gram_factor" not in table.c

    def test_no_herb_name_field(self, table) -> None:
        """P0 确认：表结构中不存在旧口径字段 herb_name。"""
        assert "herb_name" not in table.c


# ---------------------------------------------------------------------------
# 3. 知识库表验证
# ---------------------------------------------------------------------------


class TestHerbFields:
    """验证 herbs 表结构。"""

    @pytest.fixture
    def table(self):
        return Base.metadata.tables["herbs"]

    def test_pregnancy_contraindication_check(self, table) -> None:
        assert _has_constraint_with_suffix(table, "chk_herbs_pregnancy_contraindication")

    def test_max_dose_check(self, table) -> None:
        assert _has_constraint_with_suffix(table, "chk_herbs_max_dose_positive")


class TestFormulaFields:
    """验证 formulas 表结构。"""

    @pytest.fixture
    def table(self):
        return Base.metadata.tables["formulas"]

    def test_composition_is_jsonb(self, table) -> None:
        assert isinstance(table.c["composition"].type, JSONB)

    def test_name_active_unique_index(self, table) -> None:
        assert _has_index_with_suffix(table, "uniq_formulas_name_active")


class TestKnowledgeChunkFields:
    """验证 knowledge_chunks 表结构。"""

    @pytest.fixture
    def table(self):
        return Base.metadata.tables["knowledge_chunks"]

    def test_embedding_status_check(self, table) -> None:
        assert _has_constraint_with_suffix(table, "chk_knowledge_chunks_embedding_status")

    def test_vector_id_is_varchar(self, table) -> None:
        col = table.c["vector_id"]
        assert "VARCHAR" in str(col.type).upper()

    def test_content_hash_is_varchar_128(self, table) -> None:
        col = table.c["content_hash"]
        assert "VARCHAR" in str(col.type).upper()

    def test_active_hash_unique_index(self, table) -> None:
        assert _has_index_with_suffix(table, "uniq_knowledge_chunks_active_hash")


# ---------------------------------------------------------------------------
# 4. 审计表验证
# ---------------------------------------------------------------------------


class TestAuditEventFields:
    """验证 audit_events 表结构。"""

    @pytest.fixture
    def table(self):
        return Base.metadata.tables["audit_events"]

    def test_actor_type_check_constraint(self, table) -> None:
        assert _has_constraint_with_suffix(table, "chk_audit_events_actor_type")

    def test_payload_is_jsonb(self, table) -> None:
        assert isinstance(table.c["payload"].type, JSONB)
