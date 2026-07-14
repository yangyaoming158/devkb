"""T8.2 检索集成测试：FakeEmbedder 下已知查询精确命中期望 chunk，元数据完整。

FakeEmbedder 对同一文本给出相同的 query/doc 向量——以某 chunk 的原文全文为查询，
该 chunk 余弦相似度=1，必为 top1（确定性断言，不依赖模型语义）。
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from devkb.embedding import FakeEmbedder
from devkb.ingest.markdown import approx_token_counter
from devkb.ingest.pipeline import ingest_directory
from devkb.repositories import ProjectRepo
from devkb.retrieval import retrieve

CORPUS_MD = Path(__file__).parents[1] / "fixtures" / "corpus_md"


async def _ingest_corpus(session: AsyncSession) -> uuid.UUID:
    project = await ProjectRepo(session).create(slug=f"t8-{uuid.uuid4().hex[:8]}", name="t8")
    await session.commit()
    report = await ingest_directory(
        session, project.id, CORPUS_MD, embedder=FakeEmbedder(), count_tokens=approx_token_counter
    )
    assert report.count("failed") == 0
    return project.id


async def test_known_query_hits_expected_chunk(session: AsyncSession) -> None:
    project_id = await _ingest_corpus(session)
    embedder = FakeEmbedder()

    # 查询 = zh.md「并发控制」chunk 的逐字原文（含标题行，见 golden 快照 A）
    target = (
        "### 并发控制\n\n库存扣减使用数据库行锁（`SELECT ... FOR UPDATE`）保证并发安全，\n"
        "超卖场景由唯一约束兜底。"
    )
    hits = await retrieve(session, project_id, target, embedder=embedder, top_k=3)

    assert len(hits) == 3
    top = hits[0]
    assert top.rel_path == "zh.md"
    assert top.title_path == "架构总览 > 库存 > 并发控制"
    assert top.content == target
    assert top.score > 0.999  # 同向量余弦=1（浮点余量）
    assert hits[0].score >= hits[1].score >= hits[2].score  # 降序


async def test_result_metadata_complete(session: AsyncSession) -> None:
    project_id = await _ingest_corpus(session)
    hits = await retrieve(session, project_id, "任意查询文本", embedder=FakeEmbedder(), top_k=8)

    assert len(hits) == 8
    for h in hits:
        assert h.rel_path.endswith(".md")
        assert h.title_path  # 面包屑非空
        assert h.content
        assert 1 <= h.start_line <= h.end_line
        assert -1.0 <= h.score <= 1.0
        assert isinstance(h.chunk_id, uuid.UUID)


async def test_top_k_respected(session: AsyncSession) -> None:
    project_id = await _ingest_corpus(session)
    hits = await retrieve(session, project_id, "查询", embedder=FakeEmbedder(), top_k=2)
    assert len(hits) == 2
