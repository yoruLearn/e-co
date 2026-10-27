"""数据层契约测试：ORM 模型 ↔ Alembic 迁移 ↔ 表清单三向一致。

M0 的表清单（Spec §10.1）与 M1 增量表分开登记，避免"新增一张表就悄悄改了 Spec 契约"。
"""

from __future__ import annotations

import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.common.config import Settings
from app.db.models import (
    ALL_MODELS,
    ALL_TABLES,
    EXPECTED_TABLES,
    M1_TABLES,
    Base,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_DIR = REPO_ROOT / "app" / "migrations" / "versions"
INITIAL_MIGRATION = MIGRATION_DIR / "0001_m0_initial_schema.py"
RAG_CHUNK_MIGRATION = MIGRATION_DIR / "0002_rag_chunk.py"


def _created_tables(path: Path) -> set[str]:
    sql = path.read_text(encoding="utf-8")
    return set(re.findall(r'op\.create_table\(\s*"([a-z_]+)"', sql))


def _dropped_tables(path: Path) -> set[str]:
    sql = path.read_text(encoding="utf-8")
    return set(re.findall(r'op\.drop_table\(\s*"([a-z_]+)"', sql))


def test_models_cover_spec_and_m1_tables() -> None:
    assert set(Base.metadata.tables) == set(ALL_TABLES)
    assert len(ALL_MODELS) == len(EXPECTED_TABLES) + len(M1_TABLES)
    assert len(EXPECTED_TABLES) == 9


def test_m1_increment_is_declared_explicitly() -> None:
    assert {"rag_chunk"} == M1_TABLES
    assert M1_TABLES.isdisjoint(EXPECTED_TABLES)


def test_initial_migration_creates_exactly_the_spec_tables() -> None:
    assert _created_tables(INITIAL_MIGRATION) == set(EXPECTED_TABLES)


def test_initial_migration_drop_covers_every_spec_table() -> None:
    assert _dropped_tables(INITIAL_MIGRATION) == set(EXPECTED_TABLES)


def test_rag_chunk_migration_matches_model() -> None:
    assert _created_tables(RAG_CHUNK_MIGRATION) == set(M1_TABLES)
    assert _dropped_tables(RAG_CHUNK_MIGRATION) == set(M1_TABLES)


def test_all_migrations_union_equals_all_tables() -> None:
    created: set[str] = set()
    dropped: set[str] = set()
    for migration in sorted(MIGRATION_DIR.glob("*.py")):
        created |= _created_tables(migration)
        dropped |= _dropped_tables(migration)
    assert created == set(ALL_TABLES)
    assert dropped == set(ALL_TABLES)


def test_rag_chunk_has_fulltext_ngram_index() -> None:
    sql = RAG_CHUNK_MIGRATION.read_text(encoding="utf-8")
    assert "FULLTEXT INDEX ft_rag_chunk" in sql
    assert "WITH PARSER ngram" in sql, "中文关键词召回必须使用 ngram parser"


def test_rag_chunk_model_columns() -> None:
    table = Base.metadata.tables["rag_chunk"]
    for column in (
        "index_version",
        "doc_id",
        "title",
        "section",
        "chunk_index",
        "content",
        "content_hash",
        "char_count",
    ):
        assert column in table.columns
    unique_names = {constraint.name for constraint in table.constraints}
    assert "uk_ver_doc_chunk" in unique_names


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


def test_alembic_head_is_latest_migration() -> None:
    config = Config()
    config.set_main_option("script_location", str(REPO_ROOT / "app" / "migrations"))
    script = ScriptDirectory.from_config(config)
    assert script.get_current_head() == "0002_rag_chunk"
    revisions = list(script.walk_revisions())
    assert len(revisions) == 2


def test_alembic_revision_chain_is_linear() -> None:
    config = Config()
    config.set_main_option("script_location", str(REPO_ROOT / "app" / "migrations"))
    script = ScriptDirectory.from_config(config)
    head = script.get_revision("0002_rag_chunk")
    assert head.down_revision == "0001_m0_initial"
    base = script.get_revision("0001_m0_initial")
    assert base.down_revision is None


def test_settings_migrations_dir_points_to_alembic_scripts() -> None:
    settings = Settings(_env_file=None)
    assert (settings.migrations_dir / "env.py").exists()
    assert (settings.migrations_dir / "versions" / "0001_m0_initial_schema.py").exists()
    assert (settings.migrations_dir / "versions" / "0002_rag_chunk.py").exists()
