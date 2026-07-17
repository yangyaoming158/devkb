"""T12.4 数据层集成测试：GIN/FTS、HNSW/exact、run-step-tool FK/顺序/删除。

backfill 与跨项目越权已分别由 test_backfill.py / test_isolation.py 覆盖，
本文件补齐其余判据：排名行为、注入安全、索引计划、约束与级联行为。
"""

from __future__ import annotations

import math
import uuid

import pytest
from sqlalchemy import text
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


def _graded_vec(step: int) -> list[float]:
    """与 _vec(0) 的余弦相似度随 step 单调下降的单位向量。"""
    theta = step * (math.pi / 2) / 40
    v = [0.0] * 1024
    v[0] = math.cos(theta)
    v[1] = math.sin(theta)
    return v


def _vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def _slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


async def _seed_project(session: AsyncSession, prefix: str, drafts: list[ChunkDraft]) -> uuid.UUID:
    project = await ProjectRepo(session).create(_slug(prefix), prefix)
    doc = await DocumentRepo(session, project.id).upsert(
        rel_path="docs/data.md", title="Data", doc_type="markdown", content_hash="h"
    )
    # search_text 由 replace_for_document 在插入点统一生成，无需 backfill
    await ChunkRepo(session, project.id).replace_for_document(doc.id, drafts)
    await session.commit()
    return project.id


# ---------------------------------------------------------------------------
# GIN / FTS
# ---------------------------------------------------------------------------


async def test_lexical_search_ranks_denser_match_first(session: AsyncSession) -> None:
    pid = await _seed_project(
        session,
        "fts-rank",
        [
            ChunkDraft(
                0, "Data > 状态机", "OrderStateMachine 定义状态迁移", "c0", 8, 1, 3, _vec(0)
            ),
            ChunkDraft(
                1,
                "Data > 密集",
                "OrderStateMachine OrderStateMachine OrderStateMachine 状态迁移矩阵",
                "c1",
                8,
                4,
                6,
                _vec(1),
            ),
            ChunkDraft(2, "Data > 无关", "库存扣减与补偿", "c2", 8, 7, 9, _vec(2)),
        ],
    )

    hits = await ChunkRepo(session, pid).lexical_search("orderstatemachine", top_k=10)
    assert [c.ordinal for c, _, _ in hits] == [1, 0], "词面更密集的块应排前，无关块不出现"
    scores = [s for _, _, s in hits]
    assert scores[0] > scores[1] > 0
    top1 = await ChunkRepo(session, pid).lexical_search("orderstatemachine", top_k=1)
    assert len(top1) == 1, "top_k 必须生效"


async def test_lexical_search_is_safe_for_hostile_and_cjk_input(session: AsyncSession) -> None:
    pid = await _seed_project(
        session,
        "fts-safe",
        [
            ChunkDraft(
                0, "Data > 支付", "支付 成功 事件 由 PaymentService 发布", "c0", 8, 1, 3, _vec(0)
            )
        ],
    )

    repo = ChunkRepo(session, pid)
    hits = await repo.lexical_search("支付 paymentservice", top_k=5)
    assert len(hits) == 1, "中文 + 标识符 token 应命中"
    for hostile in [
        "'; DROP TABLE chunks; --",
        "a & | ! ( ) : * <-> b",
        "((((",
        "x" * 5000,
        "",
    ]:
        assert await repo.lexical_search(hostile, top_k=5) == [], (
            f"恶意/畸形输入应安全返回空：{hostile[:30]!r}"
        )
    injected = await repo.lexical_search("PaymentService'; DELETE FROM chunks --", top_k=5)
    assert len(injected) == 1, "夹带注入的查询按普通词素处理，正常返回命中"
    assert await repo.count() == 1, "chunks 表必须还在（未被注入删除）"


async def test_lexical_search_or_semantics_and_multi_term_ranking(session: AsyncSession) -> None:
    pid = await _seed_project(
        session,
        "fts-or",
        [
            ChunkDraft(0, "Data > 状态机", "OrderStateMachine 状态迁移", "c0", 8, 1, 3, _vec(0)),
            ChunkDraft(1, "Data > 支付", "PaymentService 处理 支付", "c1", 8, 4, 6, _vec(1)),
        ],
    )
    repo = ChunkRepo(session, pid)

    # AND 语义（websearch 默认）下这类中英混合问题会因 CJK 词不在语料中而零命中，
    # 是 T14.2 冻结 OR 构造的直接动机
    hits = await repo.lexical_search("OrderStateMachine 在什么情况下取消", top_k=10)
    assert [c.ordinal for c, _, _ in hits] == [0], "部分 token 命中即可返回，且无关块不出现"

    hits = await repo.lexical_search("支付 状态迁移 OrderStateMachine", top_k=10)
    assert [c.ordinal for c, _, _ in hits] == [0, 1], "命中更多查询词的块必须排前"


async def test_lexical_search_excludes_failed_documents(session: AsyncSession) -> None:
    project = await ProjectRepo(session).create(_slug("fts-failed"), "fts-failed")
    doc_repo = DocumentRepo(session, project.id)
    chunk_repo = ChunkRepo(session, project.id)
    good = await doc_repo.upsert(
        rel_path="docs/good.md", title="Good", doc_type="markdown", content_hash="h1"
    )
    bad = await doc_repo.upsert(
        rel_path="docs/bad.md", title="Bad", doc_type="markdown", content_hash="h2"
    )
    await chunk_repo.replace_for_document(
        good.id, [ChunkDraft(0, "", "lexicalprobe alpha", "c0", 4, 1, 1, _vec(0))]
    )
    await chunk_repo.replace_for_document(
        bad.id, [ChunkDraft(0, "", "lexicalprobe beta", "c1", 4, 1, 1, _vec(1))]
    )
    await doc_repo.mark_failed("docs/bad.md", "ParseError: 模拟更新失败")
    await session.commit()

    hits = await chunk_repo.lexical_search("lexicalprobe", top_k=10)
    assert [c.content for c, _, _ in hits] == ["lexicalprobe alpha"], (
        "failed 文档保留的陈旧 chunks 不得进入 lexical 检索（与 vector 同口径）"
    )


async def test_fts_tsv_predicate_is_gin_servable(session: AsyncSession) -> None:
    # 确定性验证：seqscan 关闭后，tsv 谓词唯一可用的索引就是 GIN——生成列 +
    # websearch_to_tsquery 的查询形状必须能被它服务。带 project_id 全形状的
    # 计划选择取决于 planner 成本模型（合成小数据上不稳定），真实 1370 块
    # 语料上的自然计划验证在 T14.4 落盘。
    await _seed_project(
        session,
        "fts-plan",
        [ChunkDraft(0, "Data > 探针", "GinPlanProbe token", "c0", 4, 1, 2, None)],
    )

    await session.execute(text("SELECT set_config('enable_seqscan', 'off', true)"))
    plan_rows = await session.execute(
        text(
            "EXPLAIN (COSTS OFF) SELECT c.id FROM chunks c"
            " WHERE c.search_tsv @@ websearch_to_tsquery('simple', :q)"
        ),
        {"q": "ginplanprobe"},
    )
    plan = "\n".join(row[0] for row in plan_rows)
    await session.rollback()  # 一并撤销 planner 开关
    assert "ix_chunks_search_tsv" in plan, f"tsv 谓词应能命中 GIN 索引：\n{plan}"


# ---------------------------------------------------------------------------
# HNSW / exact
# ---------------------------------------------------------------------------


async def test_hnsw_and_exact_agree_on_small_corpus(session: AsyncSession) -> None:
    drafts = [
        ChunkDraft(i, f"Data > {i}", f"内容 {i}", f"c{i}", 4, i, i + 1, _graded_vec(i))
        for i in range(30)
    ]
    drafts.append(ChunkDraft(30, "Data > 无嵌入", "无嵌入块", "c30", 4, 40, 41, None))
    pid = await _seed_project(session, "hnsw-agree", drafts)

    repo = ChunkRepo(session, pid)
    exact = await repo.vector_search(_vec(0), top_k=10, mode="exact")
    hnsw = await repo.vector_search(_vec(0), top_k=10, mode="hnsw", ef_search=200)
    assert [c.ordinal for c, _, _ in exact] == list(range(10)), "exact 顺序 = 相似度真值"
    assert [c.ordinal for c, _, _ in hnsw] == [c.ordinal for c, _, _ in exact], (
        "小语料 + 高 ef_search 下 HNSW 应与 exact 完全一致"
    )
    assert all(c.ordinal != 30 for c, _, _ in exact + hnsw), "无嵌入块不可出现"

    with pytest.raises(ValueError, match="ef_search"):
        await repo.vector_search(_vec(0), top_k=5, mode="hnsw", ef_search=0)


async def test_vector_search_restores_previous_gucs(session: AsyncSession) -> None:
    """复评修复回归：恢复的是调用前值（含 hnsw.ef_search），不是写死 'on'。"""
    pid = await _seed_project(
        session,
        "guc-restore",
        [ChunkDraft(0, "Data > 一", "GUC 探针", "c0", 4, 1, 2, _vec(0))],
    )
    repo = ChunkRepo(session, pid)
    guc_probe = text(
        "SELECT current_setting('hnsw.ef_search'), current_setting('enable_seqscan'),"
        " current_setting('enable_sort'), current_setting('enable_indexscan')"
    )

    before = (await session.execute(guc_probe)).one()
    await repo.vector_search(_vec(0), top_k=1, mode="hnsw", ef_search=123)
    await repo.vector_search(_vec(0), top_k=1, mode="exact")
    assert (await session.execute(guc_probe)).one() == before, "查询后所有 GUC 应回到调用前值"

    # 调用前已被外部改掉的值必须按原样保留
    await session.execute(text("SELECT set_config('enable_indexscan', 'off', true)"))
    await session.execute(text("SELECT set_config('hnsw.ef_search', '77', true)"))
    await repo.vector_search(_vec(0), top_k=1, mode="exact")
    await repo.vector_search(_vec(0), top_k=1, mode="hnsw", ef_search=200)
    row = (await session.execute(guc_probe)).one()
    assert row[0] == "77" and row[3] == "off", "外部设定的前值不得被覆写为默认"
    await session.rollback()


async def test_vector_query_uses_hnsw_index_when_forced(session: AsyncSession) -> None:
    pid = await _seed_project(
        session,
        "hnsw-plan",
        [ChunkDraft(0, "Data > 一", "向量计划探针", "c0", 8, 1, 3, _vec(0))],
    )

    # 与 vector_search(mode="hnsw") 相同的强制组合：距离序只能由 HNSW 提供
    await session.execute(text("SELECT set_config('enable_seqscan', 'off', true)"))
    await session.execute(text("SELECT set_config('enable_sort', 'off', true)"))
    plan_rows = await session.execute(
        text(
            "EXPLAIN (COSTS OFF) SELECT c.id FROM chunks c"
            " JOIN documents d ON c.document_id = d.id"
            " WHERE c.project_id = :pid AND c.embedding IS NOT NULL AND d.status = 'active'"
            " ORDER BY c.embedding <=> CAST(:emb AS vector) LIMIT 10"
        ),
        {"pid": pid, "emb": _vec_literal(_vec(0))},
    )
    plan = "\n".join(row[0] for row in plan_rows)
    await session.rollback()  # 一并撤销 planner 开关
    assert "ix_chunks_embedding_hnsw" in plan, f"检索查询形状应能命中部分 HNSW 索引：\n{plan}"


# ---------------------------------------------------------------------------
# run - step - tool：FK、顺序、删除行为
# ---------------------------------------------------------------------------


async def test_step_requires_existing_run_and_unique_seq(session: AsyncSession) -> None:
    project = await ProjectRepo(session).create(_slug("fk-step"), "FK")
    steps = AgentStepRepo(session, project.id)

    with pytest.raises(IntegrityError):
        await steps.add(uuid.uuid4(), seq=1, node="plan", status="succeeded")
    await session.rollback()

    project = await ProjectRepo(session).create(_slug("fk-step2"), "FK2")
    run = await RunRepo(session, project.id).create("唯一 seq？")
    steps = AgentStepRepo(session, project.id)
    await steps.add(run.id, seq=1, node="plan", status="succeeded")
    with pytest.raises(IntegrityError):
        await steps.add(run.id, seq=1, node="retrieve", status="succeeded")
    await session.rollback()


async def test_tool_requires_existing_step(session: AsyncSession) -> None:
    project = await ProjectRepo(session).create(_slug("fk-tool"), "FK")
    run = await RunRepo(session, project.id).create("工具 FK？")

    with pytest.raises(IntegrityError):
        await ToolInvocationRepo(session, project.id).add(
            run.id, uuid.uuid4(), tool_name="hybrid_retrieve", status="succeeded"
        )
    await session.rollback()


async def test_steps_and_tools_listed_in_seq_order(session: AsyncSession) -> None:
    project = await ProjectRepo(session).create(_slug("order"), "Order")
    run = await RunRepo(session, project.id).create("顺序？")
    steps = AgentStepRepo(session, project.id)
    tools = ToolInvocationRepo(session, project.id)

    # 乱序写入：读取必须按 seq 还原执行顺序
    s2 = await steps.add(run.id, seq=2, node="retrieve", status="succeeded", attempt=1)
    s3 = await steps.add(
        run.id, seq=3, node="evaluate", status="failed", error="结构化输出解析失败"
    )
    s1 = await steps.add(run.id, seq=1, node="plan", status="succeeded", latency_ms=120)
    await tools.add(s3.run_id, s3.id, tool_name="evaluate_probe", status="failed")
    await tools.add(s2.run_id, s2.id, tool_name="hybrid_retrieve", status="succeeded")
    await session.commit()

    listed = await steps.list_for_run(run.id)
    assert [(s.seq, s.node) for s in listed] == [
        (1, "plan"),
        (2, "retrieve"),
        (3, "evaluate"),
    ]
    assert listed[0].id == s1.id and listed[0].latency_ms == 120
    assert listed[2].error == "结构化输出解析失败"

    listed_tools = await tools.list_for_run(run.id)
    assert [t.tool_name for t in listed_tools] == ["hybrid_retrieve", "evaluate_probe"], (
        "工具按所属 step 的 seq 排序，而非写入顺序"
    )


async def test_deleting_run_cascades_steps_and_tools(session: AsyncSession) -> None:
    project = await ProjectRepo(session).create(_slug("cascade"), "Cascade")
    run = await RunRepo(session, project.id).create("级联删除？")
    keep_run = await RunRepo(session, project.id).create("保留的 run")
    steps = AgentStepRepo(session, project.id)
    tools = ToolInvocationRepo(session, project.id)
    step = await steps.add(run.id, seq=1, node="retrieve", status="succeeded")
    await tools.add(run.id, step.id, tool_name="hybrid_retrieve", status="succeeded")
    keep_step = await steps.add(keep_run.id, seq=1, node="plan", status="succeeded")
    await tools.add(keep_run.id, keep_step.id, tool_name="plan_probe", status="succeeded")
    await session.commit()
    run_id, keep_run_id = run.id, keep_run.id

    # Repository 不提供 delete（P1 无该需求）；直接验证数据库级联行为
    await session.execute(text("DELETE FROM agent_runs WHERE id = :rid"), {"rid": run_id})
    await session.commit()
    session.expire_all()  # 之后只用捕获的 UUID，不再触碰已过期的 ORM 对象

    assert await steps.list_for_run(run_id) == []
    assert await tools.list_for_run(run_id) == []
    assert len(await steps.list_for_run(keep_run_id)) == 1, "无关 run 的轨迹不得被波及"
    assert len(await tools.list_for_run(keep_run_id)) == 1
