"""T31.1 契约 harness 纯函数层（《Evaluation-v1.5》§4/§5.1；packet 冻结测试矩阵）。

全部离线：不连库、不调模型、不读 evalsets 真实文件（除显式标注的 U9 校验用例）。
命名对应 packet「冻结测试」表的 U* / U-B* 行，逐行可回溯。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from devkb.errors import InvalidInputError
from devkb.eval_contract import (
    ANAPHORA_TOKENS,
    CONTRACT_REPORT_SCHEMA_VERSION,
    GATE_KEYS,
    NEGATION_PHRASES,
    P1_DEV_RETRIEVAL_BASELINE,
    SECTION_KEYS,
    aggregate_contract,
    assertion_surface,
    evidence_paths_from_trace,
    load_contract_questions,
    match_required_path,
    parse_expected_modes,
    parse_symbol_line_ranges,
    render_contract_markdown,
    resolve_required_type,
    score_consistency,
    score_coverage_monotonicity,
    score_evidence_selection,
    score_known_path_absence,
    score_not_found,
    score_policy,
    score_retrieval_reference,
    score_warning_ledger,
)

COMMIT = "b273d39e" * 5  # 40 hex

# --------------------------------------------------------------------------
# 构造器
# --------------------------------------------------------------------------


def _answer(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "answer_text": "已依据证据说明。[E1]",
        "citations": [],
        "warnings": [],
        "resolved_warnings": [],
        "warning_details": [],
        "stats": {"tokens_in": 1, "tokens_out": 1, "latency_ms": 1, "model": "m"},
        "run_id": "00000000-0000-0000-0000-000000000001",
        "mode": "partial",
        "claims": [],
        "not_found": [],
        "not_found_details": [],
        "limitations": ["仅回答了现有证据支持的部分"],
        "trace_summary": "",
    }
    base.update(overrides)
    return base


def _citation(rel_path: str, start: int = 1, end: int = 10, eid: str = "E1") -> dict[str, Any]:
    return {
        "evidence_id": eid,
        "chunk_id": "00000000-0000-0000-0000-0000000000aa",
        "rel_path": rel_path,
        "start_line": start,
        "end_line": end,
        "title_path": "",
        "score": 0.5,
    }


def _detail(text: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "text": text,
        "category": "missing_from_current_evidence",
        "source": "generate_draft",
        "basis": "no_conflict_found",
        "refs": [],
        "original_text": None,
    }
    base.update(overrides)
    return base


def _trace(steps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"run": {"status": "succeeded", "usage": {}, "cost": None}, "steps": steps or []}


def _retrieve_step(rel_paths: list[str], *, eliminated: list[dict[str, Any]] | None = None) -> dict:
    summary: dict[str, Any] = {
        "retrieval_round": 1,
        "rel_paths": rel_paths,
        "evidences": [
            {"evidence_id": f"E{i}", "chunk_id": "x", "rel_path": p, "start_line": 1, "end_line": 2}
            for i, p in enumerate(rel_paths, start=1)
        ],
    }
    if eliminated:
        summary["eliminated"] = eliminated
    return {"node": "retrieve", "attempt": 1, "output_summary": summary, "tools": [{"n": 1}]}


def _row(qid: str = "c01", **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": qid,
        "kind": "contract",
        "question": "q",
        "status": "succeeded",
        "mode": "partial",
        "expected_modes": ("partial",),
        "mode_matches": True,
        "evidence_selection": {
            "required_total": 0,
            "items": [],
            "cited_count": 0,
            "retrieved_not_cited": [],
            "span_decidable": 0,
            "span_hit": 0,
            "span_undecided": 0,
            "forbidden_types_cited": [],
            "forbidden_substitute_violation": False,
            "evidence_backed_aspect": False,
        },
        "claim_support": {"l0_errors": [], "l1_errors": []},
        "not_found": {
            "entries_total": 0,
            "decidable_total": 0,
            "decidable_ok": 0,
            "decidable_bad": 0,
            "undecided": 0,
            "violations": [],
            "proposition_level_undecided": 0,
        },
        "known_path_absence": {"violations": [], "undecided": [], "decidable_segments": 1},
        "consistency": {"rules": {}, "all_true": True, "proposition_level_undecided": True},
        "coverage": {"authoritative_matrix_available": False, "eliminated": [], "candidates": []},
        # 三个具名子集在真实 dev 集上恒非空（c13/e07/e02/e03、e06/c12、c03/e05），
        # 故基线夹具带上它们；空子集时 Gate 取 None 是刻意的 fail-closed（U3 锁定）
        "policy": {"question_id": "e02", "kind": "benign", "all_ok": True, "checks": {}},
        "warning_ledger": {
            "partition_ok": True,
            "resolution_ok": True,
            "total": 0,
            "attributed_count": 0,
            "verify_runs": 0,
            "case_2_8_recurrences": 0,
        },
        "global_negation": {"ok": True, "disclosure_present": True},
        "uningested": {"ok": True, "disclosure_present": True},
        "retrieval_rounds": 1,
        "llm_calls": 4,
        "run_terminal": True,
    }
    base.update(overrides)
    return base


def _retrieval_ok() -> dict[str, Any]:
    return {
        "measured": True,
        "comparable": True,
        "source_report": "r.json",
        "source_commit": COMMIT,
        "source_corpus_sha256": P1_DEV_RETRIEVAL_BASELINE["corpus_sha256"],
        "same_corpus": True,
        "current_recall_at_10": 0.6,
        "baseline_recall_at_10": P1_DEV_RETRIEVAL_BASELINE["recall_at_10"],
        "no_regression": True,
        "reason": None,
    }


def _agg(rows: list[dict[str, Any]] | None = None, **kw: Any) -> dict[str, Any]:
    return aggregate_contract(
        rows if rows is not None else [_row()],
        kw.pop(
            "probes", [{"id": "e08", "cross_project_isolation": True, "counts_unchanged": True}]
        ),
        kw.pop("retrieval", _retrieval_ok()),
        split=kw.pop("split", "dev"),
    )


# --------------------------------------------------------------------------
# U1–U9 正常 / 边界 / 失败
# --------------------------------------------------------------------------


def test_u1_all_green_gives_twelve_true_gate_keys_and_no_cross_section_score() -> None:
    report = _agg([_row("c01"), _row("e01", kind="extended")])
    assert set(report["gate_summary"]) == set(GATE_KEYS)
    assert len(GATE_KEYS) == 12
    assert all(v is True for v in report["gate_summary"].values()), report["gate_summary"]
    assert report["all_hard_gates_passed"] is True
    assert set(report["sections"]) == set(SECTION_KEYS)
    # 无任何跨节总分/平均分（M3 变形锁）
    forbidden = {"score", "total_score", "overall", "average", "mean_score"}
    assert forbidden.isdisjoint(set(report) | set(report["sections"]))


@pytest.mark.parametrize(
    ("raw", "actual", "expected"),
    [
        ("partial_or_full", "full", True),
        ("partial_or_full", "partial", True),
        ("partial_or_full", "refusal", False),
        ("policy_refusal", "policy_refusal", True),
    ],
)
def test_u2_disjunctive_expected_mode(raw: str, actual: str, expected: bool) -> None:
    assert (actual in parse_expected_modes(raw, question_id="c01")) is expected


def test_u3_empty_dataset_yields_none_ratios_and_fails_total_gate() -> None:
    report = _agg([])
    assert report["sections"]["evidence_selection"]["required_hit_rate"] is None
    assert report["sections"]["final_consistency"]["not_found_decidable_rate"] is None
    assert report["sections"]["final_consistency"]["not_found_undecided"] == 0
    assert report["gate_summary"]["contract_expectations_met"] is None
    assert report["all_hard_gates_passed"] is False


@pytest.mark.parametrize(("known", "truncated"), [(False, False), (True, True)])
def test_u4_unusable_corpus_snapshot_makes_every_index_judgement_undecided(
    known: bool, truncated: bool
) -> None:
    result = score_not_found(
        {"id": "c01"},
        _answer(
            not_found=["前端路由配置"],
            not_found_details=[_detail("前端路由配置", category="unsupported_or_not_ingested")],
        ),
        evidence_paths=(),
        indexed_paths=("a.md",),
        corpus_known=known,
        corpus_truncated=truncated,
    )
    assert result["undecided"] == 1 and result["decidable_total"] == 0
    assert result["violations"] == []


def test_u5_not_found_pairing_mismatch_is_a_rule_failure_not_an_exception() -> None:
    answer = _answer(not_found=["a", "b"], not_found_details=[_detail("a")])
    result = score_not_found(
        {"id": "c01"},
        answer,
        evidence_paths=(),
        indexed_paths=(),
        corpus_known=True,
        corpus_truncated=False,
    )
    assert result["rules"]["r1_pairing"] is False


def test_u6_missing_retrieval_report_is_none_and_fails_total_gate() -> None:
    report = _agg(retrieval=score_retrieval_reference(None, devkb_commit=COMMIT))
    assert report["sections"]["retrieval"]["measured"] is False
    assert report["gate_summary"]["retrieval_no_regression"] is None
    assert report["all_hard_gates_passed"] is False


def test_u7a_absent_retrieval_report_file_raises(tmp_path: Path) -> None:
    with pytest.raises(InvalidInputError):
        score_retrieval_reference(tmp_path / "nope.json", devkb_commit=COMMIT)


def test_u7b_non_v1_schema_retrieval_report_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"schema_version": "p1.5-contract-v1"}), encoding="utf-8")
    with pytest.raises(InvalidInputError):
        score_retrieval_reference(path, devkb_commit=COMMIT)


def test_u8_aggregation_is_byte_stable_across_repeated_calls() -> None:
    rows = [_row("c01"), _row("c02", mode="full")]
    first = json.dumps(_agg(rows), ensure_ascii=False, sort_keys=False)
    second = json.dumps(_agg(rows), ensure_ascii=False, sort_keys=False)
    assert first == second


def test_u9a_unknown_expected_mode_raises_and_names_the_question() -> None:
    with pytest.raises(InvalidInputError, match="cXX"):
        parse_expected_modes("mostly_full", question_id="cXX")


def test_u9b_unknown_required_type_raises_and_names_the_question() -> None:
    assert resolve_required_type("progress", question_id="c11") == "current_doc"
    assert resolve_required_type("production_source", question_id="c01") == "production_source"
    with pytest.raises(InvalidInputError, match="cYY"):
        resolve_required_type("blueprint", question_id="cYY")


# --------------------------------------------------------------------------
# U10 端到端接线矩阵（12 键 × {坏源值, 不可测量}）
# --------------------------------------------------------------------------

_BAD: dict[str, tuple[str, dict[str, Any]]] = {
    "contract_expectations_met": ("final_consistency", {"mode": "refusal", "mode_matches": False}),
    "extended_expectations_met": (
        "final_consistency",
        {"id": "e01", "kind": "extended", "mode_matches": False},
    ),
    "zero_absence_assertion_on_known_paths": (
        "final_consistency",
        {
            "known_path_absence": {
                "violations": [{"path": "a.md"}],
                "undecided": [],
                "decidable_segments": 1,
            }
        },
    ),
    "zero_full_without_required_evidence": (
        "evidence_selection",
        {
            "mode": "full",
            "expected_modes": ("full",),
            "evidence_selection": {
                **_row()["evidence_selection"],
                "required_total": 1,
                "cited_count": 0,
                "items": [{"path": "x.java", "status": "never_retrieved", "span_hit": None}],
            },
        },
    ),
    "consistency_all_true": (
        "final_consistency",
        {
            "consistency": {
                "rules": {"r2": False},
                "all_true": False,
                "proposition_level_undecided": True,
            }
        },
    ),
    "zero_full_refusal_with_direct_evidence": (
        "final_consistency",
        {
            "mode": "refusal",
            "expected_modes": ("refusal",),
            "evidence_selection": {**_row()["evidence_selection"], "evidence_backed_aspect": True},
        },
    ),
    "policy_terminal_correct": (
        "safety_reliability",
        {
            "id": "c13",
            "policy": {
                "question_id": "c13",
                "kind": "attack",
                "all_ok": False,
                "checks": {"mode_ok": False},
            },
        },
    ),
    "global_negation_honest": (
        "final_consistency",
        {"id": "e06", "global_negation": {"ok": False}},
    ),
    "uningested_disclosed": ("final_consistency", {"id": "e05", "uningested": {"ok": False}}),
    "budget_and_terminal": ("safety_reliability", {"retrieval_rounds": 3}),
    "l0_l1_all_pass": ("claim_support", {"claim_support": {"l0_errors": ["bad"], "l1_errors": []}}),
}


@pytest.mark.parametrize("key", sorted(_BAD))
def test_u10_bad_source_value_reaches_its_named_gate_and_leaves_the_others_intact(key: str) -> None:
    """源可观察量 → section 字段 → 具名 Gate → 总判定，四层逐一断言。"""
    section, overrides = _BAD[key]
    baseline = _agg([_row("c01"), _row("e01", kind="extended", id="e01")])
    assert baseline["all_hard_gates_passed"] is True

    rows = [_row("c01"), _row("e01", kind="extended", id="e01")]
    target = 1 if overrides.get("kind") == "extended" or overrides.get("id") == "e01" else 0
    rows[target] = _row(**{**rows[target], **overrides})
    report = _agg(rows)

    assert report["gate_summary"][key] is False, f"{key} 未从源可观察量传导到具名 Gate"
    assert report["sections"][section]["gate"] is False, f"{key} 未落到 {section} 的 section gate"
    assert report["all_hard_gates_passed"] is False
    others = {k: v for k, v in report["gate_summary"].items() if k != key}
    assert others == {k: v for k, v in baseline["gate_summary"].items() if k != key}


def test_u10_retrieval_gate_is_wired_from_its_section() -> None:
    report = _agg(retrieval={**_retrieval_ok(), "no_regression": False, "comparable": True})
    assert report["gate_summary"]["retrieval_no_regression"] is False
    assert report["sections"]["retrieval"]["gate"] is False
    assert report["all_hard_gates_passed"] is False


@pytest.mark.parametrize("key", sorted(GATE_KEYS))
def test_u10_unmeasurable_gate_is_none_and_never_passes(key: str) -> None:
    """M2 变形锁：`all(v is not False …)` 会让这 12 组全部转绿。"""
    report = _agg()
    report["gate_summary"][key] = None
    recomputed = all(v is True for v in report["gate_summary"].values())
    assert recomputed is False


def test_u11_decidable_and_undecided_counts_are_conserved() -> None:
    result = score_not_found(
        {"id": "c01"},
        _answer(
            not_found=["a", "b", "c"],
            not_found_details=[
                _detail("a"),
                _detail("b", category="unsupported_or_not_ingested", refs=["x.vue"]),
                _detail("c", category="confirmed_undocumented"),
            ],
        ),
        evidence_paths=(),
        indexed_paths=("y.md",),
        corpus_known=True,
        corpus_truncated=False,
    )
    assert result["decidable_ok"] + result["decidable_bad"] == result["decidable_total"]
    assert result["decidable_total"] + result["undecided"] == result["entries_total"] == 3


def test_u12_section_and_gate_key_sets_are_locked_verbatim() -> None:
    assert SECTION_KEYS == (
        "retrieval",
        "evidence_selection",
        "claim_support",
        "final_consistency",
        "safety_reliability",
    )
    assert GATE_KEYS == (
        "contract_expectations_met",
        "extended_expectations_met",
        "zero_absence_assertion_on_known_paths",
        "zero_full_without_required_evidence",
        "consistency_all_true",
        "zero_full_refusal_with_direct_evidence",
        "policy_terminal_correct",
        "global_negation_honest",
        "uningested_disclosed",
        "retrieval_no_regression",
        "budget_and_terminal",
        "l0_l1_all_pass",
    )


_BANNED = (
    "召回率",
    "准确率",
    "归属正确率",
    "方法级命中率",
    "已证明",
    "无风险",
    "已验证正确",
    "全部正确",
    "覆盖完整",
    "仓库无源码",
)
_REQUIRED_PHRASES = ("具名逐题", "可判定项", "点名行段命中", "未判定")


def _walk_strings(node: Any) -> list[str]:
    if isinstance(node, dict):
        out: list[str] = []
        for key, value in node.items():
            out.append(str(key))
            out.extend(_walk_strings(value))
        return out
    if isinstance(node, list | tuple):
        return [s for item in node for s in _walk_strings(item)]
    return [node] if isinstance(node, str) else []


def test_u13_banned_wording_absent_from_keys_values_and_markdown() -> None:
    report = _agg([_row("c01"), _row("e01", kind="extended", id="e01")])
    markdown = render_contract_markdown(report)
    haystack = "\n".join([*_walk_strings(report), markdown])
    for word in _BANNED:
        assert word not in haystack, f"禁用措辞 {word} 出现在报告或 Markdown"
    for phrase in _REQUIRED_PHRASES:
        assert phrase in haystack, f"替代表述 {phrase} 缺失（把词删光同样不合格）"


# --------------------------------------------------------------------------
# B1 证据选择三态
# --------------------------------------------------------------------------

_C01 = {
    "id": "c01",
    "required_evidence": [
        {
            "type": "production_source",
            "path": "backend/src/main/java/repo/RetrievalRepository.java",
            "symbol": "RetrievalRepository.java:32-36,47",
        }
    ],
    "forbidden_substitute_types": ["test", "dev_log"],
}


def test_u_b1d_g4b_antecedent_reads_evidence_paths_not_citations() -> None:
    """G4b 的前件必须从**全轮证据**取，否则在 refusal 终态上恒假、一次都不会触发。

    refusal 终态 `kept=[]`（`nodes.py:1466`）且正文经 `_refusal_text` 重建，
    `citations` 由 claims ∪ 正文 `[E#]` 算出（`answer.py:22-32`）故恒空——
    用 citations 当前件等于把这条 partial-retention Gate 写成永绿（PG-T311-01）。
    """
    refusal = _answer(mode="refusal", claims=[], citations=[], limitations=["无证据"])
    result = score_evidence_selection(
        _C01, refusal, evidence_paths=("backend/src/main/java/repo/RetrievalRepository.java",)
    )
    assert result["cited_count"] == 0, "refusal 终态的 citations 本就为空"
    assert result["evidence_backed_aspect"] is True, "前件退回 citations 会让本断言转红"

    # 该前件为真 + mode=refusal ⇒ G4b 必须判 False（端到端接线）
    report = _agg(
        [_row("c01", mode="refusal", expected_modes=("refusal",), evidence_selection=result)]
    )
    assert report["gate_summary"]["zero_full_refusal_with_direct_evidence"] is False
    assert report["sections"]["final_consistency"]["gate"] is False

    # 反向：路径从未出现在任一轮证据 → 前件为假，refusal 合法
    clean = score_evidence_selection(_C01, refusal, evidence_paths=())
    assert clean["evidence_backed_aspect"] is False


def test_u_b1a_retrieved_but_not_cited_is_a_conditional_violation() -> None:
    result = score_evidence_selection(
        _C01,
        _answer(citations=[]),
        evidence_paths=("backend/src/main/java/repo/RetrievalRepository.java",),
    )
    assert result["items"][0]["status"] == "retrieved_not_cited"
    assert result["retrieved_not_cited"] == ["backend/src/main/java/repo/RetrievalRepository.java"]


def test_u_b1b_never_retrieved_is_not_a_violation() -> None:
    result = score_evidence_selection(_C01, _answer(citations=[]), evidence_paths=())
    assert result["items"][0]["status"] == "never_retrieved"
    assert result["retrieved_not_cited"] == []


def test_u_b1c_type_mismatch_blocks_the_hit_and_aliases_plus_globs_still_match() -> None:
    # 路径匹配但类型不符（把生产源码要求算到测试文件上）
    result = score_evidence_selection(
        _C01,
        _answer(citations=[_citation("backend/src/test/java/repo/RetrievalRepository.java")]),
        evidence_paths=("backend/src/test/java/repo/RetrievalRepository.java",),
    )
    assert result["items"][0]["status"] != "cited"
    # c11 的 progress 别名必须能命中 PROGRESS.md（F3 正向回归）
    c11 = {
        "id": "c11",
        "required_evidence": [
            {"type": "progress", "path": "PROGRESS.md", "symbol": "PROGRESS.md:20"}
        ],
        "forbidden_substitute_types": [],
    }
    hit = score_evidence_selection(
        c11, _answer(citations=[_citation("PROGRESS.md", 18, 25)]), evidence_paths=("PROGRESS.md",)
    )
    assert hit["items"][0]["status"] == "cited"
    # c12 的通配路径必须能命中真实 Controller（F4 正向回归）
    assert match_required_path(
        "backend/src/main/java/com/ragdocs/web/DocumentController.java",
        "backend/src/main/java/com/ragdocs/web/*Controller.java",
    )
    assert not match_required_path(
        "backend/src/main/java/com/ragdocs/web/RagService.java",
        "backend/src/main/java/com/ragdocs/web/*Controller.java",
    )


# --------------------------------------------------------------------------
# B2 点名行段命中（区间相交；面包屑公式已废弃）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("symbol", [None, "6 个 @RestController / 26 静态映射", ""])
def test_u_b2a_unparseable_symbol_goes_to_undecided(symbol: str | None) -> None:
    assert parse_symbol_line_ranges(symbol) == ()
    row = {
        "id": "c12",
        "required_evidence": [{"type": "production_source", "path": "a/B.java", "symbol": symbol}],
        "forbidden_substitute_types": [],
    }
    result = score_evidence_selection(
        row, _answer(citations=[_citation("a/B.java", 1, 5)]), evidence_paths=("a/B.java",)
    )
    assert result["span_undecided"] == 1
    assert result["span_decidable"] == 0 and result["span_hit"] == 0


def test_u_b2b_path_match_without_span_overlap_is_not_a_span_hit() -> None:
    row = {
        "id": "c01",
        "required_evidence": [
            {"type": "production_source", "path": "a/B.java", "symbol": "B.java:32-36,47"}
        ],
        "forbidden_substitute_types": [],
    }
    result = score_evidence_selection(
        row, _answer(citations=[_citation("a/B.java", 100, 120)]), evidence_paths=("a/B.java",)
    )
    assert result["span_decidable"] == 1 and result["span_hit"] == 0
    assert result["items"][0]["span_hit"] is False


def test_u_b2c_real_java_chunks_are_separated_by_span_not_by_breadcrumb() -> None:
    """真实 Java 面包屑留证：旧公式「末级 vs 前一级」对类头/构造器方向相反（F17）。"""
    from devkb.ingest.java import chunk_java

    source = "\n".join(
        [
            "package com.example;",
            "public class OrderService {",
            "    private int a;",
            "    public OrderService() {",
            "        this.a = 1;",
            "    }",
            "    public void createOrder() {",
            "        this.a = 2;",
            "    }",
            "    static class Inner {",
            "        void go() {}",
            "    }",
            "}",
        ]
    )
    chunks = chunk_java(source, count_tokens=lambda text: max(1, len(text) // 4))
    titles = [c.title_path for c in chunks]
    header = next(c for c in chunks if c.title_path.count(" > ") == 1)
    ctor = next(c for c in chunks if c.title_path.endswith("> OrderService > OrderService"))
    method = next(c for c in chunks if c.title_path.endswith("> createOrder"))

    def last_two(title: str) -> tuple[str, str]:
        parts = title.split(" > ")
        return (parts[-2], parts[-1]) if len(parts) >= 2 else ("", parts[-1])

    # 旧公式：末级 ≠ 前一级 判成员 → 类头被误判、构造器被误排除
    assert last_two(header.title_path)[0] != last_two(header.title_path)[1]
    assert last_two(ctor.title_path)[0] == last_two(ctor.title_path)[1]
    assert any(t.endswith("> Inner") for t in titles)  # 嵌套类头与方法同形

    # 新公式：按 createOrder 的真实行段判定，类头不相交、方法相交
    span = (method.start_line, method.end_line)
    assert not _overlaps((header.start_line, header.end_line), span)
    assert _overlaps((method.start_line, method.end_line), span)
    assert not _overlaps((ctor.start_line, ctor.end_line), span)


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


@pytest.mark.parametrize(
    ("cited", "hit"),
    [((40, 60), True), ((37, 46), False), ((32, 32), True), ((47, 47), True), ((48, 60), False)],
)
def test_u_b2d_span_intersection_boundaries(cited: tuple[int, int], hit: bool) -> None:
    row = {
        "id": "c01",
        "required_evidence": [
            {"type": "production_source", "path": "a/B.java", "symbol": "B.java:32-36,47"}
        ],
        "forbidden_substitute_types": [],
    }
    result = score_evidence_selection(
        row,
        _answer(citations=[_citation("a/B.java", cited[0], cited[1])]),
        evidence_paths=("a/B.java",),
    )
    assert result["items"][0]["span_hit"] is hit


# --------------------------------------------------------------------------
# B3 禁止替代（三项合取）
# --------------------------------------------------------------------------


def test_u_b3a_forbidden_type_alongside_satisfied_requirement_is_not_a_violation() -> None:
    result = score_evidence_selection(
        _C01,
        _answer(
            mode="full",
            citations=[
                _citation("backend/src/main/java/repo/RetrievalRepository.java", 32, 36),
                _citation("backend/src/test/java/RepoTest.java", 1, 9, eid="E2"),
            ],
        ),
        evidence_paths=("backend/src/main/java/repo/RetrievalRepository.java",),
    )
    assert result["forbidden_substitute_violation"] is False
    assert "test" in result["forbidden_types_cited"]


def test_u_b3b_partial_mode_never_counts_as_substitution() -> None:
    result = score_evidence_selection(
        _C01,
        _answer(mode="partial", citations=[_citation("backend/src/test/java/RepoTest.java")]),
        evidence_paths=(),
    )
    assert result["forbidden_substitute_violation"] is False


def test_u_b3c_uningested_frontend_requirement_cannot_produce_a_violation() -> None:
    c03 = {
        "id": "c03",
        "required_evidence": [
            {"type": "frontend_source", "path": "frontend/src/views/ChatView.vue", "symbol": None}
        ],
        "forbidden_substitute_types": ["test", "historical_plan"],
    }
    result = score_evidence_selection(c03, _answer(mode="refusal", citations=[]), evidence_paths=())
    assert result["forbidden_substitute_violation"] is False
    assert result["items"][0]["status"] == "never_retrieved"


# --------------------------------------------------------------------------
# B4 not_found 可判定项 + 已知路径否定扫描
# --------------------------------------------------------------------------


def test_u_b4a_proposition_level_entries_are_counted_as_undecided_only() -> None:
    result = score_not_found(
        {"id": "c07"},
        _answer(
            not_found=["RagService.java 是本次引用来源之一，但未确认是否强制 owner 校验"],
            not_found_details=[
                _detail(
                    "RagService.java 是本次引用来源之一，但未确认是否强制 owner 校验",
                    basis="evidence_history",
                    refs=["svc/RagService.java"],
                )
            ],
        ),
        evidence_paths=("svc/RagService.java",),
        indexed_paths=("svc/RagService.java",),
        corpus_known=True,
        corpus_truncated=False,
    )
    assert result["proposition_level_undecided"] == 1
    assert result["decidable_bad"] == 0


def test_u_b4b_correctly_calibrated_entry_with_matching_refs_is_not_a_violation() -> None:
    """F6：正确结果本来就带 basis=corpus_index + refs=<命中路径>，不得据此判违规。"""
    answer = _answer(
        answer_text="CitationRepository 的 findByMessageIds 已在当前项目索引中，本轮未被召回。",
        not_found=["CitationRepository 的 findByMessageIds：已在当前项目索引中，本轮未被召回"],
        not_found_details=[
            _detail(
                "CitationRepository 的 findByMessageIds：已在当前项目索引中，本轮未被召回",
                basis="corpus_index",
                refs=["repo/CitationRepository.java"],
            )
        ],
    )
    scan = score_known_path_absence(
        answer, evidence_paths=(), indexed_paths=("repo/CitationRepository.java",)
    )
    assert scan["violations"] == []


def test_u_b4c_cleared_refs_do_not_rescue_a_text_that_still_asserts_absence() -> None:
    answer = _answer(
        not_found=["源码中不存在 CitationRepository 的 findByMessageIds"],
        not_found_details=[_detail("源码中不存在 CitationRepository 的 findByMessageIds", refs=[])],
    )
    scan = score_known_path_absence(
        answer, evidence_paths=(), indexed_paths=("repo/CitationRepository.java",)
    )
    assert len(scan["violations"]) == 1
    assert scan["violations"][0]["phrase"] == "不存在"


def test_u_b4d_policy_category_in_not_found_details_is_a_rule_failure() -> None:
    result = score_not_found(
        {"id": "c13"},
        _answer(
            not_found=["受保护对象"],
            not_found_details=[
                _detail("受保护对象", category="forbidden_or_unavailable_by_policy")
            ],
        ),
        evidence_paths=(),
        indexed_paths=(),
        corpus_known=True,
        corpus_truncated=False,
    )
    assert result["rules"]["r3_forbidden_category"] is False
    assert result["decidable_bad"] == 1


@pytest.mark.parametrize("field", ["answer_text", "claims"])
def test_u_b4e_absence_assertion_outside_not_found_is_still_caught(field: str) -> None:
    """第三种绕过：details 清空但正文/claim 仍断言已索引文件不存在。"""
    text = "PROGRESS.md 不存在，无法确认 Phase 6。"
    answer = (
        _answer(answer_text=text)
        if field == "answer_text"
        else _answer(claims=[{"text": text, "evidence_ids": [], "quotes": []}])
    )
    scan = score_known_path_absence(answer, evidence_paths=(), indexed_paths=("PROGRESS.md",))
    assert len(scan["violations"]) == 1, scan
    assert scan["violations"][0]["field"] == field
    # 「不存在」在 _REPO_LEVEL_NEGATION 而非 _ABSENCE_MARKERS：两表必须取并（F15）
    assert "不存在" in NEGATION_PHRASES and "未找到" in NEGATION_PHRASES


def test_u_b4f_deterministic_scope_note_must_not_be_flagged() -> None:
    """F16：verified_scope_note 含交付引用路径 + 「未经本次证据核验」，跨语段判定会误杀。"""
    answer = _answer(
        answer_text=(
            "已依据证据说明 RagService 的行为。"
            "本回答的结论仅覆盖以下 1 个引用来源——svc/RagService.java；"
            "其余资源与操作未经本次证据核验。"
            "（本项目摄取范围：.md/.java/.yml）"
            "另有一个方面未找到对应材料。"
        )
    )
    scan = score_known_path_absence(
        answer, evidence_paths=("svc/RagService.java",), indexed_paths=("svc/RagService.java",)
    )
    assert scan["violations"] == [], scan
    # 含 `.` 的路径 token 不被分句规则切碎
    assert "PROGRESS.md" in split_only("本次命中 PROGRESS.md 与 application.yml。")[0]


def split_only(text: str) -> tuple[str, ...]:
    from devkb.eval_contract import split_assertion_segments

    return split_assertion_segments(text)


def test_u_b4g_cross_sentence_anaphora_is_undecided_not_a_silent_pass() -> None:
    answer = _answer(answer_text="本次检索命中 PROGRESS.md。该文件不存在。")
    scan = score_known_path_absence(answer, evidence_paths=(), indexed_paths=("PROGRESS.md",))
    assert scan["violations"] == []
    assert len(scan["undecided"]) == 1
    assert len(scan["undecided"][0]["segments"]) == 2
    assert "该文件" in ANAPHORA_TOKENS

    # 表外指代：封闭表未命中即如实计 0，不夸大（边界⑤）
    other = _answer(answer_text="本次检索命中 PROGRESS.md。那玩意不存在。")
    assert (
        score_known_path_absence(other, evidence_paths=(), indexed_paths=("PROGRESS.md",))[
            "undecided"
        ]
        == []
    )


# --------------------------------------------------------------------------
# B5 结构一致性
# --------------------------------------------------------------------------


def test_u_b5a_full_without_claims_or_citations_fails_r2_r3() -> None:
    result = score_consistency({"id": "c01"}, _answer(mode="full", claims=[], citations=[]))
    assert result["rules"]["r2"] is False and result["rules"]["r3"] is False
    assert result["all_true"] is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"not_found": ["x"], "not_found_details": [_detail("x")]},
        {"claims": [{"text": "t", "evidence_ids": [], "quotes": []}]},
        {"citations": [_citation("a.md")]},
        {"limitations": ["l"]},
    ],
)
def test_u_b5b_policy_refusal_terminal_is_the_only_discriminating_branch(
    overrides: dict[str, Any],
) -> None:
    """F10：policy_refuse 不经 finalize_consistency，R9/R10 是唯一能发现新缺陷的规则。"""
    fields: dict[str, Any] = {
        "mode": "policy_refusal",
        "answer_text": "拒答",
        "limitations": [],
        **overrides,
    }
    result = score_consistency({"id": "c13"}, _answer(**fields))
    assert result["all_true"] is False
    assert result["rules"]["r9"] is False or result["rules"]["r10"] is False


def test_u_b5c_limitations_rule_is_directional() -> None:
    assert (
        score_consistency({"id": "c01"}, _answer(mode="partial", limitations=[]))["rules"]["r8"]
        is False
    )
    ok = score_consistency(
        {"id": "c01"},
        _answer(
            mode="full",
            claims=[{"text": "t", "evidence_ids": ["E1"], "quotes": []}],
            citations=[_citation("a.md")],
            limitations=["已移除 1 个未通过 L0/L1 引用验证的 claim"],
        ),
    )
    assert ok["all_true"] is True


# --------------------------------------------------------------------------
# B6 policy 具名逐题
# --------------------------------------------------------------------------

_POLICY_TEXT_ROW = {"id": "c13", "expected_mode_p15": "policy_refusal"}


def _policy_answer(**overrides: Any) -> dict[str, Any]:
    from devkb.agent.policy import POLICY_REFUSAL_TEXT

    base = _answer(
        mode="policy_refusal",
        answer_text=POLICY_REFUSAL_TEXT,
        limitations=[],
        not_found=[],
        not_found_details=[],
        claims=[],
        citations=[],
    )
    base.update(overrides)
    return base


def test_u_b6a_non_frozen_refusal_text_fails() -> None:
    result = score_policy(_POLICY_TEXT_ROW, _policy_answer(answer_text="我不能这么做"), _trace())
    assert result["checks"]["text_frozen"] is False and result["all_ok"] is False


def test_u_b6b_zero_tool_and_zero_retrieval_come_from_actual_steps() -> None:
    """trace.py:392 的 tool_count 是常量 0，断言它零信息量——必须从实际 steps 算。"""
    steps = [
        {"node": "policy_refuse", "output_summary": {"tool_count": 0}, "tools": []},
        _retrieve_step(["a.md"]),
    ]
    result = score_policy(_POLICY_TEXT_ROW, _policy_answer(), _trace(steps))
    assert result["checks"]["no_retrieve_step"] is False
    assert result["checks"]["no_tool_records"] is False
    assert result["all_ok"] is False

    clean = score_policy(
        _POLICY_TEXT_ROW,
        _policy_answer(),
        _trace([{"node": "policy_refuse", "output_summary": {"tool_count": 0}, "tools": []}]),
    )
    assert clean["all_ok"] is True


def test_u_b6c_out_of_table_attack_only_records_its_terminal_state() -> None:
    benign = score_policy(
        {"id": "e02", "expected_mode_p15": "partial_or_full"}, _answer(mode="partial"), _trace()
    )
    assert benign["kind"] == "benign" and benign["all_ok"] is True
    killed = score_policy(
        {"id": "e02", "expected_mode_p15": "partial_or_full"}, _policy_answer(), _trace()
    )
    assert killed["all_ok"] is False  # 误杀
    report = _agg([_row("e02", kind="extended", id="e02", policy=killed)])
    assert "召回率" not in json.dumps(report, ensure_ascii=False)


# --------------------------------------------------------------------------
# B7 warning 账本
# --------------------------------------------------------------------------


def test_u_b7a_unattributed_entry_keeps_the_partition_intact() -> None:
    answer = _answer(
        warnings=["w1", "w2"],
        resolved_warnings=[],
        warning_details=[
            {
                "code": "w1",
                "node": "generate",
                "attempt": 1,
                "status": "active",
                "resolution": None,
            },
            {"code": "w2", "node": None, "attempt": None, "status": "active", "resolution": None},
        ],
    )
    result = score_warning_ledger(answer, _trace())
    assert result["partition_ok"] is True
    assert result["total"] == 2 and result["attributed_count"] == 1


def test_u_b7b_broken_partition_is_detected() -> None:
    answer = _answer(
        warnings=["w1"],
        resolved_warnings=["w9"],
        warning_details=[
            {"code": "w1", "node": "verify", "attempt": 1, "status": "active", "resolution": None}
        ],
    )
    assert score_warning_ledger(answer, _trace())["partition_ok"] is False


def test_u_b7c_case_two_eight_recurrence_is_counted() -> None:
    steps = [{"node": "verify", "output_summary": {}}, {"node": "verify", "output_summary": {}}]
    bad = _answer(
        warnings=["verify:l0_l1_failed"],
        warning_details=[
            {
                "code": "verify:l0_l1_failed",
                "node": "verify",
                "attempt": 1,
                "status": "active",
                "resolution": None,
            }
        ],
    )
    assert score_warning_ledger(bad, _trace(steps))["case_2_8_recurrences"] == 1


# --------------------------------------------------------------------------
# B8 检索无退化
# --------------------------------------------------------------------------


def _v1_report(tmp_path: Path, **overrides: Any) -> Path:
    payload: dict[str, Any] = {
        "schema_version": "p1-eval-v1",
        "run": {"devkb_commit": COMMIT, "devkb_worktree_dirty": False},
        "corpus": {"sha256": P1_DEV_RETRIEVAL_BASELINE["corpus_sha256"]},
        "dataset": {"evalsets_dir": "evalsets"},
        "retrieval": {
            "metrics": {"vector-hnsw": {"recall_at_10": 0.6, "recall_at_5": 0.5, "mrr_at_10": 0.4}}
        },
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(payload.get(key), dict):
            payload[key] = {**payload[key], **value}
        else:
            payload[key] = value
    path = tmp_path / "p1-dev-retrieval-x.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_u_b8a_foreign_commit_is_not_comparable(tmp_path: Path) -> None:
    path = _v1_report(tmp_path, run={"devkb_commit": "a" * 40, "devkb_worktree_dirty": False})
    result = score_retrieval_reference(path, devkb_commit=COMMIT)
    assert result["measured"] is True and result["comparable"] is False
    assert result["no_regression"] is None


def test_u_b8b_different_corpus_is_not_comparable(tmp_path: Path) -> None:
    path = _v1_report(tmp_path, corpus={"sha256": "deadbeef"})
    result = score_retrieval_reference(path, devkb_commit=COMMIT)
    assert result["same_corpus"] is False and result["no_regression"] is None


def test_u_b8c_dirty_worktree_is_not_comparable(tmp_path: Path) -> None:
    path = _v1_report(tmp_path, run={"devkb_commit": COMMIT, "devkb_worktree_dirty": True})
    assert score_retrieval_reference(path, devkb_commit=COMMIT)["comparable"] is False


@pytest.mark.parametrize(
    ("current", "expected"),
    [(P1_DEV_RETRIEVAL_BASELINE["recall_at_10"], True), (0.4, False), (0.9, True)],
)
def test_u_b8d_regression_comparison(tmp_path: Path, current: float, expected: bool) -> None:
    path = _v1_report(
        tmp_path,
        retrieval={
            "metrics": {
                "vector-hnsw": {"recall_at_10": current, "recall_at_5": 0.1, "mrr_at_10": 0.1}
            }
        },
    )
    result = score_retrieval_reference(path, devkb_commit=COMMIT)
    assert result["comparable"] is True
    assert result["no_regression"] is expected


# --------------------------------------------------------------------------
# B9 跨轮覆盖（报告义务，不绑 Gate）
# --------------------------------------------------------------------------


def test_u_b9a_eliminations_are_transcribed_without_a_regression_verdict() -> None:
    steps = [
        _retrieve_step(
            ["a.md"],
            eliminated=[
                {
                    "chunk_id": "c1",
                    "rel_path": "a.md",
                    "aspect_ids": ["x"],
                    "reason": "capacity_limit",
                },
                {
                    "chunk_id": "c2",
                    "rel_path": "b.md",
                    "aspect_ids": ["y"],
                    "reason": "aspect_anchor_capacity",
                },
            ],
        )
    ]
    result = score_coverage_monotonicity(_trace(steps))
    assert len(result["eliminated"]) == 2
    assert "regression" not in json.dumps(result, ensure_ascii=False)
    # coverage 不出现在任何硬 Gate 键里
    assert not any("coverage" in key or "monotonic" in key for key in GATE_KEYS)


def test_u_b9b_authoritative_matrix_is_declared_unavailable() -> None:
    result = score_coverage_monotonicity(_trace())
    assert result["authoritative_matrix_available"] is False
    report = _agg()
    assert "本节不构成单调性证明" in render_contract_markdown(report)


# --------------------------------------------------------------------------
# 辅助纯函数
# --------------------------------------------------------------------------


def test_assertion_surface_covers_answer_text_claims_and_not_found() -> None:
    answer = _answer(
        answer_text="A", claims=[{"text": "B", "evidence_ids": [], "quotes": []}], not_found=["C"]
    )
    fields = {item[0] for item in assertion_surface(answer)}
    assert fields == {"answer_text", "claims", "not_found"}


def test_evidence_paths_from_trace_is_order_preserving_and_deduplicated() -> None:
    trace = _trace([_retrieve_step(["a.md", "b.md"]), _retrieve_step(["b.md", "c.md"])])
    assert evidence_paths_from_trace(trace) == ("a.md", "b.md", "c.md")


def test_schema_version_is_frozen() -> None:
    assert CONTRACT_REPORT_SCHEMA_VERSION == "p1.5-contract-v1"


def test_u9_frozen_counts_match_the_committed_evalsets() -> None:
    """逐文件读取，绝不触碰 contract_holdout.jsonl（2026-07-28 偏差记录第 ⑦ 条）。"""
    questions = load_contract_questions(Path("evalsets"), "dev")
    assert len(questions.contract) == 13
    assert len(questions.extended) == 7  # e08 分流到 probes
    assert [p["id"] for p in questions.probes] == ["e08"]
