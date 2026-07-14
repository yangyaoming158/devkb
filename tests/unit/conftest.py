"""unit 测试共享设施：golden 快照比对（快照 A 与快照 B 共用同一机制）。"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from devkb.ingest.markdown import MdChunk

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
CORPUS_MD_DIR = FIXTURES_DIR / "corpus_md"

GoldenCheck = Callable[[Path, list[dict[str, Any]]], None]


def chunks_to_jsonable(chunks: list[MdChunk]) -> list[dict[str, Any]]:
    return [
        {
            "ordinal": c.ordinal,
            "title_path": c.title_path,
            "content": c.content,
            "content_hash": c.content_hash,
            "token_count": c.token_count,
            "start_line": c.start_line,
            "end_line": c.end_line,
        }
        for c in chunks
    ]


@pytest.fixture
def golden_check() -> GoldenCheck:
    """比对（或以 DEVKB_UPDATE_GOLDEN=1 重新生成）golden 快照。"""

    def check(golden_path: Path, actual: list[dict[str, Any]]) -> None:
        if os.environ.get("DEVKB_UPDATE_GOLDEN") == "1":
            golden_path.parent.mkdir(parents=True, exist_ok=True)
            golden_path.write_text(
                json.dumps(actual, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        if not golden_path.exists():
            pytest.fail(
                f"golden 快照缺失：{golden_path}（用 DEVKB_UPDATE_GOLDEN=1 生成后人工审阅）"
            )
        expected = json.loads(golden_path.read_text(encoding="utf-8"))
        assert actual == expected, f"chunk 结构与 golden 快照不一致：{golden_path}"

    return check
