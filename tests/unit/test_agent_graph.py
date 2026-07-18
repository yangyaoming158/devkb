"""有界 LangGraph Fake 路径矩阵（T16.3–T16.5 + T17.5）。

Evaluation-v1 §5.3 十二类路径在本仓库的覆盖映射：
 1 首轮充分直接生成          → test_first_round_sufficient_has_exact_bounded_path
 2 首轮不足→refine→二次充分  → test_refine_then_second_round_sufficient
 3 二次仍不足→refusal        → test_second_round_insufficient_finishes_with_deterministic_refusal
 4 部分证据→partial+not_found → test_partial_evidence_yields_partial_answer_with_merged_not_found
 5 L0 失败→重生成一次        → test_l0_failure_feeds_machine_errors_back_and_regenerates_once
 6 L1 篡改引文→重生成一次    → test_l1_tampered_quote_second_failure_strips_claims_and_downgrades
 7 第二次验证仍失败→降级     → 同上 + test_second_failure_with_all_claims_removed_refuses
 8 结构化解析失败默认路径    → test_invalid_structured_output_defaults_without_identity_override
                              + test_refine_parse_failure_keeps_query_and_does_not_retrieve_again
 9 检索工具异常/LLM 超时     → test_retriever_exception_records_error_and_refuses
                              + test_llm_timeout_retries_then_succeeds / double-timeout 降级
   数据库异常                → tests/integration/test_agent_service.py（run 终态 failed）
10 跨项目越权不可见          → tests/integration/test_isolation.py（runs/chunks/steps/tools）
11 API 并发边界/事件循环     → 依赖 T19 FastAPI，见清单偏差记录（待裁决）
12 轮次/调用/重试硬上限      → 各用例逐项断言 + test_global_budget_can_reach_but_never_exceed_six
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from devkb.agent.graph import (
    GRAPH_RECURSION_LIMIT,
    can_refine,
    route_after_evaluate,
    run_agent,
)
from devkb.agent.nodes import AgentRuntime, make_pg_retriever
from devkb.agent.state import AgentInput, EvaluateOutput, Evidence, initial_agent_state
from devkb.embedding import FakeEmbedder
from devkb.errors import LLMTimeoutError
from devkb.llm import FakeLLM
from devkb.retrieval import HNSW_EF_SEARCH, RetrievedChunk

PLAN = '{"intent":"knowledge_qa","queries":["库存扣减"]}'
EVAL_OK = '{"sufficiency":"sufficient","supported_aspects":["库存"],"missing_aspects":[]}'
EVAL_NO = '{"sufficiency":"insufficient","supported_aspects":[],"missing_aspects":["补偿"]}'
EVAL_PART = '{"sufficiency":"partial","supported_aspects":["库存"],"missing_aspects":["回滚补偿"]}'
REFINE = '{"queries":["库存补偿机制"]}'
GENERATE = (
    '{"answer_text":"库存扣减由事务保护 [E1]。","claims":[{"text":"库存扣减由事务保护",'
    '"evidence_ids":["E1"],"quotes":["库存扣减由事务保护。"]}],"not_found":[]}'
)
GENERATE_NOCLAIMS = '{"answer_text":"库存扣减由事务保护。","claims":[],"not_found":[]}'
GENERATE_PART = (
    '{"answer_text":"仅库存部分有证据 [E1]。","claims":[{"text":"库存扣减由事务保护",'
    '"evidence_ids":["E1"],"quotes":["库存扣减由事务保护。"]}],"not_found":["回滚补偿细节"]}'
)
BAD_GEN_L0 = (
    '{"answer_text":"答案 [E9]。","claims":[{"text":"越界断言",'
    '"evidence_ids":["E9"],"quotes":[]}],"not_found":[]}'
)
BAD_GEN_L1 = (
    '{"answer_text":"库存扣减由锁保护 [E1]。","claims":[{"text":"篡改断言",'
    '"evidence_ids":["E1"],"quotes":["库存扣减由锁保护。"]}],"not_found":[]}'
)
GEN_MIXED = (
    '{"answer_text":"部分修正 [E1]。","claims":['
    '{"text":"正确断言","evidence_ids":["E1"],"quotes":["库存扣减由事务保护。"]},'
    '{"text":"仍越界","evidence_ids":["E9"],"quotes":[]}],"not_found":[]}'
)


def _evidence(seed: int = 1) -> Evidence:
    return Evidence(
        evidence_id="E1",
        chunk_id=uuid.UUID(int=seed),
        rel_path="docs/order.md",
        title_path="Order > Inventory",
        content="库存扣减由事务保护。",
        start_line=10,
        end_line=12,
        score=0.5,
    )


def _runtime(script: list[str | Exception], calls: list[tuple[str, ...]]) -> AgentRuntime:
    async def retriever(_project_id: uuid.UUID, queries: tuple[str, ...]) -> list[Evidence]:
        calls.append(queries)
        return [_evidence(len(calls))]

    return AgentRuntime(llm=FakeLLM(script), retriever=retriever)


def _input() -> AgentInput:
    return AgentInput(run_id=uuid.uuid4(), project_id=uuid.uuid4(), question="库存如何扣减？")


async def test_first_round_sufficient_has_exact_bounded_path() -> None:
    retrievals: list[tuple[str, ...]] = []
    result = await run_agent(_runtime([PLAN, EVAL_OK, GENERATE], retrievals), _input())

    assert result["node_history"] == [
        "plan",
        "retrieve",
        "evaluate",
        "generate",
        "verify",
        "finalize",
    ]
    assert result["retrieval_round"] == 1
    assert result["llm_calls"] == 3 and result["llm_retries"] == 0
    assert result["final_mode"] == "full" and result["status"] == "succeeded"
    assert result["final_answer"] == "库存扣减由事务保护 [E1]。"
    assert [claim.text for claim in result["final_claims"]] == ["库存扣减由事务保护"]
    assert result["final_not_found"] == []
    assert retrievals == [("库存扣减",)]


async def test_refine_then_second_round_sufficient() -> None:
    retrievals: list[tuple[str, ...]] = []
    result = await run_agent(
        _runtime([PLAN, EVAL_NO, REFINE, EVAL_OK, GENERATE], retrievals), _input()
    )

    assert result["node_history"] == [
        "plan",
        "retrieve",
        "evaluate",
        "refine",
        "retrieve",
        "evaluate",
        "generate",
        "verify",
        "finalize",
    ]
    assert result["retrieval_round"] == 2 and result["refine_calls"] == 1
    assert result["llm_calls"] == 5
    assert retrievals == [("库存扣减",), ("库存补偿机制",)]
    assert [e.chunk_id for e in result["evidences"]] == [uuid.UUID(int=2), uuid.UUID(int=1)]


async def test_second_round_insufficient_finishes_with_deterministic_refusal() -> None:
    retrievals: list[tuple[str, ...]] = []
    result = await run_agent(_runtime([PLAN, EVAL_NO, REFINE, EVAL_NO], retrievals), _input())

    assert result["node_history"][-2:] == ["evaluate", "finalize"]
    assert result["retrieval_round"] == 2
    assert result["llm_calls"] == 4
    assert result["final_mode"] == "refusal"
    assert result["generate_calls"] == 0
    # 拒答模板为确定性代码生成并说明缺什么；FakeLLM 脚本恰好耗尽证明无额外调用
    assert result["final_answer"] == "现有资料不足以回答该问题。缺少：补偿。"
    assert result["final_not_found"] == ["补偿"]


async def test_partial_evidence_yields_partial_answer_with_merged_not_found() -> None:
    retrievals: list[tuple[str, ...]] = []
    result = await run_agent(
        _runtime([PLAN, EVAL_PART, REFINE, EVAL_PART, GENERATE_PART], retrievals), _input()
    )

    assert result["node_history"][-3:] == ["generate", "verify", "finalize"]
    assert result["final_mode"] == "partial"
    assert result["final_answer"] == "仅库存部分有证据 [E1]。"
    assert [claim.text for claim in result["final_claims"]] == ["库存扣减由事务保护"]
    assert result["final_not_found"] == ["回滚补偿细节", "回滚补偿"]


async def test_sufficient_without_structured_claims_downgrades_to_partial() -> None:
    retrievals: list[tuple[str, ...]] = []
    result = await run_agent(_runtime([PLAN, EVAL_OK, GENERATE_NOCLAIMS], retrievals), _input())

    assert result["final_mode"] == "partial"
    assert result["final_claims"] == []
    assert any("无结构化 claim" in warning for warning in result["warnings"])


async def test_l0_failure_feeds_machine_errors_back_and_regenerates_once() -> None:
    llm = FakeLLM([PLAN, EVAL_OK, BAD_GEN_L0, GENERATE])

    async def retriever(_project_id: uuid.UUID, _queries: tuple[str, ...]) -> list[Evidence]:
        return [_evidence()]

    result = await run_agent(AgentRuntime(llm=llm, retriever=retriever), _input())

    assert result["node_history"] == [
        "plan",
        "retrieve",
        "evaluate",
        "generate",
        "verify",
        "generate",
        "verify",
        "finalize",
    ]
    assert result["llm_calls"] == 4 and result["generate_calls"] == 2
    verification = result["verification"]
    assert verification is not None and verification.passed
    assert result["final_mode"] == "full"
    # 机器可读错误作为 verification_errors 回传给重生成调用
    assert "verification_errors" in llm.prompts[3]["user"]
    assert "L0:claim[0]:unknown_evidence:E9" in llm.prompts[3]["user"]


async def test_l1_tampered_quote_second_failure_strips_claims_and_downgrades() -> None:
    retrievals: list[tuple[str, ...]] = []
    result = await run_agent(_runtime([PLAN, EVAL_OK, BAD_GEN_L1, GEN_MIXED], retrievals), _input())

    # 第一次 L1 篡改触发重生成；第二稿仍有 L0 失败 claim → 删除后确定性 partial
    assert result["node_history"][-4:] == ["verify", "generate", "verify", "finalize"]
    assert result["llm_calls"] == 4 and result["generate_calls"] == 2
    verification = result["verification"]
    assert verification is not None and not verification.passed
    assert verification.failed_claims == [1]
    assert [claim.text for claim in result["final_claims"]] == ["正确断言"]
    assert result["final_mode"] == "partial"
    assert any("已移除 1 个" in warning for warning in result["warnings"])


async def test_second_failure_with_all_claims_removed_refuses() -> None:
    retrievals: list[tuple[str, ...]] = []
    result = await run_agent(
        _runtime([PLAN, EVAL_OK, BAD_GEN_L1, BAD_GEN_L0], retrievals), _input()
    )

    assert result["generate_calls"] == 2
    assert result["final_claims"] == [] and result["final_mode"] == "refusal"
    assert result["final_answer"] == "现有资料不足以回答该问题。"


async def test_retriever_exception_records_error_and_refuses() -> None:
    attempts = 0

    async def broken_retriever(_project_id: uuid.UUID, _queries: tuple[str, ...]) -> list[Evidence]:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("db down")

    runtime = AgentRuntime(
        llm=FakeLLM([PLAN, EVAL_NO, REFINE, EVAL_NO]), retriever=broken_retriever
    )
    result = await run_agent(runtime, _input())

    assert attempts == 2  # 两轮检索都尝试过（工具调用可断言）
    assert result["errors"] == ["retrieve:RuntimeError", "retrieve:RuntimeError"]
    assert result["evidences"] == [] and result["generate_calls"] == 0
    assert result["final_mode"] == "refusal"
    assert result["llm_calls"] == 4


async def test_llm_timeout_retries_then_succeeds_within_budget() -> None:
    retrievals: list[tuple[str, ...]] = []
    result = await run_agent(
        _runtime([LLMTimeoutError("超时"), PLAN, EVAL_OK, GENERATE], retrievals), _input()
    )

    assert result["llm_calls"] == 4 and result["llm_retries"] == 1
    assert result["retry_counts"] == {"plan": 1}
    assert any("plan:request_failed:LLMTimeoutError" in w for w in result["warnings"])
    assert result["final_mode"] == "full"


async def test_llm_double_timeout_on_generate_degrades_deterministically() -> None:
    retrievals: list[tuple[str, ...]] = []
    result = await run_agent(
        _runtime([PLAN, EVAL_OK, LLMTimeoutError("超时1"), LLMTimeoutError("超时2")], retrievals),
        _input(),
    )

    assert result["generate_failed"] is True and result["generate_calls"] == 1
    assert result["llm_calls"] == 4
    assert result["final_mode"] == "partial"
    assert result["final_answer"] == "现有证据不足以可靠生成回答。"


async def test_regeneration_blocked_when_budget_exhausted() -> None:
    retrievals: list[tuple[str, ...]] = []
    result = await run_agent(
        _runtime(["{}", PLAN, "{}", EVAL_OK, "{}", BAD_GEN_L1], retrievals), _input()
    )

    # 重试耗尽预算：verify 失败但 remaining=0，确定性放弃重生成
    assert result["llm_calls"] == 6 and result["generate_calls"] == 1
    assert result["node_history"][-3:] == ["generate", "verify", "finalize"]
    verification = result["verification"]
    assert verification is not None and not verification.passed
    assert result["final_mode"] == "refusal" and result["final_claims"] == []


async def test_empty_evidence_cannot_be_promoted_to_full_by_evaluate() -> None:
    retrievals: list[tuple[str, ...]] = []

    async def empty_retriever(_project_id: uuid.UUID, queries: tuple[str, ...]) -> list[Evidence]:
        retrievals.append(queries)
        return []

    runtime = AgentRuntime(
        llm=FakeLLM([PLAN, EVAL_OK, REFINE, EVAL_OK]),
        retriever=empty_retriever,
    )
    result = await run_agent(runtime, _input())

    assert result["evidences"] == []
    assert result["node_history"] == [
        "plan",
        "retrieve",
        "evaluate",
        "refine",
        "retrieve",
        "evaluate",
        "finalize",
    ]
    assert result["retrieval_round"] == 2 and result["generate_calls"] == 0
    assert result["final_mode"] == "refusal"
    assert result["final_answer"] == "现有资料不足以回答该问题。"


async def test_invalid_structured_output_defaults_without_identity_override() -> None:
    agent_input = _input()
    malicious_plan = f'{{"intent":"knowledge_qa","queries":["篡改"],"project_id":"{uuid.uuid4()}"}}'
    retrievals: list[tuple[str, ...]] = []
    result = await run_agent(
        _runtime([malicious_plan, malicious_plan, "{}", "{}"], retrievals), agent_input
    )

    assert result["project_id"] == agent_input.project_id
    assert result["run_id"] == agent_input.run_id
    assert retrievals == [(agent_input.question,)]
    assert result["llm_calls"] == 4 and result["llm_retries"] == 2
    assert result["retry_counts"] == {"plan": 1, "evaluate:1": 1}
    assert result["final_mode"] == "refusal"
    # evaluate 双失败走冻结默认 insufficient；拒答模板说明缺什么
    assert result["final_answer"] == "现有资料不足以回答该问题。缺少：证据充分性无法确认。"
    assert all(count <= 1 for count in result["retry_counts"].values())


async def test_retry_budget_deterministically_cuts_optional_refine() -> None:
    retrievals: list[tuple[str, ...]] = []
    result = await run_agent(_runtime(["{}", PLAN, "{}", EVAL_NO], retrievals), _input())

    assert result["llm_calls"] == 4
    assert result["llm_retries"] == 2
    assert result["node_history"] == ["plan", "retrieve", "evaluate", "finalize"]
    assert result["refine_calls"] == 0 and result["retrieval_round"] == 1
    assert result["final_mode"] == "refusal"


async def test_refine_parse_failure_keeps_query_and_does_not_retrieve_again() -> None:
    retrievals: list[tuple[str, ...]] = []
    result = await run_agent(_runtime([PLAN, EVAL_NO, "{}", "{}"], retrievals), _input())

    assert retrievals == [("库存扣减",)]
    assert result["queries"] == ["库存扣减"]
    assert result["refine_failed"] is True
    assert result["node_history"][-2:] == ["refine", "finalize"]
    assert result["retry_counts"]["refine"] == 1


async def test_generate_failure_uses_deterministic_partial_instead_of_full() -> None:
    retrievals: list[tuple[str, ...]] = []
    result = await run_agent(_runtime([PLAN, EVAL_OK, "{}", "{}"], retrievals), _input())

    assert result["generate_failed"] is True
    assert result["generate_calls"] == 1 and result["retry_counts"]["generate:1"] == 1
    assert result["final_mode"] == "partial"
    assert result["final_answer"] == "现有证据不足以可靠生成回答。"


async def test_global_budget_can_reach_but_never_exceed_six() -> None:
    retrievals: list[tuple[str, ...]] = []
    result = await run_agent(
        _runtime([PLAN, EVAL_NO, REFINE, "{}", EVAL_OK, GENERATE], retrievals), _input()
    )

    assert result["llm_calls"] == 6
    assert result["retry_counts"] == {"evaluate:2": 1}
    assert result["final_mode"] == "full"


def test_router_uses_business_limits_in_addition_to_recursion_guard() -> None:
    state = initial_agent_state(_input())
    state["evaluation"] = EvaluateOutput(
        sufficiency="insufficient", supported_aspects=[], missing_aspects=["缺失"]
    )
    state["retrieval_round"] = 2
    assert route_after_evaluate(state) == "finalize"
    state["retrieval_round"] = 1
    state["refine_calls"] = 1
    assert can_refine(state) is False
    state["evaluation"] = EvaluateOutput(
        sufficiency="sufficient", supported_aspects=["已有证据"], missing_aspects=[]
    )
    state["generate_calls"] = 2
    assert route_after_evaluate(state) == "finalize"
    assert GRAPH_RECURSION_LIMIT > 2


async def test_pg_retriever_explicitly_uses_online_hnsw_defaults(monkeypatch: Any) -> None:
    seen: list[dict[str, Any]] = []

    async def fake_retrieve(
        _session: AsyncSession,
        _project_id: uuid.UUID,
        query: str,
        **kwargs: Any,
    ) -> list[RetrievedChunk]:
        seen.append({"query": query, **kwargs})
        return [
            RetrievedChunk(
                chunk_id=uuid.UUID(int=len(seen)),
                rel_path="docs/order.md",
                title_path="Order",
                content="evidence",
                start_line=1,
                end_line=1,
                score=0.9,
            )
        ]

    monkeypatch.setattr("devkb.agent.nodes.retrieve", fake_retrieve)
    retriever = make_pg_retriever(cast(AsyncSession, object()), FakeEmbedder(), top_k=8)
    evidences = await retriever(uuid.uuid4(), ("q1", "q2"))

    assert len(evidences) == 2
    assert [item["mode"] for item in seen] == ["hnsw", "hnsw"]
    assert [item["ef_search"] for item in seen] == [HNSW_EF_SEARCH, HNSW_EF_SEARCH]
    assert all(item["top_k"] == 8 for item in seen)
