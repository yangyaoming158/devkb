"""D7 双项目隔离断言（T4.4）：项目 A 的任何查询都不得看见项目 B 的数据。"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from devkb.repositories import ChunkDraft, ChunkRepo, DocumentRepo, ProjectRepo, RunRepo


def _vec(hot: int) -> list[float]:
    v = [0.0] * 1024
    v[hot] = 1.0
    return v


def _slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


async def test_vector_search_cannot_cross_projects(session: AsyncSession) -> None:
    projects = ProjectRepo(session)
    pa = await projects.create(_slug("iso-a"), "Project A")
    pb = await projects.create(_slug("iso-b"), "Project B")

    doc_a = await DocumentRepo(session, pa.id).upsert(
        rel_path="a.md", title="A", doc_type="markdown", content_hash="ha"
    )
    doc_b = await DocumentRepo(session, pb.id).upsert(
        rel_path="b.md", title="B", doc_type="markdown", content_hash="hb"
    )
    await ChunkRepo(session, pa.id).replace_for_document(
        doc_a.id, [ChunkDraft(0, "A > 一", "A 的内容", "ca", 10, 1, 5, _vec(0))]
    )
    await ChunkRepo(session, pb.id).replace_for_document(
        doc_b.id, [ChunkDraft(0, "B > 一", "B 的内容", "cb", 10, 1, 5, _vec(1))]
    )
    await session.commit()

    # 最严苛场景：用与 B 的块完全相同的查询向量（余弦相似度=1）在 A 范围检索，
    # B 的完美匹配也绝不可见
    hits = await ChunkRepo(session, pa.id).vector_search(_vec(1), top_k=10)
    assert hits, "A 项目应命中自己的块"
    assert all(chunk.project_id == pa.id for chunk, _, _ in hits)


async def test_documents_scoped_by_project(session: AsyncSession) -> None:
    projects = ProjectRepo(session)
    pa = await projects.create(_slug("doc-a"), "A")
    pb = await projects.create(_slug("doc-b"), "B")
    await DocumentRepo(session, pa.id).upsert(
        rel_path="same/path.md", title="A 的", doc_type="markdown", content_hash="1"
    )
    await session.commit()

    assert await DocumentRepo(session, pb.id).get_by_rel_path("same/path.md") is None
    assert await DocumentRepo(session, pa.id).get_by_rel_path("same/path.md") is not None


async def test_runs_scoped_by_project(session: AsyncSession) -> None:
    projects = ProjectRepo(session)
    pa = await projects.create(_slug("run-a"), "A")
    pb = await projects.create(_slug("run-b"), "B")
    run = await RunRepo(session, pa.id).create("库存扣减的幂等键是什么？")
    await session.commit()

    assert await RunRepo(session, pb.id).get(run.id) is None
    found = await RunRepo(session, pa.id).get(run.id)
    assert found is not None and found.question.startswith("库存")
