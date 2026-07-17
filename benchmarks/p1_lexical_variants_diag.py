"""T14.4 诊断（复评归档）：lexical 排序变体对 dev 17 题首命中名次的影响。

支撑 lexical-only 报告"低召回不是排序调参问题"的结论：在同一快照上对照
ts_rank_cd 归一化 flag（0/1/4/32）与查询侧丢弃单字 CJK token 的组合，
输出每题相关块在 top-100 内的名次与各变体的 R@5/R@10/MRR。

注意：本脚本为**只读诊断**，为了注入 ts_rank_cd 归一化参数（业务路径不
提供、也不应提供）使用了本地 SQL；业务查询构造仍全部收口在
src/devkb/repositories.py（D7 静态检查范围为 src/devkb，与集成测试中的
诊断 SQL 同一口径）。tsquery 文本由 tokenize_query 的受限字符集 token
构造并经参数绑定传入。

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

from sqlalchemy import text

from devkb.config import get_settings
from devkb.db import create_engine, create_session_factory
from devkb.fts import tokenize_query
from devkb.repositories import ProjectRepo

ROOT = Path(__file__).resolve().parent.parent
DEV_SET = ROOT / "evalsets/v0/retrieval_dev.jsonl"
TIMEZONE = ZoneInfo("Asia/Shanghai")

SQL = """
SELECT d.rel_path, c.title_path
FROM chunks c
JOIN documents d ON c.document_id = d.id,
     (SELECT CAST(:tsq_text AS tsquery) AS tsq) q
WHERE c.project_id = :pid AND d.status = 'active' AND c.search_tsv @@ q.tsq
ORDER BY ts_rank_cd(c.search_tsv, q.tsq, :norm) DESC, c.id
LIMIT 100
"""

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


def build_tsq(tokens: list[str]) -> str:
    """token（tokenize_query 受限字符集）→ OR tsquery 文本，经绑定参数传入 CAST。"""

    def quote(token: str) -> str:
        return "'" + token.replace("'", "''") + "'"

    return " | ".join(quote(token) for token in tokens)


def drop_single_cjk(tokens: list[str]) -> list[str]:
    return [t for t in tokens if not (len(t) == 1 and not t.isascii())]


def first_hit(rows: list[Any], relevant: list[dict[str, str]]) -> int | None:
    for i, (rel_path, title_path) in enumerate(rows, start=1):
        for anchor in relevant:
            if (
                rel_path == anchor["rel_path"]
                and anchor["anchor"].casefold() in title_path.casefold()
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
            for question in questions:
                tokens = tokenize_query(question["question"])
                for name, (norm, nostop) in VARIANTS.items():
                    toks = drop_single_cjk(tokens) if nostop else tokens
                    if not toks:
                        summary[name].append(None)
                        continue
                    rows = (
                        await session.execute(
                            text(SQL),
                            {"tsq_text": build_tsq(toks), "pid": project.id, "norm": norm},
                        )
                    ).all()
                    summary[name].append(first_hit(list(rows), question["relevant"]))
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
    print("# 名次为相关块在该变体 top-100 内的位置，- 表示 top-100 外")


asyncio.run(main())
