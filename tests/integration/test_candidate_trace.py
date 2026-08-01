"""m15 · 轨迹可判定性（`evalsets/v1.5/mechanism_checks.md`，T30.1/RT-07）。

脚本化两轮检索（FakeLLM + FakeEmbedder + 真实 PG + 真实 `make_pg_retriever`），
制造"目标 chunk 未召回 / 低排名被截断 / 被融合淘汰 / 已给模型但未被采用"四种情形，
**全部断言只读 `get_run_trace()` 的持久化载荷**——内存 recorder 不作数，m15 断言的
是"每轮**持久化**的 per-query 候选元数据足以事后区分四种情形"。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from devkb.agent.service import agentic_answer_question, get_run_trace
from devkb.agent.trace import MAX_CANDIDATE_RECORDS
from devkb.embedding import FakeEmbedder
from devkb.ingest.markdown import approx_token_counter
from devkb.ingest.pipeline import ingest_directory
from devkb.llm import FakeLLM
from devkb.repositories import DocumentRepo, ProjectRepo, RunRepo

# 只出现在 chunk 正文里，绝不出现在任何 query / answer 脚本中：
# 它若出现在轨迹中，就说明某条新增字段把正文带进了持久化载荷
SENTINEL = "SK-DEVKB-T301-SENTINEL-DO-NOT-PERSIST"
# 语料规模 > 两轮候选并集上限（2 轮 × 2 子查询 × TOP_K = 32），保证"未召回"一态非空
CORPUS_FILES = 40
TOP_K = 8

# 问题**不点名任何证据**、evaluate 两轮都报 supported_aspects=[]：使 plan_retention 的
# required_ids 与 diagnostic_ids 双空，退化为纯排名填位——这样第二轮的容量淘汰才确实
# 是"低排名被截断"，而不是"被锚点保留策略挤出"（前审 PG-T301-01）
QUESTION = "库存扣减和补偿事务是怎么配合的？"
# 每轮**两条**子查询：单条子查询时通道候选恰好等于 final top_k，融合序无人出局，
# "被融合淘汰"一态在结构上无法出现（首跑实测）
PLAN = '{"intent":"knowledge_qa","queries":["库存扣减","订单事务"]}'
EVAL_ROUND1 = '{"sufficiency":"insufficient","supported_aspects":[],"missing_aspects":["补偿"]}'
REFINE = '{"queries":["补偿事务回滚","重试次数"]}'
EVAL_ROUND2 = '{"sufficiency":"sufficient","supported_aspects":[],"missing_aspects":[]}'
GENERATE = (
    '{"answer_text":"库存扣减与补偿事务的配合见证据 [E1]。","claims":[{"text":"配合方式见证据",'
    '"evidence_ids":["E1"],"quotes":[]}],"not_found":[]}'
)


async def _seeded_project(session: AsyncSession, corpus: Path) -> uuid.UUID:
    corpus.mkdir(parents=True, exist_ok=True)
    for index in range(1, CORPUS_FILES + 1):
        extra = f"\n{SENTINEL}\n" if index == 1 else ""
        (corpus / f"note-{index:02d}.md").write_text(
            f"# 主题 {index}\n\n库存扣减与补偿事务的第 {index} 段说明，编号 {index:02d}。{extra}",
            encoding="utf-8",
        )
    project = await ProjectRepo(session).create(slug=f"t301-{uuid.uuid4().hex[:8]}", name="t301")
    await session.commit()
    report = await ingest_directory(
        session, project.id, corpus, embedder=FakeEmbedder(), count_tokens=approx_token_counter
    )
    assert report.count("failed") == 0
    return project.id


@pytest.fixture
async def m15_project(
    session: AsyncSession, migrated_db_url: str, tmp_path: Path
) -> AsyncIterator[uuid.UUID]:
    """灌完即收：本例要往共享测试库写 40 个带向量的 chunk。

    集成测试库全程不清空（`conftest.py` 只在 session 级建库/删库），而
    `ix_chunks_embedding_hnsw`（迁移 0002）是**全表**索引、不按 project 分区：留下的
    向量会挤占其他用例（如 `test_isolation` 的跨项目 HNSW 断言）`ef_search` 窗口内的
    名额，实测会让它由绿转红。

    只 DELETE 不够——pgvector 的 HNSW 索引项要等 VACUUM 才回收，在此之前墓碑照样
    占名额（实测：`make ci` 偶绿、`make verify-full` 复现红）。故 DELETE 之后再另开
    一个 AUTOCOMMIT 连接跑 `VACUUM chunks`（VACUUM 不能在事务内执行，手法与
    `conftest._admin_exec` 一致）。原始 SQL 沿用 `test_fail_fast_order.py:213` 的既有
    做法；`projects` 的 FK 级联负责带走 documents/chunks/runs/steps/tools。
    """
    project_id = await _seeded_project(session, tmp_path / "corpus")
    try:
        yield project_id
    finally:
        await session.execute(text("delete from projects where id = :pid"), {"pid": project_id})
        await session.commit()
        engine = create_async_engine(migrated_db_url, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as conn:
                await conn.execute(text("VACUUM chunks"))
        finally:
            await engine.dispose()


def _retrieve_steps(trace: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in trace["steps"] if step["node"] == "retrieve"]


def _step(trace: dict[str, Any], node: str) -> dict[str, Any]:
    return next(step for step in trace["steps"] if step["node"] == node)


async def test_m15_persisted_trace_separates_all_four_candidate_fates(
    session: AsyncSession, m15_project: uuid.UUID
) -> None:
    project_id = m15_project
    llm = FakeLLM([PLAN, EVAL_ROUND1, REFINE, EVAL_ROUND2, GENERATE], model="deepseek-v4-flash")

    answer = await agentic_answer_question(
        session, project_id, QUESTION, embedder=FakeEmbedder(), llm=llm, top_k=TOP_K
    )

    run = (await RunRepo(session, project_id).list_recent(1))[0]
    trace = await get_run_trace(session, project_id, run.id)

    # a 两轮检索各自持久化了候选账本
    rounds = _retrieve_steps(trace)
    assert len(rounds) == 2
    for step in rounds:
        summary = step["output_summary"]
        assert summary["candidates"] and summary["candidates_truncated"] is False
        # g 有界：条数受冻结常量硬约束
        assert len(summary["candidates"]) <= MAX_CANDIDATE_RECORDS

    candidate_paths = {
        item["rel_path"] for step in rounds for item in step["output_summary"]["candidates"]
    }
    corpus_paths = set(await DocumentRepo(session, project_id).list_active_rel_paths(200))
    assert len(corpus_paths) == CORPUS_FILES

    # b 未召回：语料里存在两轮候选都没出现过的文件。判据只在 candidates_truncated
    #   为 false 时成立（否则只能降级为"未观测"），上面已逐轮断言
    never_retrieved = corpus_paths - candidate_paths
    assert never_retrieved

    # c 被融合淘汰：进了通道候选，但完整融合序名次落在 top_k 之外
    fusion_dropped = {
        item["chunk_id"]
        for step in rounds
        for item in step["output_summary"]["candidates"]
        if item["outcome"] == "truncated_after_fusion"
    }
    assert fusion_dropped
    assert all(
        item["fused_rank"] > TOP_K
        for step in rounds
        for item in step["output_summary"]["candidates"]
        if item["outcome"] == "truncated_after_fusion"
    )

    # d 低排名被截断：**断言位次关系**，不是断言 eliminated 的 reason
    second = rounds[1]["output_summary"]
    position = {chunk_id: index for index, chunk_id in enumerate(second["retention_input"])}
    kept_last = max(position[item["chunk_id"]] for item in second["evidences"])
    eliminated = [item["chunk_id"] for item in second["eliminated"]]
    assert eliminated
    assert all(position[chunk_id] > kept_last for chunk_id in eliminated)
    assert second["retention_limit"] >= len(second["evidences"])

    # e 已给模型但未采用：unused 非空，且是 generate 真实入参账本的子集
    given = _step(trace, "generate")["input_summary"]["evidence_ids"]
    usage = _step(trace, "finalize")["output_summary"]["evidence_usage"]
    assert usage["generate_executed"] is True
    assert usage["given"] == given
    assert usage["unused"] and set(usage["unused"]) <= set(given)
    # 与用户看到的 citations 互补（同源账本）
    assert usage["cited"] == [item["evidence_id"] for item in answer["citations"]]
    assert set(usage["cited"]) | set(usage["unused"]) == set(given)
    assert not set(usage["cited"]) & set(usage["unused"])

    # f 四态互不重叠，且终态证据必来自被选中的候选。**逐轮判定**：同一 chunk 在两轮
    #   可以有不同结局（第一轮 selected、第二轮换了子查询后被融合淘汰），跨轮取并集
    #   再判不交会把这件正常的事读成矛盾
    for step in rounds:
        summary = step["output_summary"]
        selected_now = {
            item["chunk_id"] for item in summary["candidates"] if item["outcome"] == "selected"
        }
        dropped_now = {
            item["chunk_id"]
            for item in summary["candidates"]
            if item["outcome"] == "truncated_after_fusion"
        }
        merge_input = set(summary["retention_input"])
        assert not selected_now & dropped_now
        # 本轮检索器交出的候选全部进入跨轮合并输入；保留下来的证据是它的子集
        assert selected_now <= merge_input
        assert {item["chunk_id"] for item in summary["evidences"]} <= merge_input
    assert not candidate_paths & never_retrieved
    assert not set(eliminated) & {item["chunk_id"] for item in second["evidences"]}
    # 防漂移：retention_input 恰为"保留 ∪ 淘汰"
    assert set(second["retention_input"]) == {
        item["chunk_id"] for item in second["evidences"]
    } | set(eliminated)

    # refine term 逐条有来源标签，且与已落盘的 queries 按位对齐（m15 断言 2）
    refine_summary = _step(trace, "refine")["output_summary"]
    assert len(refine_summary["refine_term_sources"]) == len(refine_summary["queries"])

    # finalize 每条 missing 带来源与事实校验依据（m15 断言 3）
    finalize_summary = _step(trace, "finalize")["output_summary"]
    assert all(
        set(item) == {"category", "source", "basis", "refs"}
        for item in finalize_summary["not_found_details"]
    )

    # h 轨迹中无文档正文/secret（m15 断言 4 后半句）
    serialized = json.dumps(trace["steps"], ensure_ascii=False, default=str)
    assert SENTINEL not in serialized
    assert answer["answer_text"] not in serialized
    for word in ("未召回", "不存在", "无匹配", "不相关", "已穷举", "全部候选"):
        assert word not in serialized
