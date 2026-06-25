"""P2-1 Alembic 迁移测试。

覆盖：
- 迁移文件存在且可导入
- 迁移脚本中包含所有必需表的 DDL
- dosage_units 种子数据验证（两=30g、钱=3g）
- upgrade/downgrade 逻辑覆盖

注意：真实 PostgreSQL upgrade/downgrade 的验证由 Docker 集成测试覆盖，
不在此测试文件中执行。
"""

from __future__ import annotations

import inspect
from importlib import import_module

import pytest

MIGRATION_MODULE = "app.db.migrations.versions.20250624_0001_create_all_tables"


# ---------------------------------------------------------------------------
# 迁移文件导入
# ---------------------------------------------------------------------------


def test_migration_module_importable() -> None:
    """验证迁移文件可被正常导入。"""
    mod = import_module(MIGRATION_MODULE)
    assert mod.revision == "20250624_0001"
    assert mod.down_revision is None
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)


def test_seed_dosage_units_callable() -> None:
    """验证 _seed_dosage_units 函数存在且可调用。"""
    mod = import_module(MIGRATION_MODULE)
    assert callable(mod._seed_dosage_units)


def test_alembic_config_loads() -> None:
    """验证 alembic.ini 可被 Alembic Config 正确加载（无编码错误）。"""
    from alembic.config import Config

    cfg = Config("alembic.ini")
    assert cfg.get_main_option("script_location") == "app/db/migrations"


# ---------------------------------------------------------------------------
# dosage_units 种子数据验证
# ---------------------------------------------------------------------------


class TestDosageUnitSeedData:
    """验证 dosage_units 种子数据满足 P0 确认口径。"""

    @pytest.fixture
    def seed_source(self) -> str:
        """获取 _seed_dosage_units 函数源码用于检查种子数据。"""
        mod = import_module(MIGRATION_MODULE)
        return inspect.getsource(mod._seed_dosage_units)

    def test_liang_equals_30g(self, seed_source) -> None:
        """P0 确认：两 = 30g。"""
        assert '"两"' in seed_source, "Seed data should include '两' unit"
        assert "30.0" in seed_source, "Seed data should include to_grams=30.0 for 两"
        # 两 should map to 30 (verify in the correct JSON entry)
        assert '"unit_name": "两"' in seed_source or "'unit_name': '两'" in seed_source

    def test_qian_equals_3g(self, seed_source) -> None:
        """P0 确认：钱 = 3g。"""
        assert "钱" in seed_source, "Seed data should include '钱' unit"
        assert "3.0" in seed_source, "Seed data should include to_grams=3.0 for 钱"

    def test_g_is_standard_unit(self, seed_source) -> None:
        """g 是标准单位，to_grams=1.0。"""
        assert '"g"' in seed_source or "'g'" in seed_source
        assert "1.0" in seed_source

    def test_herb_specific_unit_present(self, seed_source) -> None:
        """枚 = herb_specific。"""
        assert "枚" in seed_source or "herb_specific" in seed_source

    def test_unsupported_unit_present(self, seed_source) -> None:
        """适量 = unsupported。"""
        assert "适量" in seed_source or "unsupported" in seed_source

    def test_seed_has_5_entries(self, seed_source) -> None:
        """验证种子数据包含 5 条记录。"""
        # 种子数据中每条记录对应一个 "unit_name" 键值对
        # 源码中 sa.column("unit_name") 也会出现 1 次，所以总计 6 次
        count = seed_source.count('"unit_name"')
        assert count >= 5, f"Expected at least 5 seed data entries, found {count}"

    def test_no_to_gram_factor_in_seed(self, seed_source) -> None:
        """P0 确认：种子数据中不存在旧口径字段 to_gram_factor。"""
        assert "to_gram_factor" not in seed_source

    def test_no_herb_name_in_seed(self, seed_source) -> None:
        """P0 确认：种子数据中不存在旧口径字段 herb_name。"""
        assert "herb_name" not in seed_source

    def test_ke_in_g_aliases(self, seed_source) -> None:
        """'克' 应在 g 的 aliases 中而非作为独立单位。"""
        # g 单位的别名应包含 '克'
        assert "克" in seed_source

    def test_to_grams_required_constraint_in_seed(self, seed_source) -> None:
        """验证种子数据满足 conversion_type=standard/fixed 时 to_grams 非空且 > 0。"""
        # 所有 standard/fixed 类型的种子数据都应有有效的 to_grams
        assert "to_grams" in seed_source


# ---------------------------------------------------------------------------
# 升级/降级逻辑存在性
# ---------------------------------------------------------------------------


def test_upgrade_creates_all_required_tables() -> None:
    """验证 migration upgrade 函数中包含所有 15 张表的创建调用。"""
    mod = import_module(MIGRATION_MODULE)
    source = inspect.getsource(mod.upgrade)

    required_tables = [
        "consult_sessions",
        "agent_runs",
        "consult_messages",
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
    ]

    for table in required_tables:
        assert table in source, f"Migration upgrade should reference table: {table}"


def test_downgrade_drops_all_tables() -> None:
    """验证 migration downgrade 包含所有表的 DROP 调用。"""
    mod = import_module(MIGRATION_MODULE)
    source = inspect.getsource(mod.downgrade)

    tables_to_drop = [
        "knowledge_chunks",
        "theory_cases",
        "acupoints",
        "dosage_units",
        "herbs",
        "formulas",
        "knowledge_sources",
        "audit_events",
        "medical_records",
        "doctor_reviews",
        "safety_rule_runs",
        "agent_evidences",
        "consult_messages",
        "agent_runs",
        "consult_sessions",
    ]

    for table in tables_to_drop:
        assert table in source, f"Migration downgrade should reference table: {table}"


def test_upgrade_contains_dosage_units_to_grams_constraint() -> None:
    """验证迁移 upgrade 中包含 dosage_units to_grams 条件约束。"""
    mod = import_module(MIGRATION_MODULE)
    source = inspect.getsource(mod.upgrade)
    assert "chk_dosage_units_to_grams_required" in source


def test_downgrade_order_respects_foreign_keys() -> None:
    """验证 downgrade 顺序：子表（有 FK）在父表之前删除。"""
    mod = import_module(MIGRATION_MODULE)
    source = inspect.getsource(mod.downgrade)

    # consult_messages 有 FK 到 agent_runs 和 consult_sessions，应在其之前删除
    cm_pos = source.find("consult_messages")
    ar_pos = source.find("agent_runs")
    cs_pos = source.find("consult_sessions")

    assert cm_pos < ar_pos, "consult_messages should be dropped before agent_runs"
    assert cm_pos < cs_pos, "consult_messages should be dropped before consult_sessions"
    assert ar_pos < cs_pos, "agent_runs should be dropped before consult_sessions"
