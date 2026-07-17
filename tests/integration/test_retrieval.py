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
from devkb.repositories import ChunkDraft, ChunkRepo, DocumentRepo, ProjectRepo
from devkb.retrieval import lexical_retrieve, retrieve

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


# ---------------------------------------------------------------------------
# T14.3 lexical channel：排名与解释性字段
# ---------------------------------------------------------------------------

CORPUS_JAVA = Path(__file__).parents[1] / "fixtures" / "corpus_java"


async def test_lexical_hit_explanation_fields_and_exact_ordering(session: AsyncSession) -> None:
    project = await ProjectRepo(session).create(slug=f"t14-{uuid.uuid4().hex[:8]}", name="t14")
    doc = await DocumentRepo(session, project.id).upsert(
        rel_path="docs/mq.md", title="MQ", doc_type="markdown", content_hash="h"
    )
    # 词面密度受控：ordinal 0 命中 2 个查询词、1 命中 1 个、2 无关
    await ChunkRepo(session, project.id).replace_for_document(
        doc.id,
        [
            ChunkDraft(
                0, "MQ > 事件", "order.paid.event 由 PaymentService 发布", "c0", 8, 1, 2, None
            ),
            ChunkDraft(1, "MQ > 队列", "PaymentService 消费超时队列", "c1", 8, 3, 4, None),
            ChunkDraft(2, "MQ > 无关", "库存回补策略", "c2", 8, 5, 6, None),
        ],
    )
    await session.commit()

    query = "PaymentService 发布 order.paid.event"
    hits = await lexical_retrieve(session, project.id, query, top_k=10)

    assert [h.rel_path for h in hits] == ["docs/mq.md", "docs/mq.md"], "无关块不得出现"
    assert [h.lexical_rank for h in hits] == [1, 2], "lexical_rank 为 channel 内 1-based 名次"
    assert hits[0].title_path == "MQ > 事件" and hits[1].title_path == "MQ > 队列", (
        "命中更多查询词的块必须精确排在第 1"
    )
    assert hits[0].score > hits[1].score > 0, "score 为 ts_rank_cd 原始分且严格降序"
    for h in hits:
        assert h.query == query, "解释字段须回带命中 query"
        assert "paymentservice" in h.query_tokens and "order.paid.event" in h.query_tokens, (
            "query_tokens 须暴露实际参与 tsquery 的 token"
        )
        assert 1 <= h.start_line <= h.end_line and h.content, "引用元数据完整"


async def test_lexical_known_identifier_ranks_first_on_java_fixture(session: AsyncSession) -> None:
    project = await ProjectRepo(session).create(slug=f"t14j-{uuid.uuid4().hex[:8]}", name="t14j")
    await session.commit()
    report = await ingest_directory(
        session,
        project.id,
        CORPUS_JAVA,
        embedder=FakeEmbedder(),
        count_tokens=approx_token_counter,
    )
    assert report.count("ingested") >= 3  # broken.java 预期 failed，不影响本测试

    hits = await lexical_retrieve(session, project.id, "shouldRetry 的判断条件", top_k=10)
    top = hits[0]
    assert top.lexical_rank == 1
    assert top.rel_path == "order_service.java"
    assert top.title_path.endswith("shouldRetry"), "唯一包含该标识符原词的方法块必须排第 1"
    assert top.start_line <= 69 <= top.end_line, "行号指向 shouldRetry 方法真实区间"
