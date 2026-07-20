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


def test_dev_reports_never_touch_holdout() -> None:
    for path, report in _load_reports():
        if not path.name.startswith("p1-holdout"):
            assert not report["run"].get("holdout_accessed"), (
                f"{path.name} 是 dev 报告却标记访问了 holdout"
            )


def test_gate_reports_carry_complete_locked_gate_fields() -> None:
    for path, report in _load_reports():
        schema = report["schema_version"]
        if schema == "p1-dev-hybrid-gate-v1":
            assert isinstance(report["gates"].get("gate_passed"), bool), path.name
        if schema not in ("p1-eval-v1", "p1-eval-v1.1"):
            continue
        assert report["corpus"].get("sha256") and report["corpus"].get("manifest"), path.name
        assert report["config"].get("prompt_version"), path.name
        gates = report.get("gates") or {}
        retrieval, agentic = gates.get("retrieval"), gates.get("agentic")
        assert retrieval is not None or agentic is not None, f"{path.name} 无任何 Gate 字段"
        if schema == "p1-eval-v1":
            assert path.name in LEGACY_V1_REPORTS, (
                f"{path.name} 使用已冻结的历史 schema p1-eval-v1：新报告必须为 v1.1"
            )
            if retrieval is not None:
                assert set(retrieval) == V1_RETRIEVAL_GATE_KEYS, path.name
            if agentic is not None:
                assert set(agentic) in [set(s) for s in V1_AGENTIC_GATE_SHAPES], path.name
            continue
        holdout = path.name.startswith("p1-holdout")
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


def test_current_harness_emits_exactly_the_locked_v1_1_shapes() -> None:
    """常量与 harness 双向互锁：任何 Gate 键集合改动必须显式改本文件并 bump schema。"""
    assert REPORT_SCHEMA_VERSION == "p1-eval-v1.1"
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
