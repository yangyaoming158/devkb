"""T17.4 agentic service：Answer v1 落库、warning/调用数/cost 正确、失败终态。"""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from devkb.agent.service import agentic_answer_question, get_run_trace
from devkb.embedding import FakeEmbedder
from devkb.errors import InvalidInputError, NotFoundError
from devkb.ingest.markdown import approx_token_counter
from devkb.ingest.pipeline import ingest_directory
from devkb.llm import FakeLLM, compute_cost
from devkb.repositories import AgentStepRepo, ProjectRepo, RunRepo, ToolInvocationRepo

CORPUS_MD = Path(__file__).parents[1] / "fixtures" / "corpus_md"

PLAN = '{"intent":"knowledge_qa","queries":["库存 并发"]}'
EVAL_OK = '{"sufficiency":"sufficient","supported_aspects":["库存"],"missing_aspects":[]}'
GEN = (
    '{"answer_text":"库存并发由行锁保证 [E1]。","claims":[{"text":"库存并发由行锁保证",'
    '"evidence_ids":["E1"],"quotes":[]}],"not_found":[]}'
)
GEN_BAD_QUOTE = (
    '{"answer_text":"库存并发由行锁保证 [E1]。","claims":[{"text":"库存并发由行锁保证",'
    '"evidence_ids":["E1"],"quotes":["这段引文在任何证据中都不存在"]}],"not_found":[]}'
)


async def _seeded_project(session: AsyncSession) -> uuid.UUID:
    project = await ProjectRepo(session).create(slug=f"t17-{uuid.uuid4().hex[:8]}", name="t17")
    await session.commit()
    report = await ingest_directory(
        session, project.id, CORPUS_MD, embedder=FakeEmbedder(), count_tokens=approx_token_counter
    )
    assert report.count("failed") == 0
    return project.id


async def test_agentic_answer_v1_persists_warnings_calls_and_cost(session: AsyncSession) -> None:
    project_id = await _seeded_project(session)
    llm = FakeLLM([PLAN, EVAL_OK, GEN], model="deepseek-v4-flash")

    answer = await agentic_answer_question(
        session, project_id, "库存怎么保证并发安全？", embedder=FakeEmbedder(), llm=llm, top_k=4
    )

    assert answer["mode"] == "full"
    assert [c["evidence_id"] for c in answer["citations"]] == ["E1"]
    run = await RunRepo(session, project_id).get(uuid.UUID(answer["run_id"]))
    assert run is not None and run.status == "succeeded"
    assert run.answer is not None and run.answer["mode"] == "full"
    assert run.answer["warnings"] == answer["warnings"]
    assert run.model == "deepseek-v4-flash"
    # 调用数与 tokens 由明细可复算（FakeLLM 每次 100+50）
    assert run.usage is not None and run.usage["llm_calls"] == 3
    assert run.tokens_in == 300 and run.tokens_out == 150
    # cost = 3 × 单次调用成本（价目表内模型，逐档复算）
    per_call = compute_cost(
        "deepseek-v4-flash",
        {"prompt_cache_hit_tokens": 20, "prompt_cache_miss_tokens": 80, "completion_tokens": 50},
    )
    assert per_call is not None and run.cost == 3 * per_call == Decimal("0.000540")
    assert run.latency_ms is not None and run.latency_ms >= 0


async def test_regeneration_run_persists_verify_warning_and_extra_calls(
    session: AsyncSession,
) -> None:
    project_id = await _seeded_project(session)
    llm = FakeLLM([PLAN, EVAL_OK, GEN_BAD_QUOTE, GEN], model="deepseek-v4-flash")

    answer = await agentic_answer_question(
        session, project_id, "库存怎么保证并发安全？", embedder=FakeEmbedder(), llm=llm, top_k=4
    )

    assert answer["mode"] == "full"
    assert "verify:l0_l1_failed" in answer["warnings"]
    run = await RunRepo(session, project_id).get(uuid.UUID(answer["run_id"]))
    assert run is not None and run.usage is not None
    assert run.usage["llm_calls"] == 4  # 含 1 次 L1 触发的重生成
    assert run.answer is not None and "verify:l0_l1_failed" in run.answer["warnings"]


async def test_invalid_question_rejected_before_run_creation(session: AsyncSession) -> None:
    project_id = await _seeded_project(session)
    with pytest.raises(InvalidInputError):
        await agentic_answer_question(
            session, project_id, "x" * 4001, embedder=FakeEmbedder(), llm=FakeLLM([]), top_k=4
        )
    assert await RunRepo(session, project_id).list_recent(5) == []


async def test_trace_steps_and_tools_persisted_for_successful_run(session: AsyncSession) -> None:
    """T18.1：成功 run 的节点/工具轨迹落库，seq 连续、摘要有界且无正文。"""
    project_id = await _seeded_project(session)
    llm = FakeLLM([PLAN, EVAL_OK, GEN], model="deepseek-v4-flash")

    answer = await agentic_answer_question(
        session, project_id, "库存怎么保证并发安全？", embedder=FakeEmbedder(), llm=llm, top_k=4
    )

    run_id = uuid.UUID(answer["run_id"])
    steps = await AgentStepRepo(session, project_id).list_for_run(run_id)
    assert [step.seq for step in steps] == list(range(1, len(steps) + 1))
    assert [step.node for step in steps] == [
        "plan",
        "retrieve",
        "evaluate",
        "generate",
        "verify",
        "finalize",
    ]
    assert all(step.status == "ok" for step in steps)
    assert all(step.latency_ms is not None and step.latency_ms >= 0 for step in steps)
    by_node = {step.node: step for step in steps}
    plan_summary = by_node["plan"].output_summary
    assert plan_summary is not None and len(plan_summary["llm_requests"]) == 1
    assert plan_summary["llm_requests"][0]["status"] == "ok"
    finalize_summary = by_node["finalize"].output_summary
    assert finalize_summary is not None and finalize_summary["final_mode"] == "full"
    # 轨迹不含回答正文（只有 answer_chars）
    assert answer["answer_text"] not in str(finalize_summary)

    tools = await ToolInvocationRepo(session, project_id).list_for_run(run_id)
    assert [tool.tool_name for tool in tools] == ["retrieve"]
    assert tools[0].step_id == by_node["retrieve"].id and tools[0].status == "ok"
    assert tools[0].result_summary == {"result_count": 4}

    # 跨项目不可见：另一个项目的 Repo 查同一 run_id 得到空
    other = await ProjectRepo(session).create(slug=f"other-{uuid.uuid4().hex[:8]}", name="other")
    await session.commit()
    assert await AgentStepRepo(session, other.id).list_for_run(run_id) == []
    assert await ToolInvocationRepo(session, other.id).list_for_run(run_id) == []


async def test_unexpected_node_failure_persists_failed_run_and_failed_last_step(
    session: AsyncSession, monkeypatch: Any
) -> None:
    """T18.1/T18.4：节点内未捕获异常 → run 终态 failed，轨迹保留且最后 step 为 failed。"""
    project_id = await _seeded_project(session)

    def broken_verify(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("verifier crashed")

    monkeypatch.setattr("devkb.agent.nodes.verify_draft", broken_verify)
    llm = FakeLLM([PLAN, EVAL_OK, GEN], model="deepseek-v4-flash")
    with pytest.raises(RuntimeError, match="verifier crashed"):
        await agentic_answer_question(
            session, project_id, "库存怎么保证并发安全？", embedder=FakeEmbedder(), llm=llm, top_k=4
        )

    runs = await RunRepo(session, project_id).list_recent(5)
    assert runs and runs[0].status == "failed"
    steps = await AgentStepRepo(session, project_id).list_for_run(runs[0].id)
    assert [step.seq for step in steps] == list(range(1, len(steps) + 1))
    assert steps[-1].node == "verify" and steps[-1].status == "failed"
    assert steps[-1].error is not None and "RuntimeError" in steps[-1].error


async def _persisted_llm_requests(
    session: AsyncSession, project_id: uuid.UUID, run_id: uuid.UUID
) -> list[dict[str, Any]]:
    steps = await AgentStepRepo(session, project_id).list_for_run(run_id)
    return [
        request
        for step in steps
        if step.output_summary
        for request in step.output_summary.get("llm_requests", [])
    ]


async def test_run_usage_and_cost_recomputable_from_persisted_request_details(
    session: AsyncSession,
) -> None:
    """T18.2：含重问的多调用 run，汇总 tokens/cost 可由落库逐请求明细复算。"""
    project_id = await _seeded_project(session)
    # 首个 plan 输出非法 JSON 触发 1 次重问：共 4 次实际请求（重试也消耗并记录）
    llm = FakeLLM(["不是JSON", PLAN, EVAL_OK, GEN], model="deepseek-v4-flash")

    answer = await agentic_answer_question(
        session, project_id, "库存怎么保证并发安全？", embedder=FakeEmbedder(), llm=llm, top_k=4
    )

    run_id = uuid.UUID(answer["run_id"])
    run = await RunRepo(session, project_id).get(run_id)
    assert run is not None and run.usage is not None
    requests = await _persisted_llm_requests(session, project_id, run_id)
    assert len(requests) == 4 == run.usage["llm_calls"]
    assert [r["status"] for r in requests] == ["invalid_output", "ok", "ok", "ok"]
    # tokens 汇总 = 明细求和（解析失败的请求同样计入）
    assert sum(r["usage"]["prompt_tokens"] for r in requests) == run.tokens_in == 400
    assert sum(r["usage"]["completion_tokens"] for r in requests) == run.tokens_out == 200
    # cost 汇总 = 明细分档单价求和；每条明细可独立按价目表复算
    assert all(r["cost"] is not None for r in requests)
    assert sum(Decimal(r["cost"]) for r in requests) == run.cost == Decimal("0.000720")
    for request in requests:
        assert Decimal(request["cost"]) == compute_cost("deepseek-v4-flash", request["usage"])
        assert "prompt_cache_hit_tokens" in request["usage"]


async def test_unknown_model_never_fabricates_cost_in_run_or_details(
    session: AsyncSession,
) -> None:
    """T18.2：价目表外模型 cost 一律为 None（不编造），tokens 照常聚合。"""
    project_id = await _seeded_project(session)
    llm = FakeLLM([PLAN, EVAL_OK, GEN])  # 默认 model="fake-llm"，不在价目表

    answer = await agentic_answer_question(
        session, project_id, "库存怎么保证并发安全？", embedder=FakeEmbedder(), llm=llm, top_k=4
    )

    run_id = uuid.UUID(answer["run_id"])
    run = await RunRepo(session, project_id).get(run_id)
    assert run is not None and run.cost is None
    assert run.tokens_in == 300 and run.tokens_out == 150
    requests = await _persisted_llm_requests(session, project_id, run_id)
    assert len(requests) == 3
    assert all(r["cost"] is None for r in requests)


async def test_get_run_trace_replays_without_reexecution_and_isolates_projects(
    session: AsyncSession,
) -> None:
    """T18.3：回放只读数据库（FakeLLM 脚本已耗尽仍可回放）；跨项目 run_id → NotFound。"""
    project_id = await _seeded_project(session)
    llm = FakeLLM([PLAN, EVAL_OK, GEN_BAD_QUOTE, GEN], model="deepseek-v4-flash")
    answer = await agentic_answer_question(
        session, project_id, "库存怎么保证并发安全？", embedder=FakeEmbedder(), llm=llm, top_k=4
    )
    run_id = uuid.UUID(answer["run_id"])
    prompts_before = len(llm.prompts)

    trace = await get_run_trace(session, project_id, run_id)

    # 回放未触发任何节点/LLM 执行
    assert len(llm.prompts) == prompts_before == 4
    assert trace["run"]["run_id"] == answer["run_id"]
    assert trace["run"]["answer"] is not None and trace["run"]["answer"]["mode"] == "full"
    nodes = [step["node"] for step in trace["steps"]]
    # 能看出为什么重生成：verify#1 未过 → generate#2 → verify#2 通过
    assert nodes == [
        "plan",
        "retrieve",
        "evaluate",
        "generate",
        "verify",
        "generate",
        "verify",
        "finalize",
    ]
    verifies = [step for step in trace["steps"] if step["node"] == "verify"]
    assert verifies[0]["output_summary"]["passed"] is False
    assert any("no_verbatim_match" in e for e in verifies[0]["output_summary"]["errors"])
    assert verifies[1]["output_summary"]["passed"] is True
    retrieve_step = next(step for step in trace["steps"] if step["node"] == "retrieve")
    assert retrieve_step["tools"] and retrieve_step["tools"][0]["tool_name"] == "retrieve"

    # 其他项目的 run_id：统一 NotFound，不泄漏存在性
    other = await ProjectRepo(session).create(slug=f"other-{uuid.uuid4().hex[:8]}", name="other")
    await session.commit()
    with pytest.raises(NotFoundError):
        await get_run_trace(session, other.id, run_id)
    with pytest.raises(NotFoundError):
        await get_run_trace(session, project_id, uuid.uuid4())


async def test_database_error_during_retrieval_degrades_to_persisted_refusal(
    session: AsyncSession, monkeypatch: Any
) -> None:
    """§5.3 第 9 类（数据库异常）：检索期 SQLAlchemyError 走确定性降级并落库。"""
    project_id = await _seeded_project(session)

    async def broken_retrieve(*args: Any, **kwargs: Any) -> Any:
        raise SQLAlchemyError("connection lost")

    monkeypatch.setattr("devkb.agent.nodes.retrieve", broken_retrieve)
    llm = FakeLLM([PLAN, EVAL_OK, '{"queries":["补偿查询"]}', EVAL_OK], model="deepseek-v4-flash")

    answer = await agentic_answer_question(
        session, project_id, "库存怎么保证并发安全？", embedder=FakeEmbedder(), llm=llm, top_k=4
    )

    assert answer["mode"] == "refusal"
    assert answer["citations"] == [] and answer["claims"] == []
    run = await RunRepo(session, project_id).get(uuid.UUID(answer["run_id"]))
    assert run is not None and run.status == "succeeded"
    assert run.answer is not None and run.answer["mode"] == "refusal"
    assert run.usage is not None and run.usage["llm_calls"] == 4


async def test_real_aborted_transaction_during_retrieval_still_persists_run_and_trace(
    session: AsyncSession, monkeypatch: Any
) -> None:
    """T18.4：真实 DB 错误使事务 aborted——rollback 后降级 run 与轨迹仍须落库。

    T18.2 真实 run 实测缺口：此前 aborted 事务令后续 INSERT 全部失败，
    run 永久 running。坏 SQL 在被测 session 上真实执行以复现 aborted 状态。
    """
    from sqlalchemy import text

    project_id = await _seeded_project(session)

    async def poisoned_retrieve(inner_session: Any, *args: Any, **kwargs: Any) -> Any:
        await inner_session.execute(text("SELECT 1/0"))

    monkeypatch.setattr("devkb.agent.nodes.retrieve", poisoned_retrieve)
    llm = FakeLLM([PLAN, EVAL_OK, '{"queries":["补偿查询"]}', EVAL_OK], model="deepseek-v4-flash")

    answer = await agentic_answer_question(
        session, project_id, "库存怎么保证并发安全？", embedder=FakeEmbedder(), llm=llm, top_k=4
    )

    assert answer["mode"] == "refusal"
    run_id = uuid.UUID(answer["run_id"])
    run = await RunRepo(session, project_id).get(run_id)
    assert run is not None and run.status == "succeeded"
    steps = await AgentStepRepo(session, project_id).list_for_run(run_id)
    assert [step.seq for step in steps] == list(range(1, len(steps) + 1))
    retrieves = [step for step in steps if step.node == "retrieve"]
    assert retrieves and all(step.status == "degraded" for step in retrieves)
    assert all(step.error is not None and step.error.startswith("retrieve:") for step in retrieves)
    tools = await ToolInvocationRepo(session, project_id).list_for_run(run_id)
    assert tools and all(tool.status == "failed" for tool in tools)
    # 无永久 running
    assert all(r.status != "running" for r in await RunRepo(session, project_id).list_recent(10))


async def test_llm_timeout_run_persists_degraded_step_with_request_errors(
    session: AsyncSession,
) -> None:
    """T18.4：generate 双超时降级——run succeeded 终态，step 明细含 request_failed。"""
    from devkb.errors import LLMTimeoutError

    project_id = await _seeded_project(session)
    llm = FakeLLM(
        [PLAN, EVAL_OK, LLMTimeoutError("超时"), LLMTimeoutError("超时")],
        model="deepseek-v4-flash",
    )

    answer = await agentic_answer_question(
        session, project_id, "库存怎么保证并发安全？", embedder=FakeEmbedder(), llm=llm, top_k=4
    )

    run_id = uuid.UUID(answer["run_id"])
    run = await RunRepo(session, project_id).get(run_id)
    assert run is not None and run.status == "succeeded"
    steps = await AgentStepRepo(session, project_id).list_for_run(run_id)
    generate = next(step for step in steps if step.node == "generate")
    assert generate.status == "degraded"
    assert generate.output_summary is not None
    requests = generate.output_summary["llm_requests"]
    assert [r["status"] for r in requests] == ["request_failed", "request_failed"]
    assert all("LLMTimeoutError" in r["error"] for r in requests)
    assert steps[-1].node == "finalize" and steps[-1].status == "ok"
    assert all(r.status != "running" for r in await RunRepo(session, project_id).list_recent(10))


async def test_database_error_before_persistence_marks_run_failed(
    session: AsyncSession, monkeypatch: Any
) -> None:
    project_id = await _seeded_project(session)

    async def broken_run_agent(*args: Any, **kwargs: Any) -> Any:
        raise SQLAlchemyError("db write aborted")

    monkeypatch.setattr("devkb.agent.service.run_agent", broken_run_agent)
    with pytest.raises(SQLAlchemyError):
        await agentic_answer_question(
            session, project_id, "问题", embedder=FakeEmbedder(), llm=FakeLLM([]), top_k=4
        )

    runs = await RunRepo(session, project_id).list_recent(5)
    assert runs and runs[0].status == "failed"
    assert runs[0].answer is not None and "SQLAlchemyError" in runs[0].answer["error"]


GEN_FALSE_MISSING = (
    '{"answer_text":"库存并发由行锁保证 [E1]。","claims":[{"text":"库存并发由行锁保证",'
    '"evidence_ids":["E1"],"quotes":[]}],'
    '"not_found":["源码中不存在 table.md 的说明","缺少 V1__init_schema.sql 的建表语句"]}'
)


async def test_agentic_answer_classifies_not_found_against_real_corpus_snapshot(
    session: AsyncSession,
) -> None:
    # T23：service 必须把 documents 表快照注入 runtime——否则 finalize 无法区分
    # "已索引但本轮未召回"与"格式未摄取"，分类退化成一律 missing
    project_id = await _seeded_project(session)
    eval_partial = (
        '{"sufficiency":"partial","supported_aspects":["库存"],"missing_aspects":["表格说明"]}'
    )
    refine = '{"queries":["表格 说明"]}'
    llm = FakeLLM(
        [PLAN, eval_partial, refine, eval_partial, GEN_FALSE_MISSING], model="deepseek-v4-flash"
    )

    answer = await agentic_answer_question(
        session, project_id, "库存怎么保证并发安全？", embedder=FakeEmbedder(), llm=llm, top_k=1
    )

    by_basis = {detail["basis"]: detail for detail in answer["not_found_details"]}
    assert set(by_basis) >= {"corpus_index", "static_suffix_rule"}
    indexed = by_basis["corpus_index"]
    assert indexed["category"] == "missing_from_current_evidence"
    assert indexed["refs"] == ["table.md"]
    assert indexed["original_text"] == "源码中不存在 table.md 的说明"
    assert "不存在" not in indexed["text"].replace("不代表仓库中不存在", "")
    uningested = by_basis["static_suffix_rule"]
    assert uningested["category"] == "unsupported_or_not_ingested"
    assert uningested["refs"] == [".sql"]
    # not_found 契约不变：仍是 list[str]，与 details 一一对应
    assert answer["not_found"] == [d["text"] for d in answer["not_found_details"]]
    run = await RunRepo(session, project_id).get(uuid.UUID(answer["run_id"]))
    assert run is not None and run.answer is not None
    assert run.answer["not_found_details"] == answer["not_found_details"]
