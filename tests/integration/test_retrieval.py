"""T8.2 检索集成测试：FakeEmbedder 下已知查询精确命中期望 chunk，元数据完整。

FakeEmbedder 对同一文本给出相同的 query/doc 向量——以某 chunk 的原文全文为查询，
该 chunk 余弦相似度=1，必为 top1（确定性断言，不依赖模型语义）。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from devkb.embedding import FakeEmbedder
from devkb.ingest.markdown import approx_token_counter
from devkb.ingest.pipeline import ingest_directory
from devkb.repositories import ChunkDraft, ChunkRepo, DocumentRepo, ProjectRepo
from devkb.retrieval import hybrid_retrieve, lexical_retrieve, retrieve

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


# ---------------------------------------------------------------------------
# T15.2 Hybrid 编排：双 channel 融合、多 query、硬上限、不可见性
# ---------------------------------------------------------------------------


def _fake_embedding(text: str) -> list[float]:
    """与 FakeEmbedder 完全一致的确定性向量：以 chunk 原文为查询时余弦=1。"""
    return FakeEmbedder().embed_query(text)


async def _seed_hybrid_project(session: AsyncSession) -> uuid.UUID:
    """受控种子：A 同时是 vector（查询=原文）与 lexical（多 token）最强命中，
    C 仅共享标识符 token（lexical 第 2），B 仅可能经 vector 出现。"""
    project = await ProjectRepo(session).create(slug=f"t15-{uuid.uuid4().hex[:8]}", name="t15")
    doc = await DocumentRepo(session, project.id).upsert(
        rel_path="docs/timeout.md", title="Timeout", doc_type="markdown", content_hash="h"
    )
    contents = [
        ("任务 > 扫描", "OrderTimeoutJob 扫描 pending 订单并触发取消"),
        ("支付 > 幂等", "支付回调重复投递的幂等处理"),
        ("任务 > 配置", "OrderTimeoutJob 的调度周期配置"),
    ]
    await ChunkRepo(session, project.id).replace_for_document(
        doc.id,
        [
            ChunkDraft(i, title, text, f"c{i}", 8, 2 * i + 1, 2 * i + 2, _fake_embedding(text))
            for i, (title, text) in enumerate(contents)
        ],
    )
    await session.commit()
    return project.id


async def test_hybrid_fuses_both_channels_with_full_metadata(session: AsyncSession) -> None:
    project_id = await _seed_hybrid_project(session)
    query = "OrderTimeoutJob 扫描 pending 订单并触发取消"  # = A 的逐字原文

    # vector_mode=exact：本测试断言精确名次，测的是 RRF 融合语义。HNSW 是跨项目
    # 共享的全局索引 + 随机图层级，项目过滤后小样本偶发漏召回（T20.3 实测 ~1/3
    # 概率 A 掉出 vector channel），会把融合语义断言变成近似召回抽签
    hits = await hybrid_retrieve(
        session, project_id, [query], embedder=FakeEmbedder(), top_k=8, vector_mode="exact"
    )

    top = hits[0]
    assert top.title_path == "任务 > 扫描", "双 channel 都排第 1 的块必须融合后居首"
    assert top.vector_rank == 1 and top.lexical_rank == 1
    assert top.fused_score == pytest.approx(2 / 61), "fused_score 只能是 1/(k+rank) 求和"
    assert top.hit_queries == (query,)

    by_title = {h.title_path: h for h in hits}
    assert by_title["任务 > 配置"].lexical_rank == 2, "共享标识符 token 的块经 lexical 进入"
    assert by_title["支付 > 幂等"].lexical_rank is None, "词面无重叠的块只能来自 vector channel"
    assert by_title["支付 > 幂等"].vector_rank is not None
    for h in hits:
        assert h.rel_path == "docs/timeout.md" and h.title_path and h.content
        assert 1 <= h.start_line <= h.end_line
        assert isinstance(h.chunk_id, uuid.UUID) and h.fused_score > 0
    scores = [h.fused_score for h in hits]
    assert scores == sorted(scores, reverse=True)


async def test_hybrid_multi_query_merges_and_dedupes(session: AsyncSession) -> None:
    project_id = await _seed_hybrid_project(session)
    q1, q2 = "OrderTimeoutJob 扫描", "OrderTimeoutJob 调度周期"

    # vector_mode=exact 的原因同上一个测试：断言精确名次须排除 HNSW 近似抽签
    hits = await hybrid_retrieve(
        session, project_id, [q1, q2], embedder=FakeEmbedder(), top_k=8, vector_mode="exact"
    )
    top = hits[0]
    assert top.hit_queries == (q1, q2), "两个子查询都命中的块须回带全部命中 query"
    assert {h.title_path for h in hits[:2]} == {"任务 > 扫描", "任务 > 配置"}

    single = await hybrid_retrieve(
        session, project_id, [q1], embedder=FakeEmbedder(), top_k=8, vector_mode="exact"
    )
    duplicated = await hybrid_retrieve(
        session, project_id, [q1, q1], embedder=FakeEmbedder(), top_k=8, vector_mode="exact"
    )
    assert duplicated == single, "重复子查询去重后不得重复计分"


async def test_hybrid_hard_caps_cannot_be_bypassed(session: AsyncSession) -> None:
    project_id = await _seed_hybrid_project(session)
    embedder = FakeEmbedder()

    with pytest.raises(ValueError, match="子查询"):
        await hybrid_retrieve(session, project_id, ["a", "b", "c", "d"], embedder=embedder)
    with pytest.raises(ValueError, match="query"):
        await hybrid_retrieve(session, project_id, [], embedder=embedder)
    for bad_n in (0, 51):
        with pytest.raises(ValueError, match="per_channel_n"):
            await hybrid_retrieve(
                session, project_id, ["q"], embedder=embedder, per_channel_n=bad_n
            )
    for bad_k in (0, 13):
        with pytest.raises(ValueError, match="top_k"):
            await hybrid_retrieve(session, project_id, ["q"], embedder=embedder, top_k=bad_k)


async def test_hybrid_failed_docs_and_foreign_projects_invisible(session: AsyncSession) -> None:
    projects = ProjectRepo(session)
    pa = await projects.create(slug=f"t15a-{uuid.uuid4().hex[:8]}", name="a")
    pb = await projects.create(slug=f"t15b-{uuid.uuid4().hex[:8]}", name="b")
    query = "hybridprobe 探针"

    doc_repo_a = DocumentRepo(session, pa.id)
    good = await doc_repo_a.upsert(
        rel_path="good.md", title="G", doc_type="markdown", content_hash="hg"
    )
    bad = await doc_repo_a.upsert(
        rel_path="bad.md", title="B", doc_type="markdown", content_hash="hb"
    )
    await ChunkRepo(session, pa.id).replace_for_document(
        good.id, [ChunkDraft(0, "", "hybridprobe alpha", "c0", 4, 1, 1, _fake_embedding(query))]
    )
    # 最严苛场景：failed 文档陈旧块与其他项目的块都与查询向量完全相同（余弦=1）
    # 且词面精确命中——依然绝不可见
    await ChunkRepo(session, pa.id).replace_for_document(
        bad.id, [ChunkDraft(0, "", "hybridprobe beta", "c1", 4, 1, 1, _fake_embedding(query))]
    )
    await doc_repo_a.mark_failed("bad.md", "ParseError: 模拟更新失败")
    doc_b = await DocumentRepo(session, pb.id).upsert(
        rel_path="foreign.md", title="F", doc_type="markdown", content_hash="hf"
    )
    await ChunkRepo(session, pb.id).replace_for_document(
        doc_b.id, [ChunkDraft(0, "", "hybridprobe gamma", "c2", 4, 1, 1, _fake_embedding(query))]
    )
    await session.commit()

    hits = await hybrid_retrieve(session, pa.id, [query], embedder=FakeEmbedder(), top_k=8)
    assert [h.content for h in hits] == ["hybridprobe alpha"], (
        "failed 文档陈旧块与其他 project 的完美匹配块都不得出现"
    )
