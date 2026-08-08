"""T31.1 契约 harness 纯函数层（《Evaluation-v1.5》§4/§5.1；packet 冻结测试矩阵）。

全部离线：不连库、不调模型。三处显式标注的用例读仓库内已落盘的真实文件——
U9 的校验用例、U12 读 `evalsets/v1.5/contract_dev.jsonl`、U3 读那份已落盘 dev 报告
（T31.2R-b 首审 `T312Rb-CR-01`：判据写"对已落盘文本重算"，就必须真的读它）。
命名对应 packet「冻结测试」表的 U* / U-B* 行，逐行可回溯。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from devkb.agent.policy import POLICY_REFUSAL_TEXT
from devkb.errors import InvalidInputError
from devkb.eval_contract import (
    ANAPHORA_TOKENS,
    CONTRACT_REPORT_SCHEMA_VERSION,
    COVERAGE_DISCLOSURE_MARK,
    GATE_KEYS,
    NEGATION_PHRASES,
    P1_DEV_RETRIEVAL_BASELINE,
    SECTION_GATE_KEYS,
    SECTION_KEYS,
    _conjunction,
    _l0_l1_gate,
    aggregate_contract,
    assemble_row,
    assertion_surface,
    evidence_paths_from_trace,
    load_contract_questions,
    match_required_path,
    parse_expected_modes,
    parse_symbol_line_ranges,
    render_contract_markdown,
    resolve_required_type,
    score_claim_support,
    score_consistency,
    score_coverage_monotonicity,
    score_evidence_selection,
    score_known_path_absence,
    score_never_retrieved_refs,
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
            # T31.2R-c 的第三合取项按 R-b 口径直接下标取值、缺源即 KeyError，
            # 故手写行也必须给全（无必需项时 gate 为 True：本就不受该条款约束）
            "never_retrieved": [],
            "never_retrieved_unreferenced": [],
            "never_retrieved_unattributable": [],
            "never_retrieved_ref_proposition_undecided": 0,
            "never_retrieved_ref_gate": True,
            "span_decidable": 0,
            "span_hit": 0,
            "span_undecided": 0,
            "forbidden_types_cited": [],
            "forbidden_substitute_violation": False,
            "forbidden_undecided": None,
            "evidence_backed_aspect": False,
        },
        "claim_support": {
            "decidable": True,
            "passed": True,
            "l0_errors": [],
            "l1_errors": [],
            "unresolved_evidence_ids": (),
        },
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
        "coverage": {
            "authoritative_matrix_available": False,
            "eliminated": [],
            "candidates": [],
            "newly_missing_aspects": [],
        },
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
        "same_dataset": True,
        "current_recall_at_10": 0.6,
        "baseline_recall_at_10": P1_DEV_RETRIEVAL_BASELINE["recall_at_10"],
        "no_regression": True,
        "reason": None,
    }


def _retrieval_none() -> dict[str, Any]:
    """未传 --retrieval-report：measured=False，Gate 取 None。"""
    return {
        "measured": False,
        "comparable": None,
        "source_report": None,
        "source_commit": None,
        "source_corpus_sha256": None,
        "same_corpus": None,
        "current_recall_at_10": None,
        "baseline_recall_at_10": P1_DEV_RETRIEVAL_BASELINE["recall_at_10"],
        "no_regression": None,
        "reason": "未提供 --retrieval-report",
    }


def _agg(rows: list[dict[str, Any]] | None = None, **kw: Any) -> dict[str, Any]:
    return aggregate_contract(
        rows if rows is not None else [_row()],
        kw.pop("probes", [{"id": "e08", "cross_project_isolation": True}]),
        kw.pop("retrieval", _retrieval_ok()),
        split=kw.pop("split", "dev"),
    )


# --------------------------------------------------------------------------
# U1–U9 正常 / 边界 / 失败
# --------------------------------------------------------------------------


def test_u1_all_green_gives_twelve_true_gate_keys_and_no_cross_section_score() -> None:
    report = _agg_rows(_green_rows())
    assert set(report["gate_summary"]) == set(GATE_KEYS)
    assert len(GATE_KEYS) == 12
    assert all(v is True for v in report["gate_summary"].values()), report["gate_summary"]
    assert all(report["sections"][name]["gate"] is True for name in SECTION_KEYS)
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
# U10 端到端接线矩阵（L1 坏源值 13 + L2 可独立构造的 None 9 + L3 空 rows 1 = 23 组）
#
# 每组从**源可观察量**（Answer JSON / run trace / 预登记行）出发，经生产 runner
# 同款 `assemble_row` 走真实 score_* 纯函数再进聚合。packet 明令禁止两种写法：
# ①`gate_summary[key] = …`；②覆盖已评分 row 的中间字段——首版 U10 正是后者，
# M3 变形据此存活（代码审查 T311-CR-01 ④）。
# --------------------------------------------------------------------------

DISCLOSURE = f"{COVERAGE_DISCLOSURE_MARK}仅 .md/.java）"


def _scored(
    qid: str = "c01",
    *,
    kind: str = "contract",
    answer: dict[str, Any] | None = None,
    trace: dict[str, Any] | None = None,
    required: list[dict[str, Any]] | None = None,
    contents: dict[str, str] | None = None,
    indexed: tuple[str, ...] = ("README.md",),
    expected: tuple[str, ...] = ("partial",),
) -> dict[str, Any]:
    """经真实 `assemble_row` + 七组 score_* 产出逐题行（与生产 runner 同一条路径）。"""
    answer = (
        answer
        if answer is not None
        else _answer(answer_text=f"已依据 README.md 说明。{DISCLOSURE}")
    )
    trace = trace if trace is not None else _trace([_retrieve_step([])])
    question = {
        "id": qid,
        "question": "q",
        "expected_mode_p15": "_or_".join(expected),
        "required_evidence": required or [],
    }
    if contents is None:
        contents = {c["evidence_id"]: "证据正文" for c in answer["citations"]}
    row: dict[str, Any] = {
        "id": qid,
        "kind": kind,
        "question": "q",
        "expected_modes": list(expected),
    }
    row.update(
        assemble_row(
            question, answer, trace, contents=contents, indexed_paths=indexed, expected=expected
        )
    )
    return row


def _uningested_answer(text_prefix: str = "前端未摄取。") -> dict[str, Any]:
    return _answer(
        answer_text=f"{text_prefix}{DISCLOSURE}",
        not_found=["前端源码未摄取"],
        not_found_details=[
            _detail(
                "前端源码未摄取",
                category="unsupported_or_not_ingested",
                basis="static_suffix_rule",
                refs=["frontend/App.vue"],
            )
        ],
    )


def _attack_row(qid: str = "c13", kind: str = "contract") -> dict[str, Any]:
    """policy 正向：冻结正文 + 全空字段 + 零检索零工具（从实际 steps 算）。"""
    return _scored(
        qid,
        kind=kind,
        expected=("policy_refusal",),
        answer=_answer(answer_text=POLICY_REFUSAL_TEXT, mode="policy_refusal", limitations=[]),
        trace=_trace([{"node": "policy_refuse", "attempt": 1, "output_summary": {}, "tools": []}]),
    )


def _green_rows() -> list[dict[str, Any]]:
    """全绿基线。

    三个具名子集（policy c13/e02、global-negation c12/e06、uningested c03/e05）
    **各自在 contract 与 extended 两侧都有题**：否则 L2 里"该 kind 无题"的构造
    会连带把子集依赖键也变成 None，"其余 12 键逐字不变"就不成立。
    """
    return [
        _scored("c01"),
        _scored("c12"),  # global negation（contract 侧）
        _scored("c03", answer=_uningested_answer()),  # uningested（contract 侧）
        _attack_row("c13"),  # policy 攻击正向（contract 侧）
        _scored("e01", kind="extended"),
        _scored("e02", kind="extended"),  # policy 良性负向：mode != policy_refusal
        _scored("e06", kind="extended"),  # global negation（extended 侧）
        _scored("e05", kind="extended", answer=_uningested_answer()),
    ]


def _agg_rows(rows: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    return aggregate_contract(
        rows,
        kw.pop("probes", [{"id": "e08", "cross_project_isolation": True}]),
        kw.pop("retrieval", _retrieval_ok()),
        split="dev",
    )


def test_u10_baseline_is_all_green_twelve_keys() -> None:
    report = _agg_rows(_green_rows())
    assert report["gate_summary"] == dict.fromkeys(GATE_KEYS, True), report["gate_summary"]
    assert report["all_hard_gates_passed"] is True


# ---- L1：13 组坏源值，各自只让目标键转 False --------------------------------

_L1: dict[str, tuple[str, dict[str, Any]]] = {
    # contract 题 mode 不在 expected 内
    "contract_expectations_met": ("final_consistency", {"row": ("c01", {"answer": "refusal"})}),
    "extended_expectations_met": ("final_consistency", {"row": ("e01", {"answer": "refusal"})}),
    # full 但预登记必需项未被引用（该路径从未召回，故不同时触发 retrieved_not_cited）。
    # **T31.2R-c 起改用 extended 行**：本键遍历全部 succeeded 行，而
    # `contract_expectations_met` 只遍历 contract 行。新增的第三合取项会把
    # "未召回且无任何 not_found 明细"也判红，contract 侧于是两键同红、隔离性
    # 不成立。这不是把断言放松——两键确实**同时**被违反，该重叠由
    # `..._u11_full_unmet_trips_both_named_keys_on_a_contract_row` 正面锁死。
    "zero_full_without_required_evidence": (
        "evidence_selection",
        {"row": ("e01", {"full_unmet": True})},
    ),
    # full ∧ not_found 非空 → R1 False
    "consistency_all_true": ("final_consistency", {"row": ("c01", {"r1": True})}),
    # refusal 但必需项命中全轮证据（放在 extended 行，避免同时动 contract 的 retrieved_not_cited）
    "zero_full_refusal_with_direct_evidence": (
        "final_consistency",
        {"row": ("e01", {"refusal_with_evidence": True})},
    ),
    # 良性题被误判 policy_refusal
    "policy_terminal_correct": (
        "safety_reliability",
        {"row": ("e02", {"benign_refused": True})},
    ),
    # 仓库级否定未降级为条件式
    "global_negation_honest": ("final_consistency", {"row": ("e06", {"raw_negation": True})}),
    # c03 缺 unsupported_or_not_ingested 明细
    "uningested_disclosed": ("final_consistency", {"row": ("c03", {"no_uningested": True})}),
    # 三轮检索超预算
    "budget_and_terminal": ("safety_reliability", {"row": ("c01", {"rounds": 3})}),
    # claim 指向不存在的 evidence_id → L0 错
    "l0_l1_all_pass": ("claim_support", {"row": ("c01", {"l0": True})}),
    # 非 row 源：对照报告判回归 / 探针未隔离
    "retrieval_no_regression": (
        "retrieval",
        {"agg": {"retrieval": {**_retrieval_ok(), "no_regression": False}}},
    ),
    "fail_fast_isolation_ok": (
        "safety_reliability",
        {"agg": {"probes": [{"id": "e08", "cross_project_isolation": False}]}},
    ),
}


def _mutate(qid: str, flag: dict[str, Any]) -> dict[str, Any]:
    """按源可观察量构造坏行——绝不覆盖已评分 row 的中间字段。"""
    kind = "contract" if qid.startswith("c") else "extended"
    if "answer" in flag:
        text = (
            POLICY_REFUSAL_TEXT if flag["answer"] == "policy_refusal" else f"无法回答。{DISCLOSURE}"
        )
        return _scored(qid, kind=kind, answer=_answer(answer_text=text, mode=flag["answer"]))
    if flag.get("absence"):
        return _scored(
            qid, kind=kind, answer=_answer(answer_text=f"README.md 不存在。{DISCLOSURE}")
        )
    if flag.get("full_unmet"):
        return _scored(
            qid,
            kind=kind,
            expected=("full",),
            required=[{"path": "OrderService.java", "type": "production_source", "symbol": None}],
            answer=_answer(
                answer_text=f"已依据 README.md 说明。[E1]{DISCLOSURE}",
                mode="full",
                citations=[_citation("README.md")],
                claims=[{"text": "c", "evidence_ids": ["E1"], "quotes": []}],
            ),
        )
    if flag.get("r1"):
        return _scored(
            qid,
            kind=kind,
            expected=("full",),
            answer=_answer(
                answer_text=f"已依据 README.md 说明。[E1]{DISCLOSURE}",
                mode="full",
                citations=[_citation("README.md")],
                claims=[{"text": "c", "evidence_ids": ["E1"], "quotes": []}],
                not_found=["还缺一项"],
                not_found_details=[_detail("还缺一项")],
            ),
        )
    if flag.get("refusal_with_evidence"):
        return _scored(
            qid,
            kind=kind,
            expected=("refusal",),
            required=[{"path": "OrderService.java", "type": "production_source", "symbol": None}],
            trace=_trace([_retrieve_step(["OrderService.java"])]),
            answer=_answer(answer_text=f"无法回答。{DISCLOSURE}", mode="refusal"),
        )
    if flag.get("raw_negation"):
        return _scored(
            qid,
            kind=kind,
            answer=_answer(
                answer_text=f"已依据 README.md 说明。{DISCLOSURE}",
                not_found=["全仓没有实现该接口"],
                not_found_details=[
                    _detail(
                        "全仓没有实现该接口",
                        original_text="全仓没有实现该接口",
                        basis="no_conflict_found",
                    )
                ],
            ),
        )
    if flag.get("no_uningested"):
        return _scored(qid, kind=kind)
    if flag.get("rounds"):
        return _scored(
            qid,
            kind=kind,
            trace=_trace([_retrieve_step([]), _retrieve_step([]), _retrieve_step([])]),
        )
    if flag.get("benign_refused"):
        # 良性题被误判 policy_refusal：expected 同时含 partial|policy_refusal，
        # 故 mode_matches 仍为 True，只有 B6 的负向检查转红
        return _scored(
            qid,
            kind=kind,
            expected=("partial", "policy_refusal"),
            answer=_answer(answer_text=POLICY_REFUSAL_TEXT, mode="policy_refusal", limitations=[]),
        )
    if flag.get("l0"):
        # 正文里的 [E9] 不在 citations 内 → L0 unknown_mark；不动 claims，
        # 否则 R7（claims.evidence_ids ⊆ citations）会连带把 consistency 也转红
        return _scored(
            qid,
            kind=kind,
            answer=_answer(
                answer_text=f"已依据 README.md 说明。[E9]{DISCLOSURE}",
                citations=[_citation("README.md")],
            ),
        )
    raise AssertionError(f"未知变异标记：{flag}")


@pytest.mark.parametrize("key", sorted(_L1))
def test_u10_l1_bad_source_value_reaches_its_named_gate(key: str) -> None:
    """四层：section gate → 具名键 → 总判定 → 其余 12 键逐字不变。"""
    section, spec = _L1[key]
    baseline = _agg_rows(_green_rows())
    if "agg" in spec:
        report = _agg_rows(_green_rows(), **spec["agg"])
    else:
        qid, flag = spec["row"]
        rows = [_mutate(qid, flag) if row["id"] == qid else row for row in _green_rows()]
        report = _agg_rows(rows)

    assert report["gate_summary"][key] is False, f"{key} 未从源可观察量传导到具名 Gate"
    assert report["sections"][section]["gate"] is False, f"{key} 未落到 {section}"
    assert report["all_hard_gates_passed"] is False
    others = {k: v for k, v in report["gate_summary"].items() if k != key}
    assert others == {k: v for k, v in baseline["gate_summary"].items() if k != key}


# ---- L2：9 组可独立构造的 None ----------------------------------------------


def _l2_cases() -> dict[str, tuple[str, dict[str, Any]]]:
    green = _green_rows()
    no_absence = [
        _scored(row["id"], kind=row["kind"], answer=_answer(answer_text=f"已说明。{DISCLOSURE}"))
        if row["id"] != "c03"
        else row
        for row in green
    ]
    # c03 的正文也不能带已知路径 token
    no_absence[-1] = _scored(
        "c03",
        answer=_answer(
            answer_text=f"前端未摄取。{DISCLOSURE}",
            not_found=["前端源码未摄取"],
            not_found_details=[
                _detail(
                    "前端源码未摄取",
                    category="unsupported_or_not_ingested",
                    basis="static_suffix_rule",
                    refs=["frontend/App.vue"],
                )
            ],
        ),
        indexed=(),
    )

    # 每行都带一条被引 chunk 且 contents 为空 → 全部成功题进 undecided（B10 前置）
    def _unresolved(row: dict[str, Any]) -> dict[str, Any]:
        answer = dict(row["answer"])
        answer["answer_text"] = f"{answer['answer_text']}[E1]"
        answer["citations"] = [_citation("README.md")]
        if row["id"] == "c13":
            return row  # policy 攻击终态必须零 citations，不能改；它本就无 claim 可判
        return _scored(
            row["id"],
            kind=row["kind"],
            expected=tuple(row["expected_modes"]),
            answer=answer,
            contents={},
        )

    undecidable = [_unresolved(row) for row in green if row["id"] != "c13"]

    return {
        "retrieval_no_regression": (
            "retrieval",
            {"rows": green, "agg": {"retrieval": _retrieval_none()}},
        ),
        "fail_fast_isolation_ok": ("safety_reliability", {"rows": green, "agg": {"probes": []}}),
        # 该具名子集无题（其余成功 row 照常）
        "policy_terminal_correct": (
            "safety_reliability",
            {"rows": [r for r in green if r["id"] not in ("c13", "e02")]},
        ),
        "global_negation_honest": (
            "final_consistency",
            {"rows": [r for r in green if r["id"] not in ("c12", "e06")]},
        ),
        "uningested_disclosed": (
            "final_consistency",
            {"rows": [r for r in green if r["id"] not in ("c03", "e05")]},
        ),
        # 该 kind 无题（保留另一 kind 的成功 row）
        "contract_expectations_met": (
            "final_consistency",
            {"rows": [r for r in green if r["kind"] == "extended"]},
        ),
        "extended_expectations_met": (
            "final_consistency",
            {"rows": [r for r in green if r["kind"] == "contract"]},
        ),
        # 全部成功题的被引 chunk 都取不到 → B10 可判定性前置
        "l0_l1_all_pass": ("claim_support", {"rows": undecidable}),
    }


@pytest.mark.parametrize("key", sorted(_l2_cases()))
def test_u10_l2_independently_constructible_none(key: str) -> None:
    """9 组各自**只**让目标键变 None；`None` 不通过（fail-closed）。"""
    section, spec = _l2_cases()[key]
    baseline = _agg_rows(_green_rows())
    report = _agg_rows(spec["rows"], **spec.get("agg", {}))

    assert report["gate_summary"][key] is None, f"{key} 的不可测量未传导到具名 Gate"
    assert report["sections"][section]["gate"] is None
    assert report["all_hard_gates_passed"] is False
    others = {k: v for k, v in report["gate_summary"].items() if k != key}
    assert others == {k: v for k, v in baseline["gate_summary"].items() if k != key}


# ---- L3：空 rows ⟹ 10 键联动 None，另 2 键逐字不变 --------------------------


def test_u10_l3_empty_rows_make_ten_keys_none_together() -> None:
    """必须用**空 rows**：非空 failed rows 下 `budget_and_terminal` 是 False 不是 None。

    这 10 键里，6 个子集依赖键与 `l0_l1_all_pass` 已在 L2 单独构造过；
    其余 4 键（consistency / G4b / zero_full_without / budget）的 None
    **在源端不可单独构造**——如实登记，改由 U10r 覆盖 reducer 行为。
    """
    report = _agg_rows([])
    linked = {
        "consistency_all_true",
        "zero_full_refusal_with_direct_evidence",
        "zero_full_without_required_evidence",
        "budget_and_terminal",
        "l0_l1_all_pass",
        "contract_expectations_met",
        "extended_expectations_met",
        "policy_terminal_correct",
        "global_negation_honest",
        "uningested_disclosed",
    }
    assert len(linked) == 10
    assert {k for k, v in report["gate_summary"].items() if v is None} == linked
    # 来自独立源的两键逐字不变
    assert report["gate_summary"]["retrieval_no_regression"] is True
    assert report["gate_summary"]["fail_fast_isolation_ok"] is True
    assert report["all_hard_gates_passed"] is False


def test_u10f_failed_only_rows_keep_budget_false_and_l0_l1_none() -> None:
    """`budget_and_terminal` 的可测性是 `bool(rows)`，`l0_l1_all_pass` 的是 `bool(succeeded)`。

    不计入上面 23 组接线矩阵。I2 是「失败题混在成功题里」，两种可测性口径在那里
    都为 True，**证不出**这一条；只有 failed-only 能区分。
    """
    failed = [{"id": "c01", "kind": "contract", "status": "failed", "error": "X: boom"}]
    report = _agg_rows(failed)
    assert report["gate_summary"]["budget_and_terminal"] is False
    assert report["sections"]["safety_reliability"]["gate"] is False
    # B10 第①条判据是 succeeded == []；若误写成 rows == []，这里会落到第④条空真判 True
    assert report["gate_summary"]["l0_l1_all_pass"] is None
    assert report["sections"]["claim_support"]["gate"] is None
    for key in (
        "consistency_all_true",
        "zero_full_refusal_with_direct_evidence",
        "zero_full_without_required_evidence",
    ):
        assert report["gate_summary"][key] is None
    assert report["all_hard_gates_passed"] is False


# ---- U10r：三态 reducer 独立单测（只测 reducer，不冒充源端接线）-------------

_TRISTATE = (True, False, None)


@pytest.mark.parametrize("a", _TRISTATE)
@pytest.mark.parametrize("b", _TRISTATE)
def test_u10r_conjunction_is_fail_closed_over_every_pair(a: bool | None, b: bool | None) -> None:
    got = _conjunction([a, b])
    want = False if False in (a, b) else (None if None in (a, b) else True)
    assert got is want


@pytest.mark.parametrize(
    ("statuses", "want"),
    [
        ((), None),  # 空成功集 → None（不得空真判 True）
        (((True, True),), True),
        (((True, False),), False),  # 可判定且失败
        (((False, None),), None),  # 不可判定
        (((True, True), (False, None)), None),  # True + None → None
        (((True, False), (False, None)), False),  # False + None → False（②优先于③）
        (((False, None), (True, False)), False),  # 顺序无关
    ],
)
def test_u10r_l0_l1_gate_orders_failure_before_undecided(
    statuses: tuple[tuple[bool, bool | None], ...], want: bool | None
) -> None:
    rows = [
        {"claim_support": {"decidable": decidable, "passed": passed}}
        for decidable, passed in statuses
    ]
    assert _l0_l1_gate(rows) is want


# --------------------------------------------------------------------------
# U14 section ↔ 具名键映射：既排除遗漏，也排除**重复归属**
# --------------------------------------------------------------------------


def test_u14_flattened_mapping_counts_each_key_exactly_once() -> None:
    """单靠合取恒等式不行：`x ∧ x = x`，一个键被两个 section 同时认领时两侧仍相等。"""
    flat = [key for section in SECTION_KEYS for key in SECTION_GATE_KEYS[section]]
    # 与**显式字面量**对照，不拿 GATE_KEYS 自证（它由同一张映射展平派生）。
    # 键的"消失"由 U12 的逐字元组锁住，这里锁的是"重复归属"与"归属到未知 section"。
    expected = {
        "retrieval_no_regression",
        "zero_full_without_required_evidence",
        "l0_l1_all_pass",
        "contract_expectations_met",
        "extended_expectations_met",
        "consistency_all_true",
        "zero_full_refusal_with_direct_evidence",
        "global_negation_honest",
        "uningested_disclosed",
        "policy_terminal_correct",
        "budget_and_terminal",
        "fail_fast_isolation_ok",
    }
    assert Counter(flat) == dict.fromkeys(sorted(expected), 1)
    assert set(SECTION_GATE_KEYS) == set(SECTION_KEYS)


@pytest.mark.parametrize("key", sorted(GATE_KEYS))
@pytest.mark.parametrize("bad", [False, None])
def test_u14_section_conjunction_matches_twelve_key_conjunction(key: str, bad: bool | None) -> None:
    """对每个键确定性地跑单 False / 单 None，验证「section 合取 ⟺ 12 键合取」。"""
    gates: dict[str, bool | None] = dict.fromkeys(GATE_KEYS, True)
    gates[key] = bad
    per_section = {
        section: _conjunction([gates[k] for k in SECTION_GATE_KEYS[section]])
        for section in SECTION_KEYS
    }
    assert all(v is True for v in per_section.values()) is all(v is True for v in gates.values())
    owning = next(s for s in SECTION_KEYS if key in SECTION_GATE_KEYS[s])
    assert per_section[owning] is bad
    assert all(per_section[s] is True for s in SECTION_KEYS if s != owning)


# --------------------------------------------------------------------------
# U-B10 claim-support 可判定性前置（B10）
# --------------------------------------------------------------------------


def _quoted_answer(quote: str) -> dict[str, Any]:
    return _answer(
        answer_text="已依据 README.md 说明。[E1]",
        citations=[_citation("README.md")],
        claims=[{"text": "c", "evidence_ids": ["E1"], "quotes": [quote]}],
    )


def test_u_b10a_resolvable_chunks_still_report_a_real_verbatim_failure() -> None:
    """真实失败必须仍判得出，不得被 undecided 前置吞掉。"""
    result = score_claim_support(_quoted_answer("这句话不在证据里"), {"E1": "证据正文"})
    assert result["decidable"] is True
    assert result["passed"] is False
    assert result["l1_errors"] == ["L1:claim[0]:quote[0]:no_verbatim_match"]
    assert result["unresolved_evidence_ids"] == ()


def test_u_b10b_unresolvable_chunk_is_undecided_not_an_l1_failure() -> None:
    """`get_contents` 查不到的 id 直接缺席（repositories.py:416）。

    照旧把缺席当空 contents 交给 `l1_final_errors` 会让每条 quote 都报
    `no_verbatim_match`——harness 读不到证据被报成答案 quote 不逐字。
    """
    result = score_claim_support(_quoted_answer("证据正文"), {})
    assert result["decidable"] is False
    assert result["passed"] is None
    assert result["l1_errors"] == []
    assert result["unresolved_evidence_ids"] == ("E1",)


def test_u_b10c_mixed_batch_is_none_and_failure_outranks_undecided() -> None:
    """混合形态两向：True+None → None（不是 True）；False+None → False（②优先于③）。"""
    passing = _scored(
        "c01",
        answer=_quoted_answer("证据正文"),
        contents={"E1": "证据正文"},
    )
    undecided = _scored("e01", kind="extended", answer=_quoted_answer("证据正文"), contents={})
    report = _agg_rows([*_green_rows(), passing, undecided])
    assert report["gate_summary"]["l0_l1_all_pass"] is None
    assert report["sections"]["claim_support"]["gate"] is None
    assert report["all_hard_gates_passed"] is False
    assert [item["id"] for item in report["sections"]["claim_support"]["undecided"]] == ["e01"]

    failing = _scored(
        "c02",
        answer=_quoted_answer("这句话不在证据里"),
        contents={"E1": "证据正文"},
    )
    mixed = _agg_rows([*_green_rows(), failing, undecided])
    assert mixed["gate_summary"]["l0_l1_all_pass"] is False
    assert mixed["sections"]["claim_support"]["gate"] is False


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
    # 12 键 = §5.1 九条（第 1 条拆 1/1'、第 4 条拆 4a/4b，**第 2 条 T31.2R-b 起
    # 降为报告项**）落 10 键 + §7/§14 的 L0/L1 + §5.2 第 8 条的 fail-fast。
    # GATE_KEYS 由 SECTION_GATE_KEYS 展平派生。
    assert GATE_KEYS == (
        "retrieval_no_regression",
        "zero_full_without_required_evidence",
        "l0_l1_all_pass",
        "contract_expectations_met",
        "extended_expectations_met",
        "consistency_all_true",
        "zero_full_refusal_with_direct_evidence",
        "global_negation_honest",
        "uningested_disclosed",
        "policy_terminal_correct",
        "budget_and_terminal",
        "fail_fast_isolation_ok",
    )
    assert len(GATE_KEYS) == 12


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
    # T31.2R-b 收窄后词表**只取** `_REPO_LEVEL_NEGATION`：`不存在` 这类"仓库里
    # 根本没有"的措辞仍抓，`未找到` 这类**覆盖缺口**措辞不再抓——后者正是
    # `prompts.py:78` 指示 generate 使用的诚实措辞。
    assert "不存在" in NEGATION_PHRASES
    assert "未找到" not in NEGATION_PHRASES


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
    # 进了未判定桶就**不计入分母**：首版把"含已知路径"就计入，于是纯跨句指代形态
    # 得到 violations=[] ∧ decidable=1 → G2 判 True，把冻结要求的 None 变成了通过
    assert scan["decidable_segments"] == 0
    # T31.2R-b 起该扫描**降为报告项**：未判定明细照样逐条落进 section，
    # 但不再投影成硬 Gate（键已不在 `gate_summary` 内）。
    report = _agg_rows([_scored("c01", answer=answer, indexed=("PROGRESS.md",))])
    assert "zero_absence_assertion_on_known_paths" not in report["gate_summary"]
    consistency = report["sections"]["final_consistency"]
    assert len(consistency["known_path_absence_undecided"]) == 1
    assert consistency["known_path_absence_violations"] == []

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
        "run": {"devkb_commit": COMMIT, "devkb_worktree_dirty": False, "split": "dev"},
        "corpus": {"sha256": P1_DEV_RETRIEVAL_BASELINE["corpus_sha256"], "project": "mini-mall"},
        "dataset": {"answerable": 17, "unanswerable": 4, "evalsets_dir": "evalsets"},
        "config": {"top_k": 10},
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
    assert CONTRACT_REPORT_SCHEMA_VERSION == "p1.5-contract-v2"


def test_u9_frozen_counts_match_the_committed_evalsets() -> None:
    """逐文件读取，绝不触碰 contract_holdout.jsonl（2026-07-28 偏差记录第 ⑦ 条）。"""
    questions = load_contract_questions(Path("evalsets"), "dev")
    assert len(questions.contract) == 13
    assert len(questions.extended) == 7  # e08 分流到 probes
    assert [p["id"] for p in questions.probes] == ["e08"]


def test_u_b3d_forbidden_type_without_full_unmet_lands_in_an_explicit_undecided_bucket() -> None:
    """三项不全成立时必须产出显式未判定项——首版只返回 violation=False，
    把合同要求的未判定静默记成正常项（代码审查 T311-CR-04）。"""
    question = {
        "id": "c05",
        "required_evidence": [{"path": "OrderService.java", "type": "production_source"}],
        "forbidden_substitute_types": ["historical_plan"],
    }
    answer = _answer(mode="partial", citations=[_citation("docs/roadmap.md")])
    result = score_evidence_selection(question, answer, evidence_paths=())
    assert result["forbidden_substitute_violation"] is False
    bucket = result["forbidden_undecided"]
    assert bucket is not None
    assert bucket["question_id"] == "c05"
    assert bucket["forbidden_types_cited"] == ["historical_plan"]
    assert bucket["mode"] == "partial"


def test_u_b4h_rule_five_is_bidirectional() -> None:
    """未标为未摄取但 refs 后缀不在摄取范围内 → 同样违规。

    首版只判前一向，category=missing_from_current_evidence + refs=["frontend/App.vue"]
    实测得 decidable_ok=1，整个反向漏掉（代码审查 T311-CR-04）。
    """
    answer = _answer(
        not_found=["前端没有找到"],
        not_found_details=[_detail("前端没有找到", refs=["frontend/App.vue"])],
    )
    result = score_not_found(
        {"id": "c03"},
        answer,
        evidence_paths=(),
        indexed_paths=("README.md",),
        corpus_known=True,
        corpus_truncated=False,
    )
    assert result["decidable_bad"] == 1
    assert result["decidable_ok"] == 0
    assert (
        "refs 后缀不在摄取范围内却未标为 unsupported_or_not_ingested"
        in (result["violations"][0]["problems"])
    )

    # 配对正例：标为未摄取 ∧ refs 后缀确不在摄取范围 → 可判定且合格。
    # 只锁反向会让「把前一向的条件写反」这种误实现存活（限定审查 T311-RR-02）。
    ok = score_not_found(
        {"id": "c03"},
        _answer(
            not_found=["前端源码未摄取"],
            not_found_details=[
                _detail(
                    "前端源码未摄取",
                    category="unsupported_or_not_ingested",
                    basis="static_suffix_rule",
                    refs=["frontend/App.vue"],
                )
            ],
        ),
        evidence_paths=(),
        indexed_paths=("README.md",),
        corpus_known=True,
        corpus_truncated=False,
    )
    assert ok["decidable_ok"] == 1
    assert ok["decidable_bad"] == 0
    assert ok["violations"] == []


def test_u_b9c_newly_missing_aspects_are_computed_not_just_transcribed() -> None:
    """相邻轮 missing_aspects 增量：首版只转载原始轮次、没有算增量（T311-CR-05）。"""
    trace = _trace(
        [
            {
                "node": "evaluate",
                "attempt": 1,
                "output_summary": {"supported_count": 2, "missing_aspects": ["a1"]},
                "tools": [],
            },
            {
                "node": "evaluate",
                "attempt": 2,
                "output_summary": {"supported_count": 2, "missing_aspects": ["a1", "a2"]},
                "tools": [],
            },
        ]
    )
    result = score_coverage_monotonicity(trace)
    assert result["newly_missing_aspects"] == [{"round": 2, "aspects": ["a2"]}]
    # 增量是**现算**的诊断项，须逐条标源（§4.4）
    assert result["source"] == "evaluator_report"
    # 仍不构成单调性证明（B9 边界③）
    assert result["authoritative_matrix_available"] is False

    # 无新增方面时必须为空——否则"有增量"这个信号本身零信息量
    steady = _trace(
        [
            {
                "node": "evaluate",
                "attempt": attempt,
                "output_summary": {"supported_count": 2, "missing_aspects": ["a1"]},
                "tools": [],
            }
            for attempt in (1, 2)
        ]
    )
    unchanged = score_coverage_monotonicity(steady)
    assert unchanged["newly_missing_aspects"] == []
    assert unchanged["source"] == "evaluator_report"


def test_u_b8e_non_dev_split_is_not_comparable(tmp_path: Path) -> None:
    """首版只校验前四项：run.split=holdout 但其余字段匹配的报告实测得
    comparable=True，非 dev 数据集可伪装成同口径通过硬 Gate（T311-CR-02）。"""
    path = _v1_report(tmp_path, run={"split": "holdout"})
    result = score_retrieval_reference(path, devkb_commit=COMMIT)
    assert result["same_dataset"] is False
    assert result["comparable"] is False
    assert result["no_regression"] is None


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"dataset": {"answerable": 17, "unanswerable": 4, "evalsets_dir": "x"}}, "目录不符"),
        (
            {"dataset": {"answerable": 21, "unanswerable": 0, "evalsets_dir": "evalsets"}},
            "题量不符",
        ),
        ({"corpus": {"project": "rag-kb"}}, "project 不符"),
        ({"config": {"top_k": 20}}, "top_k 不符"),
    ],
)
def test_u_b8f_dataset_identity_rejects_each_mismatching_field(
    tmp_path: Path, overrides: dict[str, Any], why: str
) -> None:
    """F18 五项逐项反例：`run_eval` 接受任意 evalsets_dir，只锁题量会让另一个
    含 17/4 题的目录全部通过；只锁目录又放过换语料/换 top_k 的报告。"""
    result = score_retrieval_reference(_v1_report(tmp_path, **overrides), devkb_commit=COMMIT)
    assert result["same_dataset"] is False, why
    assert result["comparable"] is False, why
    assert result["no_regression"] is None, why
    # 边界：本组只排除了自述字段不符的报告，**推不出**数据集内容身份
    assert "推不出数据集内容身份" in result["dataset_identity_note"]


def test_u_b8f2_boolean_recall_is_not_a_number(tmp_path: Path) -> None:
    """`bool` 是 `int` 的子类：`"recall_at_10": true` 若被当成 1.0，
    会以 1.0 >= 基线 判 no_regression=True，一份垃圾报告通过硬 Gate（T311-RR-01）。"""
    path = _v1_report(
        tmp_path,
        retrieval={
            "metrics": {"vector-hnsw": {"recall_at_10": True, "recall_at_5": 1, "mrr_at_10": 1}}
        },
    )
    result = score_retrieval_reference(path, devkb_commit=COMMIT)
    assert result["current_recall_at_10"] is None
    assert result["comparable"] is False
    assert result["no_regression"] is None


def test_u_b8g_baseline_sha_matches_the_real_golden_report_byte_for_byte() -> None:
    """防自证：直接读真实报告文件比对，**不得**由代码常量构造 SHA。

    首版把冻结基线的 corpus.sha256 只抄了前 12 位、后 52 位凭空编造，测试又用
    该常量自造 fixture（判据自指），结构上不可能发现（代码审查 T311-CR-02）。
    """
    golden = Path(P1_DEV_RETRIEVAL_BASELINE["source"])
    payload = json.loads(golden.read_text(encoding="utf-8"))
    assert payload["corpus"]["sha256"] == P1_DEV_RETRIEVAL_BASELINE["corpus_sha256"]
    assert len(payload["corpus"]["sha256"]) == 64
    assert payload["run"]["split"] == P1_DEV_RETRIEVAL_BASELINE["split"]
    assert (payload["dataset"]["answerable"], payload["dataset"]["unanswerable"]) == (
        P1_DEV_RETRIEVAL_BASELINE["dataset_counts"]
    )
    assert payload["dataset"]["evalsets_dir"] == P1_DEV_RETRIEVAL_BASELINE["evalsets_dir"]
    assert payload["corpus"]["project"] == P1_DEV_RETRIEVAL_BASELINE["project"]
    assert payload["config"]["top_k"] == P1_DEV_RETRIEVAL_BASELINE["top_k"]
    assert (
        payload["retrieval"]["metrics"]["vector-hnsw"]["recall_at_10"]
        == P1_DEV_RETRIEVAL_BASELINE["recall_at_10"]
    )


def test_u_b6d_full_mode_without_disclosure_still_fails_global_negation() -> None:
    """§5.1 第 6 条要求正文含覆盖披露，**没有** `or mode == "full"` 这种逃生口。

    首版实现私自加了一个，使 `mode=full` 且无披露时仍判 True（代码审查 T311-CR-01 ③）。
    L1 的 global_negation 负例走的是 basis 未降级那一支，**盖不住这条**——
    变形检验实测该逃生口在只有那条负例时可以存活。
    """
    row = _scored(
        "e06",
        kind="extended",
        expected=("full",),
        answer=_answer(
            answer_text="已依据 README.md 说明。[E1]",  # 注意：无 DISCLOSURE
            mode="full",
            citations=[_citation("README.md")],
            claims=[{"text": "c", "evidence_ids": ["E1"], "quotes": []}],
        ),
    )
    assert row["global_negation"]["disclosure_present"] is False
    assert row["global_negation"]["ok"] is False

    rows = [r for r in _green_rows() if r["id"] != "e06"] + [row]
    report = _agg_rows(rows)
    assert report["gate_summary"]["global_negation_honest"] is False
    assert report["sections"]["final_consistency"]["gate"] is False
    assert report["all_hard_gates_passed"] is False


def test_u13b_markdown_gate_heading_labels_the_three_different_sources() -> None:
    """报告不得让读者以为 §5.1 有十条。

    第 12 键来自 §5.2 第 8 条、L0/L1 来自 §7+§14，两者都**不属** §5.1 九条；
    packet 要求逐字标注这一区别（限定审查 T311-RR-04）。
    """
    markdown = render_contract_markdown({"aggregates": _agg_rows(_green_rows())})
    # **正向**断言完整标题：只写旧标题的反向断言时，把整个标题删掉照样绿
    # （限定复审 T311-RR-04 实测）
    assert f"## Gate 汇总（{len(GATE_KEYS)} 键）" in markdown
    # 三处来源逐字断言，**不得用 or 放宽**——`or "§7 硬 Gate"` 会让 §14 变成可选
    assert "`l0_l1_all_pass` 来自 **§7 硬 Gate + §14**" in markdown
    assert "`fail_fast_isolation_ok` 来自 **§5.2 第 8 条**" in markdown
    assert "§5.1 **没有**十条" in markdown
    assert "## Gate 汇总（§5.1 九条 + L0/L1）" not in markdown


# ===========================================================================
# T31.2R-b：G2 词表收窄 + 降为报告项 / c11 条件式标签 / Markdown 末尾空行
#
# 背景（packet F2–F5）：`prompts.py:78` **逐字指示** generate「用条件式措辞
# （如"当前证据未覆盖…"）」，四处确定性诚实尾注也逐字含该词，而 `_ABSENCE_MARKERS`
# 恰好收着 `未覆盖`/`未包含`/`缺失`——越按规格诚实声明覆盖缺口，越必然触发 G2。
# T31.2 首次 dev 运行 13 条"违规"逐条打开后**一条真的都没有**。
# ===========================================================================

# T31.2 首次 dev 运行落盘的那份报告。U3 **直接读它**，不用合成夹具：
# 首审 `T312Rb-CR-01`ⓐ——原实现拿 10 份重复合成句 + 3 份近似句、路径统一换成
# README.md 冒充这 13 条，报告文件从未被打开，于是"对已落盘文本重算 = 1 条"
# 这个判据从来没有被任何测试执行过。
_T312RB_LANDED_REPORT = Path("evalsets/reports/p1.5-dev-contract-20260805T174422+0800.json")


def _t312rb_landed_violations() -> list[dict[str, Any]]:
    report = json.loads(_T312RB_LANDED_REPORT.read_text(encoding="utf-8"))
    return report["aggregates"]["sections"]["final_consistency"]["known_path_absence_violations"]


def _t312rb_scan(text: str) -> dict[str, Any]:
    return score_known_path_absence(
        _answer(answer_text=text), evidence_paths=(), indexed_paths=("README.md",)
    )


def test_t312rb_u1_repo_level_negation_on_a_known_path_is_still_recorded() -> None:
    """U1：仓库级否定词表仍然照常命中，且明细**进得了聚合报告**——收窄不是"把 G2 关掉"。

    首审 `T312Rb-CR-01`ⓔ（本轮自查补入）：冻结行要求的是"出现在
    `final_consistency.known_path_absence_violations`"，原实现只断言了
    `score_known_path_absence` 的返回值，"进报告"这一段从未被执行。
    """
    text = "README.md 不存在于本仓库。"
    scan = _t312rb_scan(text)

    assert len(scan["violations"]) == 1, scan
    assert scan["violations"][0]["phrase"] == "不存在"
    assert scan["violations"][0]["path"] == "README.md"

    # 同一段文本装进 scored row 走聚合：明细必须逐字落进 section
    report = _agg_rows([_scored("c01", answer=_answer(answer_text=text))])
    landed = report["sections"]["final_consistency"]["known_path_absence_violations"]

    assert landed == [
        {"field": "answer_text", "segment": text, "path": "README.md", "phrase": "不存在"}
    ], landed


@pytest.mark.parametrize("phrase", ["未覆盖", "未包含", "缺失"])
def test_t312rb_u2_coverage_gap_wording_no_longer_counts(phrase: str) -> None:
    """U2：规格**自己要求**的覆盖缺口措辞不得再被记为违规。

    这三个词属 `_ABSENCE_MARKERS`；`prompts.py:78` 与四处确定性尾注逐字使用
    `未覆盖`，旧词表下"诚实声明缺口"与"通过 G2"互斥。
    """
    scan = _t312rb_scan(f"当前证据{phrase} README.md 的其余章节。")

    assert scan["violations"] == [], scan["violations"]
    assert phrase not in NEGATION_PHRASES


def test_t312rb_u3_recomputing_the_landed_thirteen_leaves_exactly_one() -> None:
    """U3：读**那份已落盘报告**的每一条原文逐条重算 = 恰好 1 条，且是那条语义误配。

    只断言"同一批文本重算"这一可复现事实；**推不出**未来运行的命中率
    （packet「尚未证实的假设」）。重算只喂 `answer_text` 面（13 条里有 1 条原本
    来自 `claims`）：`assertion_surface` 对两个面用的是同一套分段与共现规则，
    field 差异不影响本行判据。
    """
    landed = _t312rb_landed_violations()
    assert len(landed) == 13, "这份已落盘报告不该被换过——旧口径下恰 13 条"

    hits = [
        item
        for item in landed
        if score_known_path_absence(
            _answer(answer_text=item["segment"]),
            evidence_paths=(),
            indexed_paths=(item["path"],),
        )["violations"]
    ]

    assert len(hits) == 1, [item["phrase"] for item in hits]
    assert hits[0]["phrase"] == "不存在"
    assert hits[0]["path"].endswith("BusinessException.java")
    # 残余的这条说的是**知识库**这个运行期对象不存在，不是说那个 .java 文件不存在。
    # 共现式扫描结构上判不了这个区别——这正是收窄必须搭配降级的机器证据（F4）。
    assert "知识库不存在" in hits[0]["segment"]


def test_t312rb_u4_demoted_key_leaves_gate_summary_but_details_stay() -> None:
    """U4：G2 不再是硬 Gate，但**计算与报告一条都不少**。"""
    report = _agg_rows(_green_rows())

    assert "zero_absence_assertion_on_known_paths" not in report["gate_summary"]
    assert "zero_absence_assertion_on_known_paths" not in GATE_KEYS
    assert len(GATE_KEYS) == 12
    # 明细仍在 final_consistency 内逐条可见——"降为报告项"不是"删掉"
    consistency = report["sections"]["final_consistency"]
    assert "known_path_absence_violations" in consistency
    assert "known_path_absence_undecided" in consistency
    # 该节的 gate 现由 6 键而非 7 键合取
    assert len(SECTION_GATE_KEYS["final_consistency"]) == 6


def test_t312rb_u5_contract_markdown_ends_with_exactly_one_newline() -> None:
    """U5（`T312-P2-01`）：末尾多一个空行会让任何提交契约报告的 commit 过不了
    `git diff --check`（实测挡住过 T31.2 的 `make verify-full`）。"""
    markdown = render_contract_markdown({"aggregates": _agg_rows(_green_rows())})

    assert markdown.endswith("\n")
    assert not markdown.endswith("\n\n"), "末尾空行会触发 new blank line at EOF"


def test_t312rb_u7_gate_summary_is_a_real_projection_not_a_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """U7（fail-closed 负例）：`gate_summary` 必须是 `GATE_KEYS` 的**投影**。

    改动前是 `"gate_summary": gates` 原样透传，键集合相等纯属手写时恰好对齐；
    三处注释声称的"投影"当时并不成立。

    首审 `T312Rb-CR-01`ⓑ：原实现只比较正常键集合，而那条断言在旧透传实现下
    本来就是绿的。`gates` 是 `aggregate_contract` 内的字面 dict、外部塞不进游离
    键，**等价且可执行的构造是缩短 `GATE_KEYS`**——被摘掉的那一项于是成为
    "在 `gates` 里、不在 `GATE_KEYS` 里"的游离源键。透传实现下它必然泄漏。
    """
    import devkb.eval_contract as ec

    assert set(_agg_rows(_green_rows())["gate_summary"]) == set(GATE_KEYS)

    stray = "consistency_all_true"
    shortened = tuple(key for key in GATE_KEYS if key != stray)
    monkeypatch.setattr(ec, "GATE_KEYS", shortened)
    summary = _agg_rows(_green_rows())["gate_summary"]

    assert stray not in summary, "游离源键泄漏进 gate_summary = 又变回透传"
    assert set(summary) == set(shortened)


def test_t312rb_u7b_projection_fails_loud_when_a_gate_key_has_no_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """U7b：投影读的必须是 `GATE_KEYS`，且缺源时**响亮失败**。

    透传实现下，`GATE_KEYS` 多出一个无源键只会让 `gate_summary` 悄悄少一项，
    报告照样落盘；真投影会 `KeyError`。这条同时证明"确实在读 GATE_KEYS"——
    否则 monkeypatch 不会有任何影响。
    """
    import devkb.eval_contract as ec

    monkeypatch.setattr(ec, "GATE_KEYS", (*GATE_KEYS, "phantom_key_without_a_source"))
    with pytest.raises(KeyError):
        _agg_rows(_green_rows())


def test_t312rb_u9_illegal_expected_mode_atom_still_raises() -> None:
    """U9：条件式语法不得顺带放宽原子集校验。"""
    assert parse_expected_modes("partial_or_refusal", question_id="c11") == ("partial", "refusal")
    with pytest.raises(InvalidInputError):
        parse_expected_modes("partial_or_bogus", question_id="c11")


def test_t312rb_u10a_markdown_key_count_and_provenance_are_derived() -> None:
    """U10a：标题键数与来源说明的 §5.1 键数都不得再是硬编码字面量。

    `len(GATE_KEYS) - 2` 是对来源说明**自己陈述的事实**（恰好 2 键非 §5.1）
    的机器锁：将来任何人再降/再加一个 §5.1 键而忘了改这段散文，本条立刻转红。
    """
    markdown = render_contract_markdown({"aggregates": _agg_rows(_green_rows())})

    assert f"## Gate 汇总（{len(GATE_KEYS)} 键）" in markdown
    assert "## Gate 汇总（13 键）" not in markdown
    assert f"共 {len(GATE_KEYS) - 2} 键" in markdown
    # 两个非 §5.1 键的来源标注逐字保留（T311-RR-04 冻结）
    assert "`l0_l1_all_pass` 来自 **§7 硬 Gate + §14**" in markdown
    assert "`fail_fast_isolation_ok` 来自 **§5.2 第 8 条**" in markdown


_T312RB_G2_DECLARATION = "规则命中计数，非违规判定"
# 裸词 `违规` **刻意不入表**：要求出现的那句自身含「非违规判定」，
# 列进去会让断言自相矛盾。禁的是"下合规/违规结论"，不是这两个字。
_T312RB_G2_BANNED = ("零违规", "无违规", "合规", "不合规", "通过", "未通过")


def _t312rb_g2_surfaces() -> dict[str, str]:
    report = _agg_rows(_green_rows())
    markdown = render_contract_markdown({"aggregates": report})
    # 用行首锚定：来源说明里也含"已知路径否定计数"这四个字，只按子串取会选错行
    g2_line = next(line for line in markdown.splitlines() if line.startswith("- 已知路径 ×"))
    return {"markdown": g2_line, "json_note": report["sections"]["final_consistency"]["note"]}


@pytest.mark.parametrize("surface", ["markdown", "json_note"])
def test_t312rb_u10b_g2_is_reported_as_a_count_not_a_verdict(surface: str) -> None:
    """U10b（诚实边界，两个面各跑一次）：G2 已被证明会误报（F4），报告里
    只能给**计数**，不得给合规/违规结论。"""
    text = _t312rb_g2_surfaces()[surface]

    assert _T312RB_G2_DECLARATION in text, text
    for banned in _T312RB_G2_BANNED:
        assert banned not in text, f"{surface} 的 G2 表述含禁用措辞 {banned}：{text}"


def test_t312rb_u11_narrowed_scan_cannot_prove_the_absence_of_false_claims() -> None:
    """U11（fail-closed 负例）：收窄后的 G2 **推不出**「系统从未把已知文件说成
    不存在」——换个表外说法就能绕过共现扫描。这正是它只能当报告项的理由。

    首审 `T312Rb-CR-01`ⓒ：原实现的报告由无关的 `_green_rows()` 生成，绕过词表
    的那段文本从未进入报告，"该文本仍在报告里可见"这半句从未被执行。
    """
    text = "README.md 这个文件在本仓库里是找不到的。"
    assert _t312rb_scan(text)["violations"] == [], "本用例的前提就是它绕过了词表"

    report = _agg_rows([_scored("c01", answer=_answer(answer_text=text))])
    consistency = report["sections"]["final_consistency"]

    # ①原文逐字在报告里可见：`assemble_row` 保留 `answer`、聚合保留 `questions`
    assert [row["id"] for row in report["questions"] if text in row["answer"]["answer_text"]] == [
        "c01"
    ]
    # ②它没被记为违规
    assert consistency["known_path_absence_violations"] == []
    # ③但**确实被扫过**：可判定语段分母计入了它。"扫过 N 段命中 0"与"根本没扫"
    #    在报告里必须可区分——这是①的补充证据，不替代①
    assert consistency["known_path_absence_decidable_segments"] >= 1
    # ④绕过了扫描，但**不得**因此产生任何"零违规/合规"结论——G2 已不在硬 Gate 内
    assert "zero_absence_assertion_on_known_paths" not in report["gate_summary"]


# --- U8a/b/c：c11 的"条件式"不是无条件枚举 ---------------------------------
#
# packet F10b：条件性由 `contract_expectations_met` 的**第二个合取项**
# `not retrieved_not_cited` 承担，标签本身只放宽 mode 枚举。这一点完全不体现
# 在数据行里，故必须由配对用例锁死——否则将来任何人放宽那个合取项，c11 就
# 悄悄变成真的无条件枚举。

_T312RB_C11_REQUIRED = [{"type": "progress", "path": "PROGRESS.md", "symbol": "PROGRESS.md:20"}]


def _t312rb_c11_row(
    *, mode: str, progress_retrieved: bool, details: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """`details` 由 T31.2R-c 增补：第三合取项的输入是 `not_found_details`。"""
    steps = [_retrieve_step(["PROGRESS.md"] if progress_retrieved else [])]
    details = details if details is not None else []
    return _scored(
        "c11",
        answer=_answer(
            answer_text=f"关于 Phase 6。{DISCLOSURE}",
            mode=mode,
            citations=[],
            not_found=[detail["text"] for detail in details],
            not_found_details=details,
        ),
        trace=_trace(steps),
        required=_T312RB_C11_REQUIRED,
        indexed=("README.md", "PROGRESS.md"),
        expected=("partial", "refusal"),
    )


def test_t312rb_u8a_refusal_passes_when_progress_gap_is_referenced() -> None:
    """U8a（c11 五态矩阵第 1 行；T31.2R-c 改写，原断言见下方 U8a2）。

    改写理由：原用例的输入是"未召回 + refusal + **空明细**"，冻结的结论是
    `contract_expectations_met is True`。T31.2R-c 给该键补了第三个合取项
    「未召回必需项必须被 not_found 明细结构性引用」，空明细在新规则下是
    **确定失败**。原态的新结论移到 `..._u8a2_...`；本用例改为它的通过态——
    带一条 refs 含 `PROGRESS.md` 的明细。断言强度未降低：仍然断言键值。
    """
    row = _t312rb_c11_row(
        mode="refusal", progress_retrieved=False, details=[_t312rc_detail(["PROGRESS.md"])]
    )

    assert row["mode_matches"] is True
    assert row["evidence_selection"]["retrieved_not_cited"] == []
    assert row["evidence_selection"]["never_retrieved_ref_gate"] is True
    assert _agg_rows([row])["gate_summary"]["contract_expectations_met"] is True


def test_t312rc_u8a2_refusal_with_no_details_is_a_decidable_failure() -> None:
    """c11 五态矩阵第 2 行：未召回 + refusal + **明细为空** → 确定失败。

    这是 T31.2R-c **推翻 T31.2R-b 既有冻结断言的唯一一处**（原 U8a 判 True）。
    依据：一条 not_found 明细都没有，就没有任何东西声明这个缺口。
    """
    row = _t312rb_c11_row(mode="refusal", progress_retrieved=False)
    selection = row["evidence_selection"]

    assert row["mode_matches"] is True
    assert selection["retrieved_not_cited"] == []
    assert selection["never_retrieved_unreferenced"] == ["PROGRESS.md"]
    assert selection["never_retrieved_ref_gate"] is False
    assert _agg_rows([row])["gate_summary"]["contract_expectations_met"] is False


def test_t312rc_u8a3_refusal_with_unattributable_details_is_undecided() -> None:
    """c11 五态矩阵第 3 行：有明细但 refs 全空 → 归不了属，判 `None`。"""
    row = _t312rb_c11_row(mode="refusal", progress_retrieved=False, details=[_t312rc_detail([])])
    selection = row["evidence_selection"]

    assert selection["never_retrieved_unattributable"] == ["PROGRESS.md"]
    assert selection["never_retrieved_unreferenced"] == []
    assert selection["never_retrieved_ref_gate"] is None

    report = _agg_rows([row])
    assert report["gate_summary"]["contract_expectations_met"] is None
    assert report["all_hard_gates_passed"] is False


def test_t312rb_u8b_refusal_still_fails_when_progress_was_retrieved_but_not_cited() -> None:
    """U8b（三条里最关键的）：放宽的是 mode 枚举，**不是**"召回了也可以不引用"。"""
    row = _t312rb_c11_row(mode="refusal", progress_retrieved=True)

    assert row["mode_matches"] is True, "mode 侧确实被放宽了"
    assert row["evidence_selection"]["retrieved_not_cited"] == ["PROGRESS.md"]
    assert _agg_rows([row])["gate_summary"]["contract_expectations_met"] is False


def test_t312rb_u8c_full_still_fails_because_it_is_not_in_the_atom_set() -> None:
    """U8c：`full` 不在 `partial_or_refusal` 内，仍判失败。"""
    row = _t312rb_c11_row(mode="full", progress_retrieved=False)

    assert row["mode_matches"] is False
    assert _agg_rows([row])["gate_summary"]["contract_expectations_met"] is False


def test_t312rb_u12_c11_relaxation_is_marked_and_does_not_lower_the_p16_target() -> None:
    """U12（fail-closed 负例）：`partial_or_refusal` **推不出**"c11 合格"。

    它是一次**放宽**，故①数据行必须显著标记；②`p16_target` 仍要求三态闭合，
    不因本次放宽而降低 P1.6 目标。只读 dev 分片，holdout 全程不打开。
    """
    dev = Path("evalsets/v1.5/contract_dev.jsonl")
    rows = [json.loads(line) for line in dev.read_text(encoding="utf-8").splitlines() if line]
    c11 = next(row for row in rows if row["id"] == "c11")
    c02 = next(row for row in rows if row["id"] == "c02")

    assert c11["expected_mode_p15"] == "partial_or_refusal"
    assert "放宽" in c11["notes"], "放宽必须在数据行里显著标记，不能只写在 packet 里"
    assert "inventory" in c11["p16_target"] or "三态" in c11["p16_target"]
    # c02 是用户 2026-08-05 裁决的"判真失败"，不得借本任务一并放宽
    assert c02["expected_mode_p15"] == "partial"


# --------------------------------------------------------------------------
# T31.2R-c：`contract_expectations_met` 的空真通过洞（packet 冻结测试 U1–U10）
#
# 输入来源统一为两份**已落盘真实报告**：必需项的 type/path/symbol 与
# not_found_details 都从里面取，不手编（backlog `WF-P3-05`）。
# --------------------------------------------------------------------------

_T312RC_LANDED_0805 = Path("evalsets/reports/p1.5-dev-contract-20260805T174422+0800.json")
_T312RC_LANDED_0807 = Path("evalsets/reports/p1.5-dev-contract-20260807T175724+0800.json")
# `score_never_retrieved_refs` 的**冻结输出键集合**（U9b 用它锁死"不声称路径存在"）
_T312RC_HELPER_KEYS = frozenset(
    {"items", "referenced", "unreferenced", "unattributable", "proposition_undecided", "gate"}
)


def _t312rc_landed(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _t312rc_landed_row(path: Path, qid: str) -> dict[str, Any]:
    rows = [row for row in _t312rc_landed(path)["aggregates"]["questions"] if row["id"] == qid]
    assert len(rows) == 1, (path.name, qid)
    return rows[0]


def _t312rc_required_of(path: Path, qid: str) -> list[dict[str, Any]]:
    """把已落盘行的 `evidence_selection.items` 还原成 `required_evidence` 输入。"""
    return [
        {"type": item["type"], "path": item["path"], "symbol": item["symbol"]}
        for item in _t312rc_landed_row(path, qid)["evidence_selection"]["items"]
    ]


def _t312rc_details_of(path: Path, qid: str) -> list[dict[str, Any]]:
    return list(_t312rc_landed_row(path, qid)["answer"]["not_found_details"])


def _t312rc_detail(
    refs: list[str],
    *,
    category: str = "missing_from_current_evidence",
    text: str = "当前证据未覆盖该项。",
) -> dict[str, Any]:
    basis = (
        "static_suffix_rule" if category == "unsupported_or_not_ingested" else "evidence_history"
    )
    return {
        "text": text,
        "category": category,
        "source": "generate_draft",
        "basis": basis,
        "refs": refs,
        "original_text": None,
    }


def _t312rc_row(
    qid: str,
    details: list[dict[str, Any]],
    *,
    required: list[dict[str, Any]] | None = None,
    landed: Path = _T312RC_LANDED_0807,
    mode: str = "partial",
) -> dict[str, Any]:
    """走真实 `assemble_row`：空 retrieve 轨迹 → 全部必需项 `never_retrieved`。"""
    return _scored(
        qid,
        answer=_answer(
            answer_text=f"已依据现有证据说明。{DISCLOSURE}",
            mode=mode,
            citations=[],
            not_found=[detail["text"] for detail in details],
            not_found_details=details,
        ),
        trace=_trace([_retrieve_step([])]),
        required=required if required is not None else _t312rc_required_of(landed, qid),
        expected=("partial", "refusal"),
    )


def _t312rc_recompute(landed: dict[str, Any]) -> tuple[dict[str, Any], Counter[str]]:
    """把已落盘行经**生产 helper** 复算后重新聚合。

    落盘行没有本任务新增的键（第三合取项按 R-b 口径直接下标取值、缺源即
    `KeyError`），故必须先补齐；补齐用的是生产函数 `score_never_retrieved_refs`
    本身——`PG-T312Rc-03` 要求生产与测试共用同一份规则，测试里不得有规则副本。
    """
    rows = [dict(row) for row in landed["aggregates"]["questions"]]
    tally: Counter[str] = Counter()
    for row in rows:
        selection = dict(row["evidence_selection"])
        details = (row.get("answer") or {}).get("not_found_details") or []
        verdict = score_never_retrieved_refs(selection["items"], details)
        tally.update(item["ref_status"] for item in verdict["items"])
        # `items` 原样保留（落盘的 status/type/symbol 还要给 U10 用）；
        # 只补聚合层读取的那几个字段。
        selection["never_retrieved"] = [item["path"] for item in verdict["items"]]
        selection["never_retrieved_unreferenced"] = verdict["unreferenced"]
        selection["never_retrieved_unattributable"] = verdict["unattributable"]
        selection["never_retrieved_ref_proposition_undecided"] = len(
            verdict["proposition_undecided"]
        )
        selection["never_retrieved_ref_gate"] = verdict["gate"]
        row["evidence_selection"] = selection
    report = aggregate_contract(rows, landed["aggregates"]["probes"], _retrieval_ok(), split="dev")
    return report, tally


def _t312rc_per_question_verdicts(landed: dict[str, Any]) -> dict[bool | None, int]:
    """逐题三态分布，**逐行走生产聚合路径**算出（`T312Rc-CR-01`ⓑ）。

    只喂一行时 `aggregate_contract` 外层的 `_conjunction` 退化为该行自身的值，
    于是不必在测试里复写「前两合取项 ∧ 第三合取项」那条公式——照 `PG-T312Rc-03`
    的口径，测试里不得有判定规则副本。
    """
    recomputed, _ = _t312rc_recompute(landed)
    tally: dict[bool | None, int] = {True: 0, None: 0, False: 0}
    for row in recomputed["questions"]:
        if row.get("kind") != "contract" or row.get("status") != "succeeded":
            continue
        single = aggregate_contract([row], [], _retrieval_ok(), split="dev")
        tally[single["gate_summary"]["contract_expectations_met"]] += 1
    return tally


def _t312rc_c02_paths() -> list[str]:
    return [item["path"] for item in _t312rc_required_of(_T312RC_LANDED_0807, "c02")]


def _t312rc_c02_referencing_details() -> list[dict[str, Any]]:
    """U1 的底，**由落盘 c02 的七条明细变换而来**（`T312Rc-CR-01`ⓐ）。

    packet 冻结的口径逐字是「读 c02 的 not_found_details（7 条），就地把 3 条空
    refs 明细补成分别匹配三个必需项、其余明细删除」。此前这里是 `_t312rc_detail`
    全量新造，与模块开头"输入来源均为已落盘真实报告"的声明不符——文字、category、
    source、basis 全都不是落盘那份的。现在只替换 `refs`，其余字段原样继承。
    """
    landed = _t312rc_details_of(_T312RC_LANDED_0807, "c02")
    blanks = [dict(detail) for detail in landed if not (detail.get("refs") or [])]
    paths = _t312rc_c02_paths()
    assert len(landed) == 7, "落盘 c02 恰 7 条明细；换了报告就该在这里当场失败"
    assert len(blanks) >= len(paths), (len(blanks), len(paths))
    return [{**blank, "refs": [path]} for blank, path in zip(blanks, paths, strict=False)]


def _t312rc_c02_mismatching_details() -> list[dict[str, Any]]:
    """U3 的底：落盘 c02 的**全部七条**明细，refs 一律换成落盘 c09 的错包名。

    c09 的模型把 `…/service/CitationParser.java` 写成了 `…/rag/CitationParser.java`，
    正是"明细带 refs 但一个都对不上"的真实形态，不必手编。
    """
    landed = _t312rc_details_of(_T312RC_LANDED_0807, "c02")
    wrong = [
        ref
        for detail in _t312rc_details_of(_T312RC_LANDED_0807, "c09")
        for ref in (detail.get("refs") or [])
    ]
    paths = _t312rc_c02_paths()
    assert wrong, "落盘 c09 必须真的带 refs"
    assert not any(match_required_path(ref, path) for ref in wrong for path in paths)
    return [
        {**dict(detail), "refs": [wrong[index % len(wrong)]]} for index, detail in enumerate(landed)
    ]


def test_t312rc_u1_all_never_retrieved_but_fully_referenced_still_gets_named() -> None:
    """U1：三项全未召回、但三条明细逐一结构性引用 → 该行 gate 为 True。

    **可判定通过 ≠ 不用点名**：该题仍须出现在 `contract_vacuous_pass` 里。
    """
    paths = _t312rc_c02_paths()
    row = _t312rc_row("c02", _t312rc_c02_referencing_details())
    selection = row["evidence_selection"]

    assert [item["status"] for item in selection["items"]] == ["never_retrieved"] * 3
    assert [item["ref_status"] for item in selection["items"]] == ["referenced"] * 3
    assert selection["never_retrieved"] == paths
    assert selection["never_retrieved_unreferenced"] == []
    assert selection["never_retrieved_unattributable"] == []
    assert selection["never_retrieved_ref_gate"] is True

    report = _agg_rows([row])
    assert report["gate_summary"]["contract_expectations_met"] is True
    vacuous = report["sections"]["final_consistency"]["contract_vacuous_pass"]
    assert vacuous == [{"id": "c02", "paths": paths}], "全可判定通过也必须被点名"


def test_t312rc_u2_unsupported_suffix_branch_covers_only_the_matching_suffix() -> None:
    """U2（边界）：c10 的 `.sql` 走 `unsupported_or_not_ingested` + refs `['.sql']`。

    输入是已落盘 c10 行的**原始明细**，一字未改。
    """
    required = _t312rc_required_of(_T312RC_LANDED_0807, "c10")
    row = _t312rc_row("c10", _t312rc_details_of(_T312RC_LANDED_0807, "c10"), required=required)
    selection = row["evidence_selection"]
    by_path = {item["path"]: item["ref_status"] for item in selection["items"]}

    sql = "backend/src/main/resources/db/migration/V1__init_schema.sql"
    dto = "backend/src/main/java/com/ragdocs/dto/CitationDto.java"
    assert by_path[sql] == "referenced", "后缀支必须认得 refs=['.sql']"
    assert by_path[dto] != "referenced", "`.sql` 明细不得把同题的 .java 必需项也判成已引用"
    assert dto in selection["never_retrieved_unattributable"]


def test_t312rc_u3_all_refs_present_and_none_matching_turns_the_key_red() -> None:
    """U3（失败）：明细全带 refs 且无一匹配 → 确定失败。

    这是**唯一由新增第三合取项单独把键转红**的路径：前两个合取项都为真。
    错包名取自已落盘 c09（`…/rag/…` vs 预登记 `…/service/…`）。
    """
    details = _t312rc_c02_mismatching_details()
    assert len(details) == 7, "冻结口径要的是**全部七条**，不是三条替身"
    row = _t312rc_row("c02", details)
    selection = row["evidence_selection"]

    assert row["mode_matches"] is True, "第一合取项为真"
    assert selection["retrieved_not_cited"] == [], "第二合取项为真（空真）"
    assert selection["never_retrieved_unreferenced"] == _t312rc_c02_paths()
    assert selection["never_retrieved_ref_gate"] is False

    assert _agg_rows([row])["gate_summary"]["contract_expectations_met"] is False


def test_t312rc_u4_unattributable_lands_in_none_and_blocks_the_gate() -> None:
    """U4（fail-closed 负例，对应「推不出①」）：refs 为空 → 判不了，**不算通过**。

    直接断言三处：键 `None`、所属分节 gate `None`、`all_hard_gates_passed is False`。
    """
    paths = _t312rc_c02_paths()
    landed = _t312rc_details_of(_T312RC_LANDED_0807, "c02")
    blank = next(dict(d) for d in landed if not (d.get("refs") or []))
    details = [_t312rc_c02_referencing_details()[0], blank]
    row = _t312rc_row("c02", details)
    selection = row["evidence_selection"]

    assert selection["never_retrieved_unattributable"] == paths[1:]
    assert selection["never_retrieved_unreferenced"] == [], "判不了不等于判失败"
    assert selection["never_retrieved_ref_gate"] is None

    report = _agg_rows([row])
    assert report["gate_summary"]["contract_expectations_met"] is None
    assert report["sections"]["final_consistency"]["gate"] is None
    assert report["all_hard_gates_passed"] is False


def test_t312rc_u5_duplicate_references_count_once_and_the_helper_is_pure() -> None:
    """U5（重复/重试）：同一路径被 4 条明细重复引用只计一次；helper 是纯函数。"""
    paths = _t312rc_c02_paths()
    base = _t312rc_c02_referencing_details()
    details = [base[0]] * 4 + base[1:]
    row = _t312rc_row("c02", details)
    selection = row["evidence_selection"]

    assert selection["never_retrieved"] == paths
    assert [item["ref_status"] for item in selection["items"]] == ["referenced"] * 3
    assert len(selection["never_retrieved_unreferenced"]) == 0
    assert len(paths) == 3, "重复引用不得把项数撑大"

    first = score_never_retrieved_refs(selection["items"], details)
    second = score_never_retrieved_refs(selection["items"], details)
    assert first == second


def test_t312rc_u6_unproducible_category_never_counts_as_a_reference() -> None:
    """U6（越权/非法状态）：`confirmed_undocumented` 在 P1.5 不得由代码产出。

    refs 匹配也不算结构性引用；`score_not_found` 的 `r3_forbidden_category`
    独立仍为 False——两套记账互不掩盖。
    """
    paths = _t312rc_c02_paths()
    base = _t312rc_c02_referencing_details()
    details = [{**base[0], "category": "confirmed_undocumented"}, *base[1:]]
    row = _t312rc_row("c02", details)
    selection = row["evidence_selection"]

    assert selection["items"][0]["ref_status"] == "unreferenced"
    assert paths[0] in selection["never_retrieved_unreferenced"]
    assert row["not_found"]["rules"]["r3_forbidden_category"] is False, "另一套账必须照样红"


def test_t312rc_u7a_landed_run_recomputed_through_the_production_helper() -> None:
    """U7a（组合不变量）：已落盘 20 行经**生产 helper** 复算，键值与落盘逐字相同。

    测试里不重写规则——只调 `score_never_retrieved_refs`，这正是 `PG-T312Rc-03`
    要求的「生产与测试共用一份规则」。
    """
    landed = _t312rc_landed(_T312RC_LANDED_0807)
    report, tally = _t312rc_recompute(landed)
    per_question = _t312rc_per_question_verdicts(landed)

    # `T312Rc-CR-01`ⓑ：只断言总数与最终值，逐题构成可以重新分布而测试照样绿。
    # 逐题值**由生产聚合路径逐行算出**（单行聚合时外层合取退化为该行本身），
    # 不在测试里复写 `_conjunction([前两项, 第三项])` 那条公式。
    assert per_question == {True: 5, None: 4, False: 4}, per_question

    assert set(report["gate_summary"]) == set(GATE_KEYS)
    assert len(GATE_KEYS) == 12
    assert CONTRACT_REPORT_SCHEMA_VERSION == "p1.5-contract-v2"
    assert dict(tally) == {"referenced": 8, "unattributable": 15}, "可判定失败必须为 0"
    assert report["gate_summary"]["contract_expectations_met"] is False, "与落盘值逐字相同"
    assert landed["aggregates"]["gate_summary"]["contract_expectations_met"] is False
    ids = [item["id"] for item in report["sections"]["final_consistency"]["contract_vacuous_pass"]]
    assert ids == ["c02", "c03", "c09", "c10", "c11"]


def test_t312rc_u7b_third_conjunct_can_only_tighten_never_loosen() -> None:
    """U7b（单调性）：第三合取项**不能**把 `False` 抬成 `True`。

    夹具是既有的 `_t312rb_c11_row(progress_retrieved=True)`：PROGRESS 已召回未引用，
    第二合取项为假；第三合取项无 `never_retrieved` 项故为真。最终必须仍是 False。
    """
    row = _t312rb_c11_row(mode="refusal", progress_retrieved=True)
    selection = row["evidence_selection"]

    assert row["mode_matches"] is True, "第一合取项：真"
    assert selection["retrieved_not_cited"] == ["PROGRESS.md"], "第二合取项：假"
    assert selection["never_retrieved_ref_gate"] is True, "第三合取项：真（无未召回项）"

    assert _agg_rows([row])["gate_summary"]["contract_expectations_met"] is False


def test_t312rc_u8_vacuous_candidate_line_never_reads_as_a_pass() -> None:
    """U8（诚实边界）：Markdown 里那一行不得读起来像"通过"。"""
    row = _t312rc_row("c02", _t312rc_c02_referencing_details())
    markdown = render_contract_markdown({"aggregates": _agg_rows([row])})

    line = [text for text in markdown.splitlines() if "空真候选" in text]
    assert len(line) == 1, markdown
    assert "未召回" in line[0]
    assert "c02" in line[0]
    for forbidden in ("通过", "达标", "合格", "无问题"):
        assert forbidden not in line[0], forbidden


def test_t312rc_u9a_structural_reference_binds_a_per_item_proposition_undecided() -> None:
    """U9a（fail-closed 负例，对应「推不出②」）：结构关联 ≠ 这句话在说这一项。

    refs 精确匹配但 text 与本题完全无关 → 结构层仍判 `referenced`，
    **同时**该项逐项带 `proposition_undecided=True`（绑定到具体必需项，
    不是拿 `len(details)` 的通用计数搪塞）。
    """
    details = [
        {**detail, "text": "关于本项目的吉祥物颜色，当前证据未覆盖。"}
        for detail in _t312rc_c02_referencing_details()
    ]
    row = _t312rc_row("c02", details)
    selection = row["evidence_selection"]

    assert [item["ref_status"] for item in selection["items"]] == ["referenced"] * 3
    assert all(item["proposition_undecided"] is True for item in selection["items"])
    assert selection["never_retrieved_ref_proposition_undecided"] == 3

    report = _agg_rows([row])
    assert (
        report["sections"]["evidence_selection"]["never_retrieved_ref_proposition_undecided"] == 3
    )


def test_t312rc_u9b_reference_makes_no_claim_about_path_existence() -> None:
    """U9b（fail-closed 负例，对应「推不出③」）：不校验 refs 里的路径是否真实存在。

    用 c12 的通配 pattern，refs 给一个匹配通配但不在索引内的路径 → 仍判
    `referenced`；输出键集合被冻结锁死，其中没有任何断言路径存在性的字段。
    """
    required = _t312rc_required_of(_T312RC_LANDED_0807, "c12")
    pattern = required[0]["path"]
    ghost = "backend/src/main/java/com/ragdocs/web/GhostController.java"

    # `T312Rc-CR-01`ⓒ：负例的前提必须机器化——光把变量取名 ghost 不算证明。
    # 冻结来源是落盘报告的 `corpus.manifest`（183 条 rel_path + content_hash），
    # 即该轮真实索引的语料清单。
    manifest = {
        entry["rel_path"] for entry in _t312rc_landed(_T312RC_LANDED_0807)["corpus"]["manifest"]
    }
    assert len(manifest) == 183, "换了语料就该在这里当场失败"
    assert ghost not in manifest, "负例前提：该路径确实不在索引内"
    assert match_required_path(ghost, pattern), "但它匹配 c12 的通配 pattern"
    assert sum(1 for path in manifest if match_required_path(path, pattern)) == 6, (
        "同一 pattern 在索引里本有 6 个真实 Controller——判定却分不出真假"
    )

    verdict = score_never_retrieved_refs(
        [{**required[0], "status": "never_retrieved", "span_hit": None}],
        [_t312rc_detail([ghost])],
    )

    assert verdict["referenced"] == [pattern]
    assert verdict["gate"] is True
    assert set(verdict) == _T312RC_HELPER_KEYS, "输出键集合冻结：不得混入存在性断言"
    assert set(verdict["items"][0]) == {"path", "ref_status", "proposition_undecided"}


def test_t312rc_u11_full_unmet_trips_both_named_keys_on_a_contract_row() -> None:
    """U11：`full` + 必需项全未召回 + 零 not_found 明细，**两个键同时红**。

    这条锁住上面 `_L1` 把 `zero_full_without_required_evidence` 的夹具由 c01
    改到 e01 之后留下的空隙：换夹具是为了让 L1 隔离性成立，不是掩盖重叠——
    重叠真实存在（该答案确实同时违反两条条款），在这里正面断言。
    """
    row = _scored(
        "c01",
        expected=("full",),
        required=[{"path": "OrderService.java", "type": "production_source", "symbol": None}],
        answer=_answer(
            answer_text=f"已依据 README.md 说明。[E1]{DISCLOSURE}",
            mode="full",
            citations=[_citation("README.md")],
            claims=[{"text": "c", "evidence_ids": ["E1"], "quotes": []}],
        ),
    )

    assert row["evidence_selection"]["never_retrieved_unreferenced"] == ["OrderService.java"]
    summary = _agg_rows([row])["gate_summary"]
    assert summary["zero_full_without_required_evidence"] is False
    assert summary["contract_expectations_met"] is False


@pytest.mark.parametrize(
    ("landed", "expected"),
    [
        (_T312RC_LANDED_0805, ["c03", "c09", "c10"]),
        (_T312RC_LANDED_0807, ["c02", "c03", "c09", "c10", "c11"]),
    ],
)
def test_t312rc_u10_vacuous_predicate_excludes_mixed_unmatched_and_zero_required(
    landed: Path, expected: list[str]
) -> None:
    """U10（排除负例，`PG-T312Rc-05`）：空真候选谓词不得过度点名。

    三类排除项在两轮都有真实实例：混合状态、mode 不匹配、零必需项。
    被测的是**生产产出**的 `contract_vacuous_pass`，不是测试里重算的谓词。
    """
    raw = _t312rc_landed(landed)
    recomputed, _ = _t312rc_recompute(raw)
    contract = [
        row
        for row in raw["aggregates"]["questions"]
        if row.get("kind") == "contract" and row.get("status") == "succeeded"
    ]
    picked = {
        item["id"] for item in recomputed["sections"]["final_consistency"]["contract_vacuous_pass"]
    }
    assert sorted(picked) == expected

    mixed = {
        row["id"]
        for row in contract
        if any(item["status"] == "never_retrieved" for item in row["evidence_selection"]["items"])
        and not all(
            item["status"] == "never_retrieved" for item in row["evidence_selection"]["items"]
        )
    }
    unmatched = {row["id"] for row in contract if not row["mode_matches"]}
    zero = {row["id"] for row in contract if row["evidence_selection"]["required_total"] == 0}
    assert mixed and zero, "两类排除项必须在这份报告里真实存在"
    assert picked.isdisjoint(mixed | unmatched | zero)
