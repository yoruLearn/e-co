"""数据层契约测试：ORM 模型 ↔ Alembic 迁移 ↔ Spec §10.1 三向一致。"""

from __future__ import annotations

import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.common.config import Settings
from app.db.models import ALL_MODELS, EXPECTED_TABLES, Base

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_DIR = REPO_ROOT / "app" / "migrations" / "versions"
INITIAL_MIGRATION = MIGRATION_DIR / "0001_m0_initial_schema.py"


def test_models_cover_all_spec_tables() -> None:
    assert set(Base.metadata.tables) == set(EXPECTED_TABLES)
    assert len(ALL_MODELS) == 9


def test_migration_creates_exactly_the_spec_tables() -> None:
    sql = INITIAL_MIGRATION.read_text(encoding="utf-8")
    created = set(re.findall(r'op\.create_table\(\s*"([a-z_]+)"', sql))
    assert created == set(EXPECTED_TABLES)


def test_migration_drop_covers_every_table() -> None:
    sql = INITIAL_MIGRATION.read_text(encoding="utf-8")
    dropped = set(re.findall(r'op\.drop_table\(\s*"([a-z_]+)"', sql))
    assert dropped == set(EXPECTED_TABLES)


def test_aftersales_biz_has_idempotency_and_optimistic_lock() -> None:
    table = Base.metadata.tables["aftersales_biz"]
    for column in ("biz_id", "idem_key", "version", "trace_id", "status", "action"):
        assert column in table.columns
    unique_names = {constraint.name for constraint in table.constraints}
    assert {"uk_biz", "uk_idem"} <= unique_names


def test_outbox_dispatch_index_exists() -> None:
    table = Base.metadata.tables["outbox_message"]
    assert "idx_dispatch" in {index.name for index in table.indexes}
    assert "msg_id" in table.columns
    assert "next_retry_at" in table.columns


def test_approval_task_callback_counter_exists() -> None:
    table = Base.metadata.tables["approval_task"]
    assert "callback_count" in table.columns
    assert "expires_at" in table.columns


def test_alembic_head_is_single_initial_revision() -> None:
    config = Config()
    config.set_main_option("script_location", str(REPO_ROOT / "app" / "migrations"))
    script = ScriptDirectory.from_config(config)
    assert script.get_current_head() == "0001_m0_initial"
    assert list(script.walk_revisions()) != []


def test_settings_migrations_dir_points_to_alembic_scripts() -> None:
    settings = Settings(_env_file=None)
    assert (settings.migrations_dir / "env.py").exists()
    assert (settings.migrations_dir / "versions" / "0001_m0_initial_schema.py").exists()
