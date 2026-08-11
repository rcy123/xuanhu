"""R6-A async-command migration unit tests.

Verifies the new Alembic revision is importable, chains directly off the
previous head, is the current head, and that its upgrade/downgrade DDL matches
the SQLAlchemy ``AsyncCommand`` model (columns, indexes, constraints). Real
PostgreSQL upgrade/downgrade execution is covered by the integration marker.
"""

from __future__ import annotations

import inspect
from importlib import import_module

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.models.async_command import AsyncCommand
from app.models.consult import ConsultSession

MIGRATION_MODULE = "app.db.migrations.versions.20260729_0016_async_commands"


# ---------------------------------------------------------------------------
# importable + revision chain
# ---------------------------------------------------------------------------


def test_migration_module_importable() -> None:
    mod = import_module(MIGRATION_MODULE)
    assert mod.revision == "20260729_0016"
    assert mod.down_revision == "20260729_0015"
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)


def test_migration_is_the_current_head() -> None:
    directory = ScriptDirectory.from_config(Config("alembic.ini"))
    assert directory.get_current_head() == "20260811_0017"


def test_chain_is_linear_to_previous_head() -> None:
    mod = import_module(MIGRATION_MODULE)
    assert mod.down_revision == "20260729_0015"


# ---------------------------------------------------------------------------
# upgrade DDL presence
# ---------------------------------------------------------------------------


def test_upgrade_creates_table_and_indexes() -> None:
    source = inspect.getsource(import_module(MIGRATION_MODULE).upgrade)
    for fragment in (
        "create_table(",
        '"async_commands"',
        "uq_async_commands_logical_command",
        "uq_async_commands_active_session",
        "idx_async_commands_claim",
        "idx_async_commands_session_created",
        "fk_async_commands_session_id",
    ):
        assert fragment in source, f"upgrade should reference {fragment}"


def test_upgrade_relaxes_outbox_graph_run_id_and_adds_boundary_check() -> None:
    source = inspect.getsource(import_module(MIGRATION_MODULE).upgrade)
    assert "alter_column(\"outbox_events\", \"graph_run_id\"" in source
    assert "nullable=True" in source
    assert "chk_outbox_events_graph_run_boundary" in source


def test_upgrade_includes_invariants() -> None:
    source = inspect.getsource(import_module(MIGRATION_MODULE).upgrade)
    for name in (
        "chk_async_commands_status",
        "chk_async_commands_key_digest",
        "chk_async_commands_request_digest",
        "chk_async_commands_request_object",
        "chk_async_commands_result_object",
        "chk_async_commands_error_object",
        "chk_async_commands_attempt_count",
        "chk_async_commands_http_status",
        "chk_async_commands_error_code",
        "chk_async_commands_lease_relation",
        "chk_async_commands_terminal_payload",
        "chk_async_commands_completed_relation",
        "chk_async_commands_operation_allowlist",
    ):
        assert name in source, f"upgrade should reference {name}"


def test_downgrade_reverses_everything() -> None:
    source = inspect.getsource(import_module(MIGRATION_MODULE).downgrade)
    for fragment in (
        "drop_table(\"async_commands\")",
        "uq_async_commands_active_session",
        "idx_async_commands_claim",
        "idx_async_commands_session_created",
        "chk_outbox_events_graph_run_boundary",
        "nullable=False",
    ):
        assert fragment in source, f"downgrade should reference {fragment}"


# ---------------------------------------------------------------------------
# model <-> migration parity
# ---------------------------------------------------------------------------


def test_model_table_matches_migration() -> None:
    table = AsyncCommand.__table__
    assert table.name == "async_commands"

    expected_columns = {
        "id",
        "session_id",
        "operation",
        "idempotency_key_digest",
        "request_digest",
        "request_payload",
        "status",
        "available_at",
        "attempt_count",
        "lease_owner",
        "lease_token",
        "lease_expires_at",
        "result_http_status",
        "result_payload",
        "error_code",
        "error_payload",
        "created_at",
        "started_at",
        "completed_at",
        "updated_at",
    }
    assert set(table.columns.keys()) == expected_columns


def test_model_foreign_key_targets_consult_sessions() -> None:
    table = AsyncCommand.__table__
    fk = next(iter(table.foreign_keys))
    assert fk.column.table is ConsultSession.__table__
    assert fk.ondelete == "CASCADE"


def test_model_has_active_session_boundary_and_logical_key() -> None:
    table = AsyncCommand.__table__
    constraint_names = {c.name for c in table.constraints}
    assert "uq_async_commands_logical_command" in constraint_names
    index_names = {idx.name for idx in table.indexes}
    assert "uq_async_commands_active_session" in index_names
    assert "idx_async_commands_claim" in index_names
    assert "idx_async_commands_session_created" in index_names

    logical = next(c for c in table.constraints if c.name == "uq_async_commands_logical_command")
    logical_cols = [getattr(col, "name", col) for col in logical.columns]
    assert logical_cols == ["session_id", "operation", "idempotency_key_digest"]

    # The operation DB check constraint matches the finite allowlist constant.
    # (The ORM prepends the ``ck_async_commands_`` naming-convention prefix, so
    # match on the rendered SQL content to validate parity of the constraint.)
    from app.models.async_command import ASYNC_COMMAND_OPERATIONS

    expected_operation_check = (
        "operation IN ("
        + ", ".join(repr(op) for op in sorted(ASYNC_COMMAND_OPERATIONS))
        + ")"
    )
    operation_checks = [
        c
        for c in table.constraints
        if getattr(c, "sqltext", None) is not None
        and c.sqltext.text == expected_operation_check
    ]
    assert operation_checks, f"operation allowlist check not found: {expected_operation_check}"


def test_model_repr_excludes_private_fields() -> None:
    import uuid as _uuid

    cmd = AsyncCommand(
        id=_uuid.uuid4(),
        session_id=_uuid.uuid4(),
        operation="doctor.prescribe",
        idempotency_key_digest="a" * 64,
        request_digest="b" * 64,
        request_payload={"patient": "secret PHI"},
        status="queued",
        attempt_count=0,
    )
    text = repr(cmd)
    assert "secret PHI" not in text
    assert "request_payload" not in text
    assert "lease_token" not in text
    assert "doctor.prescribe" in text


def test_migration_exported_via_models_package() -> None:
    from app.models import AsyncCommand as Exported

    assert Exported is AsyncCommand
