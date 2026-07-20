"""T10.4 P0 holdout 检索基线（脚本级，Evaluation-v0 §5：仅阶段验收时运行）。

对已摄取的 mini-mall 项目跑 holdout 检索 8 问的 Recall@5 / Recall@10 / MRR@10；
不可答 2 问记录 top-1 相似度备档（P0 无拒答策略，该分数是 P1 拒答阈值的基线观察）。
命中判定：chunk.rel_path == 锚点 rel_path 且锚点串是 chunk.title_path 的子串（大小写不敏感）。

运行（真实模型调用双开关，见 CLAUDE.md 环境事实）：
    env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY \
        HF_HUB_OFFLINE=1 uv run python benchmarks/holdout_baseline.py
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from devkb.config import get_settings
from devkb.db import create_engine, create_session_factory
from devkb.embedding import SentenceTransformerEmbedder
from devkb.repositories import ProjectRepo
from devkb.retrieval import RetrievedChunk, retrieve

PROJECT_SLUG = "mini-mall"
TOP_K = 10
ROOT = Path(__file__).resolve().parent.parent


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def hit_rank(results: list[RetrievedChunk], relevant: list[dict]) -> int | None:
    """返回首个命中锚点的名次（1 起），无命中返回 None。"""
    for rank, r in enumerate(results, start=1):
        for a in relevant:
            if r.rel_path == a["rel_path"] and a["anchor"].lower() in r.title_path.lower():
                return rank
    return None


async def main() -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    embedder = SentenceTransformerEmbedder(
        model_id=settings.embedding_model_id,
        device=settings.embedding_device,
        batch_size=settings.embedding_batch_size,
    )
    retrieval_qs = load_jsonl(ROOT / "evalsets/v0/retrieval_holdout.jsonl")
    unanswerable_qs = load_jsonl(ROOT / "evalsets/v0/unanswerable_holdout.jsonl")
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT
    ).stdout.strip()

    async with factory() as session:
        project = await ProjectRepo(session).get_by_slug(PROJECT_SLUG)
        assert project is not None, f"项目 {PROJECT_SLUG} 不存在，先运行 T10.2 摄取"

        ranks: dict[str, int | None] = {}
        for q in retrieval_qs:
            rs = await retrieve(session, project.id, q["question"], embedder=embedder, top_k=TOP_K)
            rank = hit_rank(rs, q["relevant"])
            ranks[q["id"]] = rank
            top = rs[0]
            print(
                f"{q['id']} rank={rank} top1={top.score:.3f} {top.rel_path} · {top.title_path[:60]}"
            )

        print("--- 不可答（记录 top-1 分数备档，P0 无拒答）")
        for q in unanswerable_qs:
            rs = await retrieve(session, project.id, q["question"], embedder=embedder, top_k=TOP_K)
            print(f"{q['id']} top1={rs[0].score:.3f} top3={[round(r.score, 3) for r in rs[:3]]}")

    await engine.dispose()

    n = len(ranks)
    recall5 = sum(1 for r in ranks.values() if r is not None and r <= 5) / n
    recall10 = sum(1 for r in ranks.values() if r is not None and r <= 10) / n
    mrr10 = sum(1 / r for r in ranks.values() if r is not None and r <= 10) / n
    print(f"--- commit={commit} n={n} top_k={TOP_K}")
    print(f"Recall@5={recall5:.3f} Recall@10={recall10:.3f} MRR@10={mrr10:.3f}")


if __name__ == "__main__":
    asyncio.run(main())
