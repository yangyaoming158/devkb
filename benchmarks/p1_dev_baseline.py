"""T11.1：运行 P1 开发前的 vector-exact retrieval dev 基线。

该脚本只读取 17 个 retrieval dev 问题，不接受 holdout 输入；输出不可覆盖的
JSON 原始报告和 Markdown 摘要。当前任务位于 Java/config 摄取之前，因此这是
P0 冻结范围（39 个 Markdown）的全量生产语料基线，不是 P1 最终 445 文件快照。

运行（真实模型双开关见 CLAUDE.md）：
    env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        -u all_proxy -u ALL_PROXY HF_HUB_OFFLINE=1 \
        uv run python benchmarks/p1_dev_baseline.py
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from devkb.config import get_settings
from devkb.db import create_engine, create_session_factory
from devkb.embedding import MAX_SEQ_LENGTH, SentenceTransformerEmbedder
from devkb.repositories import ChunkRepo, DocumentRepo, ProjectRepo
from devkb.retrieval import RetrievedChunk, retrieve

ROOT = Path(__file__).resolve().parent.parent
DEV_SET = ROOT / "evalsets/v0/retrieval_dev.jsonl"
DEFAULT_OUTPUT_DIR = ROOT / "evalsets/reports"
DEFAULT_SOURCE_REPO = Path("/home/oslab/projects/mini-mall-order")
TIMEZONE = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class Metrics:
    recall_at_5: float
    recall_at_10: float
    mrr_at_10: float


def load_dev_questions() -> list[dict[str, Any]]:
    questions = [
        json.loads(line)
        for line in DEV_SET.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(questions) != 17 or any(not item.get("answerable") for item in questions):
        raise RuntimeError("T11.1 只允许运行冻结的 17 个 retrieval dev 可答问题")
    return questions


def git_value(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def corpus_hash(documents: list[tuple[str, str]]) -> str:
    payload = json.dumps(documents, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def hit_rank(results: list[RetrievedChunk], relevant: list[dict[str, str]]) -> int | None:
    for rank, result in enumerate(results, start=1):
        for anchor in relevant:
            if (
                result.rel_path == anchor["rel_path"]
                and anchor["anchor"].casefold() in result.title_path.casefold()
            ):
                return rank
    return None


def validate_anchor_coverage(
    questions: list[dict[str, Any]], reference_anchors: list[tuple[str, str]]
) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    for question in questions:
        for relevant in question["relevant"]:
            if not any(
                rel_path == relevant["rel_path"]
                and relevant["anchor"].casefold() in title_path.casefold()
                for rel_path, title_path in reference_anchors
            ):
                missing.append(
                    {
                        "question_id": question["id"],
                        "rel_path": relevant["rel_path"],
                        "anchor": relevant["anchor"],
                    }
                )
    return missing


def calculate_metrics(ranks: list[int | None]) -> Metrics:
    count = len(ranks)
    if count == 0:
        raise ValueError("不能对空问题集计算指标")
    return Metrics(
        recall_at_5=sum(rank is not None and rank <= 5 for rank in ranks) / count,
        recall_at_10=sum(rank is not None and rank <= 10 for rank in ranks) / count,
        mrr_at_10=sum(1 / rank for rank in ranks if rank is not None and rank <= 10) / count,
    )


def render_markdown(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    corpus = report["corpus"]
    config = report["config"]
    rows = []
    for item in report["questions"]:
        rank = item["hit_rank"] if item["hit_rank"] is not None else "未命中"
        top = item["results"][0]
        rows.append(
            f"| {item['id']} | {rank} | {top['score']:.3f} | "
            f"{top['rel_path']} · {top['title_path']} |"
        )
    return "\n".join(
        [
            "# P1 开发前 vector-exact dev 基线（T11.1）",
            "",
            f"> 运行：{report['run']['started_at']} ｜ devkb commit："
            f"`{report['run']['devkb_commit']}` ｜ source commit："
            f"`{report['run']['source_commit']}`",
            f"> 语料：P0 冻结范围全量 {corpus['document_count']} 文档 / "
            f"{corpus['chunk_count']} chunks ｜ corpus sha256：`{corpus['sha256']}`",
            f"> 检索：`vector-exact` top_k={config['top_k']}，"
            f"`{config['embedding_model_id']}` / {config['embedding_device']}",
            "",
            "## 机器计算指标",
            "",
            "| Recall@5 | Recall@10 | MRR@10 | 标注锚点覆盖 |",
            "|---:|---:|---:|---:|",
            f"| {metrics['recall_at_5']:.3f} | {metrics['recall_at_10']:.3f} | "
            f"{metrics['mrr_at_10']:.3f} | {report['coverage']['resolved']}/"
            f"{report['coverage']['total']} |",
            "",
            "## 逐题结果",
            "",
            "| ID | 首个标注命中名次 | top-1 score | top-1 位置 |",
            "|---|---:|---:|---|",
            *rows,
            "",
            "每题完整 top-10、分数、chunk_id 和人工 relevant 标注见同名 JSON 原始报告。",
            "",
            "## 口径与已知限制",
            "",
            "- 这是 T13 Java/config 摄取前的 P1 开发前基线，覆盖已验收的 P0 "
            "Markdown 全量语料，不是 P1 最终 445 文件语料快照。",
            "- 指标命中依据为 `rel_path + title_path anchor`；运行前已检查所有标注"
            "锚点在当前可检索 chunks 中可解析。",
            "- 本报告只描述机器计算结果，未运行 holdout，不代表 Hybrid/HNSW/Agent Gate 已通过。",
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
                raise RuntimeError(f"项目 {args.project!r} 不存在，请先完成 P0 全量摄取")
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
            ranks: list[int | None] = []
            for question in questions:
                results = await retrieve(
                    session,
                    project.id,
                    question["question"],
                    embedder=embedder,
                    top_k=args.top_k,
                )
                rank = hit_rank(results, question["relevant"])
                ranks.append(rank)
                question_rows.append(
                    {
                        "id": question["id"],
                        "question": question["question"],
                        "relevant": question["relevant"],
                        "hit_rank": rank,
                        "reciprocal_rank_at_10": 1 / rank
                        if rank is not None and rank <= 10
                        else 0.0,
                        "results": [
                            {
                                "rank": result_rank,
                                "chunk_id": str(result.chunk_id),
                                "rel_path": result.rel_path,
                                "title_path": result.title_path,
                                "start_line": result.start_line,
                                "end_line": result.end_line,
                                "score": result.score,
                            }
                            for result_rank, result in enumerate(results, start=1)
                        ],
                    }
                )
    finally:
        await engine.dispose()

    now = datetime.now(TIMEZONE)
    document_snapshot = sorted((doc.rel_path, doc.content_hash) for doc in documents)
    repo_dirty = bool(git_value(ROOT, "status", "--short"))
    source_dirty = bool(git_value(args.source_repo, "status", "--short"))
    command = (
        "env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY "
        "-u all_proxy -u ALL_PROXY HF_HUB_OFFLINE=1 "
        "uv run python benchmarks/p1_dev_baseline.py"
    )
    report = {
        "schema_version": "p1-dev-retrieval-pre-v1",
        "run": {
            "started_at": now.isoformat(timespec="seconds"),
            "devkb_commit": git_value(ROOT, "rev-parse", "HEAD"),
            "devkb_worktree_dirty": repo_dirty,
            "source_commit": git_value(args.source_repo, "rev-parse", "HEAD"),
            "source_worktree_dirty": source_dirty,
            "command": command,
            "holdout_accessed": False,
        },
        "corpus": {
            "scope": "p0-frozen-markdown-full",
            "project": args.project,
            "document_count": len(document_snapshot),
            "chunk_count": chunk_count,
            "sha256": corpus_hash(document_snapshot),
            "documents": [
                {"rel_path": rel_path, "content_hash": content_hash}
                for rel_path, content_hash in document_snapshot
            ],
        },
        "dataset": {"path": str(DEV_SET.relative_to(ROOT)), "question_count": len(questions)},
        "config": {
            "mode": "vector-exact",
            "top_k": args.top_k,
            "distance": "cosine",
            "embedding_model_id": settings.embedding_model_id,
            "embedding_device": settings.embedding_device,
            "embedding_batch_size": settings.embedding_batch_size,
            "embedding_max_seq_length": MAX_SEQ_LENGTH,
            "query_prompt_name": "query",
        },
        "coverage": {
            "total": sum(len(question["relevant"]) for question in questions),
            "resolved": sum(len(question["relevant"]) for question in questions),
            "missing": [],
        },
        "metrics": asdict(calculate_metrics(ranks)),
        "questions": question_rows,
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"p1-dev-retrieval-vector-exact-pre-{now.strftime('%Y%m%dT%H%M%S%z')}"
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
    parser.add_argument("--top-k", type=int, default=10, choices=range(10, 11))
    parser.add_argument("--source-repo", type=Path, default=DEFAULT_SOURCE_REPO)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> None:
    json_path, markdown_path = asyncio.run(run(parse_args()))
    print(json_path.relative_to(ROOT))
    print(markdown_path.relative_to(ROOT))


if __name__ == "__main__":
    main()
