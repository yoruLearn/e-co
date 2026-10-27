"""SQLAlchemy 2.0 模型（Spec §10.1 DDL 的 ORM 映射）。

约定：

- 类型使用 MySQL 原生类型（``DATETIME(3)`` / ``ENUM`` / ``INT UNSIGNED`` / ``JSON``），
  与 Spec DDL 一一对应；
- ``version`` 为乐观锁列（§5.2 约束 2），业务侧只允许通过 ``state_machine`` 修改；
- 迁移脚本是 schema 的权威来源，模型与迁移的表集合由单测守护。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.mysql import (
    BIGINT,
    DATETIME,
    DECIMAL,
    ENUM,
    INTEGER,
    JSON,
    MEDIUMTEXT,
    TINYINT,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

_MYSQL_TABLE_OPTIONS: dict[str, Any] = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}
_NOW_MS = text("CURRENT_TIMESTAMP(3)")


class Base(DeclarativeBase):
    """声明式基类；表选项（InnoDB/utf8mb4）由各模型显式给出。"""


class AfterSalesBiz(Base):
    """售后单主表（办事链路）。"""

    __tablename__ = "aftersales_biz"
    __table_args__ = (
        UniqueConstraint("biz_id", name="uk_biz"),
        UniqueConstraint("idem_key", name="uk_idem"),
        Index("idx_user_order", "user_id", "order_id"),
        Index("idx_status_time", "status", "created_at"),
        _MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    biz_id: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    order_id: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(ENUM("refund", "return", "exchange"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 2), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    idem_key: Mapped[str] = mapped_column(String(80), nullable=False)
    version: Mapped[int] = mapped_column(
        INTEGER(unsigned=True), nullable=False, default=0, server_default=text("0")
    )
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=3), nullable=False, server_default=_NOW_MS
    )
    updated_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=3), nullable=False, server_default=_NOW_MS, onupdate=_NOW_MS
    )


class AfterSalesTransition(Base):
    """状态流水（审计 + 时间线，不可变）。"""

    __tablename__ = "aftersales_transition"
    __table_args__ = (Index("idx_biz", "biz_id", "created_at"), _MYSQL_TABLE_OPTIONS)

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    biz_id: Mapped[str] = mapped_column(String(32), nullable=False)
    from_status: Mapped[str] = mapped_column(String(32), nullable=False)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=3), nullable=False, server_default=_NOW_MS
    )


class ApprovalTask(Base):
    """HITL 审批任务。"""

    __tablename__ = "approval_task"
    __table_args__ = (
        UniqueConstraint("task_id", name="uk_task"),
        Index("idx_biz", "biz_id"),
        Index("idx_pending", "status", "expires_at"),
        _MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(32), nullable=False)
    biz_id: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        ENUM("pending", "approved", "rejected", "expired"),
        nullable=False,
        default="pending",
        server_default=text("'pending'"),
    )
    threshold: Mapped[Decimal] = mapped_column(DECIMAL(10, 2), nullable=False)
    approver: Mapped[str | None] = mapped_column(String(64), nullable=True)
    comment: Mapped[str | None] = mapped_column(String(255), nullable=True)
    callback_count: Mapped[int] = mapped_column(
        INTEGER(unsigned=True), nullable=False, default=0, server_default=text("0")
    )
    expires_at: Mapped[datetime] = mapped_column(DATETIME(fsp=0), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=3), nullable=False, server_default=_NOW_MS
    )


class OutboxMessage(Base):
    """本地消息表（与业务同事务写入，Spec §10.2）。"""

    __tablename__ = "outbox_message"
    __table_args__ = (
        UniqueConstraint("msg_id", name="uk_msg"),
        Index("idx_dispatch", "status", "next_retry_at"),
        Index("idx_topic", "topic", "created_at"),
        _MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    msg_id: Mapped[str] = mapped_column(String(64), nullable=False)
    topic: Mapped[str] = mapped_column(String(64), nullable=False)
    sharding_key: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        ENUM("pending", "published", "consumed", "dead"),
        nullable=False,
        default="pending",
        server_default=text("'pending'"),
    )
    retry: Mapped[int] = mapped_column(
        INTEGER(unsigned=True), nullable=False, default=0, server_default=text("0")
    )
    next_retry_at: Mapped[datetime] = mapped_column(DATETIME(fsp=0), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=3), nullable=False, server_default=_NOW_MS
    )


class IndexVersion(Base):
    """RAG 索引版本（可回滚）。"""

    __tablename__ = "index_version"
    __table_args__ = (
        UniqueConstraint("version", name="uk_version"),
        Index("idx_status", "status"),
        _MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    version: Mapped[str] = mapped_column(String(16), nullable=False)
    collection: Mapped[str] = mapped_column(String(64), nullable=False)
    doc_count: Mapped[int] = mapped_column(
        INTEGER(unsigned=True), nullable=False, default=0, server_default=text("0")
    )
    status: Mapped[str] = mapped_column(
        ENUM("building", "active", "deprecated", "rolled_back"), nullable=False
    )
    eval_score: Mapped[Decimal | None] = mapped_column(DECIMAL(5, 4), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=3), nullable=False, server_default=_NOW_MS
    )


class PromptRegistry(Base):
    """Prompt 注册表（Spec §6.3）。"""

    __tablename__ = "prompt_registry"
    __table_args__ = (
        UniqueConstraint("prompt_key", "version", name="uk_key_ver"),
        Index("idx_published", "prompt_key", "status"),
        _MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    prompt_key: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    content: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    status: Mapped[str] = mapped_column(
        ENUM("draft", "published", "deprecated"),
        nullable=False,
        default="draft",
        server_default=text("'draft'"),
    )
    eval_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=3), nullable=False, server_default=_NOW_MS, onupdate=_NOW_MS
    )


class EvalCase(Base):
    """评测用例（Spec §13.3 四类）。"""

    __tablename__ = "eval_case"
    __table_args__ = (UniqueConstraint("case_key", name="uk_case"), _MYSQL_TABLE_OPTIONS)

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    case_key: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(
        ENUM("typical", "multi_hop", "no_answer", "multi_turn"), nullable=False
    )
    input: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    expectation: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    enabled: Mapped[int] = mapped_column(
        TINYINT(1), nullable=False, default=1, server_default=text("1")
    )


class EvalRun(Base):
    """评测运行结果。"""

    __tablename__ = "eval_run"
    __table_args__ = (UniqueConstraint("run_id", name="uk_run"), _MYSQL_TABLE_OPTIONS)

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    suite: Mapped[str] = mapped_column(String(32), nullable=False)
    total: Mapped[int] = mapped_column(INTEGER, nullable=False)
    pass_cnt: Mapped[int] = mapped_column(INTEGER, nullable=False)
    citation_acc: Mapped[Decimal | None] = mapped_column(DECIMAL(5, 4), nullable=True)
    p95_ms: Mapped[int | None] = mapped_column(INTEGER, nullable=True)
    report: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DATETIME(fsp=3), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=3), nullable=True)


class ToolAudit(Base):
    """工具调用审计（§8.2 第 5 条）。"""

    __tablename__ = "tool_audit"
    __table_args__ = (
        Index("idx_trace", "trace_id"),
        Index("idx_user_time", "user_id", "created_at"),
        _MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[str] = mapped_column(ENUM("ro", "write"), nullable=False)
    params_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    result_code: Mapped[str] = mapped_column(String(32), nullable=False)
    cost_ms: Mapped[int] = mapped_column(INTEGER, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=3), nullable=False, server_default=_NOW_MS
    )


ALL_MODELS = (
    AfterSalesBiz,
    AfterSalesTransition,
    ApprovalTask,
    OutboxMessage,
    IndexVersion,
    PromptRegistry,
    EvalCase,
    EvalRun,
    ToolAudit,
)

# 供契约测试使用（与 Spec §10.1 的 9 张表一一对应）
EXPECTED_TABLES = frozenset(
    {
        "aftersales_biz",
        "aftersales_transition",
        "approval_task",
        "outbox_message",
        "index_version",
        "prompt_registry",
        "eval_case",
        "eval_run",
        "tool_audit",
    }
)
