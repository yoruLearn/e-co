"""M1: 调用树表（Spec §13.4）。

- 每个图节点一行；``(trace_id, start_ms)`` 支撑按时间还原调用顺序；
- 保留 ``meta`` JSON 便于后续补 tokens/缓存命中/检索分数，不频繁改表；
- trace 是可重建的观测数据，TTL 清理放 M3（当前不做自动删除）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0003_trace_span"
down_revision: str | None = "0002_rag_chunk"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}
NOW_MS = sa.text("CURRENT_TIMESTAMP(3)")


def upgrade() -> None:
    op.create_table(
        "trace_span",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("trace_id", mysql.CHAR(36), nullable=False),
        sa.Column("node", sa.String(32), nullable=False),
        sa.Column("start_ms", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("duration_ms", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'ok'")),
        sa.Column("tokens", mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0")),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=3), nullable=False, server_default=NOW_MS),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine=TABLE_OPTIONS["mysql_engine"],
        mysql_charset=TABLE_OPTIONS["mysql_charset"],
    )
    op.create_index("idx_trace_span_trace", "trace_span", ["trace_id", "start_ms"])


def downgrade() -> None:
    op.drop_index("idx_trace_span_trace", table_name="trace_span")
    op.drop_table("trace_span")
