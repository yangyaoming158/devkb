"""D7 双项目隔离断言（T4.4 / P1 T12.3）：项目 A 的任何查询都不得看见项目 B 的数据。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from devkb.repositories import (
    AgentStepRepo,
    ChunkDraft,
    ChunkRepo,
    DocumentRepo,
    ProjectRepo,
    RunRepo,
    ToolInvocationRepo,
)


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


async def test_lexical_search_cannot_cross_projects(session: AsyncSession) -> None:
    projects = ProjectRepo(session)
    pa = await projects.create(_slug("lex-a"), "A")
    pb = await projects.create(_slug("lex-b"), "B")
    doc_a = await DocumentRepo(session, pa.id).upsert(
        rel_path="a.md", title="A", doc_type="markdown", content_hash="ha"
    )
    doc_b = await DocumentRepo(session, pb.id).upsert(
        rel_path="b.md", title="B", doc_type="markdown", content_hash="hb"
    )
    # 两个项目含同一独特标识符：A 检索时 B 的完美词面命中也绝不可见
    await ChunkRepo(session, pa.id).replace_for_document(
        doc_a.id, [ChunkDraft(0, "A > 一", "SharedTokenXyz 在 A", "ca", 10, 1, 5, _vec(0))]
    )
    await ChunkRepo(session, pb.id).replace_for_document(
        doc_b.id, [ChunkDraft(0, "B > 一", "SharedTokenXyz 在 B", "cb", 10, 1, 5, _vec(1))]
    )
    await session.commit()

    hits = await ChunkRepo(session, pa.id).lexical_search("sharedtokenxyz", top_k=10)
    assert hits, "A 项目应词面命中自己的块"
    assert all(chunk.project_id == pa.id for chunk, _, _ in hits)


async def test_hnsw_vector_search_cannot_cross_projects(session: AsyncSession) -> None:
    projects = ProjectRepo(session)
    pa = await projects.create(_slug("hnsw-a"), "A")
    pb = await projects.create(_slug("hnsw-b"), "B")
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

    hits = await ChunkRepo(session, pa.id).vector_search(
        _vec(1), top_k=10, mode="hnsw", ef_search=100
    )
    assert hits, "A 项目应命中自己的块"
    assert all(chunk.project_id == pa.id for chunk, _, _ in hits)


async def test_steps_and_tools_scoped_by_project(session: AsyncSession) -> None:
    projects = ProjectRepo(session)
    pa = await projects.create(_slug("st-a"), "A")
    pb = await projects.create(_slug("st-b"), "B")
    run = await RunRepo(session, pa.id).create("轨迹隔离？")
    step = await AgentStepRepo(session, pa.id).add(
        run.id, seq=1, node="retrieve", status="succeeded", output_summary={"evidence": 3}
    )
    await ToolInvocationRepo(session, pa.id).add(
        run.id,
        step.id,
        tool_name="hybrid_retrieve",
        status="succeeded",
        arguments={"query": "库存"},
    )
    await session.commit()

    # 跨项目用同一 run_id 读取：返回空，而非报错或泄漏
    assert await AgentStepRepo(session, pb.id).list_for_run(run.id) == []
    assert await ToolInvocationRepo(session, pb.id).list_for_run(run.id) == []
    own_steps = await AgentStepRepo(session, pa.id).list_for_run(run.id)
    own_tools = await ToolInvocationRepo(session, pa.id).list_for_run(run.id)
    assert [s.node for s in own_steps] == ["retrieve"]
    assert [t.tool_name for t in own_tools] == ["hybrid_retrieve"]


async def test_trace_writes_reject_cross_project_and_cross_run_mismatch(
    session: AsyncSession,
) -> None:
    """复评修复回归（0003 复合 FK）：错配写入必须在数据库层被拒。"""
    projects = ProjectRepo(session)
    pa = await projects.create(_slug("mis-a"), "A")
    pb = await projects.create(_slug("mis-b"), "B")
    run_a = await RunRepo(session, pa.id).create("A 的 run")
    await session.commit()

    # 项目 B 的 step 引用项目 A 的 run
    with pytest.raises(IntegrityError):
        await AgentStepRepo(session, pb.id).add(run_a.id, seq=1, node="plan", status="succeeded")
    await session.rollback()

    # run 2 的 tool 引用 run 1 的 step（同项目内跨 run 错配）
    pa2 = await ProjectRepo(session).create(_slug("mis-c"), "C")
    run1 = await RunRepo(session, pa2.id).create("run 1")
    run2 = await RunRepo(session, pa2.id).create("run 2")
    step_run1 = await AgentStepRepo(session, pa2.id).add(
        run1.id, seq=1, node="retrieve", status="succeeded"
    )
    with pytest.raises(IntegrityError):
        await ToolInvocationRepo(session, pa2.id).add(
            run2.id, step_run1.id, tool_name="hybrid_retrieve", status="succeeded"
        )
    await session.rollback()
