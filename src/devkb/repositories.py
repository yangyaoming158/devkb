"""数据访问层（D7 数据隔离纪律）：

- 本文件是 src/devkb 下唯一允许构造 sqlalchemy 查询的位置（tests/unit/test_layering.py 静态强制）；
- 除 ProjectRepo（管理项目实体本身）外，Repository 以 project_id 实例化，
  查询方法不接受外部传入的 project_id——跨项目访问在 API 层面即不可表达。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from devkb.models import AgentRun, Chunk, Document, Project


class ProjectRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, slug: str, name: str) -> Project:
        project = Project(slug=slug, name=name)
        self._session.add(project)
        await self._session.flush()
        return project

    async def get_by_slug(self, slug: str) -> Project | None:
        return (
            await self._session.execute(select(Project).where(Project.slug == slug))
        ).scalar_one_or_none()


@dataclass(frozen=True)
class ChunkDraft:
    """摄取管道产出的待入库块（无身份，身份由仓储赋予）。"""

    ordinal: int
    title_path: str
    content: str
    content_hash: str
    token_count: int
    start_line: int
    end_line: int
    embedding: list[float] | None = None


class DocumentRepo:
    def __init__(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        self._session = session
        self._project_id = project_id

    async def get_by_rel_path(self, rel_path: str) -> Document | None:
        stmt = select(Document).where(
            Document.project_id == self._project_id, Document.rel_path == rel_path
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def upsert(
        self, *, rel_path: str, title: str, doc_type: str, content_hash: str
    ) -> Document:
        stmt = (
            pg_insert(Document)
            .values(
                id=uuid.uuid4(),
                project_id=self._project_id,
                rel_path=rel_path,
                title=title,
                doc_type=doc_type,
                content_hash=content_hash,
                status="active",
                parse_error=None,
            )
            .on_conflict_do_update(
                index_elements=[Document.project_id, Document.rel_path],
                set_={
                    "title": title,
                    "doc_type": doc_type,
                    "content_hash": content_hash,
                    "status": "active",
                    "parse_error": None,
                    "updated_at": func.now(),
                },
            )
            .returning(Document)
        )
        # populate_existing：同一行已在身份映射中时，用 RETURNING 的新值刷新旧对象
        # （否则二次 upsert 会拿到第一次的陈旧属性——集成测试抓到的真实陷阱）
        result = await self._session.execute(stmt, execution_options={"populate_existing": True})
        return result.scalar_one()

    async def mark_failed(self, rel_path: str, error: str) -> None:
        stmt = (
            pg_insert(Document)
            .values(
                id=uuid.uuid4(),
                project_id=self._project_id,
                rel_path=rel_path,
                title="",
                doc_type="markdown",
                content_hash="",
                status="failed",
                parse_error=error[:2000],
            )
            .on_conflict_do_update(
                index_elements=[Document.project_id, Document.rel_path],
                set_={"status": "failed", "parse_error": error[:2000], "updated_at": func.now()},
            )
        )
        await self._session.execute(stmt)

    async def list_active(self) -> Sequence[Document]:
        stmt = (
            select(Document)
            .where(Document.project_id == self._project_id, Document.status == "active")
            .order_by(Document.rel_path)
        )
        return (await self._session.execute(stmt)).scalars().all()


class ChunkRepo:
    def __init__(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        self._session = session
        self._project_id = project_id

    async def replace_for_document(
        self, document_id: uuid.UUID, drafts: Sequence[ChunkDraft]
    ) -> int:
        """单文档事务内删旧插新（规格 §7）。"""
        await self._session.execute(
            delete(Chunk).where(
                Chunk.project_id == self._project_id, Chunk.document_id == document_id
            )
        )
        self._session.add_all(
            Chunk(
                project_id=self._project_id,
                document_id=document_id,
                ordinal=d.ordinal,
                title_path=d.title_path,
                content=d.content,
                content_hash=d.content_hash,
                token_count=d.token_count,
                start_line=d.start_line,
                end_line=d.end_line,
                embedding=d.embedding,
            )
            for d in drafts
        )
        await self._session.flush()
        return len(drafts)

    async def vector_search(
        self, embedding: list[float], top_k: int
    ) -> list[tuple[Chunk, str, float]]:
        """余弦相似度精确扫描（P0 无 HNSW），返回 (chunk, 所属文档 rel_path, 相似度) 降序。

        仅检索 status='active' 文档的 chunks：active 文档更新失败时事务回滚会保留
        上一版 chunks（文档已标 failed），不过滤会引用与当前文件行号不符的陈旧内容。
        """
        distance = Chunk.embedding.cosine_distance(embedding).label("distance")
        stmt = (
            select(Chunk, Document.rel_path, distance)
            .join(Document, Chunk.document_id == Document.id)
            .where(
                Chunk.project_id == self._project_id,
                Chunk.embedding.is_not(None),
                Document.status == "active",
            )
            .order_by(distance)
            .limit(top_k)
        )
        rows = (await self._session.execute(stmt)).all()
        return [(row[0], row[1], 1.0 - float(row[2])) for row in rows]

    async def count(self) -> int:
        stmt = select(func.count()).where(Chunk.project_id == self._project_id)
        return (await self._session.execute(stmt)).scalar_one()


class RunRepo:
    def __init__(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        self._session = session
        self._project_id = project_id

    async def create(self, question: str) -> AgentRun:
        run = AgentRun(project_id=self._project_id, question=question, status="running")
        self._session.add(run)
        await self._session.flush()
        return run

    async def finish(
        self,
        run_id: uuid.UUID,
        *,
        answer: dict[str, Any],
        model: str,
        tokens_in: int,
        tokens_out: int,
        usage: dict[str, Any] | None,
        cost: Decimal | None,
        latency_ms: int,
        status: str = "succeeded",
    ) -> None:
        run = await self.get(run_id)
        if run is None:
            return
        run.answer = answer
        run.model = model
        run.tokens_in = tokens_in
        run.tokens_out = tokens_out
        run.usage = usage
        run.cost = cost
        run.latency_ms = latency_ms
        run.status = status
        await self._session.flush()

    async def get(self, run_id: uuid.UUID) -> AgentRun | None:
        stmt = select(AgentRun).where(
            AgentRun.project_id == self._project_id, AgentRun.id == run_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_recent(self, limit: int = 20) -> Sequence[AgentRun]:
        stmt = (
            select(AgentRun)
            .where(AgentRun.project_id == self._project_id)
            .order_by(AgentRun.created_at.desc())
            .limit(limit)
        )
        return (await self._session.execute(stmt)).scalars().all()
