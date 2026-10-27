"""M1: 检索分块表（Spec §7.1）。

- ``rag_chunk`` 是可重建的派生数据，权威源是 ``seeds/corpus`` + ``index_version``；
- 关键词通道依赖 MySQL 8 的 ``FULLTEXT ... WITH PARSER ngram``（中文按 2-gram 切分），
  该语法 SQLAlchemy 无法表达，因此用 ``op.execute`` 手工建索引；
- 一个索引版本一份数据，切换版本后旧数据保留以便回滚（M3 清理策略负责回收）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0002_rag_chunk"
down_revision: str | None = "0001_m0_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}
NOW_MS = sa.text("CURRENT_TIMESTAMP(3)")


def upgrade() -> None:
    op.create_table(
        "rag_chunk",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("index_version", sa.String(16), nullable=False),
        sa.Column("doc_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(128), nullable=False),
        sa.Column("section", sa.String(255), nullable=False, server_default=sa.text("''")),
        sa.Column("chunk_index", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", mysql.CHAR(64), nullable=False),
        sa.Column("char_count", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=3), nullable=False, server_default=NOW_MS),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("index_version", "doc_id", "chunk_index", name="uk_ver_doc_chunk"),
        **TABLE_OPTIONS,
    )
    op.create_index("idx_ver_doc", "rag_chunk", ["index_version", "doc_id"])
    # 中文关键词召回：ngram parser（默认 ngram_token_size=2）
    op.execute(
        "ALTER TABLE rag_chunk "
        "ADD FULLTEXT INDEX ft_rag_chunk (title, content) WITH PARSER ngram"
    )


def downgrade() -> None:
    op.drop_table("rag_chunk")
