"""T20.2 指标手算对齐（《Evaluation-v1》§4）。

每个断言值都来自微数据集手算（注释给出算式）；空集、多 relevant、
partial 期望、预算越界与失败 run 等边界逐项覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from devkb.evaluation import (
    aggregate_agentic,
    citation_hits_anchor,
    hit_rank,
    l0_final_errors,
    l1_final_errors,
    mrr_at,
    percentile,
    question_group,
    rank_improved,
    recall_at,
    top_overlap,
)


@dataclass(frozen=True)
class _Hit:
    rel_path: str
    title_path: str


# ---------------------------------------------------------------------------
# 检索指标
# ---------------------------------------------------------------------------


def test_hit_rank_multi_relevant_takes_first_matching_result() -> None:
    results = [
        _Hit("a.md", "总览 > 支付"),
        _Hit("b.md", "总览 > 库存 > 并发控制"),
        _Hit("a.md", "总览 > 回补策略"),
    ]
    relevant = [
        {"rel_path": "a.md", "anchor": "回补策略"},
        {"rel_path": "b.md", "anchor": "并发控制"},
    ]
    # 多 relevant：rank2 命中第二个 anchor，早于 rank3 命中第一个 anchor
    assert hit_rank(results, relevant) == 2
    # anchor casefold 包含匹配；rel_path 必须精确相等
    assert (
        hit_rank(
            [_Hit("a.md", "API > OrderService")], [{"rel_path": "a.md", "anchor": "orderservice"}]
        )
        == 1
    )
    assert (
        hit_rank([_Hit("aa.md", "总览 > 支付")], [{"rel_path": "a.md", "anchor": "支付"}]) is None
    )
    assert hit_rank([], relevant) is None


def test_recall_and_mrr_hand_computed() -> None:
    ranks: list[int | None] = [1, 3, 6, 11, None]
    # Recall@5 = 命中且 ≤5 的 2 个 / 5 题 = 0.4；Recall@10 = 3/5 = 0.6
    assert recall_at(ranks, 5) == pytest.approx(0.4)
    assert recall_at(ranks, 10) == pytest.approx(0.6)
    # MRR@10 = (1/1 + 1/3 + 1/6 + 0 + 0) / 5 = 1.5/5 = 0.3
    assert mrr_at(ranks, 10) == pytest.approx(0.3)
    # rank=11 超出截断，不进 MRR@10
    assert mrr_at([11], 10) == 0.0
    assert recall_at([None, None], 10) == 0.0


def test_recall_mrr_percentile_overlap_reject_empty_inputs() -> None:
    with pytest.raises(ValueError):
        recall_at([], 10)
    with pytest.raises(ValueError):
        mrr_at([], 10)
    with pytest.raises(ValueError):
        percentile([], 50)
    with pytest.raises(ValueError):
        top_overlap([], [], 10)


def test_top_overlap_hand_computed() -> None:
    # 截断到 k=3：{a,b,c} ∩ {b,c,d} = 2，分母 max(3,3)=3
    assert top_overlap(["a", "b", "c", "x"], ["b", "c", "d", "y"], 3) == pytest.approx(2 / 3)
    assert top_overlap(["a", "b"], ["a", "b"], 10) == 1.0
    assert top_overlap(["a"], ["b"], 10) == 0.0
    # 长度不等：{a,b,c} ∩ {a} = 1，分母 max(3,1)=3
    assert top_overlap(["a", "b", "c"], ["a"], 10) == pytest.approx(1 / 3)


def test_percentile_nearest_rank_hand_computed() -> None:
    values = [10.0, 20.0, 30.0, 40.0]
    # P50：ceil(0.5*4)=2 → 第 2 小 = 20；P95：ceil(0.95*4)=4 → 40
    assert percentile(values, 50) == 20.0
    assert percentile(values, 95) == 40.0
    assert percentile([7.0], 50) == 7.0


def test_question_group_mechanical_rule() -> None:
    assert question_group("订单状态机允许哪些状态流转？") == "natural-language"
    assert question_group("InventoryService 的扣减逻辑在哪？") == "token"  # ≥5 字符标识符
    assert question_group("报错 40901 是什么含义？") == "token"  # ≥3 位数字
    assert question_group("qty 是什么？") == "natural-language"  # 短标识符不算


def test_rank_improved_unhit_is_infinity() -> None:
    assert rank_improved(3, 5) is True
    assert rank_improved(3, None) is True
    assert rank_improved(None, 5) is False
    assert rank_improved(5, 5) is False  # 必须严格更优


# ---------------------------------------------------------------------------
# 回答与引用指标
# ---------------------------------------------------------------------------


def _answer(**overrides: Any) -> dict[str, Any]:
    answer: dict[str, Any] = {
        "answer_text": "库存由行锁保证 [E1]。",
        "citations": [
            {"evidence_id": "E1", "rel_path": "zh.md", "title_path": "总览 > 库存 > 并发控制"}
        ],
        "claims": [{"text": "库存由行锁保证", "evidence_ids": ["E1"], "quotes": ["数据库行锁"]}],
    }
    answer.update(overrides)
    return answer


def test_l0_final_errors_detects_unknown_marks_and_evidence() -> None:
    assert l0_final_errors(_answer()) == []
    assert l0_final_errors(_answer(answer_text="见 [E9]。")) == ["L0:answer_text:unknown_mark:E9"]
    bad_claim = [{"text": "x", "evidence_ids": ["E7"], "quotes": []}]
    assert l0_final_errors(_answer(claims=bad_claim)) == ["L0:claim[0]:unknown_evidence:E7"]


def test_l1_final_errors_verbatim_only_within_bound_evidence() -> None:
    contents = {"E1": "库存扣减使用数据库行锁（SELECT FOR UPDATE）保证并发安全"}
    assert l1_final_errors(_answer(), contents) == []
    # 篡改引文：任何绑定证据中都不存在
    tampered = _answer(claims=[{"text": "x", "evidence_ids": ["E1"], "quotes": ["使用乐观锁重试"]}])
    assert l1_final_errors(tampered, contents) == ["L1:claim[0]:quote[0]:no_verbatim_match"]
    # quote 出现在别的证据但未绑定 → 仍失败（只查绑定证据）
    elsewhere = _answer(
        claims=[{"text": "x", "evidence_ids": ["E1"], "quotes": ["补偿任务异步回补"]}]
    )
    other_contents = {**contents, "E2": "库存通过补偿任务异步回补"}
    assert l1_final_errors(elsewhere, other_contents) == ["L1:claim[0]:quote[0]:no_verbatim_match"]
    # 无 quotes 的 claim 不触发 L1
    assert (
        l1_final_errors(
            _answer(claims=[{"text": "x", "evidence_ids": ["E1"], "quotes": []}]), contents
        )
        == []
    )


def test_citation_hits_anchor_requires_path_and_anchor() -> None:
    citations = _answer()["citations"]
    assert citation_hits_anchor(citations, [{"rel_path": "zh.md", "anchor": "并发控制"}]) is True
    assert citation_hits_anchor(citations, [{"rel_path": "en.md", "anchor": "并发控制"}]) is False
    assert citation_hits_anchor([], [{"rel_path": "zh.md", "anchor": "并发控制"}]) is False


# ---------------------------------------------------------------------------
# §5.2 聚合
# ---------------------------------------------------------------------------


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "q",
        "answerable": True,
        "expected_mode": None,
        "status": "succeeded",
        "mode": "full",
        "l0_errors": [],
        "l1_errors": [],
        "citation_hit": True,
        "retrieval_rounds": 1,
        "llm_calls": 3,
        "llm_retries": 0,
        "tokens_in": 100,
        "tokens_out": 50,
        "latency_ms": 1000,
        "run_terminal": True,
        "within_budget": True,
    }
    row.update(overrides)
    return row


def test_aggregate_agentic_hand_computed_gates() -> None:
    rows = [
        _row(id="q1"),
        _row(id="q2", mode="refusal", citation_hit=False),  # 1 个误拒
        _row(id="q3", citation_hit=False),
        # u04 类边界题：期望 partial 且实际 partial → 计入正确
        _row(id="u1", answerable=False, expected_mode="partial", mode="partial", citation_hit=None),
        _row(id="u2", answerable=False, expected_mode="refusal", mode="refusal", citation_hit=None),
        # 期望 refusal 实际 full → 不正确
        _row(id="u3", answerable=False, expected_mode="refusal", mode="full", citation_hit=None),
    ]
    agg = aggregate_agentic(rows)
    gates = agg["gates"]
    assert gates["correct_unanswerable"] == 2  # u1(partial 对) + u2；u3 错
    assert gates["false_refusals"] == 1 and gates["false_refusal_gate"] is True
    # citation proxy 只对可答成功题：q1 命中 / q2 q3 未命中 = 1/3
    assert gates["citation_proxy"] == pytest.approx(1 / 3)
    assert gates["citation_proxy_gate"] is False
    assert gates["l0_pass"] is True and gates["l1_pass"] is True
    assert gates["gate_passed"] is False  # citation gate 未过
    assert agg["mode_counts"] == {"full": 3, "partial": 1, "refusal": 2}
    # tokens 求和 = 6×100 / 6×50
    assert agg["total_tokens_in"] == 600 and agg["total_tokens_out"] == 300


def test_aggregate_agentic_budget_violation_and_failed_run() -> None:
    rows = [
        _row(id="q1", retrieval_rounds=3, llm_calls=7, within_budget=False),
        {
            "id": "q2",
            "answerable": True,
            "expected_mode": None,
            "status": "failed",
            "error": "LLMError: boom",
            "run_terminal": True,
        },
    ]
    agg = aggregate_agentic(rows)
    assert agg["succeeded"] == 1 and agg["failed"] == 1
    assert agg["gates"]["within_budget"] is False
    assert agg["gates"]["terminal_complete"] is True
    assert agg["gates"]["gate_passed"] is False
    # 失败 run 无终态落库 → 终态完整率破
    rows[1]["run_terminal"] = False
    assert aggregate_agentic(rows)["gates"]["terminal_complete"] is False


def test_aggregate_agentic_l0_l1_failures_break_gate() -> None:
    rows = [_row(l0_errors=["L0:claim[0]:unknown_evidence:E9"])]
    agg = aggregate_agentic(rows)
    assert agg["gates"]["l0_pass"] is False and agg["gates"]["gate_passed"] is False
    rows = [_row(l1_errors=["L1:claim[0]:quote[0]:no_verbatim_match"])]
    assert aggregate_agentic(rows)["gates"]["l1_pass"] is False


def test_aggregate_agentic_empty_rows_never_pass() -> None:
    agg = aggregate_agentic([])
    assert agg["question_count"] == 0
    assert agg["gates"]["gate_passed"] is False
    assert agg["latency_ms_p50"] is None and agg["gates"]["citation_proxy"] is None
