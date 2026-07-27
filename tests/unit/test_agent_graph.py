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
   数据库异常                → tests/integration/test_agent_service.py（SQLAlchemyError：
                              检索期降级 refusal + 持久化前失败 run 终态 failed）
10 跨项目越权不可见          → tests/integration/test_isolation.py（runs/chunks/steps/tools）
11 API 并发边界/事件循环     → 依赖 T19 FastAPI，见清单偏差记录（待裁决）
12 轮次/调用/重试硬上限      → 各用例逐项断言 + test_global_budget_can_reach_but_never_exceed_six
"""

from __future__ import annotations

import random
import uuid
from typing import Any, cast

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from devkb.agent.answer import build_answer
from devkb.agent.graph import (
    GRAPH_RECURSION_LIMIT,
    can_refine,
    route_after_evaluate,
    run_agent,
)
from devkb.agent.nodes import (
    AgentNodes,
    AgentRuntime,
    finalize_consistency,
    make_pg_retriever,
    regen_full_block,
)
from devkb.agent.state import (
    AgentInput,
    ClaimOutput,
    DraftSnapshot,
    EvaluateOutput,
    Evidence,
    GenerateOutput,
    initial_agent_state,
)
from devkb.agent.verification import l0_errors, verify_draft
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
    # 正文按保留 claim 重建，失败 claim 的句子与标记不残留
    assert result["final_answer"] == "正确断言 [E1]。"
    assert any("已移除 1 个" in warning for warning in result["warnings"])


async def test_stripped_claims_leave_no_untrusted_text_marks_or_citations() -> None:
    """复评发现 1 的回归：二次失败删除 claim 后，正文/引用不得残留不可信内容。"""
    evidences = [
        Evidence(
            evidence_id="E1",
            chunk_id=uuid.UUID(int=1),
            rel_path="docs/order.md",
            title_path="Order",
            content="库存扣减由事务保护。",
            start_line=1,
            end_line=2,
            score=0.9,
        ),
        Evidence(
            evidence_id="E2",
            chunk_id=uuid.UUID(int=2),
            rel_path="docs/cancel.md",
            title_path="Cancel",
            content="订单取消走补偿事务。",
            start_line=1,
            end_line=2,
            score=0.8,
        ),
    ]

    async def retriever(_project_id: uuid.UUID, _queries: tuple[str, ...]) -> list[Evidence]:
        return evidences

    mixed = (
        '{"answer_text":"库存扣减由事务保护 [E1]。编造的补偿说法 [E2]。",'
        '"claims":['
        '{"text":"库存扣减由事务保护","evidence_ids":["E1"],"quotes":["库存扣减由事务保护。"]},'
        '{"text":"编造的补偿说法","evidence_ids":["E2"],"quotes":["订单取消不需要补偿"]}],'
        '"not_found":[]}'
    )
    runtime = AgentRuntime(llm=FakeLLM([PLAN, EVAL_OK, mixed, mixed]), retriever=retriever)
    result = await run_agent(runtime, _input())

    assert result["final_mode"] == "partial"
    assert result["final_answer"] == "库存扣减由事务保护 [E1]。"
    assert "编造" not in (result["final_answer"] or "")
    assert "[E2]" not in (result["final_answer"] or "")
    assert [claim.text for claim in result["final_claims"]] == ["库存扣减由事务保护"]
    assert result["final_not_found"] == []

    answer = build_answer(result)
    # E2 合法存在但只被失败 claim 引用：citations 不得包含它
    assert [c["evidence_id"] for c in answer["citations"]] == ["E1"]
    assert any("已移除 1 个" in item for item in answer["limitations"])


async def test_rebuilt_answer_strips_unchecked_marks_inside_kept_claim_text() -> None:
    """复评发现的缺口：保留 claim 的 text 内嵌未经 L0 检查的 [E#]，重建不得注入终稿。"""
    evidences = [
        Evidence(
            evidence_id="E1",
            chunk_id=uuid.UUID(int=1),
            rel_path="docs/order.md",
            title_path="Order",
            content="库存扣减由事务保护。",
            start_line=1,
            end_line=2,
            score=0.9,
        ),
        Evidence(
            evidence_id="E2",
            chunk_id=uuid.UUID(int=2),
            rel_path="docs/cancel.md",
            title_path="Cancel",
            content="订单取消走补偿事务。",
            start_line=1,
            end_line=2,
            score=0.8,
        ),
    ]

    async def retriever(_project_id: uuid.UUID, _queries: tuple[str, ...]) -> list[Evidence]:
        return evidences

    # 保留 claim（下标 0）的 text 含越界 [E9] 与合法但未绑定的 [E2]；
    # 下标 1 的 claim 引文篡改触发重生成，二稿相同 → 删除后按保留 claim 重建
    mixed = (
        '{"answer_text":"库存扣减由事务保护 [E1]。编造的补偿说法 [E2]。",'
        '"claims":['
        '{"text":"库存扣减由事务保护[E9]，另见[E2]","evidence_ids":["E1"],'
        '"quotes":["库存扣减由事务保护。"]},'
        '{"text":"编造的补偿说法","evidence_ids":["E2"],"quotes":["订单取消不需要补偿"]}],'
        '"not_found":[]}'
    )
    runtime = AgentRuntime(llm=FakeLLM([PLAN, EVAL_OK, mixed, mixed]), retriever=retriever)
    result = await run_agent(runtime, _input())

    assert result["final_mode"] == "partial"
    # 终稿只含由 evidence_ids 规范生成的 [E1]；text 内嵌的 E9/E2 全部剔除
    assert result["final_answer"] == "库存扣减由事务保护，另见 [E1]。"
    assert "[E9]" not in (result["final_answer"] or "")
    assert "[E2]" not in (result["final_answer"] or "")

    # 最终防线：对重建后的 GenerateOutput 重新执行确定性 L0 必须零错误
    rebuilt = GenerateOutput(
        answer_text=result["final_answer"] or "",
        claims=result["final_claims"],
        not_found=result["final_not_found"],
    )
    errors, failed = l0_errors(rebuilt, evidences)
    assert errors == [] and failed == set()


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


# ---- T25.1 finalize 前确定性一致性检查 --------------------------------------
#
# 合同：docs/tasks/T25.1-finalize-consistency.md（I* 图级用例 + U7–U10 判据函数级）

_RAG_PATH = "backend/src/main/java/svc/RagService.java"
_RAG_CONTENT = (
    "public Conversation findConversation(Long conversationId, Long ownerId) {\n"
    "  return conversationRepository.findByIdAndOwner(conversationId, ownerId);\n}"
)
CASE7_GEN = (
    '{"answer_text":"会话删除前先经 findConversation 做 owner 校验 [E1]。","claims":'
    '[{"text":"RagService.findConversation 调用 findByIdAndOwner 强制 owner 隔离",'
    '"evidence_ids":["E1"],"quotes":["conversationRepository.findByIdAndOwner"]}],'
    '"not_found":["RagService.findConversation 实现未给出，无法确认会话删除是否强制 owner"]}'
)
BODY_ONLY_GEN = (
    '{"answer_text":"依据 [E1] 可知会话删除有 owner 校验。","claims":[],'
    '"not_found":["未找到 RagService"]}'
)
# 诚实边界不变量（PG-01/PG-05）：交付文案与 warning 都不得出现这些未经证明的措辞
FORBIDDEN_CLAIMS = (
    "已被证明",
    "已被支撑",
    "直接支撑",
    "不成立",
    "可以确认",
    "存在冲突",
    "相矛盾",
)


def _custom_runtime(script: list[str | Exception], evidences: list[Evidence]) -> AgentRuntime:
    async def retriever(_project_id: uuid.UUID, _queries: tuple[str, ...]) -> list[Evidence]:
        return list(evidences)

    return AgentRuntime(llm=FakeLLM(script), retriever=retriever)


def _rag_evidence() -> Evidence:
    return Evidence(
        evidence_id="E1",
        chunk_id=uuid.UUID(int=77),
        rel_path=_RAG_PATH,
        title_path="RagService",
        content=_RAG_CONTENT,
        start_line=230,
        end_line=240,
        score=0.7,
    )


async def test_i1_case7_gap_pointing_at_delivered_citation_is_scoped_not_asserted() -> None:
    """I1：案例七原型——缺口指向终稿引用来源时追加可证披露，且不声称已支撑/存在冲突。"""
    result = await run_agent(
        _custom_runtime([PLAN, EVAL_OK, CASE7_GEN], [_rag_evidence()]),
        AgentInput(
            run_id=uuid.uuid4(), project_id=uuid.uuid4(), question="用户 A 能否删除用户 B 的会话？"
        ),
    )

    assert result["final_mode"] == "partial"
    (gap,) = result["final_not_found"]
    assert "无法确认会话删除是否强制 owner" in gap  # 原缺口语义保留
    assert "本次回答的引用来源之一" in gap
    assert "未经确定性核验" in gap
    assert any("未经确定性核验的命题" in warning for warning in result["warnings"])
    detail = result["final_not_found_details"][0]
    assert (
        detail.original_text
        == "RagService.findConversation 实现未给出，无法确认会话删除是否强制 owner"
    )
    assert detail.refs == (_RAG_PATH,)
    answer = build_answer(result)
    for blob in (*answer["not_found"], *answer["limitations"], *answer["warnings"]):
        for word in FORBIDDEN_CLAIMS:
            assert word not in blob, (word, blob)


async def test_i2_partial_without_gaps_does_not_point_at_empty_not_found() -> None:
    """I2：not_found 被 T23 剔空后，limitations 不得再把用户指向空清单。"""
    eval_supported = (
        '{"sufficiency":"sufficient","supported_aspects":["回滚补偿"],"missing_aspects":[]}'
    )
    generate = (
        '{"answer_text":"库存扣减由事务保护 [E1]。","claims":[{"text":"库存扣减由事务保护",'
        '"evidence_ids":["E1"],"quotes":["库存扣减由事务保护"]}],"not_found":["回滚补偿"]}'
    )
    retrievals: list[tuple[str, ...]] = []
    result = await run_agent(
        _runtime([PLAN, EVAL_PART, REFINE, eval_supported, generate], retrievals), _input()
    )

    assert result["final_mode"] == "partial"
    assert result["final_not_found"] == []
    limitations = build_answer(result)["limitations"]
    assert not any("未覆盖方面见 not_found" in item for item in limitations)
    assert any("本次未列出具体未覆盖方面" in item for item in limitations)


async def test_i3_generate_failure_limitation_names_the_deterministic_fallback() -> None:
    """I3（PG-04 对照 A）：生成失败的 partial 不得声称"回答了现有证据支持的部分"。"""
    retrievals: list[tuple[str, ...]] = []
    result = await run_agent(_runtime([PLAN, EVAL_OK, "{}", "{}"], retrievals), _input())

    assert result["final_mode"] == "partial" and result["generate_failed"] is True
    assert result["final_answer"] == "现有证据不足以可靠生成回答。"
    limitations = build_answer(result)["limitations"]
    assert any("正文为确定性降级文案" in item for item in limitations)
    assert not any("仅回答了现有证据支持的部分" in item for item in limitations)


async def test_i7_no_claims_without_generate_failure_reports_the_real_limitation() -> None:
    """I7（PG-04 对照 B）：模型真实正文但无结构化 claim ≠ 确定性降级文案。"""
    retrievals: list[tuple[str, ...]] = []
    result = await run_agent(_runtime([PLAN, EVAL_OK, GENERATE_NOCLAIMS], retrievals), _input())

    assert result["final_mode"] == "partial" and result["generate_failed"] is False
    assert result["final_answer"] == "库存扣减由事务保护。"
    limitations = build_answer(result)["limitations"]
    assert any("未经逐条引用验证" in item for item in limitations)
    assert not any("确定性降级文案" in item for item in limitations)


async def test_i4_regenerated_draft_goes_through_the_same_consistency_pass() -> None:
    """I4：L1 失败重生成后，第二稿的缺口同样过 T25.1；第一稿文案无残留。"""
    bad_quote = (
        '{"answer_text":"会话删除有 owner 校验 [E1]。","claims":[{"text":"首稿断言",'
        '"evidence_ids":["E1"],"quotes":["findByIdAndOwnerId 强制校验"]}],'
        '"not_found":["首稿缺口：构造器细节未召回"]}'
    )
    result = await run_agent(
        _custom_runtime([PLAN, EVAL_OK, bad_quote, CASE7_GEN], [_rag_evidence()]),
        AgentInput(
            run_id=uuid.uuid4(), project_id=uuid.uuid4(), question="用户 A 能否删除用户 B 的会话？"
        ),
    )

    assert result["generate_calls"] == 2 and result["llm_calls"] <= 6
    assert result["node_history"][-5:] == [
        "generate",
        "verify",
        "generate",
        "verify",
        "finalize",
    ]
    assert not any("首稿缺口" in item for item in result["final_not_found"])
    (gap,) = result["final_not_found"]
    assert "本次回答的引用来源之一" in gap


async def test_i5_full_candidate_with_evaluator_gap_is_downgraded_not_silently_dropped() -> None:
    """I5：full 终态出现缺口时降级 partial 并保留缺口，不得静默丢弃。"""
    eval_with_missing = (
        '{"sufficiency":"sufficient","supported_aspects":["库存"],"missing_aspects":["补偿路径"]}'
    )
    retrievals: list[tuple[str, ...]] = []
    result = await run_agent(_runtime([PLAN, eval_with_missing, GENERATE], retrievals), _input())

    assert result["final_mode"] == "partial"
    assert result["final_not_found"] == ["补偿路径"]
    assert any("降级 partial" in warning for warning in result["warnings"])
    limitations = build_answer(result)["limitations"]
    assert any("未覆盖方面见 not_found" in item for item in limitations)


async def test_i6_body_only_citation_counts_as_delivered_evidence() -> None:
    """I6（PG-02）：正文独立 [E#]、claims 为空时，交付引用集仍由正文标记贡献。"""
    result = await run_agent(
        _custom_runtime([PLAN, EVAL_OK, BODY_ONLY_GEN], [_rag_evidence()]),
        AgentInput(
            run_id=uuid.uuid4(), project_id=uuid.uuid4(), question="用户 A 能否删除用户 B 的会话？"
        ),
    )

    answer = build_answer(result)
    assert [citation["evidence_id"] for citation in answer["citations"]] == ["E1"]
    assert result["final_claims"] == []
    (gap,) = result["final_not_found"]
    assert gap.startswith(f"{_RAG_PATH}：")
    assert "未找到" not in gap
    assert any("已按引用事实更正" in warning for warning in result["warnings"])


def _claim() -> ClaimOutput:
    return ClaimOutput(text="断言", evidence_ids=["E1"], quotes=[])


def test_u7_consistent_partial_is_left_untouched() -> None:
    """U7：合法 partial 终态不得误报。"""
    assert finalize_consistency("partial", [_claim()], [], "正文 [E1]。") == ("partial", [])


def test_u8_finalize_consistency_is_idempotent() -> None:
    """U8：同一终态连续裁决结果一致。"""
    args = ("full", [_claim()], ["缺口"], "正文 [E1]。")
    first = finalize_consistency(*args)
    assert first == finalize_consistency(*args)
    assert first[0] == "partial"


def test_u9_full_requires_no_gap_and_at_least_one_claim() -> None:
    """U9：full 的两个结构前提各自触发降级。"""
    with_gap, gap_warnings = finalize_consistency("full", [_claim()], ["缺口"], "正文 [E1]。")
    assert with_gap == "partial"
    assert any("未覆盖缺口" in warning for warning in gap_warnings)

    no_claim, claim_warnings = finalize_consistency("full", [], [], "正文。")
    assert no_claim == "partial"
    assert any("无结构化 claim" in warning for warning in claim_warnings)


def test_u10_refusal_is_never_upgraded_and_residues_are_reported() -> None:
    """U10：refusal 只报告残留、绝不上调。"""
    mode, warnings = finalize_consistency("refusal", [_claim()], [], "正文 [E1]。")
    assert mode == "refusal"
    assert any("残留结构化 claim" in warning for warning in warnings)
    assert any("残留引用标记" in warning for warning in warnings)


# ---- T25.2 第二稿覆盖与一致性复检 -------------------------------------------
#
# 合同：docs/tasks/T25.2-regen-coverage-diff.md
# 测试 ID 前缀 t252_：本文件已被 T25.1 占用同名 U/I 编号，前缀保持两套账本各自可追溯。
# 真值表七行（packet「关键不变量 · warning 真值表」）与本节用例一一对应：
#   行1→U7/I4  行2→U18  行3→U8/I7  行4→U10  行5→U17  行6→U6/I2/I3  行7→U9/U11/I7

# 首稿：L1 篡改一字（"事务"→"锁"）触发重生成，且自述一条缺口
T252_GEN_GAP_BAD_L1 = (
    '{"answer_text":"库存扣减由锁保护 [E1]。","claims":[{"text":"篡改断言",'
    '"evidence_ids":["E1"],"quotes":["库存扣减由锁保护。"]}],'
    '"not_found":["回滚补偿细节"]}'
)
# 第二稿：缺口由 1 条增至 2 条（P3 不成立，真值表行 7）
T252_GEN_TWO_GAPS = (
    '{"answer_text":"库存扣减由事务保护 [E1]。","claims":[{"text":"库存扣减由事务保护",'
    '"evidence_ids":["E1"],"quotes":["库存扣减由事务保护。"]}],'
    '"not_found":["回滚补偿细节","延迟队列重放细节"]}'
)
T252_EVAL_MISSING = (
    '{"sufficiency":"sufficient","supported_aspects":["库存"],"missing_aspects":["补偿路径"]}'
)
# 诚实边界（继承 T25.1 并按 packet 扩四条）：跨稿复检推不出这些措辞
T252_FORBIDDEN = (*FORBIDDEN_CLAIMS, "仍然存在", "隐瞒", "遗漏", "更差")

T252_BLOCK_WARNING = (
    "finalize: 第一稿声明过 1 条未覆盖方面、第二稿未再声明，其间无新证据，据此不判 full"
)
T252_NO_SNAPSHOT_WARNING = "finalize: 发生重生成但第一稿快照缺失，跨稿复检不适用"
T252_EVIDENCE_DIFF_WARNING = "finalize: 两稿证据集不同，跨稿复检不适用"


def _t252_snapshot(*gaps: str, evidence_ids: tuple[str, ...] = ("E1",)) -> DraftSnapshot:
    return DraftSnapshot(not_found=gaps, evidence_ids=evidence_ids)


def _t252_count_warning(first_count: int, second_count: int) -> str:
    return (
        f"finalize: 第一稿 not_found {first_count} 条、第二稿 {second_count} 条，"
        "两稿逐条对应关系未作判定"
    )


def test_t252_u1_draft_snapshot_is_frozen_strict_and_closed() -> None:
    """U1：DraftSnapshot 只有两个字段，且 frozen / extra=forbid / strict 三件套齐备。"""
    snapshot = DraftSnapshot(not_found=("回滚补偿细节",), evidence_ids=("E1", "E2"))

    assert set(DraftSnapshot.model_fields) == {"not_found", "evidence_ids"}
    with pytest.raises(ValidationError):
        snapshot.not_found = ()  # type: ignore[misc]
    with pytest.raises(ValidationError):
        DraftSnapshot(not_found=(), evidence_ids=(), extra="x")  # type: ignore[call-arg]
    # strict：list 不得被静默转成 tuple，元素类型不得被强转
    with pytest.raises(ValidationError):
        DraftSnapshot(not_found=["回滚补偿细节"], evidence_ids=())  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        DraftSnapshot(not_found=(), evidence_ids=(1,))  # type: ignore[arg-type]


def test_t252_u2_initial_state_carries_no_first_draft_snapshot() -> None:
    """U2：初态无快照。"""
    assert initial_agent_state(_input())["first_draft"] is None


async def test_t252_u4_first_generate_snapshots_gaps_and_evidence_ids() -> None:
    """U4：首次 generate 后快照 == （该稿原始 not_found，当前证据编号）。"""
    retrievals: list[tuple[str, ...]] = []
    result = await run_agent(_runtime([PLAN, EVAL_OK, GENERATE_PART], retrievals), _input())

    assert result["generate_calls"] == 1
    assert result["first_draft"] == _t252_snapshot("回滚补偿细节")


async def test_t252_u5_snapshot_presence_matches_generate_calls() -> None:
    """U5：不变式 first_draft is not None ⟺ generate_calls >= 1（0/1/2 稿三态）。"""
    retrievals: list[tuple[str, ...]] = []
    no_draft = await run_agent(_runtime([PLAN, EVAL_NO, REFINE, EVAL_NO], retrievals), _input())
    one_draft = await run_agent(_runtime([PLAN, EVAL_OK, GENERATE], retrievals), _input())
    two_drafts = await run_agent(
        _runtime([PLAN, EVAL_OK, T252_GEN_GAP_BAD_L1, GENERATE], retrievals), _input()
    )

    for result in (no_draft, one_draft, two_drafts):
        assert (result["first_draft"] is not None) is (result["generate_calls"] >= 1)
    assert no_draft["generate_calls"] == 0
    assert one_draft["generate_calls"] == 1
    assert two_drafts["generate_calls"] == 2
    # 两稿路径下快照留的是**第一稿**的缺口，不是终稿的
    assert two_drafts["first_draft"] == _t252_snapshot("回滚补偿细节")


async def test_t252_u12_generate_reask_does_not_overwrite_the_snapshot() -> None:
    """U12：首次 generate 的格式重问不推进 generate_calls，快照只写一次且不被第二稿覆写。"""
    retrievals: list[tuple[str, ...]] = []
    result = await run_agent(
        _runtime([PLAN, EVAL_OK, "{}", T252_GEN_GAP_BAD_L1, GENERATE], retrievals), _input()
    )

    assert result["retry_counts"] == {"generate:1": 1}
    assert result["generate_calls"] == 2 and result["llm_calls"] == 5
    draft = result["answer_draft"]
    assert draft is not None and draft.not_found == []  # 终稿无缺口
    assert result["first_draft"] == _t252_snapshot("回滚补偿细节")


async def test_t252_u13_snapshot_ignores_failed_claims_and_body_marks() -> None:
    """U13（裁决 C1）：首稿含 L1 失败 claim 与正文独立标记，快照仍只有两个字段。"""
    retrievals: list[tuple[str, ...]] = []
    result = await run_agent(
        _runtime([PLAN, EVAL_OK, T252_GEN_GAP_BAD_L1, GEN_MIXED], retrievals), _input()
    )

    snapshot = result["first_draft"]
    assert snapshot is not None
    assert snapshot.model_dump() == {"not_found": ("回滚补偿细节",), "evidence_ids": ("E1",)}


def test_t252_u6_all_three_preconditions_block_full() -> None:
    """U6（行 6）：P1∧P2∧P3 全成立 → 否决 full + 恰好一条否决 warning。"""
    blocked, warnings = regen_full_block(2, _t252_snapshot("回滚补偿细节"), [], ("E1",))

    assert blocked is True
    assert warnings == [T252_BLOCK_WARNING]


def test_t252_u7_single_draft_state_is_a_legal_negative() -> None:
    """U7（行 1，P1 独立负例）：单稿通过验证是合法常态，零新增 warning。"""
    assert regen_full_block(1, _t252_snapshot("回滚补偿细节"), [], ("E1",)) == (False, [])


def test_t252_u18_regeneration_without_snapshot_fails_closed() -> None:
    """U18（行 2，PG-11）：重生成但快照缺失时只陈述自身状态，不断言两稿证据集。"""
    blocked, warnings = regen_full_block(2, None, [], ("E1",))

    assert blocked is False
    assert warnings == [T252_NO_SNAPSHOT_WARNING]
    assert T252_EVIDENCE_DIFF_WARNING not in warnings


def test_t252_u8_different_evidence_sets_skip_the_cross_draft_check() -> None:
    """U8（行 3，P2 独立负例）：两稿证据集不同 → 不否决，只留一条不适用 warning。"""
    first = _t252_snapshot("回滚补偿细节", evidence_ids=("E1", "E2"))

    assert regen_full_block(2, first, [], ("E1",)) == (False, [T252_EVIDENCE_DIFF_WARNING])


def test_t252_u9_empty_first_draft_only_yields_a_count_warning() -> None:
    """U9（行 7，P3 负例一）：第一稿无缺口、第二稿有缺口 → 只发计数 warning。"""
    blocked, warnings = regen_full_block(2, _t252_snapshot(), ["回滚补偿细节"], ("E1",))

    assert blocked is False
    assert warnings == [_t252_count_warning(0, 1)]


def test_t252_u10_identical_empty_not_found_adds_no_warning() -> None:
    """U10（行 4）：两稿逐字相同且均空 → 零新增 warning。"""
    assert regen_full_block(2, _t252_snapshot(), [], ("E1",)) == (False, [])


def test_t252_u17_identical_non_empty_not_found_adds_no_warning() -> None:
    """U17（行 5，PG-06-R）：逐字相同且均非空同样零 warning——非空侧不得一律报警。"""
    first = _t252_snapshot("回滚补偿细节", "延迟队列重放细节")
    second = ["回滚补偿细节", "延迟队列重放细节"]

    assert regen_full_block(2, first, second, ("E1",)) == (False, [])


def test_t252_u11_reworded_gap_gets_only_a_count_warning() -> None:
    """U11（行 7，N-2）：同一缺口改写措辞时不做逐条身份标注，只发计数并声明未判定。"""
    first = _t252_snapshot("回滚补偿细节未召回")
    blocked, warnings = regen_full_block(2, first, ["补偿回滚的细节没有找到"], ("E1",))

    assert blocked is False
    assert warnings == [_t252_count_warning(1, 1)]
    assert "两稿逐条对应关系未作判定" in warnings[0]


def test_t252_u15_new_warnings_stay_within_the_provable_boundary() -> None:
    """U15（N-1）：全部新增 warning 无禁用措辞；否决 warning 逐字含三段可证事实。"""
    emitted = [
        *regen_full_block(2, _t252_snapshot("缺口"), [], ("E1",))[1],
        *regen_full_block(2, None, [], ("E1",))[1],
        *regen_full_block(2, _t252_snapshot("缺口", evidence_ids=("E2",)), [], ("E1",))[1],
        *regen_full_block(2, _t252_snapshot(), ["缺口"], ("E1",))[1],
        *regen_full_block(2, _t252_snapshot("缺口甲"), ["缺口乙", "缺口丙"], ("E1",))[1],
    ]

    assert len(emitted) == 5
    for warning in emitted:
        for word in T252_FORBIDDEN:
            assert word not in warning, (word, warning)
    for fragment in ("第一稿声明过", "第二稿未再声明", "其间无新证据"):
        assert fragment in emitted[0], fragment


def test_t252_u14_randomized_truth_table_probe() -> None:
    """U14：300 例随机组合逐例对表——否决 ⟺ P1∧P2∧P3，且每例 warning 条数与行号一致。"""
    rng = random.Random(20260727)
    pool = ["回滚补偿细节", "延迟队列重放", "补偿任务的重放口径"]
    evidence_ids = ("E1", "E2")
    expected_count = {1: 0, 2: 1, 3: 1, 4: 0, 5: 0, 6: 1, 7: 1}
    hits = dict.fromkeys(expected_count, 0)
    blocked_side = 0

    for _ in range(300):
        generate_calls = rng.choice([1, 2])
        first_gaps = tuple(rng.sample(pool, rng.randint(0, 3)))
        roll = rng.random()
        if roll < 0.3:
            second_gaps = list(first_gaps)  # 强制"逐字相同"（行 4/5）
        elif roll < 0.6:
            second_gaps = []  # 强制第二稿为空（行 6 候选）
        else:
            second_gaps = rng.sample(pool, rng.randint(0, 3))
        first = (
            None
            if rng.random() < 0.15
            else DraftSnapshot(
                not_found=first_gaps,
                evidence_ids=evidence_ids if rng.random() < 0.8 else ("E1",),
            )
        )

        blocked, warnings = regen_full_block(generate_calls, first, second_gaps, evidence_ids)

        if generate_calls != 2:
            row = 1
        elif first is None:
            row = 2
        elif first.evidence_ids != evidence_ids:
            row = 3
        elif first.not_found == tuple(second_gaps):
            row = 4 if not second_gaps else 5
        elif first.not_found and not second_gaps:
            row = 6
        else:
            row = 7
        hits[row] += 1
        blocked_side += blocked

        assert blocked is (row == 6)
        assert len(warnings) == expected_count[row] <= 1, (row, warnings)

    assert all(count > 0 for count in hits.values()), hits
    assert 0 < blocked_side < 300


async def test_t252_i2_second_draft_dropping_its_gap_cannot_reach_full() -> None:
    """I2（行 6）：场景 B——第二稿删掉自述缺口不得把终态升成 full。

    基线 be35c12 下本脚本产出 final_mode == "full"（探针实测），这正是 RT-08 的对偶缺陷。
    """
    retrievals: list[tuple[str, ...]] = []
    result = await run_agent(
        _runtime([PLAN, EVAL_OK, T252_GEN_GAP_BAD_L1, GENERATE], retrievals), _input()
    )

    assert result["generate_calls"] == 2
    assert result["final_mode"] == "partial"
    # 只否决 full 与发 warning：其余三项与基线逐字一致，缺口一条都不许回填
    assert result["final_answer"] == "库存扣减由事务保护 [E1]。"
    assert result["final_not_found"] == []
    assert result["final_not_found_details"] == ()
    assert result["warnings"] == ["verify:l0_l1_failed", T252_BLOCK_WARNING]
    answer = build_answer(result)
    assert any("本次未列出具体未覆盖方面" in item for item in answer["limitations"])
    for blob in (*answer["not_found"], *answer["limitations"], *answer["warnings"]):
        for word in T252_FORBIDDEN:
            assert word not in blob, (word, blob)


async def test_t252_i3_frozen_default_second_draft_still_evaluates_the_check() -> None:
    """I3（PG-14）：第二稿走冻结默认值时 mode 本就是 partial——只有 warning 能证明判据求值。"""
    retrievals: list[tuple[str, ...]] = []
    result = await run_agent(
        _runtime([PLAN, EVAL_OK, T252_GEN_GAP_BAD_L1, "{}", "{}"], retrievals), _input()
    )

    assert result["generate_failed"] is True and result["generate_calls"] == 2
    assert T252_BLOCK_WARNING in result["warnings"]
    # 与基线一致仍为 partial：本例不作为 full→partial 的证据（那由 I2 单独证明）
    assert result["final_mode"] == "partial"
    assert result["final_answer"] == "现有证据不足以可靠生成回答。"


async def test_t252_i4_budget_exhausted_run_matches_the_baseline_byte_for_byte() -> None:
    """I4（行 1）：未发生重生成时终态含 warnings 与基线 be35c12 逐字节一致。"""
    retrievals: list[tuple[str, ...]] = []
    result = await run_agent(
        _runtime(["{}", PLAN, "{}", EVAL_OK, "{}", BAD_GEN_L1], retrievals), _input()
    )

    assert result["generate_calls"] == 1
    assert result["final_mode"] == "refusal"
    assert result["final_answer"] == "现有资料不足以回答该问题。"
    assert result["final_not_found"] == []
    assert result["final_not_found_details"] == ()
    assert result["warnings"] == [
        "plan:invalid_structured_output",
        "evaluate:1:invalid_structured_output",
        "generate:1:invalid_structured_output",
        "verify:l0_l1_failed",
        "finalize: 已移除 1 个未通过验证的 claim，正文按保留 claim 重建",
    ]


async def test_t252_i6_check_reads_the_raw_second_draft_not_the_calibrated_list() -> None:
    """I6（PG-13）：判据必须读第二稿原始 draft.not_found，而不是 T23 校准后的清单。

    判别器是两条 warning 的**有/无组合**：误传校准结果时 P3 不成立、无否决 warning，
    full_candidate 仍为真而落定 full，再被既有 finalize_consistency 降级并留下降级 warning。
    只断 final_mode != "full" 在两种接线下都成立，证不到任何东西。
    """
    retrievals: list[tuple[str, ...]] = []
    result = await run_agent(
        _runtime([PLAN, T252_EVAL_MISSING, T252_GEN_GAP_BAD_L1, GENERATE], retrievals), _input()
    )

    draft = result["answer_draft"]
    assert draft is not None and draft.not_found == []  # 第二稿原始字段为空
    assert result["final_not_found"] == ["补偿路径"]  # T23 校准后非空
    assert T252_BLOCK_WARNING in result["warnings"]
    assert "finalize: full 终态存在未覆盖缺口，降级 partial" not in result["warnings"]


async def _t252_finalize_once(
    *,
    generate_calls: int,
    first_draft: DraftSnapshot | None,
    draft: GenerateOutput,
    evidences: list[Evidence],
) -> dict[str, Any]:
    """直接跑 finalize 节点观测终态四项。

    真值表行 3（两稿证据集不同）在当前拓扑不可达——两次 generate 之间没有 retrieve 边
    （graph.py:185-190），故只能手工构造状态；它作为结构性 fail-closed 保留。
    """
    state = initial_agent_state(_input())
    state["evidences"] = evidences
    state["evidence_path_history"] = [evidence.rel_path for evidence in evidences]
    state["evaluation"] = EvaluateOutput(
        sufficiency="sufficient", supported_aspects=["库存"], missing_aspects=[]
    )
    state["answer_draft"] = draft
    state["verification"] = verify_draft(draft, evidences)
    state["generate_calls"] = generate_calls
    state["first_draft"] = first_draft
    return await AgentNodes(_custom_runtime([], evidences), None).finalize(state)


@pytest.mark.parametrize("row", ["row3_evidence_set_differs", "row7_gaps_differ"])
async def test_t252_i7_non_blocking_rows_leave_the_final_state_untouched(row: str) -> None:
    """I7（PG-10）：行 3/7 的终态侧——四项与基线一致，warnings 恰好多一条约定 warning。"""
    if row == "row7_gaps_differ":
        retrievals: list[tuple[str, ...]] = []
        result = await run_agent(
            _runtime([PLAN, EVAL_OK, T252_GEN_GAP_BAD_L1, T252_GEN_TWO_GAPS], retrievals), _input()
        )

        assert result["generate_calls"] == 2
        # 四项与基线 be35c12 逐字一致（探针实测）
        assert result["final_mode"] == "partial"
        assert result["final_answer"] == "库存扣减由事务保护 [E1]。"
        assert result["final_not_found"] == ["回滚补偿细节", "延迟队列重放细节"]
        assert [detail.text for detail in result["final_not_found_details"]] == [
            "回滚补偿细节",
            "延迟队列重放细节",
        ]
        assert result["warnings"] == ["verify:l0_l1_failed", _t252_count_warning(1, 2)]
        return

    draft = GenerateOutput(
        answer_text="库存扣减由事务保护 [E1]。",
        claims=[
            ClaimOutput(
                text="库存扣减由事务保护",
                evidence_ids=["E1"],
                quotes=["库存扣减由事务保护。"],
            )
        ],
        not_found=["回滚补偿细节", "延迟队列重放细节"],
    )
    evidences = [_evidence()]
    # 对照组走真值表行 1（P1 不成立，由 U7 证明零新增 warning），即基线行为
    baseline = await _t252_finalize_once(
        generate_calls=1,
        first_draft=_t252_snapshot("回滚补偿细节"),
        draft=draft,
        evidences=evidences,
    )
    actual = await _t252_finalize_once(
        generate_calls=2,
        first_draft=_t252_snapshot("回滚补偿细节", evidence_ids=("E1", "E2")),
        draft=draft,
        evidences=evidences,
    )

    for field_name in ("final_mode", "final_answer", "final_not_found", "final_not_found_details"):
        assert actual[field_name] == baseline[field_name], field_name
    assert actual["warnings"] == [*baseline["warnings"], T252_EVIDENCE_DIFF_WARNING]
