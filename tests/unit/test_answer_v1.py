"""T17.1 Answer v1 组装：P0 字段保留 + 新字段 + JSON 可序列化。"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from devkb.agent.answer import build_answer
from devkb.agent.state import (
    MAX_CLAIM_EVIDENCE_IDS,
    AgentInput,
    AgentState,
    ClaimOutput,
    Evidence,
    VerificationOutput,
    initial_agent_state,
)
from devkb.retrieval import MAX_FINAL_TOP_K

P0_ANSWER_KEYS = {"answer_text", "citations", "warnings", "stats"}
V1_NEW_KEYS = {"run_id", "mode", "claims", "not_found", "limitations", "trace_summary"}


def _evidence(number: int) -> Evidence:
    return Evidence(
        evidence_id=f"E{number}",
        chunk_id=uuid.UUID(int=number),
        rel_path=f"src/OrderService.java:{number}",
        title_path="OrderService > deductStock",
        content=f"public void deductStock() {{ /* 段落 {number} */ }}",
        start_line=number * 10,
        end_line=number * 10 + 5,
        score=0.9 - number * 0.1,
    )


def _final_state() -> AgentState:
    state = initial_agent_state(
        AgentInput(run_id=uuid.uuid4(), project_id=uuid.uuid4(), question="库存如何扣减？")
    )
    state["evidences"] = [_evidence(1), _evidence(2), _evidence(3)]
    state["retrieval_round"] = 1
    state["llm_calls"] = 3
    state["llm_retries"] = 1
    state["tokens_in"] = 300
    state["tokens_out"] = 150
    state["cost"] = Decimal("0.001")
    state["latency_ms"] = 1234
    state["model"] = "deepseek-chat"
    state["generate_calls"] = 1
    state["node_history"] = ["plan", "retrieve", "evaluate", "generate", "verify", "finalize"]
    state["final_answer"] = "库存扣减在 deductStock() 中实现 [E1]。"
    state["final_mode"] = "full"
    state["final_claims"] = [
        ClaimOutput(
            text="库存扣减在 deductStock() 中实现",
            evidence_ids=["E1", "E2"],
            quotes=["public void deductStock()"],
        )
    ]
    return state


def test_answer_v1_keeps_p0_fields_and_adds_new_fields_json_serializable() -> None:
    state = _final_state()
    answer = build_answer(state)

    assert set(answer) >= P0_ANSWER_KEYS | V1_NEW_KEYS
    assert answer["mode"] == "full"
    assert answer["run_id"] == str(state["run_id"])
    # 引用来自 claims 绑定 ∪ 文本 [E#]，按编号升序且带 P0 citation 全部字段
    assert [c["evidence_id"] for c in answer["citations"]] == ["E1", "E2"]
    assert set(answer["citations"][0]) == {
        "evidence_id",
        "chunk_id",
        "rel_path",
        "start_line",
        "end_line",
        "title_path",
        "score",
    }
    assert answer["stats"]["model"] == "deepseek-chat"
    assert answer["stats"]["tokens_in"] == 300 and answer["stats"]["llm_calls"] == 3
    # 英文术语与代码原文原样保留
    assert "deductStock()" in answer["answer_text"]
    assert answer["claims"][0]["quotes"] == ["public void deductStock()"]
    # 纯 JSON（无 UUID/Decimal 泄漏）
    assert json.loads(json.dumps(answer, ensure_ascii=False)) == answer


def test_answer_v1_partial_and_refusal_shapes() -> None:
    state = _final_state()
    state["final_mode"] = "partial"
    state["final_not_found"] = ["回滚补偿策略"]
    partial = build_answer(state)
    assert partial["mode"] == "partial"
    assert partial["not_found"] == ["回滚补偿策略"]
    assert any("not_found" in item for item in partial["limitations"])

    refusal_state = initial_agent_state(
        AgentInput(run_id=uuid.uuid4(), project_id=uuid.uuid4(), question="库存如何扣减？")
    )
    refusal_state["final_answer"] = "现有资料不足以回答该问题。缺少：回滚补偿策略。"
    refusal_state["final_mode"] = "refusal"
    refusal_state["final_not_found"] = ["回滚补偿策略"]
    refusal = build_answer(refusal_state)
    assert refusal["mode"] == "refusal"
    assert refusal["citations"] == [] and refusal["claims"] == []
    assert any("未生成实质回答" in item for item in refusal["limitations"])


def test_answer_v1_ignores_unknown_marks_and_reports_removed_claims() -> None:
    state = _final_state()
    state["final_answer"] = "答案 [E1] 与越界 [E9]。"
    state["verification"] = VerificationOutput(
        passed=False,
        l0_passed=False,
        l1_passed=True,
        errors=["L0:claim[1]:unknown_evidence:E9"],
        failed_claims=[1],
    )
    answer = build_answer(state)
    assert [c["evidence_id"] for c in answer["citations"]] == ["E1", "E2"]
    assert any("已移除 1 个" in item for item in answer["limitations"])
    assert "节点 plan>retrieve" in answer["trace_summary"]


def test_claim_schema_bounds_align_with_retrieval_cap() -> None:
    assert MAX_CLAIM_EVIDENCE_IDS == MAX_FINAL_TOP_K
    with pytest.raises(ValidationError):
        ClaimOutput(text="无证据断言", evidence_ids=[], quotes=[])
    claim = ClaimOutput(text="t", evidence_ids=["E1", "E1", "E2"], quotes=[])
    assert claim.evidence_ids == ["E1", "E2"]
