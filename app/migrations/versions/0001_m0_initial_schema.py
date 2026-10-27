"""M0 初始 schema（Spec §10.1 全部 9 张表）。

约定（Spec §17.3）：迁移只做**向前兼容**变更；破坏性变更走 expand-contract，
下个版本才删列/收紧约束。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0001_m0_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}
NOW_MS = sa.text("CURRENT_TIMESTAMP(3)")


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # 售后单主表
    # ------------------------------------------------------------------ #
    op.create_table(
        "aftersales_biz",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("biz_id", sa.String(32), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("order_id", sa.String(32), nullable=False),
        sa.Column("action", mysql.ENUM("refund", "return", "exchange"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("amount", mysql.DECIMAL(10, 2), nullable=True),
        sa.Column("reason_code", sa.String(32), nullable=True),
        sa.Column("idem_key", sa.String(80), nullable=False),
        sa.Column("version", mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0")),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=3), nullable=False, server_default=NOW_MS),
        sa.Column("updated_at", mysql.DATETIME(fsp=3), nullable=False, server_default=NOW_MS),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("biz_id", name="uk_biz"),
        sa.UniqueConstraint("idem_key", name="uk_idem"),
        **TABLE_OPTIONS,
    )
    op.create_index("idx_user_order", "aftersales_biz", ["user_id", "order_id"])
    op.create_index("idx_status_time", "aftersales_biz", ["status", "created_at"])

    # ------------------------------------------------------------------ #
    # 状态流水
    # ------------------------------------------------------------------ #
    op.create_table(
        "aftersales_transition",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("biz_id", sa.String(32), nullable=False),
        sa.Column("from_status", sa.String(32), nullable=False),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("version", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=3), nullable=False, server_default=NOW_MS),
        sa.PrimaryKeyConstraint("id"),
        **TABLE_OPTIONS,
    )
    op.create_index("idx_biz", "aftersales_transition", ["biz_id", "created_at"])

    # ------------------------------------------------------------------ #
    # HITL 审批任务
    # ------------------------------------------------------------------ #
    op.create_table(
        "approval_task",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.String(32), nullable=False),
        sa.Column("biz_id", sa.String(32), nullable=False),
        sa.Column(
            "status",
            mysql.ENUM("pending", "approved", "rejected", "expired"),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("threshold", mysql.DECIMAL(10, 2), nullable=False),
        sa.Column("approver", sa.String(64), nullable=True),
        sa.Column("comment", sa.String(255), nullable=True),
        sa.Column(
            "callback_count", mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("expires_at", mysql.DATETIME(fsp=0), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=3), nullable=False, server_default=NOW_MS),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", name="uk_task"),
        **TABLE_OPTIONS,
    )
    op.create_index("idx_biz", "approval_task", ["biz_id"])
    op.create_index("idx_pending", "approval_task", ["status", "expires_at"])

    # ------------------------------------------------------------------ #
    # outbox 本地消息表
    # ------------------------------------------------------------------ #
    op.create_table(
        "outbox_message",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("msg_id", sa.String(64), nullable=False),
        sa.Column("topic", sa.String(64), nullable=False),
        sa.Column("sharding_key", sa.String(64), nullable=False),
        sa.Column("payload", mysql.JSON, nullable=False),
        sa.Column(
            "status",
            mysql.ENUM("pending", "published", "consumed", "dead"),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("retry", mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0")),
        sa.Column("next_retry_at", mysql.DATETIME(fsp=0), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=3), nullable=False, server_default=NOW_MS),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("msg_id", name="uk_msg"),
        **TABLE_OPTIONS,
    )
    op.create_index("idx_dispatch", "outbox_message", ["status", "next_retry_at"])
    op.create_index("idx_topic", "outbox_message", ["topic", "created_at"])

    # ------------------------------------------------------------------ #
    # RAG 索引版本
    # ------------------------------------------------------------------ #
    op.create_table(
        "index_version",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("version", sa.String(16), nullable=False),
        sa.Column("collection", sa.String(64), nullable=False),
        sa.Column("doc_count", mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "status", mysql.ENUM("building", "active", "deprecated", "rolled_back"), nullable=False
        ),
        sa.Column("eval_score", mysql.DECIMAL(5, 4), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=3), nullable=False, server_default=NOW_MS),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version", name="uk_version"),
        **TABLE_OPTIONS,
    )
    op.create_index("idx_status", "index_version", ["status"])

    # ------------------------------------------------------------------ #
    # Prompt 注册表
    # ------------------------------------------------------------------ #
    op.create_table(
        "prompt_registry",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("prompt_key", sa.String(64), nullable=False),
        sa.Column("version", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("content", mysql.MEDIUMTEXT, nullable=False),
        sa.Column(
            "status",
            mysql.ENUM("draft", "published", "deprecated"),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column("eval_run_id", sa.String(64), nullable=True),
        sa.Column("updated_by", sa.String(64), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=3), nullable=False, server_default=NOW_MS),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("prompt_key", "version", name="uk_key_ver"),
        **TABLE_OPTIONS,
    )
    op.create_index("idx_published", "prompt_registry", ["prompt_key", "status"])

    # ------------------------------------------------------------------ #
    # 评测用例 / 运行
    # ------------------------------------------------------------------ #
    op.create_table(
        "eval_case",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("case_key", sa.String(64), nullable=False),
        sa.Column(
            "category", mysql.ENUM("typical", "multi_hop", "no_answer", "multi_turn"), nullable=False
        ),
        sa.Column("input", mysql.JSON, nullable=False),
        sa.Column("expectation", mysql.JSON, nullable=False),
        sa.Column("enabled", mysql.TINYINT(1), nullable=False, server_default=sa.text("1")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_key", name="uk_case"),
        **TABLE_OPTIONS,
    )

    op.create_table(
        "eval_run",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("suite", sa.String(32), nullable=False),
        sa.Column("total", mysql.INTEGER, nullable=False),
        sa.Column("pass_cnt", mysql.INTEGER, nullable=False),
        sa.Column("citation_acc", mysql.DECIMAL(5, 4), nullable=True),
        sa.Column("p95_ms", mysql.INTEGER, nullable=True),
        sa.Column("report", mysql.JSON, nullable=False),
        sa.Column("started_at", mysql.DATETIME(fsp=3), nullable=False),
        sa.Column("finished_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", name="uk_run"),
        **TABLE_OPTIONS,
    )

    # ------------------------------------------------------------------ #
    # 工具审计
    # ------------------------------------------------------------------ #
    op.create_table(
        "tool_audit",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("tool", sa.String(64), nullable=False),
        sa.Column("level", mysql.ENUM("ro", "write"), nullable=False),
        sa.Column("params_hash", mysql.CHAR(64), nullable=False),
        sa.Column("result_code", sa.String(32), nullable=False),
        sa.Column("cost_ms", mysql.INTEGER, nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=3), nullable=False, server_default=NOW_MS),
        sa.PrimaryKeyConstraint("id"),
        **TABLE_OPTIONS,
    )
    op.create_index("idx_trace", "tool_audit", ["trace_id"])
    op.create_index("idx_user_time", "tool_audit", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_table("tool_audit")
    op.drop_table("eval_run")
    op.drop_table("eval_case")
    op.drop_table("prompt_registry")
    op.drop_table("index_version")
    op.drop_table("outbox_message")
    op.drop_table("approval_task")
    op.drop_table("aftersales_transition")
    op.drop_table("aftersales_biz")
