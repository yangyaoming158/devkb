"""T20.3 已提交评测报告校验（《Evaluation-v1》§6 eval-ci 第 5 项）。

解析 evalsets/reports/ 全部 JSON：schema、commit、配置与 Gate 字段完整，
JSON/Markdown 成对提交；dev 报告绝不带 holdout 访问标记。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

REPORTS_DIR = Path(__file__).parents[2] / "evalsets" / "reports"
COMMIT_RE = re.compile(r"[0-9a-f]{40}")


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


def test_gate_reports_carry_complete_gate_fields() -> None:
    for path, report in _load_reports():
        schema = report["schema_version"]
        if schema == "p1-dev-hybrid-gate-v1":
            assert isinstance(report["gates"].get("gate_passed"), bool), path.name
        if schema == "p1-eval-v1":
            assert report["corpus"].get("sha256") and report["corpus"].get("manifest"), path.name
            assert report["config"].get("prompt_version"), path.name
            gates = report.get("gates") or {}
            assert gates.get("retrieval") is not None or gates.get("agentic") is not None, (
                f"{path.name} 无任何 Gate 字段"
            )
