"""P1 扩展（《P1实现规格》§5）：chunks 检索字段 + agent_steps / tool_invocations。

- `search_text`：应用层规范化 token 流，NOT NULL DEFAULT ''；已有 P0 行升级后为
  空串，真实内容由 T12.2 的应用层 backfill 写入（幂等、可重跑），不要求重摄/重嵌。
- `search_tsv`：STORED 生成列 `to_tsvector('simple', search_text)`。选生成列而非
  Repository 同步写入：一致性由数据库保证、无双写漂移，downgrade 只需删列（可逆
  性最简，符合 §5.1 "以迁移 spike 可逆性为准"）。
- HNSW 建在部分索引 `WHERE embedding IS NOT NULL` 上（§5.1 只覆盖非空 embedding），
  参数显式固定 m=16 / ef_construction=64（pgvector 默认值，写死以便复现）。
  vector(1024) 维度沿用 ADR-0002，不得改动。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chunks", sa.Column("search_text", sa.Text(), nullable=False, server_default="")
    )
    op.add_column(
        "chunks",
        sa.Column(
            "search_tsv",
            TSVECTOR(),
            sa.Computed("to_tsvector('simple', search_text)", persisted=True),
            nullable=True,
        ),
    )
    op.create_index("ix_chunks_search_tsv", "chunks", ["search_tsv"], postgresql_using="gin")
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON chunks "
        "USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64) "
        "WHERE embedding IS NOT NULL"
    )

    op.create_table(
        "agent_steps",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("node", sa.String(32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("input_summary", JSONB(), nullable=True),
        sa.Column("output_summary", JSONB(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("run_id", "seq"),
    )

    op.create_table(
        "tool_invocations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "step_id",
            sa.Uuid(),
            sa.ForeignKey("agent_steps.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("arguments", JSONB(), nullable=True),
        sa.Column("result_summary", JSONB(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_tool_invocations_run_id", "tool_invocations", ["run_id"])
    op.create_index("ix_tool_invocations_step_id", "tool_invocations", ["step_id"])


def downgrade() -> None:
    op.drop_table("tool_invocations")
    op.drop_table("agent_steps")
    op.execute("DROP INDEX ix_chunks_embedding_hnsw")
    op.drop_index("ix_chunks_search_tsv", table_name="chunks")
    op.drop_column("chunks", "search_tsv")
    op.drop_column("chunks", "search_text")
