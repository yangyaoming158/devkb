"""T15.4：同一快照上 vector-exact / lexical / hybrid-rrf 三模式对照与 dev Gate。

Evaluation v1 §5.1 预冻结 Gate（第 4 条 HNSW overlap 已由 T15.3 报告覆盖）：
1. hybrid-rrf Recall@10 >= vector-exact Recall@10；
2. hybrid-rrf MRR@10 不得比 vector-exact 下降超过 0.02；
3. 至少一个预先归类的标识符/路径/数字 token 问题，其首命中名次严格优于
   vector-exact（归类规则沿用 T14.4 预声明的机械正则，非事后挑选）；
5. 三模式在同一 chunk 快照、同一 query 上运行（单次脚本运行保证），
   不改任何 relevant 标注。

Gate 失败时报告如实落盘 gate_passed=false，不得据此改标注或隐瞒。

运行（真实模型双开关见 CLAUDE.md）：
    env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        -u all_proxy -u ALL_PROXY HF_HUB_OFFLINE=1 \
        uv run python benchmarks/p1_hybrid_dev_gate.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from p1_lexical_dev import (
    calculate_metrics,
    corpus_hash,
    load_dev_questions,
    question_group,
    validate_anchor_coverage,
)

from devkb.config import get_settings
from devkb.db import create_engine, create_session_factory
from devkb.embedding import MAX_SEQ_LENGTH, SentenceTransformerEmbedder
from devkb.repositories import ChunkRepo, DocumentRepo, ProjectRepo
from devkb.retrieval import (
    HNSW_EF_SEARCH,
    MAX_CHANNEL_CANDIDATES,
    RRF_K_DEFAULT,
    hybrid_retrieve,
    lexical_retrieve,
    retrieve,
)

ROOT = Path(__file__).resolve().parent.parent
DEV_SET = ROOT / "evalsets/v0/retrieval_dev.jsonl"
DEFAULT_OUTPUT_DIR = ROOT / "evalsets/reports"
TIMEZONE = ZoneInfo("Asia/Shanghai")

TOP_K = 10
MRR_MAX_DROP = 0.02
MODES = ["vector-exact", "lexical", "hybrid-rrf"]


def hit_rank(results: list[Any], relevant: list[dict[str, str]]) -> int | None:
    """三模式统一命中口径：rel_path + title_path anchor（与 T11.1/T14.4 一致）。"""
    for rank, result in enumerate(results, start=1):
        for anchor in relevant:
            if (
                result.rel_path == anchor["rel_path"]
                and anchor["anchor"].casefold() in result.title_path.casefold()
            ):
                return rank
    return None


def rank_improved(candidate: int | None, baseline: int | None) -> bool:
    """首命中名次严格优于 baseline：未命中视为无穷大。"""
    if candidate is None:
        return False
    return baseline is None or candidate < baseline


def fmt_rank(rank: int | None) -> str:
    return str(rank) if rank is not None else "未命中"


def render_markdown(report: dict[str, Any]) -> str:
    corpus = report["corpus"]
    gates = report["gates"]
    metrics = report["metrics"]
    metric_rows = [
        f"| {mode} | {m['recall_at_5']:.3f} | {m['recall_at_10']:.3f} | {m['mrr_at_10']:.3f} |"
        for mode, m in metrics.items()
    ]
    question_rows = []
    for item in report["questions"]:
        question_rows.append(
            f"| {item['id']} | {item['group']} | {fmt_rank(item['ranks']['vector-exact'])} | "
            f"{fmt_rank(item['ranks']['lexical'])} | {fmt_rank(item['ranks']['hybrid-rrf'])} |"
        )
    improved = [
        f"`{q['id']}`（vector-exact {fmt_rank(q['ranks']['vector-exact'])} → "
        f"hybrid-rrf {fmt_rank(q['ranks']['hybrid-rrf'])}）"
        for q in report["questions"]
        if q["token_question_improved"]
    ]
    return "\n".join(
        [
            "# P1 Hybrid dev Gate：vector-exact / lexical / hybrid-rrf 同快照对照（T15.4）",
            "",
            f"> 运行：{report['run']['started_at']} ｜ devkb commit："
            f"`{report['run']['devkb_commit']}`",
            f"> 语料：P1 全量 {corpus['document_count']} 文档 / "
            f"{corpus['chunk_count']} chunks ｜ corpus sha256：`{corpus['sha256']}`",
            f"> hybrid 配置：RRF k={report['config']['rrf_k']}，每路候选 "
            f"{report['config']['per_channel_n']}，vector channel=hnsw"
            f"（ef_search={report['config']['ef_search']}），top_k={TOP_K}",
            "",
            "## Gate 结果",
            "",
            f"**总判定：{'通过' if gates['gate_passed'] else '未通过'}**",
            "",
            "1. hybrid R@10 ≥ vector-exact R@10：**"
            f"{'过' if gates['recall_not_worse'] else '未过'}**"
            f"（{metrics['hybrid-rrf']['recall_at_10']:.3f} vs "
            f"{metrics['vector-exact']['recall_at_10']:.3f}）",
            f"2. hybrid MRR@10 降幅 ≤ {MRR_MAX_DROP}：**"
            f"{'过' if gates['mrr_drop_acceptable'] else '未过'}**"
            f"（{metrics['hybrid-rrf']['mrr_at_10']:.3f} vs "
            f"{metrics['vector-exact']['mrr_at_10']:.3f}，"
            f"差 {gates['mrr_delta']:+.3f}）",
            f"3. 预归类 token 题首命中改善：**"
            f"{'过' if gates['token_question_improved'] else '未过'}**"
            + ("——" + "、".join(improved) if improved else "（无改善题）"),
            "4. HNSW overlap ≥ 0.95：已由 T15.3 报告覆盖"
            f"（{report['config']['hnsw_overlap_report']}，平均 1.0）",
            "5. 三模式同快照同 query 单次运行；relevant 标注零改动。",
            "",
            "## 三模式指标（同一快照）",
            "",
            "| mode | Recall@5 | Recall@10 | MRR@10 |",
            "|---|---:|---:|---:|",
            *metric_rows,
            "",
            "## 逐题首命中名次",
            "",
            "| ID | 组 | vector-exact | lexical | hybrid-rrf |",
            "|---|---|---:|---:|---:|",
            *question_rows,
            "",
            "每题三模式完整 top-10（含 hybrid 的 vector_rank/lexical_rank/fused_score）"
            "见同名 JSON 原始报告。",
            "",
            "## 复现命令",
            "",
            "```bash",
            report["run"]["command"],
            "```",
            "",
        ]
    )


async def run(args: argparse.Namespace) -> tuple[Path, Path]:
    settings = get_settings()
    questions = load_dev_questions()
    engine = create_engine(settings.database_url)
    try:
        async with create_session_factory(engine)() as session:
            project = await ProjectRepo(session).get_by_slug(args.project)
            if project is None:
                raise RuntimeError(f"项目 {args.project!r} 不存在，请先完成 T13.5 全量摄取")
            documents = await DocumentRepo(session, project.id).list_active()
            chunk_repo = ChunkRepo(session, project.id)
            chunk_count = await chunk_repo.count()
            anchors = await chunk_repo.list_reference_anchors()
            missing = validate_anchor_coverage(questions, anchors)
            if missing:
                raise RuntimeError(
                    "当前语料快照无法解析全部 dev 标注，停止评测："
                    + json.dumps(missing, ensure_ascii=False)
                )

            embedder = SentenceTransformerEmbedder(
                settings.embedding_model_id,
                device=settings.embedding_device,
                batch_size=settings.embedding_batch_size,
            )
            question_rows: list[dict[str, Any]] = []
            ranks: dict[str, list[int | None]] = {mode: [] for mode in MODES}
            for question in questions:
                text = question["question"]
                vector_hits = await retrieve(
                    session, project.id, text, embedder=embedder, top_k=TOP_K
                )
                lexical_hits = await lexical_retrieve(session, project.id, text, top_k=TOP_K)
                hybrid_hits = await hybrid_retrieve(
                    session,
                    project.id,
                    [text],
                    embedder=embedder,
                    top_k=TOP_K,
                    vector_mode="hnsw",
                    ef_search=HNSW_EF_SEARCH,
                )
                mode_results: dict[str, list[Any]] = {
                    "vector-exact": vector_hits,
                    "lexical": lexical_hits,
                    "hybrid-rrf": hybrid_hits,
                }
                mode_ranks = {
                    mode: hit_rank(results, question["relevant"])
                    for mode, results in mode_results.items()
                }
                for mode in MODES:
                    ranks[mode].append(mode_ranks[mode])
                group = question_group(text)
                question_rows.append(
                    {
                        "id": question["id"],
                        "question": text,
                        "group": group,
                        "relevant": question["relevant"],
                        "ranks": mode_ranks,
                        "token_question_improved": group != "natural-language"
                        and rank_improved(mode_ranks["hybrid-rrf"], mode_ranks["vector-exact"]),
                        "results": {
                            "vector-exact": [
                                {
                                    "rank": i,
                                    "chunk_id": str(h.chunk_id),
                                    "rel_path": h.rel_path,
                                    "title_path": h.title_path,
                                    "score": h.score,
                                }
                                for i, h in enumerate(vector_hits, start=1)
                            ],
                            "lexical": [
                                {
                                    "rank": h.lexical_rank,
                                    "chunk_id": str(h.chunk_id),
                                    "rel_path": h.rel_path,
                                    "title_path": h.title_path,
                                    "score": h.score,
                                }
                                for h in lexical_hits
                            ],
                            "hybrid-rrf": [
                                {
                                    "rank": i,
                                    "chunk_id": str(h.chunk_id),
                                    "rel_path": h.rel_path,
                                    "title_path": h.title_path,
                                    "fused_score": h.fused_score,
                                    "vector_rank": h.vector_rank,
                                    "lexical_rank": h.lexical_rank,
                                }
                                for i, h in enumerate(hybrid_hits, start=1)
                            ],
                        },
                    }
                )
    finally:
        await engine.dispose()

    metrics = {mode: asdict(calculate_metrics(ranks[mode])) for mode in MODES}
    recall_not_worse = (
        metrics["hybrid-rrf"]["recall_at_10"] >= metrics["vector-exact"]["recall_at_10"]
    )
    mrr_delta = metrics["hybrid-rrf"]["mrr_at_10"] - metrics["vector-exact"]["mrr_at_10"]
    mrr_drop_acceptable = mrr_delta >= -MRR_MAX_DROP
    token_improved = any(q["token_question_improved"] for q in question_rows)
    gates = {
        "recall_not_worse": recall_not_worse,
        "mrr_delta": mrr_delta,
        "mrr_drop_acceptable": mrr_drop_acceptable,
        "token_question_improved": token_improved,
        "gate_passed": recall_not_worse and mrr_drop_acceptable and token_improved,
    }

    now = datetime.now(TIMEZONE)
    document_snapshot = sorted((doc.rel_path, doc.content_hash) for doc in documents)
    command = (
        "env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY "
        "-u all_proxy -u ALL_PROXY HF_HUB_OFFLINE=1 "
        "uv run python benchmarks/p1_hybrid_dev_gate.py"
    )
    report = {
        "schema_version": "p1-dev-hybrid-gate-v1",
        "run": {
            "started_at": now.isoformat(timespec="seconds"),
            "devkb_commit": subprocess.run(
                ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            "devkb_worktree_dirty": bool(
                subprocess.run(
                    ["git", "-C", str(ROOT), "status", "--short"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            ),
            "command": command,
            "holdout_accessed": False,
        },
        "corpus": {
            "scope": "p1-445-full",
            "project": args.project,
            "document_count": len(document_snapshot),
            "chunk_count": chunk_count,
            "sha256": corpus_hash(document_snapshot),
        },
        "dataset": {"path": str(DEV_SET.relative_to(ROOT)), "question_count": len(questions)},
        "config": {
            "modes": MODES,
            "top_k": TOP_K,
            "rrf_k": RRF_K_DEFAULT,
            "per_channel_n": MAX_CHANNEL_CANDIDATES,
            "vector_mode": "hnsw",
            "ef_search": HNSW_EF_SEARCH,
            "mrr_max_drop": MRR_MAX_DROP,
            "hnsw_overlap_report": "evalsets/reports/p1-dev-hnsw-overlap-20260718T122103+0800",
            "token_group_rule": "与 benchmarks/p1_lexical_dev.py TOKEN_QUESTION_RE 一致"
            "（T14.4 预声明，import 复用非复制）",
            "embedding_model_id": settings.embedding_model_id,
            "embedding_device": settings.embedding_device,
            "embedding_max_seq_length": MAX_SEQ_LENGTH,
            "query_prompt_name": "query",
        },
        "coverage": {
            "total": sum(len(question["relevant"]) for question in questions),
            "resolved": sum(len(question["relevant"]) for question in questions),
            "missing": [],
        },
        "metrics": metrics,
        "gates": gates,
        "questions": question_rows,
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"p1-dev-hybrid-gate-{now.strftime('%Y%m%dT%H%M%S%z')}"
    json_path = args.output_dir / f"{stem}.json"
    markdown_path = args.output_dir / f"{stem}.md"
    if json_path.exists() or markdown_path.exists():
        raise RuntimeError(f"报告已存在，拒绝覆盖：{stem}")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="mini-mall")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> None:
    json_path, markdown_path = asyncio.run(run(parse_args()))
    print(json_path.relative_to(ROOT))
    print(markdown_path.relative_to(ROOT))


if __name__ == "__main__":
    main()
