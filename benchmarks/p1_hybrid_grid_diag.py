"""T15.4 诊断：Hybrid Gate 失败后的每路候选数 (n_vector, n_lexical) 网格对照。

首轮官方 Gate 运行（evalsets/reports/p1-dev-hybrid-gate-20260718T122552+0800）
失败的根因：lexical channel 对中文自然语言题系统性噪声（lexical-only R@10=0.059），
per_channel=50 时"双路平庸块"的 RRF 共识分稳压"单路 rank=1"的相关块。

方法：每题用真实 Qwen 各抓一次 vector(hnsw)/lexical top-50 候选（与官方
hybrid channel 完全同口径——同 ChunkRepo 方法、同参数），然后离线对
(n_vector, n_lexical) 网格截断候选表并调用**官方 rrf_fuse**（k=60 冻结默认）
融合——截断即取前缀，融合语义与业务路径零差异，不存在诊断专用口径。

k 与 channel 权重不在 P1 规格自由度内（§8 冻结默认 k=60、无权重定义），
仅作为附录证据一并输出，供 Gate 失败时的偏差裁决参考，不据此改默认。

运行（真实模型双开关见 CLAUDE.md）：
    env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        -u all_proxy -u ALL_PROXY HF_HUB_OFFLINE=1 \
        uv run python benchmarks/p1_hybrid_grid_diag.py
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from p1_lexical_dev import calculate_metrics, load_dev_questions, question_group

from devkb.config import get_settings
from devkb.db import create_engine, create_session_factory
from devkb.embedding import SentenceTransformerEmbedder
from devkb.repositories import ChunkRepo, ProjectRepo
from devkb.retrieval import RRF_K_DEFAULT, ChannelRanking, rrf_fuse

ROOT = Path(__file__).resolve().parent.parent
RESULT_PATH = ROOT / "benchmarks/results/hybrid_grid_diag.txt"

TOP_K = 10
CAPTURE_N = 50
N_VECTOR_GRID = [5, 10, 20, 50]
N_LEXICAL_GRID = [1, 2, 3, 5, 10, 20, 50]
# 附录证据（规格外自由度，仅供偏差裁决）：k 变体在代表性 n 组合上的表现
APPENDIX_K_GRID = [10, 20, 120]


@dataclass(frozen=True)
class Candidate:
    chunk_id: str
    rel_path: str
    title_path: str


def hit_rank_of_ids(
    ordered: list[Candidate], relevant: list[dict[str, str]], top_k: int
) -> int | None:
    for rank, cand in enumerate(ordered[:top_k], start=1):
        for anchor in relevant:
            if (
                cand.rel_path == anchor["rel_path"]
                and anchor["anchor"].casefold() in cand.title_path.casefold()
            ):
                return rank
    return None


def fuse_case(
    captured: list[dict[str, Any]],
    n_vector: int,
    n_lexical: int,
    k: int,
) -> dict[str, Any]:
    """对每题截断候选表前缀后走官方 rrf_fuse，返回三 Gate 相关指标。"""
    ranks: list[int | None] = []
    vector_ranks: list[int | None] = []
    improved_ids: list[str] = []
    for item in captured:
        catalog: dict[str, Candidate] = item["catalog"]
        rankings = [
            ChannelRanking(item["question"], "vector", tuple(item["vector_ids"][:n_vector])),
            ChannelRanking(item["question"], "lexical", tuple(item["lexical_ids"][:n_lexical])),
        ]
        fused = rrf_fuse(rankings, k=k)[:TOP_K]
        ordered = [catalog[str(f.chunk_id)] for f in fused]
        rank = hit_rank_of_ids(ordered, item["relevant"], TOP_K)
        ranks.append(rank)
        vector_ranks.append(item["vector_exact_rank"])
        if item["group"] != "natural-language":
            baseline = item["vector_exact_rank"]
            if rank is not None and (baseline is None or rank < baseline):
                improved_ids.append(item["id"])
    metrics = calculate_metrics(ranks)
    vector_metrics = calculate_metrics(vector_ranks)
    recall_ok = metrics.recall_at_10 >= vector_metrics.recall_at_10
    mrr_delta = metrics.mrr_at_10 - vector_metrics.mrr_at_10
    mrr_ok = mrr_delta >= -0.02
    token_ok = bool(improved_ids)
    return {
        "n_vector": n_vector,
        "n_lexical": n_lexical,
        "k": k,
        "recall_at_5": metrics.recall_at_5,
        "recall_at_10": metrics.recall_at_10,
        "mrr_at_10": metrics.mrr_at_10,
        "mrr_delta": mrr_delta,
        "gates": (recall_ok, mrr_ok, token_ok),
        "gate_passed": recall_ok and mrr_ok and token_ok,
        "improved_ids": improved_ids,
        "per_question": ranks,
    }


async def capture() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    settings = get_settings()
    questions = load_dev_questions()
    engine = create_engine(settings.database_url)
    try:
        async with create_session_factory(engine)() as session:
            project = await ProjectRepo(session).get_by_slug("mini-mall")
            if project is None:
                raise RuntimeError("项目 mini-mall 不存在")
            repo = ChunkRepo(session, project.id)
            chunk_count = await repo.count()
            embedder = SentenceTransformerEmbedder(
                settings.embedding_model_id,
                device=settings.embedding_device,
                batch_size=settings.embedding_batch_size,
            )
            captured: list[dict[str, Any]] = []
            for question in questions:
                text = question["question"]
                embedding = embedder.embed_query(text)
                vector_rows = await repo.vector_search(embedding, CAPTURE_N, mode="hnsw")
                lexical_rows = await repo.lexical_search(text, CAPTURE_N)
                exact_rows = await repo.vector_search(embedding, TOP_K, mode="exact")
                catalog: dict[str, Candidate] = {}
                for chunk, rel_path, _ in [*vector_rows, *lexical_rows, *exact_rows]:
                    catalog.setdefault(
                        str(chunk.id), Candidate(str(chunk.id), rel_path, chunk.title_path)
                    )
                exact_ordered = [catalog[str(chunk.id)] for chunk, _, _ in exact_rows]
                captured.append(
                    {
                        "id": question["id"],
                        "question": text,
                        "group": question_group(text),
                        "relevant": question["relevant"],
                        "vector_ids": [str(chunk.id) for chunk, _, _ in vector_rows],
                        "lexical_ids": [str(chunk.id) for chunk, _, _ in lexical_rows],
                        "vector_exact_rank": hit_rank_of_ids(
                            exact_ordered, question["relevant"], TOP_K
                        ),
                        "catalog": catalog,
                    }
                )
    finally:
        await engine.dispose()
    return captured, {"chunk_count": chunk_count, "question_count": len(questions)}


def main() -> None:
    captured, meta = asyncio.run(capture())
    vector_metrics = calculate_metrics([item["vector_exact_rank"] for item in captured])
    lines: list[str] = [
        "T15.4 hybrid (n_vector, n_lexical) 网格诊断（官方 rrf_fuse 截断，k=60）",
        f"语料 chunks={meta['chunk_count']}，问题数={meta['question_count']}，"
        f"vector-exact 基线：R@10={vector_metrics.recall_at_10:.3f} "
        f"MRR@10={vector_metrics.mrr_at_10:.3f}",
        "",
        "n_vec | n_lex | R@5   | R@10  | MRR@10 | ΔMRR    | G1 G2 G3 | 过 | token 改善题",
        "------|-------|-------|-------|--------|---------|----------|----|-------------",
    ]
    passing: list[dict[str, Any]] = []
    for n_vector in N_VECTOR_GRID:
        for n_lexical in N_LEXICAL_GRID:
            row = fuse_case(captured, n_vector, n_lexical, RRF_K_DEFAULT)
            if row["gate_passed"]:
                passing.append(row)
            g1, g2, g3 = row["gates"]
            lines.append(
                f"{n_vector:5d} | {n_lexical:5d} | {row['recall_at_5']:.3f} | "
                f"{row['recall_at_10']:.3f} | {row['mrr_at_10']:.4f} | "
                f"{row['mrr_delta']:+.4f} | {'✓' if g1 else '✗'}  {'✓' if g2 else '✗'}  "
                f"{'✓' if g3 else '✗'}  | {'过' if row['gate_passed'] else '—'} | "
                f"{','.join(row['improved_ids']) or '-'}"
            )
    lines += [
        "",
        f"全 Gate 通过组合数：{len(passing)}",
        "",
        "附录（规格外自由度，仅供偏差裁决参考，不据此改默认）：k 变体 @ 代表性 n 组合",
    ]
    for k in APPENDIX_K_GRID:
        for n_vector, n_lexical in [(50, 50), (20, 5), (10, 3)]:
            row = fuse_case(captured, n_vector, n_lexical, k)
            lines.append(
                f"k={k:3d} n_vec={n_vector:2d} n_lex={n_lexical:2d}: "
                f"R@10={row['recall_at_10']:.3f} MRR={row['mrr_at_10']:.4f} "
                f"ΔMRR={row['mrr_delta']:+.4f} 过={'是' if row['gate_passed'] else '否'}"
            )
    lines += [
        "",
        "逐题名次（vector-exact 基线 → 若干代表性组合）：",
    ]
    representative = [(50, 50), (20, 5), (10, 3), (10, 1)]
    header = "ID    | v-exact | " + " | ".join(f"({v},{le})" for v, le in representative)
    lines += [header, "-" * len(header)]
    rep_rows = {
        pair: fuse_case(captured, pair[0], pair[1], RRF_K_DEFAULT) for pair in representative
    }
    for idx, item in enumerate(captured):
        cells = " | ".join(
            str(rep_rows[pair]["per_question"][idx] or "—").rjust(5) for pair in representative
        )
        lines.append(f"{item['id']} | {str(item['vector_exact_rank'] or '—').rjust(7)} | {cells}")
    output = "\n".join(lines) + "\n"
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(output, encoding="utf-8")
    print(output)
    print(f"已写入 {RESULT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
