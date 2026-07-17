"""ORM 模型：P0 四张表（《P0实现规格》§5）+ P1 检索字段与轨迹表（《P1实现规格》§5）。

EMBEDDING_DIM 由 ADR-0002 冻结为 1024（Qwen3-Embedding-0.6B），改动需新 ADR + 全量重嵌。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EMBEDDING_DIM = 1024  # ADR-0002


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("project_id", "rel_path"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    rel_path: Mapped[str] = mapped_column(String(1024))
    title: Mapped[str] = mapped_column(String(512), default="")
    doc_type: Mapped[str] = mapped_column(String(16))  # 'markdown' | 'text'
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="active")  # 'active' | 'failed'
    parse_error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class Chunk(Base):
    """chunks 的 GIN(search_tsv) 与部分 HNSW(embedding) 索引在迁移 0002 中创建，
    不在 ORM 声明（partial index 的 WHERE 需要 text()，与 D7 分层扫描冲突）。"""

    __tablename__ = "chunks"
    __table_args__ = (Index("ix_chunks_project_document", "project_id", "document_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    title_path: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    token_count: Mapped[int] = mapped_column(Integer)
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[Any | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    # P1 FTS：search_text 为应用层生成的规范化 token 流（T12.2 backfill / T14 冻结函数）；
    # search_tsv 用 STORED 生成列而非 Repository 同步写入——一致性由数据库保证，降级只需删列
    search_text: Mapped[str] = mapped_column(Text, default="", server_default="")
    search_tsv: Mapped[Any | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', search_text)", persisted=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AgentRun(Base):
    __tablename__ = "agent_runs"
    # (id, project_id) 唯一：供轨迹表复合 FK 引用，从库层锁死 step 与 run 同项目（0003）
    __table_args__ = (UniqueConstraint("id", "project_id", name="uq_agent_runs_id_project"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    model: Mapped[str | None] = mapped_column(String(64), default=None)
    tokens_in: Mapped[int | None] = mapped_column(Integer, default=None)  # 合计值，便于查询
    tokens_out: Mapped[int | None] = mapped_column(Integer, default=None)
    # 供应商原始 usage（含 prompt_cache_hit_tokens 等三档字段），计价复算以此为准（ADR-0004）
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), default=None)
    latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    status: Mapped[str] = mapped_column(String(16), default="running")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AgentStep(Base):
    """P1 节点轨迹（《P1实现规格》§5.2）：每个 LangGraph 节点执行一行，含降级/失败。

    input_summary/output_summary 只存有限摘要——不写 chain-of-thought、secret 或整篇正文。
    (run_id, project_id) 复合 FK 使"step 挂到别的项目的 run"在库层即 IntegrityError（0003）。
    """

    __tablename__ = "agent_steps"
    __table_args__ = (
        UniqueConstraint("run_id", "seq"),
        # (id, run_id) 唯一：供 tool_invocations 复合 FK 引用，锁死 tool 与 step 同 run
        UniqueConstraint("id", "run_id", name="uq_agent_steps_id_run"),
        ForeignKeyConstraint(
            ["run_id", "project_id"],
            ["agent_runs.id", "agent_runs.project_id"],
            name="fk_agent_steps_run_project",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    seq: Mapped[int] = mapped_column(Integer)
    node: Mapped[str] = mapped_column(String(32))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16))
    input_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    output_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ToolInvocation(Base):
    """P1 工具轨迹（《P1实现规格》§5.3）：检索等确定性能力每次调用一行。

    arguments/result_summary 只存脱敏参数与摘要——不写 API key、完整 Prompt 或文档正文。
    两个复合 FK 使"tool 的 step 属于别的 run"或"run 属于别的项目"在库层即 IntegrityError（0003）。
    """

    __tablename__ = "tool_invocations"
    __table_args__ = (
        # FK 列的 btree：run 级回放读取与级联删除都按这两列查
        Index("ix_tool_invocations_run_id", "run_id"),
        Index("ix_tool_invocations_step_id", "step_id"),
        ForeignKeyConstraint(
            ["run_id", "project_id"],
            ["agent_runs.id", "agent_runs.project_id"],
            name="fk_tool_invocations_run_project",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["step_id", "run_id"],
            ["agent_steps.id", "agent_steps.run_id"],
            name="fk_tool_invocations_step_run",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    step_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64))
    arguments: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    status: Mapped[str] = mapped_column(String(16))
    latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
