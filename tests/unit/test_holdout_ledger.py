"""holdout 一次性访问台账（《Evaluation-v1》§7 "不得静默重跑挑最好成绩"）。

2026-07-20 二轮复评的三个边界：台账锚点必须与题集绑定（不能被 --output-dir 绕过）、
占号必须原子（并发进程不得都拿到 attempt=1）、空白裁决理由不构成用户裁决。
"""

from __future__ import annotations

import json
import multiprocessing as mp
from pathlib import Path
from typing import Any

import pytest

from devkb.errors import InvalidInputError
from devkb.evaluation import (
    HOLDOUT_LEDGER_NAME,
    append_holdout_ledger,
    claim_holdout_attempt,
    holdout_ledger_path,
    read_holdout_ledger,
)

CONCURRENT_CLAIMS = 8


def test_ledger_is_anchored_to_evalsets_not_report_output_dir(tmp_path: Path) -> None:
    """护栏锚点 = 冻结题集目录：换输出目录不能重开一本新台账。"""
    evalsets_dir = tmp_path / "evalsets"
    assert holdout_ledger_path(evalsets_dir) == evalsets_dir / HOLDOUT_LEDGER_NAME

    assert claim_holdout_attempt(evalsets_dir, acknowledge_rerun=None, output_dir="reports-a") == 1
    # 换一个 --output-dir 再来：读的仍是同一本台账，第二次必须被拒
    with pytest.raises(InvalidInputError, match="acknowledge-rerun"):
        claim_holdout_attempt(evalsets_dir, acknowledge_rerun=None, output_dir="reports-b")
    records = read_holdout_ledger(evalsets_dir)
    assert [r["event"] for r in records] == ["started"]
    assert records[0]["output_dir"] == "reports-a"  # 输出目录随尝试留痕


def test_blank_rerun_reason_is_not_an_adjudication(tmp_path: Path) -> None:
    """空白/纯空格理由不构成用户裁决（此前 "   " 可授权重跑）。"""
    evalsets_dir = tmp_path / "evalsets"
    assert claim_holdout_attempt(evalsets_dir, acknowledge_rerun=None) == 1
    for blank in ("", "   ", "\t\n"):
        with pytest.raises(InvalidInputError, match="acknowledge-rerun"):
            claim_holdout_attempt(evalsets_dir, acknowledge_rerun=blank)
    assert len(read_holdout_ledger(evalsets_dir)) == 1
    # 非空理由才放行，且理由随尝试落盘
    reason = "用户裁决：机制缺陷修复后重跑"
    assert claim_holdout_attempt(evalsets_dir, acknowledge_rerun=reason) == 2
    assert read_holdout_ledger(evalsets_dir)[1]["acknowledge_rerun"] == reason


def test_append_only_never_rewrites_existing_records(tmp_path: Path) -> None:
    evalsets_dir = tmp_path / "evalsets"
    claim_holdout_attempt(evalsets_dir, acknowledge_rerun=None)
    append_holdout_ledger(evalsets_dir, {"event": "failed", "attempt": 1, "error": "boom"})
    lines = holdout_ledger_path(evalsets_dir).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["event"] for line in lines] == ["started", "failed"]
    # 失败的尝试同样占号：下一次仍需裁决理由
    with pytest.raises(InvalidInputError):
        claim_holdout_attempt(evalsets_dir, acknowledge_rerun=None)


def _claim_worker(evalsets_dir: str, barrier: Any, results: Any) -> None:
    barrier.wait()  # 尽量把并发窗口压到最窄
    try:
        results.put(("claimed", claim_holdout_attempt(Path(evalsets_dir), acknowledge_rerun=None)))
    except InvalidInputError:
        results.put(("refused", None))
    except Exception as exc:  # pragma: no cover — 出现即测试失败
        results.put(("error", repr(exc)))


def test_concurrent_claims_yield_exactly_one_first_attempt(tmp_path: Path) -> None:
    """读台账与写 started 若不在同一把锁内，并发进程会都以 attempt=1 起跑。"""
    evalsets_dir = tmp_path / "evalsets"
    evalsets_dir.mkdir()
    context = mp.get_context("fork")
    barrier = context.Barrier(CONCURRENT_CLAIMS)
    results: Any = context.Queue()
    processes = [
        context.Process(target=_claim_worker, args=(str(evalsets_dir), barrier, results))
        for _ in range(CONCURRENT_CLAIMS)
    ]
    for process in processes:
        process.start()
    outcomes = [results.get(timeout=30) for _ in range(CONCURRENT_CLAIMS)]
    for process in processes:
        process.join(timeout=30)

    assert all(outcome[0] != "error" for outcome in outcomes), outcomes
    claimed = [attempt for status, attempt in outcomes if status == "claimed"]
    assert claimed == [1], f"应恰有一个进程拿到 attempt=1，实得 {claimed}"
    started = [r for r in read_holdout_ledger(evalsets_dir) if r["event"] == "started"]
    assert len(started) == 1 and started[0]["attempt"] == 1
