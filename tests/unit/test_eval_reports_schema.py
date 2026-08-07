"""T20.3 已提交评测报告校验（《Evaluation-v1》§6 eval-ci 第 5 项）。

解析 evalsets/reports/ 全部 JSON：schema、commit、配置与 Gate 字段完整，
JSON/Markdown 成对提交；dev 报告绝不带 holdout 访问标记。
Gate 键集合按 schema 版本精确锁定：p1-eval-v1 是 T20.4 期间的历史 schema
（citation Gate 字段名经 2026-07-19 裁决更名，报告不改写，按文件名冻结），
新报告一律由当前 harness 以 p1-eval-v1.1 产出，形状与本文件常量互锁。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from devkb.eval_contract import CONTRACT_REPORT_SCHEMA_VERSION
from devkb.eval_contract import GATE_KEYS as CONTRACT_GATE_KEYS
from devkb.evaluation import (
    P0_HOLDOUT_BASELINE,
    REPORT_SCHEMA_VERSION,
    aggregate_agentic,
    p0_baseline_comparison,
    retrieval_gates,
)

REPORTS_DIR = Path(__file__).parents[2] / "evalsets" / "reports"
COMMIT_RE = re.compile(r"[0-9a-f]{40}")

# --- p1-eval-v1（历史，冻结）：只允许下列已提交报告，Gate 键集合精确锁定 ---
LEGACY_V1_REPORTS = {
    "p1-dev-retrieval-20260719T224424+0800.json",
    "p1-dev-agentic-20260719T224424+0800.json",  # 裁决前口径（citation_proxy_gate）
    "p1-dev-agentic-20260719T230855+0800.json",  # 裁决后口径（…meets_frozen_threshold）
}
_AGENTIC_COMMON_KEYS = frozenset(
    {
        "correct_unanswerable",
        "correct_unanswerable_gate",
        "false_refusals",
        "false_refusal_gate",
        "l0_pass",
        "l1_pass",
        "citation_proxy",
        "within_budget",
        "terminal_complete",
        "gate_passed",
    }
)
V1_AGENTIC_GATE_SHAPES = (
    _AGENTIC_COMMON_KEYS | {"citation_proxy_gate"},
    _AGENTIC_COMMON_KEYS | {"citation_proxy_meets_frozen_threshold"},
)
V1_RETRIEVAL_GATE_KEYS = frozenset(
    {
        "note",
        "record_recall_delta",
        "record_mrr_delta",
        "record_token_question_improved",
        "hnsw_overlap_mean",
        "hnsw_overlap_gate",
        "gate_passed",
    }
)

# --- p1-eval-v1.1（当前）：split-aware 固定键集合 ---
V1_1_AGENTIC_GATE_KEYS = _AGENTIC_COMMON_KEYS | {
    "note",
    "citation_proxy_meets_frozen_threshold",
    "no_failed_runs",
}
V1_1_P0_BASELINE_KEYS = frozenset(
    {"baseline", "current_vector_exact", "delta_vs_p0", "corpus_scale", "note"}
)
# §7 逐题原始结果：成功行必须带完整 Answer 原文（不可答题的"理由"即在其中）
V1_1_ANSWER_KEYS = frozenset(
    {"answer_text", "claims", "citations", "not_found", "limitations", "mode", "trace_summary"}
)
V1_1_RETRIEVAL_DEV_KEYS = V1_RETRIEVAL_GATE_KEYS
V1_1_RETRIEVAL_HOLDOUT_KEYS = frozenset(
    {
        "note",
        "record_recall_delta",
        "record_mrr_delta",
        "record_token_question_improved",
        "hnsw_overlap_mean",
        "hnsw_recall_at_10",
        "exact_recall_at_10",
        "hnsw_recall_ge_exact",
        "gate_passed",
    }
)


def _load_reports() -> list[tuple[Path, dict[str, Any]]]:
    json_files = sorted(REPORTS_DIR.glob("*.json"))
    assert json_files, "evalsets/reports 下没有任何 JSON 报告"
    return [(path, json.loads(path.read_text(encoding="utf-8"))) for path in json_files]


def test_committed_reports_have_schema_commit_config_and_markdown_pair() -> None:
    for path, report in _load_reports():
        assert report.get("schema_version"), f"{path.name} 缺 schema_version"
        run = report["run"]
        assert COMMIT_RE.fullmatch(run["devkb_commit"]), f"{path.name} commit 非法"
        assert run.get("started_at") and run.get("command"), f"{path.name} 缺运行元数据"
        assert report.get("config") and report.get("corpus"), f"{path.name} 缺配置/语料快照"
        assert path.with_suffix(".md").is_file(), f"{path.name} 缺同名 Markdown 摘要"


# holdout 报告的文件名前缀。**必须是元组**：《Evaluation-v1.5》§8 冻结的 P1.5 文件名是
# `p1.5-holdout-<ts>.json`，它不以 `p1-holdout` 开头——只判 `p1-holdout` 会把它当成 dev
# 报告，其 `holdout_accessed=True` 当场断言失败（T31.1 拆除，原本会炸在 T32.3）
HOLDOUT_PREFIXES = ("p1-holdout", "p1.5-holdout")

# mode_counts 形状按 schema 冻结（T31.1 bump 到 v1.2 的唯一原因）
LEGACY_MODE_COUNT_KEYS = frozenset({"full", "partial", "refusal"})
V1_2_MODE_COUNT_KEYS = LEGACY_MODE_COUNT_KEYS | {"policy_refusal"}

# --- p1.5-contract-v*（T31.1 契约 harness）：五分节 + 具名硬键 ---
#
# 键集合**按版本分派**：v1 是 13 键的历史快照（已落盘报告永不重写），v2 起
# `zero_absence_assertion_on_known_paths` 降为报告项（T31.2R-b）故为 12 键。
# 同一版本号不能同时表示两种键集合——这正是当时必须升版本的原因。
P15_SECTION_KEYS = frozenset(
    {
        "retrieval",
        "evidence_selection",
        "claim_support",
        "final_consistency",
        "safety_reliability",
    }
)
P15_FORBIDDEN_AGGREGATE_KEYS = frozenset(
    {"score", "total_score", "overall", "average", "mean_score"}
)


P15_V1_GATE_KEYS = frozenset(
    {
        "retrieval_no_regression",
        "zero_full_without_required_evidence",
        "l0_l1_all_pass",
        "contract_expectations_met",
        "extended_expectations_met",
        # v1 独有：T31.2R-b 起降为报告项，不再是硬键
        "zero_absence_assertion_on_known_paths",
        "consistency_all_true",
        "zero_full_refusal_with_direct_evidence",
        "global_negation_honest",
        "uningested_disclosed",
        "policy_terminal_correct",
        "budget_and_terminal",
        "fail_fast_isolation_ok",
    }
)
P15_GATE_KEYS_BY_SCHEMA: dict[str, frozenset[str]] = {
    "p1.5-contract-v1": P15_V1_GATE_KEYS,
    "p1.5-contract-v2": frozenset(CONTRACT_GATE_KEYS),
}


def _assert_p15_contract_shape(path: Path, report: dict[str, Any]) -> None:
    """五节齐备、该版本的具名键齐备、零跨节总分——报告落盘后依旧成立。"""
    aggregates = report["aggregates"]
    schema = report["schema_version"]
    # 未登记的 v* 版本**当场失败**，不得静默跳过校验
    assert schema in P15_GATE_KEYS_BY_SCHEMA, f"{path.name} 的 {schema} 未登记键集合"
    assert set(aggregates["sections"]) == P15_SECTION_KEYS, path.name
    assert set(aggregates["gate_summary"]) == P15_GATE_KEYS_BY_SCHEMA[schema], path.name
    assert isinstance(aggregates["all_hard_gates_passed"], bool), path.name
    assert P15_FORBIDDEN_AGGREGATE_KEYS.isdisjoint(set(aggregates)), path.name
    assert report["config"].get("prompt_version"), path.name
    assert report["corpus"].get("sha256"), path.name
    for section in aggregates["sections"].values():
        assert "gate" in section, path.name


def _is_holdout_report(name: str) -> bool:
    return name.startswith(HOLDOUT_PREFIXES)


def test_dev_reports_never_touch_holdout() -> None:
    for path, report in _load_reports():
        if not _is_holdout_report(path.name):
            assert not report["run"].get("holdout_accessed"), (
                f"{path.name} 是 dev 报告却标记访问了 holdout"
            )


def test_holdout_prefix_recognises_both_phases_and_still_rejects_dev_reports() -> None:
    """合成文件名双向锁：仓库里还没有 p1.5 报告，只靠上面那条测试改分支可以完全不执行。"""
    assert _is_holdout_report("p1-holdout-20260721T223756+0800.json")
    assert _is_holdout_report("p1.5-holdout-20260803T101010+0800.json")
    assert not _is_holdout_report("p1.5-dev-contract-20260803T101010+0800.json")
    assert not _is_holdout_report("p1-dev-agentic-20260719T224424+0800.json")


def test_gate_reports_carry_complete_locked_gate_fields() -> None:
    for path, report in _load_reports():
        schema = report["schema_version"]
        if schema == "p1-dev-hybrid-gate-v1":
            assert isinstance(report["gates"].get("gate_passed"), bool), path.name
        if schema.startswith("p1.5-contract-v"):
            _assert_p15_contract_shape(path, report)
            continue
        if schema not in ("p1-eval-v1", "p1-eval-v1.1", "p1-eval-v1.2"):
            continue
        assert report["corpus"].get("sha256") and report["corpus"].get("manifest"), path.name
        assert report["config"].get("prompt_version"), path.name
        gates = report.get("gates") or {}
        retrieval, agentic = gates.get("retrieval"), gates.get("agentic")
        assert retrieval is not None or agentic is not None, f"{path.name} 无任何 Gate 字段"
        if schema == "p1-eval-v1":
            assert path.name in LEGACY_V1_REPORTS, (
                f"{path.name} 使用已冻结的历史 schema p1-eval-v1：新报告必须为 v1.2"
            )
            if retrieval is not None:
                assert set(retrieval) == V1_RETRIEVAL_GATE_KEYS, path.name
            if agentic is not None:
                assert set(agentic) in [set(s) for s in V1_AGENTIC_GATE_SHAPES], path.name
            continue
        holdout = _is_holdout_report(path.name)
        # mode_counts 形状随 schema 冻结：v1/v1.1 是三键，v1.2 起补 policy_refusal
        counts = (report.get("agentic") or {}).get("aggregates", {}).get("mode_counts")
        if counts is not None:
            want = V1_2_MODE_COUNT_KEYS if schema == "p1-eval-v1.2" else LEGACY_MODE_COUNT_KEYS
            assert set(counts) == want, path.name
        if retrieval is not None:
            expected = V1_1_RETRIEVAL_HOLDOUT_KEYS if holdout else V1_1_RETRIEVAL_DEV_KEYS
            assert set(retrieval) == expected, path.name
        if agentic is not None:
            assert set(agentic) == V1_1_AGENTIC_GATE_KEYS, path.name
            if holdout:
                # §7：holdout 的拒答/误拒不是硬 Gate，只记录
                assert agentic["correct_unanswerable_gate"] is None, path.name
                assert agentic["false_refusal_gate"] is None, path.name
        for row in (report.get("agentic") or {}).get("questions", []):
            if row.get("status") == "succeeded":
                assert set(row.get("answer") or {}) >= V1_1_ANSWER_KEYS, (
                    f"{path.name} 的 {row['id']} 缺完整原始 Answer（§7 逐题原始结果）"
                )
        if holdout:
            # §7 第 3 条：P0 历史基线对照与语料规模变化声明必须在报告内
            baseline = report.get("p0_baseline")
            assert baseline is not None, f"{path.name} 缺 P0 历史基线对照"
            assert set(baseline) == V1_1_P0_BASELINE_KEYS, path.name
            assert baseline["baseline"] == P0_HOLDOUT_BASELINE, path.name
            assert "corpus_scale" in baseline and baseline["corpus_scale"]["p0"], path.name
            assert report["run"].get("holdout_attempt"), f"{path.name} 缺一次性访问序号"


def test_current_harness_emits_exactly_the_locked_v1_2_shapes() -> None:
    """常量与 harness 双向互锁：任何 Gate 键集合改动必须显式改本文件并 bump schema。"""
    assert REPORT_SCHEMA_VERSION == "p1-eval-v1.2"
    row: dict[str, Any] = {
        "id": "q",
        "answerable": True,
        "status": "succeeded",
        "mode": "full",
        "latency_ms": 1.0,
        "run_terminal": True,
        "within_budget": True,
    }
    empty_retrieval: dict[str, Any] = {"metrics": {}, "overlap_exact_hnsw": None, "questions": []}
    for split in ("dev", "holdout"):
        assert set(aggregate_agentic([row], split=split)["gates"]) == V1_1_AGENTIC_GATE_KEYS
    assert set(retrieval_gates(empty_retrieval, split="dev")) == V1_1_RETRIEVAL_DEV_KEYS
    assert set(retrieval_gates(empty_retrieval, split="holdout")) == V1_1_RETRIEVAL_HOLDOUT_KEYS
    corpus = {"document_count": 445, "chunk_count": 5000}
    assert set(p0_baseline_comparison(empty_retrieval, corpus)) == V1_1_P0_BASELINE_KEYS
    assert set(aggregate_agentic([row], split="dev")["mode_counts"]) == V1_2_MODE_COUNT_KEYS
    assert CONTRACT_REPORT_SCHEMA_VERSION == "p1.5-contract-v2"
    # 12 = §5.1 九条（1 拆 1/1'、4 拆 4a/4b，**第 2 条已降为报告项**）落 10 键
    # + §7/§14 的 L0/L1 + §5.2 第 8 条的 fail-fast
    assert len(CONTRACT_GATE_KEYS) == 12
    assert "fail_fast_isolation_ok" in CONTRACT_GATE_KEYS
    assert "zero_absence_assertion_on_known_paths" not in CONTRACT_GATE_KEYS
    # 已落盘的 v1 报告仍必须可校验：v1 的键集合是历史快照，不随 GATE_KEYS 变
    assert P15_GATE_KEYS_BY_SCHEMA["p1.5-contract-v1"] - set(CONTRACT_GATE_KEYS) == {
        "zero_absence_assertion_on_known_paths"
    }
