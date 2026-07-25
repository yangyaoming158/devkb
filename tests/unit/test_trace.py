"""T18.1 节点/工具轨迹：成功、refusal、降级、失败路径的 step 矩阵与脱敏。"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from typing import Any

import pytest

from devkb.agent.graph import run_agent
from devkb.agent.nodes import AgentRuntime
from devkb.agent.state import AgentInput, Evidence
from devkb.agent.trace import StepRecord, TraceRecorder
from devkb.llm import FakeLLM

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
