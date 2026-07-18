"""T14.4 诊断（复评归档）：lexical 排序变体对 dev 17 题首命中名次的影响。

支撑 lexical-only 报告"低召回不是排序调参问题"的结论：在同一快照上对照
ts_rank_cd 归一化 flag（0/1/4/32）与查询侧丢弃单字 CJK token 的组合，
输出每题相关块在 top-100 内的名次与各变体的 R@5/R@10/MRR。

查询全部经 `ChunkRepo.lexical_search_diagnostic`（与正式 lexical_search
共用同一 plainto_tsquery + OR 构造，只开放两个实验参数，D7 收口不破）。
每题运行前先断言 base 变体与正式 `lexical_search` 的 top-100 逐行一致，
保证对照实验"只改变排名参数、其余口径相同"。

运行（无模型调用）：
    uv run python benchmarks/p1_lexical_variants_diag.py
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from devkb.config import get_settings
from devkb.db import create_engine, create_session_factory
from devkb.models import Chunk
from devkb.repositories import ChunkRepo, ProjectRepo

ROOT = Path(__file__).resolve().parent.parent
DEV_SET = ROOT / "evalsets/v0/retrieval_dev.jsonl"
TIMEZONE = ZoneInfo("Asia/Shanghai")
DIAG_TOP_K = 100

# 变体：ts_rank_cd 归一化 flag × 是否丢弃单字 CJK token（jieba 虚词如"的/是"）
VARIANTS: dict[str, tuple[int, bool]] = {
    "base(norm=0)": (0, False),
    "norm=1(log-len)": (1, False),
    "norm=4(harmonic)": (4, False),
    "norm=32(rank/(rank+1))": (32, False),
    "drop-1char-cjk": (0, True),
    "drop-1char-cjk+norm=1": (1, True),
    "drop-1char-cjk+norm=4": (4, True),
}


def load_questions() -> list[dict[str, Any]]:
    questions = [
        json.loads(line)
        for line in DEV_SET.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(questions) != 17:
        raise RuntimeError("必须是冻结的 17 个 retrieval dev 问题")
    return questions


def first_hit(rows: list[tuple[Chunk, str, float]], relevant: list[dict[str, str]]) -> int | None:
    for i, (chunk, rel_path, _score) in enumerate(rows, start=1):
        for anchor in relevant:
            if (
                rel_path == anchor["rel_path"]
                and anchor["anchor"].casefold() in chunk.title_path.casefold()
            ):
                return i
    return None


async def main() -> None:
    commit = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    print(f"# lexical 排序变体诊断 ｜ {datetime.now(TIMEZONE).isoformat(timespec='seconds')}")
    print(f"# devkb commit: {commit}")
    print("# 复现：uv run python benchmarks/p1_lexical_variants_diag.py")

    questions = load_questions()
    summary: dict[str, list[int | None]] = {name: [] for name in VARIANTS}
    engine = create_engine(get_settings().database_url)
    try:
        async with create_session_factory(engine)() as session:
            project = await ProjectRepo(session).get_by_slug("mini-mall")
            if project is None:
                raise RuntimeError("项目 mini-mall 不存在")
            repo = ChunkRepo(session, project.id)
            for question in questions:
                official = await repo.lexical_search(question["question"], DIAG_TOP_K)
                for name, (norm, nostop) in VARIANTS.items():
                    rows = await repo.lexical_search_diagnostic(
                        question["question"],
                        DIAG_TOP_K,
                        rank_normalization=norm,
                        drop_single_cjk=nostop,
                    )
                    if name == "base(norm=0)":
                        assert [(c.id, s) for c, _, s in rows] == [
                            (c.id, s) for c, _, s in official
                        ], f"base 变体必须与正式 lexical_search 逐行一致：{question['id']}"
                    summary[name].append(first_hit(rows, question["relevant"]))
            print(
                f"# base 一致性：17/17 题 base 变体与正式 lexical_search top-{DIAG_TOP_K} 逐行一致"
            )
    finally:
        await engine.dispose()

    ids = [q["id"] for q in questions]
    print(f"{'variant':24} " + " ".join(f"{i:>4}" for i in ids))
    for name, ranks in summary.items():
        cells = " ".join(f"{r if r is not None else '-':>4}" for r in ranks)
        n = len(ranks)
        r5 = sum(1 for r in ranks if r is not None and r <= 5) / n
        r10 = sum(1 for r in ranks if r is not None and r <= 10) / n
        mrr = sum(1 / r for r in ranks if r is not None and r <= 10) / n
        print(f"{name:24} {cells}  R@5={r5:.3f} R@10={r10:.3f} MRR@10={mrr:.3f}")
    print(f"# 名次为相关块在该变体 top-{DIAG_TOP_K} 内的位置，- 表示 top-{DIAG_TOP_K} 外")


asyncio.run(main())
