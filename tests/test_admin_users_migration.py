"""Unit coverage for the administrator-account migration contract.

This deliberately inspects the additive migration and ORM invariants without
opening PostgreSQL.  The guarded integration suite exercises the real upgrade
path when a destructive test database is explicitly configured.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from importlib import import_module
from typing import Any, Protocol, cast

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.models.audit import AuditEvent
from app.models.doctor import Doctor

MIGRATION_MODULE = "app.db.migrations.versions.20260813_0020_admin_account_roles"
USERNAME_MIGRATION_MODULE = "app.db.migrations.versions.20260813_0021_doctor_username"


class _ConstrainedTable(Protocol):
    constraints: Iterable[Any]


def _check_texts(table: _ConstrainedTable) -> set[str]:
    return {
        str(constraint.sqltext)
        for constraint in table.constraints
        if getattr(constraint, "sqltext", None) is not None
    }


def test_admin_account_migration_is_current_head() -> None:
    migration = import_module(USERNAME_MIGRATION_MODULE)
    assert migration.revision == "20260813_0021"
    assert migration.down_revision == "20260813_0020"
    assert callable(migration.upgrade)
    assert callable(migration.downgrade)
    assert ScriptDirectory.from_config(Config("alembic.ini")).get_current_head() == migration.revision


def test_username_migration_backfills_and_enforces_unique() -> None:
    source = inspect.getsource(import_module(USERNAME_MIGRATION_MODULE).upgrade)
    for fragment in (
        'add_column("doctors", sa.Column("username"',
        "UPDATE doctors SET username = id::text WHERE username IS NULL",
        "nullable=False",
        'create_unique_constraint("uq_doctors_username"',
    ):
        assert fragment in source


def test_upgrade_backfills_existing_accounts_and_enforces_invariants() -> None:
    source = inspect.getsource(import_module(MIGRATION_MODULE).upgrade)
    for fragment in (
        'add_column("doctors", sa.Column("role"',
        'add_column("doctors", sa.Column("auth_version"',
        "UPDATE doctors SET role = 'doctor' WHERE role IS NULL",
        "UPDATE doctors SET auth_version = 1 WHERE auth_version IS NULL",
        "nullable=False",
        "role IN ('doctor','admin')",
        "auth_version >= 1",
        "actor_type IN ('doctor','admin','agent','system')",
        '"chk_doctors_role"',
        '"chk_doctors_auth_version_positive"',
    ):
        assert fragment in source


def test_downgrade_maps_admin_audit_rows_before_restoring_old_constraint() -> None:
    source = inspect.getsource(import_module(MIGRATION_MODULE).downgrade)
    assert "UPDATE audit_events SET actor_type = 'system' WHERE actor_type = 'admin'" in source
    assert "actor_type IN ('doctor','agent','system')" in source
    assert 'op.f("ck_doctors_chk_doctors_auth_version_positive")' in source
    assert 'op.f("ck_doctors_chk_doctors_role")' in source
    assert 'drop_column("doctors", "auth_version")' in source
    assert 'drop_column("doctors", "role")' in source


def test_orm_exposes_role_and_auth_version_with_matching_constraints() -> None:
    table = Doctor.__table__
    assert {"role", "auth_version", "username"} <= set(table.columns.keys())
    assert table.c.role.nullable is False
    assert table.c.auth_version.nullable is False
    assert table.c.username.nullable is False
    assert str(table.c.role.server_default.arg) == "doctor"
    assert str(table.c.auth_version.server_default.arg) == "1"
    checks = _check_texts(cast(_ConstrainedTable, table))
    assert "role IN ('doctor','admin')" in checks
    assert "auth_version >= 1" in checks


def test_audit_orm_accepts_admin_actor_type() -> None:
    checks = _check_texts(cast(_ConstrainedTable, AuditEvent.__table__))
    assert "actor_type IN ('doctor','admin','agent','system')" in checks
