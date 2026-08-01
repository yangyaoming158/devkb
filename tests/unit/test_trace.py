"""T18.1 节点/工具轨迹：成功、refusal、降级、失败路径的 step 矩阵与脱敏。"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from typing import Any

import pytest

from devkb.agent.answer import build_answer
from devkb.agent.graph import run_agent
from devkb.agent.nodes import AgentRuntime, build_candidate_records, classify_refine_term
from devkb.agent.not_found import CorpusProfile
from devkb.agent.state import AgentInput, Evidence
from devkb.agent.trace import (
    MAX_CANDIDATE_RECORDS,
    MAX_CANDIDATES_PER_QUERY,
    CandidateRecord,
    StepRecord,
    TraceRecorder,
)
from devkb.llm import FakeLLM
from devkb.retrieval import (
    MAX_FINAL_TOP_K,
    MAX_SUBQUERIES,
    RRF_K_DEFAULT,
    ChannelRanking,
    RetrievedChunk,
    rrf_fuse,
)

PLAN = '{"intent":"knowledge_qa","queries":["库存扣减"]}'
EVAL_OK = '{"sufficiency":"sufficient","supported_aspects":["库存"],"missing_aspects":[]}'
EVAL_NO = '{"sufficiency":"insufficient","supported_aspects":[],"missing_aspects":["补偿"]}'
REFINE = '{"queries":["库存补偿机制"]}'
GENERATE = (
    '{"answer_text":"库存扣减由事务保护 [E1]。","claims":[{"text":"库存扣减由事务保护",'
    '"evidence_ids":["E1"],"quotes":["库存扣减由事务保护。"]}],"not_found":[]}'
)

EVIDENCE_CONTENT = "库存扣减由事务保护。补偿事务负责回滚。"


def _evidence() -> Evidence:
    return Evidence(
        evidence_id="E1",
        chunk_id=uuid.UUID(int=1),
        rel_path="docs/order.md",
        title_path="Order > Inventory",
        content=EVIDENCE_CONTENT,
        start_line=10,
        end_line=12,
        score=0.9,
    )


def _input() -> AgentInput:
    return AgentInput(
        run_id=uuid.uuid4(), project_id=uuid.uuid4(), question="库存怎么保证并发安全？"
    )


def _runtime(script: list[str | Exception], *, with_evidence: bool = True) -> AgentRuntime:
    async def retriever(_project_id: uuid.UUID, _queries: tuple[str, ...]) -> list[Evidence]:
        return [_evidence()] if with_evidence else []

    return AgentRuntime(llm=FakeLLM(script), retriever=retriever)


async def test_success_path_records_continuous_steps_llm_requests_and_tool() -> None:
    recorder = TraceRecorder()
    result = await run_agent(_runtime([PLAN, EVAL_OK, GENERATE]), _input(), recorder)

    assert [step.node for step in recorder.steps] == result["node_history"]
    assert [step.seq for step in recorder.steps] == list(range(1, len(recorder.steps) + 1))
    assert all(step.status == "ok" for step in recorder.steps)
    assert all(step.attempt == 1 for step in recorder.steps)
    assert all(step.latency_ms >= 0 for step in recorder.steps)

    by_node = {step.node: step for step in recorder.steps}
    # LLM 节点各 1 次请求且挂在对应 step；确定性节点无 LLM 记录
    assert [len(by_node[n].llm_requests) for n in ("plan", "evaluate", "generate")] == [1, 1, 1]
    assert [len(by_node[n].llm_requests) for n in ("retrieve", "verify", "finalize")] == [0, 0, 0]
    request = by_node["plan"].llm_requests[0]
    assert request.call_key == "plan" and request.status == "ok" and request.attempt == 1
    assert request.usage["prompt_tokens"] == 100 and request.usage["completion_tokens"] == 50
    # 工具调用挂在 retrieve step，含有界参数与结果数
    (tool,) = by_node["retrieve"].tools
    assert tool.tool_name == "retrieve" and tool.status == "ok"
    assert tool.result_summary == {"result_count": 1}
    assert by_node["retrieve"].output_summary is not None
    assert by_node["retrieve"].output_summary["evidence_count"] == 1
    assert by_node["finalize"].output_summary is not None
    assert by_node["finalize"].output_summary["final_mode"] == "full"


async def test_refusal_path_steps_show_refine_reason_and_second_round() -> None:
    recorder = TraceRecorder()
    result = await run_agent(
        _runtime([PLAN, EVAL_NO, REFINE, EVAL_NO], with_evidence=False), _input(), recorder
    )

    assert result["final_mode"] == "refusal"
    assert [step.seq for step in recorder.steps] == list(range(1, len(recorder.steps) + 1))
    evaluates = [step for step in recorder.steps if step.node == "evaluate"]
    retrieves = [step for step in recorder.steps if step.node == "retrieve"]
    assert [step.attempt for step in evaluates] == [1, 2]
    assert [step.attempt for step in retrieves] == [1, 2]
    # 回放可见"为什么补检/拒答"：evaluate 摘要含 insufficient 与缺失方面
    assert evaluates[0].output_summary is not None
    assert evaluates[0].output_summary["sufficiency"] == "insufficient"
    assert evaluates[0].output_summary["missing_aspects"] == ["补偿"]
    final = recorder.steps[-1]
    assert final.node == "finalize" and final.status == "ok"
    assert final.output_summary is not None and final.output_summary["final_mode"] == "refusal"


async def test_default_applied_marks_step_degraded_with_request_details() -> None:
    recorder = TraceRecorder()
    await run_agent(_runtime(["不是JSON", "还不是JSON", EVAL_OK, GENERATE]), _input(), recorder)

    plan_step = recorder.steps[0]
    assert plan_step.node == "plan" and plan_step.status == "degraded"
    assert [request.status for request in plan_step.llm_requests] == [
        "invalid_output",
        "invalid_output",
    ]
    assert [request.attempt for request in plan_step.llm_requests] == [1, 2]
    # 解析失败的请求仍记录已消耗的 usage（预算/复算口径一致）
    assert all(r.usage["prompt_tokens"] == 100 for r in plan_step.llm_requests)


async def test_absorbed_retriever_failure_records_failed_tool_and_degraded_step() -> None:
    calls = 0

    async def broken(_project_id: uuid.UUID, _queries: tuple[str, ...]) -> list[Evidence]:
        nonlocal calls
        calls += 1
        raise RuntimeError("db down")

    runtime = AgentRuntime(llm=FakeLLM([PLAN, EVAL_NO, REFINE, EVAL_NO]), retriever=broken)
    recorder = TraceRecorder()
    result = await run_agent(runtime, _input(), recorder)

    assert result["final_mode"] == "refusal"
    retrieves = [step for step in recorder.steps if step.node == "retrieve"]
    assert calls == 2 and len(retrieves) == 2
    for step in retrieves:
        assert step.status == "degraded" and step.error == "retrieve:RuntimeError"
        (tool,) = step.tools
        assert tool.status == "failed" and tool.result_summary is None
        assert tool.error is not None and "RuntimeError" in tool.error


async def test_unexpected_node_exception_records_failed_last_step_and_reraises(
    monkeypatch: Any,
) -> None:
    def broken_verify(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("verifier crashed")

    monkeypatch.setattr("devkb.agent.nodes.verify_draft", broken_verify)
    recorder = TraceRecorder()
    with pytest.raises(RuntimeError, match="verifier crashed"):
        await run_agent(_runtime([PLAN, EVAL_OK, GENERATE]), _input(), recorder)

    last = recorder.steps[-1]
    assert last.node == "verify" and last.status == "failed"
    assert last.error is not None and "RuntimeError" in last.error
    # 异常前的 step 完整保留且 seq 连续
    assert [step.seq for step in recorder.steps] == list(range(1, len(recorder.steps) + 1))
    expected_nodes = ["plan", "retrieve", "evaluate", "generate", "verify"]
    assert [step.node for step in recorder.steps] == expected_nodes


async def test_trace_never_contains_answer_or_document_text() -> None:
    recorder = TraceRecorder()
    result = await run_agent(_runtime([PLAN, EVAL_OK, GENERATE]), _input(), recorder)

    serialized = json.dumps(
        [asdict(step) for step in recorder.steps], ensure_ascii=False, default=str
    )
    assert result["final_answer"] is not None
    assert result["final_answer"] not in serialized
    assert EVIDENCE_CONTENT not in serialized
    # 正文只以字符数出现
    finalize = recorder.steps[-1]
    assert finalize.output_summary is not None
    assert finalize.output_summary["answer_chars"] == len(result["final_answer"])


async def test_recorder_none_keeps_graph_behaviour_unchanged() -> None:
    with_trace = await run_agent(_runtime([PLAN, EVAL_OK, GENERATE]), _input(), TraceRecorder())
    without_trace = await run_agent(_runtime([PLAN, EVAL_OK, GENERATE]), _input())

    assert with_trace["final_answer"] == without_trace["final_answer"]
    assert with_trace["node_history"] == without_trace["node_history"]
    assert with_trace["llm_calls"] == without_trace["llm_calls"]
    # fake-llm 不在价目表：cost 不编造，聚合与单次记录均为 None
    assert with_trace["cost"] is None and without_trace["cost"] is None


def test_persisted_step_summary_keeps_every_elimination_record() -> None:
    """T24 二审发现4：step summary 是淘汰记录**唯一**的持久化载体（完整 AgentState 不落库），
    落库映射不得再按 MAX_SUMMARY_ITEMS 截断，否则回放看不全。"""
    from devkb.agent.service import _step_output_summary

    record = StepRecord(
        seq=1,
        node="retrieve",
        attempt=1,
        status="ok",
        input_summary={"queries": ["q"]},
        output_summary={
            "eliminated": [
                {
                    "chunk_id": str(uuid.UUID(int=index)),
                    "rel_path": f"docs/f{index}.md",
                    "aspect_ids": [],
                    "reason": "capacity_limit",
                }
                for index in range(12)
            ]
        },
        latency_ms=1,
        error=None,
        llm_requests=(),
        tools=(),
    )

    persisted = _step_output_summary(record)

    assert persisted is not None
    assert len(persisted["eliminated"]) == 12
    assert json.loads(json.dumps(persisted))["eliminated"][-1]["rel_path"] == "docs/f11.md"


# ---------------------------------------------------------------------------
# T30.1 有界逐候选诊断轨迹（RT-07 / m15）
# ---------------------------------------------------------------------------

CANDIDATE_FIELDS = {
    "query_index",
    "chunk_id",
    "rel_path",
    "start_line",
    "end_line",
    "vector_rank",
    "vector_score",
    "fused_score",
    "fused_rank",
    "outcome",
}
CANDIDATE_OUTCOMES = {"selected", "truncated_after_fusion"}
REFINE_SOURCES = {"default", "user_question", "retrieved_evidence", "unattributed"}
# 轨迹是人工诊断直接阅读的文本：这些词会把"未观测"读成事实断言（J1 判据可证边界）
FORBIDDEN_TRACE_WORDS = ("未召回", "不存在", "无匹配", "不相关", "无关", "已穷举", "全部候选")

GEN_TRACE = (
    '{"answer_text":"候选轨迹说明 [E1]。","claims":[{"text":"候选轨迹说明",'
    '"evidence_ids":["E1"],"quotes":[]}],"not_found":[]}'
)


def _hit(seed: int, *, path: str | None = None) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.UUID(int=seed),
        rel_path=path or f"docs/c{seed}.md",
        title_path="",
        content=f"chunk-{seed} 的正文。",
        start_line=seed,
        end_line=seed + 4,
        score=0.9 - seed / 1000,
    )


def _fuse(*channels: tuple[str, list[RetrievedChunk]]) -> list[Any]:
    """按生产口径构造完整融合序：与 make_pg_retriever 共用同一个 rrf_fuse。"""
    return rrf_fuse(
        [
            ChannelRanking(query, "vector", tuple(hit.chunk_id for hit in hits))
            for query, hits in channels
        ]
    )


def _evidence_of(hit: RetrievedChunk, *, evidence_id: str, score: float) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        chunk_id=hit.chunk_id,
        rel_path=hit.rel_path,
        title_path="",
        content=hit.content,
        start_line=hit.start_line,
        end_line=hit.end_line,
        score=score,
    )


def _recording_runtime(
    script: list[str | Exception],
    rounds: list[list[RetrievedChunk]],
    *,
    recorder: TraceRecorder | None = None,
    top_k: int = 2,
    max_evidences: int = MAX_FINAL_TOP_K,
    corpus: CorpusProfile | None = None,
) -> AgentRuntime:
    """假检索器：复用生产融合与候选构造，按 make_pg_retriever 的口径记录候选。"""
    counter = {"round": 0}

    async def retriever(_project_id: uuid.UUID, queries: tuple[str, ...]) -> list[Evidence]:
        hits = rounds[min(counter["round"], len(rounds) - 1)]
        counter["round"] += 1
        fused_all = _fuse((queries[0], hits))
        if recorder is not None:
            records, truncated = build_candidate_records([(0, hits)], fused_all, top_k=top_k)
            recorder.record_retrieval_candidates(records, truncated=truncated)
        by_id = {hit.chunk_id: hit for hit in hits}
        return [
            _evidence_of(by_id[item.chunk_id], evidence_id=f"E{index}", score=item.fused_score)
            for index, item in enumerate(fused_all[:top_k], start=1)
        ]

    return AgentRuntime(
        llm=FakeLLM(script),
        retriever=retriever,
        max_evidences=max_evidences,
        corpus=corpus if corpus is not None else CorpusProfile.unknown(),
    )


def _summary_of(recorder: TraceRecorder, node: str, *, index: int = 0) -> dict[str, Any]:
    steps = [step for step in recorder.steps if step.node == node]
    summary = steps[index].output_summary
    assert summary is not None
    return summary


def test_u1_candidate_records_carry_per_query_rank_score_and_fusion_outcome() -> None:
    """U1：逐 (子查询, chunk) 一条记录，融合分可由记录复算，selected 恰为融合序前 top_k。"""
    first = [_hit(1), _hit(2), _hit(3)]
    second = [_hit(2), _hit(4), _hit(5)]
    fused_all = _fuse(("查询甲", first), ("查询乙", second))

    records, truncated = build_candidate_records([(0, first), (1, second)], fused_all, top_k=2)

    assert truncated is False
    assert len(records) == 6
    assert {record.query_index for record in records} == {0, 1}
    assert all(isinstance(record, CandidateRecord) for record in records)
    assert all(set(asdict(record)) == CANDIDATE_FIELDS for record in records)
    # 同一 chunk 被两条子查询命中 → 两条记录：通道名次各异、融合结果相同
    shared = [record for record in records if record.chunk_id == str(uuid.UUID(int=2))]
    assert len(shared) == 2
    assert {record.vector_rank for record in shared} == {1, 2}
    assert len({record.fused_score for record in shared}) == 1
    assert len({record.fused_rank for record in shared}) == 1
    # fused_score 可由记录自身复算：Σ 1/(k + vector_rank)
    for chunk_id in {record.chunk_id for record in records}:
        rows = [record for record in records if record.chunk_id == chunk_id]
        expected = sum(1.0 / (RRF_K_DEFAULT + row.vector_rank) for row in rows)
        assert rows[0].fused_score == pytest.approx(expected, abs=1e-6)
    selected = {record.chunk_id for record in records if record.outcome == "selected"}
    assert selected == {str(item.chunk_id) for item in fused_all[:2]}
    assert all(
        record.fused_rank > 2 for record in records if record.outcome == "truncated_after_fusion"
    )


def test_b1_candidate_records_respect_frozen_per_query_and_total_caps() -> None:
    """B1：单查询上限与总上限均为冻结常量，且与检索侧上限同源（防漂移）。"""
    assert MAX_CANDIDATES_PER_QUERY == MAX_FINAL_TOP_K
    assert MAX_CANDIDATE_RECORDS == MAX_SUBQUERIES * MAX_CANDIDATES_PER_QUERY

    wide = [_hit(seed) for seed in range(1, 21)]
    records, truncated = build_candidate_records([(0, wide)], _fuse(("q", wide)), top_k=8)
    assert len(records) == MAX_CANDIDATES_PER_QUERY and truncated is True

    per_query = [[_hit(100 * index + i) for i in range(1, 13)] for index in range(4)]
    fused_all = _fuse(*[(f"q{index}", hits) for index, hits in enumerate(per_query)])
    records, truncated = build_candidate_records(list(enumerate(per_query)), fused_all, top_k=8)
    assert len(records) == MAX_CANDIDATE_RECORDS and truncated is True


def test_b2_zero_candidates_is_recorded_as_empty_not_missing() -> None:
    """B2："本轮确实没有候选"（空列表）与"本轮未记录"（无键）必须可区分。"""
    records, truncated = build_candidate_records([(0, [])], [], top_k=8)
    assert records == [] and truncated is False

    recorder = TraceRecorder()
    recorder.record_retrieval_candidates(records, truncated=truncated)
    recorder.finish_step(
        node="retrieve", status="ok", input_summary={}, output_summary={}, latency_ms=1
    )
    recorder.finish_step(
        node="evaluate", status="ok", input_summary={}, output_summary={}, latency_ms=1
    )

    assert recorder.steps[0].output_summary == {"candidates": [], "candidates_truncated": False}
    assert recorder.steps[1].output_summary == {}


def test_u5_candidate_records_never_invent_chunks_or_assert_absence() -> None:
    """U5（J1-1/J1-3 fail-closed）：只记录实际取回的 chunk，且不出现缺席断言措辞。"""
    first = [_hit(1), _hit(2)]
    second = [_hit(3)]
    fused_all = _fuse(("甲", first), ("乙", second))

    records, _ = build_candidate_records([(0, first), (1, second)], fused_all, top_k=1)

    assert {record.outcome for record in records} <= CANDIDATE_OUTCOMES
    assert {record.chunk_id for record in records} <= {
        str(hit.chunk_id) for hit in [*first, *second]
    }
    serialized = json.dumps([asdict(record) for record in records], ensure_ascii=False)
    for word in FORBIDDEN_TRACE_WORDS:
        assert word not in serialized


async def test_u2_retrieve_step_summary_links_candidates_to_delivered_evidences() -> None:
    """U2：候选账本与"交给模型的证据"在同一份摘要里可互相定位。"""
    recorder = TraceRecorder()
    runtime = _recording_runtime(
        [PLAN, EVAL_OK, GEN_TRACE], [[_hit(1), _hit(2), _hit(3)]], recorder=recorder, top_k=2
    )

    result = await run_agent(runtime, _input(), recorder)

    summary = _summary_of(recorder, "retrieve")
    assert {"candidates", "candidates_truncated", "evidences", "retention_input"} <= set(summary)
    assert [item["evidence_id"] for item in summary["evidences"]] == ["E1", "E2"]
    assert [item["chunk_id"] for item in summary["evidences"]] == [
        str(evidence.chunk_id) for evidence in result["evidences"]
    ]
    assert {
        item["chunk_id"] for item in summary["candidates"] if item["outcome"] == "selected"
    } == {str(evidence.chunk_id) for evidence in result["evidences"]}
    assert summary["candidates_truncated"] is False


async def test_u6_two_rounds_record_their_own_candidates_without_leaking() -> None:
    """U6：pending 缓冲在 finish_step 后清空——两轮候选互不混淆。"""
    recorder = TraceRecorder()
    runtime = _recording_runtime(
        [PLAN, EVAL_NO, REFINE, EVAL_OK, GEN_TRACE],
        [[_hit(1), _hit(2), _hit(3)], [_hit(11), _hit(12), _hit(13)]],
        recorder=recorder,
        top_k=2,
    )

    await run_agent(runtime, _input(), recorder)

    first = {item["chunk_id"] for item in _summary_of(recorder, "retrieve")["candidates"]}
    second = {item["chunk_id"] for item in _summary_of(recorder, "retrieve", index=1)["candidates"]}
    assert first == {str(uuid.UUID(int=seed)) for seed in (1, 2, 3)}
    assert second == {str(uuid.UUID(int=seed)) for seed in (11, 12, 13)}
    assert first.isdisjoint(second)


async def test_u7_selected_candidate_can_still_be_dropped_by_retention() -> None:
    """U7（J1-4）：selected ≠ 最终证据；retention_input 与保留/淘汰账本三者自洽。"""
    recorder = TraceRecorder()
    runtime = _recording_runtime(
        [PLAN, EVAL_NO, REFINE, EVAL_OK, GEN_TRACE],
        [[_hit(1), _hit(2), _hit(3)], [_hit(11), _hit(12), _hit(13)]],
        recorder=recorder,
        top_k=2,
        max_evidences=2,
    )

    await run_agent(runtime, _input(), recorder)

    first_round = _summary_of(recorder, "retrieve")
    second_round = _summary_of(recorder, "retrieve", index=1)
    selected_first = {
        item["chunk_id"] for item in first_round["candidates"] if item["outcome"] == "selected"
    }
    eliminated = {item["chunk_id"] for item in second_round["eliminated"]}
    assert selected_first & eliminated  # 首轮入选的证据在跨轮合并里被挤掉
    # 防漂移：retention_input 必须恰为"保留 ∪ 淘汰"
    kept = {item["chunk_id"] for item in second_round["evidences"]}
    assert set(second_round["retention_input"]) == kept | eliminated
    assert second_round["retention_limit"] == 2


async def test_u16_anchor_eviction_is_not_reported_as_low_rank_truncation() -> None:
    """U16（J1-6）：reason 相同的两条淘汰，位次关系才能区分"低排名被截断"与"被锚点挤出"。"""
    recorder = TraceRecorder()
    parser = "backend/src/main/java/com/example/CitationParser.java"
    hits = [
        _hit(1, path="backend/src/main/java/com/example/OtherA.java"),
        _hit(2, path="backend/src/main/java/com/example/OtherB.java"),
        _hit(3, path=parser),
        _hit(4, path="backend/src/main/java/com/example/OtherC.java"),
    ]
    question = "请引用 CitationParser.java 的生产源码，说明引用解析怎么做。"
    runtime = _recording_runtime(
        [PLAN, EVAL_OK, GEN_TRACE], [hits], recorder=recorder, top_k=4, max_evidences=2
    )

    await run_agent(
        runtime,
        AgentInput(run_id=uuid.uuid4(), project_id=uuid.uuid4(), question=question),
        recorder,
    )

    summary = _summary_of(recorder, "retrieve")
    position = {chunk_id: index for index, chunk_id in enumerate(summary["retention_input"])}
    kept_last = max(position[item["chunk_id"]] for item in summary["evidences"])
    eliminated = {item["chunk_id"]: item["reason"] for item in summary["eliminated"]}
    low_rank = {cid for cid in eliminated if position[cid] > kept_last}
    anchor_squeezed = {cid for cid in eliminated if position[cid] < kept_last}

    assert low_rank == {str(uuid.UUID(int=4))}
    assert anchor_squeezed == {str(uuid.UUID(int=2))}
    # 关键：两者 reason 相同——所以 reason 本身推不出"低排名"（PG-T301-01）
    assert len(set(eliminated.values())) == 1


async def test_u4_retriever_failure_records_no_candidates() -> None:
    """U4：候选在融合完成后才记录，检索抛错时一条都不写（fail-closed）。"""

    async def broken(_project_id: uuid.UUID, _queries: tuple[str, ...]) -> list[Evidence]:
        raise RuntimeError("db down")

    recorder = TraceRecorder()
    runtime = AgentRuntime(llm=FakeLLM([PLAN, EVAL_NO, REFINE, EVAL_NO]), retriever=broken)

    await run_agent(runtime, _input(), recorder)

    for step in [step for step in recorder.steps if step.node == "retrieve"]:
        assert step.status == "degraded"
        assert step.output_summary is not None
        assert "candidates" not in step.output_summary
        (tool,) = step.tools
        assert tool.status == "failed" and tool.result_summary is None


async def test_u9_failed_step_does_not_inherit_pending_diagnostics(monkeypatch: Any) -> None:
    """U9：节点异常时 pending 已在上一 step 清空，失败 step 不继承诊断键。"""

    def broken_verify(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("verifier crashed")

    monkeypatch.setattr("devkb.agent.nodes.verify_draft", broken_verify)
    recorder = TraceRecorder()
    runtime = _recording_runtime(
        [PLAN, EVAL_OK, GEN_TRACE], [[_hit(1), _hit(2)]], recorder=recorder, top_k=2
    )

    with pytest.raises(RuntimeError, match="verifier crashed"):
        await run_agent(runtime, _input(), recorder)

    assert "candidates" in _summary_of(recorder, "retrieve")
    failed = recorder.steps[-1]
    assert failed.node == "verify" and failed.status == "failed"
    assert failed.output_summary is None


async def test_u8_recorder_none_keeps_every_new_record_point_inert() -> None:
    """U8：无 recorder 时三个记录点全部短路，终态与带轨迹时逐字相同。"""
    rounds = [[_hit(1), _hit(2), _hit(3)], [_hit(11), _hit(12)]]
    script: list[str | Exception] = [PLAN, EVAL_NO, REFINE, EVAL_OK, GEN_TRACE]

    with_trace = await run_agent(
        _recording_runtime(list(script), rounds, recorder=TraceRecorder()), _input()
    )
    without_trace = await run_agent(_recording_runtime(list(script), rounds), _input())

    assert with_trace["final_answer"] == without_trace["final_answer"]
    assert with_trace["final_mode"] == without_trace["final_mode"]
    assert with_trace["node_history"] == without_trace["node_history"]
    assert with_trace["warnings"] == without_trace["warnings"]


def test_u10_refine_term_sources_are_literal_and_non_causal() -> None:
    """U10：四个来源取值各一例，全部为字面判定。"""
    evidences = [_evidence_of(_hit(1, path="docs/rag/parser.md"), evidence_id="E1", score=0.5)]
    question = "库存扣减怎么保证并发安全？"

    assert (
        classify_refine_term("任何词", used_default=True, question=question, evidences=evidences)
        == "default"
    )
    assert (
        classify_refine_term("并发安全", used_default=False, question=question, evidences=evidences)
        == "user_question"
    )
    assert (
        classify_refine_term(
            "chunk-1 的正文", used_default=False, question=question, evidences=evidences
        )
        == "retrieved_evidence"
    )
    assert (
        classify_refine_term(
            "补偿事务重试次数", used_default=False, question=question, evidences=evidences
        )
        == "unattributed"
    )


def test_u11_hint_overlap_never_becomes_an_attribution() -> None:
    """U11（J2 fail-closed）：与 uncovered hint 逐字重合不构成来源归因。"""
    evidences = [_evidence_of(_hit(1), evidence_id="E1", score=0.5)]

    # "CitationParser" 会作为 uncovered hint 进入 refine Prompt，但它既不在问题里
    # 也不在证据里——不得因此被判为任何一种可追溯来源
    assert (
        classify_refine_term(
            "CitationParser",
            used_default=False,
            question="库存扣减怎么保证并发安全？",
            evidences=evidences,
        )
        == "unattributed"
    )


async def test_u10b_refine_term_sources_align_with_persisted_queries() -> None:
    """U10：来源标签按位与既有 queries 对齐，本身不再落一份查询文本。"""
    recorder = TraceRecorder()
    runtime = _recording_runtime(
        [PLAN, EVAL_NO, REFINE, EVAL_OK, GEN_TRACE],
        [[_hit(1), _hit(2)], [_hit(11), _hit(12)]],
        recorder=recorder,
        top_k=2,
    )

    await run_agent(runtime, _input(), recorder)

    summary = _summary_of(recorder, "refine")
    assert len(summary["refine_term_sources"]) == len(summary["queries"])
    assert set(summary["refine_term_sources"]) <= REFINE_SOURCES


async def test_u13_evidence_usage_matches_delivered_citations() -> None:
    """U13：cited ∪ unused == given、两者不相交，且 cited 与 Answer citations 逐一相等。"""
    recorder = TraceRecorder()
    generate = (
        '{"answer_text":"结论一 [E1]，另有旁证 [E2]。","claims":[{"text":"结论一",'
        '"evidence_ids":["E1"],"quotes":[]}],"not_found":[]}'
    )
    runtime = _recording_runtime(
        [PLAN, EVAL_OK, generate],
        [[_hit(1), _hit(2), _hit(3), _hit(4)]],
        recorder=recorder,
        top_k=4,
    )

    result = await run_agent(runtime, _input(), recorder)

    usage = _summary_of(recorder, "finalize")["evidence_usage"]
    assert usage["generate_executed"] is True
    assert usage["given"] == ["E1", "E2", "E3", "E4"]
    assert usage["cited"] == ["E1", "E2"] and usage["unused"] == ["E3", "E4"]
    assert set(usage["cited"]) | set(usage["unused"]) == set(usage["given"])
    assert not set(usage["cited"]) & set(usage["unused"])
    assert usage["cited"] == [item["evidence_id"] for item in build_answer(result)["citations"]]


async def test_u14_refusal_path_reports_every_evidence_as_unused() -> None:
    """U14：claim 全被移除时 cited 为空、unused 为全体，且与 Answer citations 互补。"""
    recorder = TraceRecorder()
    bad = (
        '{"answer_text":"结论 [E1]。","claims":[{"text":"结论","evidence_ids":["E1"],'
        '"quotes":["这段引文在任何证据中都不存在"]}],"not_found":[]}'
    )
    runtime = _recording_runtime(
        [PLAN, EVAL_OK, bad, bad], [[_hit(1), _hit(2)]], recorder=recorder, top_k=2
    )

    result = await run_agent(runtime, _input(), recorder)

    assert result["final_mode"] == "refusal"
    usage = _summary_of(recorder, "finalize")["evidence_usage"]
    assert usage["generate_executed"] is True
    assert usage["cited"] == [] and usage["unused"] == ["E1", "E2"]
    assert build_answer(result)["citations"] == []


async def test_u15_evidence_usage_stays_silent_when_generate_never_ran() -> None:
    """U15（J3-2）：证据非空但 generate 从未执行 → 不得产出任何"已给模型"结论。"""
    recorder = TraceRecorder()
    runtime = _recording_runtime(
        [PLAN, "不是JSON", "还不是JSON", "仍不是JSON", "依然不是JSON"],
        [[_hit(1), _hit(2)]],
        recorder=recorder,
        top_k=2,
    )

    result = await run_agent(runtime, _input(), recorder)

    assert result["evidences"] and result["generate_calls"] == 0
    usage = _summary_of(recorder, "finalize")["evidence_usage"]
    assert usage == {"generate_executed": False, "given": [], "cited": [], "unused": []}


async def test_u3_not_found_details_carry_each_gap_source_and_fact_check_basis() -> None:
    """U3（m15 断言 3）：两条不同来源的缺口逐条落盘，取值与终态明细逐字相同、按位对齐。

    构造沿用 T23 的混合原因拆分：一句里同时含"数据库迁移"（`.sql` 不在摄取范围 →
    static_suffix_rule）与一个**语料索引里确实有**的路径（→ corpus_index），确定性
    拆成两条——这正是"能把格式未摄取与已索引但未召回分开"的那对明细。
    """
    recorder = TraceRecorder()
    indexed = "backend/src/main/java/svc/DocumentService.java"
    generate = json.dumps(
        {
            "answer_text": "部分结论 [E1]。",
            "claims": [{"text": "部分结论", "evidence_ids": ["E1"], "quotes": []}],
            "not_found": [f"当前证据未覆盖数据库迁移与 {indexed} 的删除实现"],
        },
        ensure_ascii=False,
    )
    runtime = _recording_runtime(
        [PLAN, EVAL_OK, generate],
        [[_hit(1), _hit(2)]],
        recorder=recorder,
        top_k=2,
        corpus=CorpusProfile.from_paths([indexed]),
    )

    result = await run_agent(runtime, _input(), recorder)

    summary = _summary_of(recorder, "finalize")
    details = summary["not_found_details"]
    state_details = result["final_not_found_details"]
    assert len(details) == 2 and len(state_details) == 2
    # 值逐条相同（不是只对字段名）
    for persisted, terminal in zip(details, state_details, strict=True):
        assert set(persisted) == {"category", "source", "basis", "refs"}
        assert persisted["category"] == terminal.category
        assert persisted["source"] == terminal.source
        assert persisted["basis"] == terminal.basis
        assert persisted["refs"] == list(terminal.refs)
    # 两条确实是不同来源的缺口，且与同一摘要的 not_found 按位对齐
    by_basis = {item["basis"]: item for item in details}
    assert set(by_basis) == {"static_suffix_rule", "corpus_index"}
    assert by_basis["static_suffix_rule"]["category"] == "unsupported_or_not_ingested"
    assert by_basis["static_suffix_rule"]["refs"] == [".sql"]
    assert by_basis["corpus_index"]["category"] == "missing_from_current_evidence"
    assert by_basis["corpus_index"]["refs"] == [indexed]
    assert summary["not_found"] == result["final_not_found"]
    assert [detail.text for detail in state_details] == list(summary["not_found"])


async def test_u14b_generate_failure_also_reports_every_evidence_as_unused() -> None:
    """U14 的另一半分支：generate 走冻结默认值（正文为确定性降级文案、无任何 [E#]）。

    模型**确实收到了**证据（请求发出过、只是产出不可解析），所以 generate_executed
    仍为 true；但交付引用为空，故全部证据进 unused。
    """
    recorder = TraceRecorder()
    runtime = _recording_runtime(
        [PLAN, EVAL_OK, "不是JSON", "还不是JSON"],
        [[_hit(1), _hit(2)]],
        recorder=recorder,
        top_k=2,
    )

    result = await run_agent(runtime, _input(), recorder)

    assert result["generate_failed"] is True and result["generate_calls"] == 1
    assert result["final_mode"] == "partial"
    usage = _summary_of(recorder, "finalize")["evidence_usage"]
    assert usage["generate_executed"] is True
    assert usage["given"] == ["E1", "E2"]
    assert usage["cited"] == [] and usage["unused"] == ["E1", "E2"]
    assert build_answer(result)["citations"] == []


async def test_u12_not_found_details_summary_is_bounded_and_not_authoritative() -> None:
    """U12（J4-2）：摘要按 MAX_SUMMARY_ITEMS 截断，权威全量留在 Answer JSON。"""
    recorder = TraceRecorder()
    gaps = ",".join(f'"缺口{index}的说明未找到"' for index in range(1, 11))
    generate = (
        '{"answer_text":"部分结论 [E1]。","claims":[{"text":"部分结论",'
        f'"evidence_ids":["E1"],"quotes":[]}}],"not_found":[{gaps}]}}'
    )
    runtime = _recording_runtime(
        [PLAN, EVAL_OK, generate], [[_hit(1), _hit(2)]], recorder=recorder, top_k=2
    )

    result = await run_agent(runtime, _input(), recorder)

    summary = _summary_of(recorder, "finalize")
    details = summary["not_found_details"]
    assert len(details) == 8
    assert all(set(item) == {"category", "source", "basis", "refs"} for item in details)
    assert len(build_answer(result)["not_found_details"]) >= 10


async def test_h1_new_trace_fields_carry_no_text_and_no_absence_claims() -> None:
    """H1：完整载荷零命中禁用措辞，且不含证据/回答正文。"""
    recorder = TraceRecorder()
    runtime = _recording_runtime(
        [PLAN, EVAL_NO, REFINE, EVAL_OK, GEN_TRACE],
        [[_hit(1), _hit(2), _hit(3)], [_hit(11), _hit(12)]],
        recorder=recorder,
        top_k=2,
        max_evidences=2,
    )

    result = await run_agent(runtime, _input(), recorder)

    serialized = json.dumps(
        [asdict(step) for step in recorder.steps], ensure_ascii=False, default=str
    )
    for word in FORBIDDEN_TRACE_WORDS:
        assert word not in serialized
    assert result["final_answer"] is not None and result["final_answer"] not in serialized
    for seed in (1, 2, 3, 11, 12):
        assert _hit(seed).content not in serialized


async def test_h2_new_trace_fields_only_hold_ids_enums_and_numbers() -> None:
    """H2：新增字段的字段级白名单——每个 str 只能是 UUID / rel_path / E# / 枚举。"""
    recorder = TraceRecorder()
    rel_paths = {f"docs/c{seed}.md" for seed in (1, 2, 3, 11, 12)}
    runtime = _recording_runtime(
        [PLAN, EVAL_NO, REFINE, EVAL_OK, GEN_TRACE],
        [[_hit(1), _hit(2), _hit(3)], [_hit(11), _hit(12)]],
        recorder=recorder,
        top_k=2,
        max_evidences=2,
    )

    await run_agent(runtime, _input(), recorder)

    for step in recorder.steps:
        summary = step.output_summary or {}
        for candidate in summary.get("candidates", []):
            assert set(candidate) == CANDIDATE_FIELDS
            assert uuid.UUID(candidate["chunk_id"])
            assert candidate["rel_path"] in rel_paths
            assert candidate["outcome"] in CANDIDATE_OUTCOMES
            assert isinstance(candidate["vector_rank"], int)
            assert isinstance(candidate["vector_score"], float)
        for chunk_id in summary.get("retention_input", []):
            assert uuid.UUID(chunk_id)
        assert set(summary.get("refine_term_sources", [])) <= REFINE_SOURCES
        usage = summary.get("evidence_usage")
        if usage is not None:
            assert set(usage) == {"generate_executed", "given", "cited", "unused"}
            for bucket in ("given", "cited", "unused"):
                assert all(item.startswith("E") for item in usage[bucket])
