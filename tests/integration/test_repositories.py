"""仓储行为断言：upsert 幂等、replace 全量替换、向量检索排序。"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from devkb.repositories import ChunkDraft, ChunkRepo, DocumentRepo, ProjectRepo


def _vec(hot: int, warm: float = 0.0) -> list[float]:
    v = [warm] * 1024
    v[hot] = 1.0
    return v


async def test_document_upsert_idempotent(session: AsyncSession) -> None:
    p = await ProjectRepo(session).create(f"up-{uuid.uuid4().hex[:6]}", "U")
    docs = DocumentRepo(session, p.id)
    d1 = await docs.upsert(rel_path="x.md", title="v1", doc_type="markdown", content_hash="h1")
    d2 = await docs.upsert(rel_path="x.md", title="v2", doc_type="markdown", content_hash="h2")
    await session.commit()

    assert d1.id == d2.id, "同一 rel_path 应更新而非新建"
    assert d2.content_hash == "h2" and d2.title == "v2"
    assert len(await docs.list_active()) == 1


async def test_replace_for_document_replaces_not_appends(session: AsyncSession) -> None:
    p = await ProjectRepo(session).create(f"rep-{uuid.uuid4().hex[:6]}", "R")
    doc = await DocumentRepo(session, p.id).upsert(
        rel_path="y.md", title="Y", doc_type="markdown", content_hash="h"
    )
    chunks = ChunkRepo(session, p.id)
    await chunks.replace_for_document(
        doc.id,
        [ChunkDraft(i, f"Y > {i}", f"内容{i}", f"c{i}", 5, i, i + 1, _vec(i)) for i in range(3)],
    )
    await chunks.replace_for_document(
        doc.id,
        [ChunkDraft(i, f"Y > {i}", f"新内容{i}", f"n{i}", 5, i, i + 1, _vec(i)) for i in range(2)],
    )
    await session.commit()

    assert await chunks.count() == 2, "第二次 replace 后应恰为 2 块（替换而非追加）"


async def test_vector_search_orders_by_similarity(session: AsyncSession) -> None:
    p = await ProjectRepo(session).create(f"vs-{uuid.uuid4().hex[:6]}", "V")
    doc = await DocumentRepo(session, p.id).upsert(
        rel_path="z.md", title="Z", doc_type="markdown", content_hash="h"
    )
    # 块 0 与查询向量完全一致；块 1 部分相似；块 2 正交
    await ChunkRepo(session, p.id).replace_for_document(
        doc.id,
        [
            ChunkDraft(0, "Z > 精确", "精确匹配", "c0", 5, 1, 2, _vec(0)),
            ChunkDraft(1, "Z > 相近", "部分相似", "c1", 5, 3, 4, _vec(0, warm=0.3)),
            ChunkDraft(2, "Z > 无关", "正交", "c2", 5, 5, 6, _vec(500)),
        ],
    )
    await session.commit()

    hits = await ChunkRepo(session, p.id).vector_search(_vec(0), top_k=3)
    assert [c.ordinal for c, _, _ in hits] == [0, 1, 2], "应按相似度降序"
    scores = [s for _, _, s in hits]
    assert scores[0] > 0.99 and scores == sorted(scores, reverse=True)
