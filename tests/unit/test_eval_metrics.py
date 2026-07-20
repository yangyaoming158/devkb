"""T20.2 指标手算对齐（《Evaluation-v1》§4）。

每个断言值都来自微数据集手算（注释给出算式）；空集、多 relevant、
partial 期望、预算越界与失败 run 等边界逐项覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from devkb.evaluation import (
    P0_HOLDOUT_BASELINE,
    aggregate_agentic,
    citation_hits_anchor,
    hit_rank,
    l0_final_errors,
    l1_final_errors,
    mrr_at,
    p0_baseline_comparison,
    percentile,
    question_group,
    rank_improved,
    recall_at,
    retrieval_gates,
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
    agg = aggregate_agentic(rows, split="dev")
    gates = agg["gates"]
    assert gates["correct_unanswerable"] == 2  # u1(partial 对) + u2；u3 错
    assert gates["correct_unanswerable_gate"] is False  # 2 < 3
    assert gates["false_refusals"] == 1 and gates["false_refusal_gate"] is True
    # citation proxy 只对可答成功题：q1 命中 / q2 q3 未命中 = 1/3；
    # 2026-07-19 裁决后 proxy 为记录义务：低于冻结阈值如实记录但不破硬 Gate
    assert gates["citation_proxy"] == pytest.approx(1 / 3)
    assert gates["citation_proxy_meets_frozen_threshold"] is False
    assert gates["l0_pass"] is True and gates["l1_pass"] is True
    # 拒答 Gate（2<3）未过 → 总判定 False；proxy 低于阈值本身已不参与硬判定
    assert gates["gate_passed"] is False
    assert agg["mode_counts"] == {"full": 3, "partial": 1, "refusal": 2}
    # tokens 求和 = 6×100 / 6×50
    assert agg["total_tokens_in"] == 600 and agg["total_tokens_out"] == 300


def test_citation_proxy_below_threshold_recorded_but_not_hard_gate() -> None:
    """2026-07-19 裁决：proxy 为记录义务——其余六项全过时低 proxy 不破 Gate。"""
    rows = [
        _row(id="q1", citation_hit=False),
        _row(id="u1", answerable=False, expected_mode="refusal", mode="refusal", citation_hit=None),
        _row(id="u2", answerable=False, expected_mode="refusal", mode="refusal", citation_hit=None),
        _row(id="u3", answerable=False, expected_mode="partial", mode="partial", citation_hit=None),
    ]
    gates = aggregate_agentic(rows, split="dev")["gates"]
    assert gates["citation_proxy"] == 0.0
    assert gates["citation_proxy_meets_frozen_threshold"] is False
    assert gates["gate_passed"] is True


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
    agg = aggregate_agentic(rows, split="dev")
    assert agg["succeeded"] == 1 and agg["failed"] == 1
    assert agg["gates"]["within_budget"] is False
    assert agg["gates"]["terminal_complete"] is True
    assert agg["gates"]["no_failed_runs"] is False
    assert agg["gates"]["gate_passed"] is False
    # 失败 run 无终态落库 → 终态完整率破
    rows[1]["run_terminal"] = False
    assert aggregate_agentic(rows, split="dev")["gates"]["terminal_complete"] is False


def test_aggregate_agentic_failed_run_breaks_gate_even_if_rest_pass() -> None:
    """失败 run 不进 L0/L1/预算统计——"无失败 run"硬项必须兜底，防止静默通过。"""
    rows = [
        _row(id="q1"),
        {
            "id": "q2",
            "answerable": True,
            "expected_mode": None,
            "status": "failed",
            "error": "LLMError: boom",
            "run_terminal": True,  # 终态已落库：其余硬项全部为通过形态
        },
        _row(id="u1", answerable=False, expected_mode="refusal", mode="refusal", citation_hit=None),
        _row(id="u2", answerable=False, expected_mode="refusal", mode="refusal", citation_hit=None),
        _row(id="u3", answerable=False, expected_mode="partial", mode="partial", citation_hit=None),
    ]
    gates = aggregate_agentic(rows, split="dev")["gates"]
    # 拒答 3/3、误拒 0、L0/L1 无错、预算/终态齐——只有失败 run 兜底能拦住
    assert gates["correct_unanswerable_gate"] is True
    assert gates["false_refusal_gate"] is True
    assert gates["l0_pass"] is True and gates["l1_pass"] is True
    assert gates["within_budget"] is True and gates["terminal_complete"] is True
    assert gates["no_failed_runs"] is False
    assert gates["gate_passed"] is False
    # holdout 口径同样必须被失败 run 拦住
    assert aggregate_agentic(rows, split="holdout")["gates"]["gate_passed"] is False


def test_aggregate_agentic_holdout_hard_gates_per_section7() -> None:
    """§7：holdout 硬 Gate 仅 L0/L1/预算/终态/无失败 run；拒答/误拒只记录不设阈值。"""
    rows = [
        _row(id="q1", mode="refusal", citation_hit=False),  # holdout 误拒只记录
        _row(id="u1", answerable=False, expected_mode="refusal", mode="full", citation_hit=None),
        _row(id="u2", answerable=False, expected_mode="refusal", mode="refusal", citation_hit=None),
    ]
    gates = aggregate_agentic(rows, split="holdout")["gates"]
    assert gates["correct_unanswerable"] == 1 and gates["correct_unanswerable_gate"] is None
    assert gates["false_refusals"] == 1 and gates["false_refusal_gate"] is None
    assert gates["gate_passed"] is True  # L0/L1/预算/终态/无失败 run 全过
    # 同一数据在 dev 口径：拒答 1<3 破 Gate（防 split 混用回归）
    assert aggregate_agentic(rows, split="dev")["gates"]["gate_passed"] is False
    # holdout 只有 2 个不可答题：完美 2/2 不得再被 dev 的 >=3 阈值误杀
    perfect = [
        _row(id="u1", answerable=False, expected_mode="refusal", mode="refusal", citation_hit=None),
        _row(id="u2", answerable=False, expected_mode="partial", mode="partial", citation_hit=None),
    ]
    holdout_gates = aggregate_agentic(perfect, split="holdout")["gates"]
    assert holdout_gates["correct_unanswerable"] == 2
    assert holdout_gates["gate_passed"] is True


def test_aggregate_agentic_l0_l1_failures_break_gate() -> None:
    rows = [_row(l0_errors=["L0:claim[0]:unknown_evidence:E9"])]
    agg = aggregate_agentic(rows, split="dev")
    assert agg["gates"]["l0_pass"] is False and agg["gates"]["gate_passed"] is False
    rows = [_row(l1_errors=["L1:claim[0]:quote[0]:no_verbatim_match"])]
    assert aggregate_agentic(rows, split="dev")["gates"]["l1_pass"] is False
    # L0/L1 在 holdout 也是硬 Gate（§7）
    assert aggregate_agentic(rows, split="holdout")["gates"]["gate_passed"] is False


def test_aggregate_agentic_empty_rows_never_pass() -> None:
    agg = aggregate_agentic([], split="dev")
    assert agg["question_count"] == 0
    assert agg["gates"]["gate_passed"] is False
    assert agg["latency_ms_p50"] is None and agg["gates"]["citation_proxy"] is None
    assert aggregate_agentic([], split="holdout")["gates"]["gate_passed"] is False


# ---------------------------------------------------------------------------
# 检索 Gate（split-aware）
# ---------------------------------------------------------------------------


def _retrieval_payload(
    metrics: dict[str, Any],
    *,
    overlap: dict[str, Any] | None = None,
    questions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {"metrics": metrics, "overlap_exact_hnsw": overlap, "questions": questions or []}


def test_retrieval_gates_dev_overlap_is_the_only_hard_item() -> None:
    metrics = {
        "vector-exact": {"recall_at_10": 0.6, "mrr_at_10": 0.5},
        "vector-hnsw": {"recall_at_10": 0.6, "mrr_at_10": 0.5},
        "hybrid-rrf": {"recall_at_10": 0.7, "mrr_at_10": 0.45},
    }
    questions = [{"group": "token", "ranks": {"hybrid-rrf": 2, "vector-exact": 5}}]
    payload = _retrieval_payload(
        metrics, overlap={"per_question": [1.0], "mean": 0.95}, questions=questions
    )
    gates = retrieval_gates(payload, split="dev")
    # 记录义务：0.7-0.6 / 0.45-0.5；token 题 2<5 改善；overlap 0.95>=0.95 硬项过
    assert gates["record_recall_delta"] == pytest.approx(0.1)
    assert gates["record_mrr_delta"] == pytest.approx(-0.05)
    assert gates["record_token_question_improved"] is True
    assert gates["hnsw_overlap_gate"] is True and gates["gate_passed"] is True
    # hybrid 大幅退化也不破 dev 硬 Gate（T15.4 裁决：第 1–3 条仅记录）
    worse = {**metrics, "hybrid-rrf": {"recall_at_10": 0.1, "mrr_at_10": 0.1}}
    gates = retrieval_gates(
        _retrieval_payload(worse, overlap={"per_question": [0.94], "mean": 0.94}), split="dev"
    )
    assert gates["record_recall_delta"] == pytest.approx(-0.5)
    assert gates["hnsw_overlap_gate"] is False and gates["gate_passed"] is False  # overlap<0.95


def test_retrieval_gates_holdout_requires_hnsw_recall_not_below_exact() -> None:
    """§7 修订：holdout 硬 Gate = vector-hnsw R@10 ≥ vector-exact；overlap 仅记录。"""
    # 复评探针场景：exact 全中、hnsw 全丢——overlap 再高也必须判不过
    metrics = {
        "vector-exact": {"recall_at_10": 1.0, "mrr_at_10": 1.0},
        "vector-hnsw": {"recall_at_10": 0.0, "mrr_at_10": 0.0},
    }
    gates = retrieval_gates(
        _retrieval_payload(metrics, overlap={"per_question": [1.0], "mean": 1.0}), split="holdout"
    )
    assert gates["hnsw_recall_at_10"] == 0.0 and gates["exact_recall_at_10"] == 1.0
    assert gates["hnsw_recall_ge_exact"] is False and gates["gate_passed"] is False
    assert "hnsw_overlap_gate" not in gates and gates["hnsw_overlap_mean"] == 1.0
    # 相等即满足"不低于"；overlap 低也只记录不判定
    equal = {
        "vector-exact": {"recall_at_10": 0.875, "mrr_at_10": 0.6},
        "vector-hnsw": {"recall_at_10": 0.875, "mrr_at_10": 0.6},
    }
    gates = retrieval_gates(
        _retrieval_payload(equal, overlap={"per_question": [0.5], "mean": 0.5}), split="holdout"
    )
    assert gates["hnsw_recall_ge_exact"] is True and gates["gate_passed"] is True


def test_p0_baseline_frozen_values_match_committed_p0_report() -> None:
    """基线数字冻结于 evalsets/reports/p0-holdout.md（commit 7fdf540），不得被静默改写。"""
    assert P0_HOLDOUT_BASELINE["corpus"]["document_count"] == 39
    assert P0_HOLDOUT_BASELINE["corpus"]["chunk_count"] == 1370
    assert P0_HOLDOUT_BASELINE["retrieval"]["recall_at_5"] == 0.625
    assert P0_HOLDOUT_BASELINE["retrieval"]["recall_at_10"] == 0.875
    assert P0_HOLDOUT_BASELINE["retrieval"]["mrr_at_10"] == 0.440
    assert P0_HOLDOUT_BASELINE["retrieval"]["mode"] == "vector-exact"


def test_p0_baseline_comparison_hand_computed_deltas_and_scale_flag() -> None:
    """§7 第 3 条：同口径差值 + 语料规模变化声明，且不产生任何 Gate 判定。"""
    retrieval = _retrieval_payload(
        {"vector-exact": {"recall_at_5": 0.500, "recall_at_10": 0.625, "mrr_at_10": 0.340}}
    )
    corpus = {"document_count": 445, "chunk_count": 5000}
    comparison = p0_baseline_comparison(retrieval, corpus)
    # 手算：0.500-0.625=-0.125；0.625-0.875=-0.250；0.340-0.440=-0.100
    assert comparison["delta_vs_p0"]["recall_at_5"] == pytest.approx(-0.125)
    assert comparison["delta_vs_p0"]["recall_at_10"] == pytest.approx(-0.250)
    assert comparison["delta_vs_p0"]["mrr_at_10"] == pytest.approx(-0.100)
    assert comparison["corpus_scale"]["changed"] is True
    assert comparison["corpus_scale"]["p0"] == "39 文档 / 1370 chunks"
    assert comparison["corpus_scale"]["p1"] == "445 文档 / 5000 chunks"
    assert "不作 P1 硬 Gate" in comparison["note"]
    assert "gate_passed" not in comparison  # 历史对照永不产生 Gate 判定
    # 同规模语料（假想）：changed 为假
    same = p0_baseline_comparison(retrieval, {"document_count": 39, "chunk_count": 1370})
    assert same["corpus_scale"]["changed"] is False
    # 无 vector-exact（不该发生于 --mode all，防御口径）：值为 None 而非崩溃
    empty = p0_baseline_comparison(_retrieval_payload({"lexical": {}}), corpus)
    assert empty["current_vector_exact"] is None and empty["delta_vs_p0"] is None
    assert p0_baseline_comparison(None, corpus)["delta_vs_p0"] is None


def test_retrieval_gates_missing_modes_yield_none_verdict_with_stable_keys() -> None:
    """键集合固定：不可计算处为 None，不判定硬 Gate（防 schema 漂移与误判）。"""
    dev = retrieval_gates(_retrieval_payload({"lexical": {"recall_at_10": 0.2}}), split="dev")
    assert dev["record_recall_delta"] is None and dev["hnsw_overlap_mean"] is None
    assert dev["hnsw_overlap_gate"] is None and dev["gate_passed"] is None
    holdout = retrieval_gates(
        _retrieval_payload({"lexical": {"recall_at_10": 0.2}}), split="holdout"
    )
    assert holdout["hnsw_recall_ge_exact"] is None and holdout["gate_passed"] is None
