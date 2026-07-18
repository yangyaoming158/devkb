"""T15.3：HNSW 与 exact 的 top-10 overlap 对照（Evaluation v1 §5.1 第 4 条）。

对 17 个 retrieval dev 问题各嵌入一次查询向量，同一向量分别跑
`vector_search(mode="exact")` 与 `mode="hnsw"` 的 top-10，按 chunk_id 集合
计算 overlap。Gate：平均 overlap ≥ 0.95；未达标沿预声明阶梯提高 ef_search
再测，全部失败则默认路径保留精确扫描并把 HNSW 记为负实验——本脚本只产出
证据报告，不修改任何默认值。

运行（真实模型双开关见 CLAUDE.md）：
    env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        -u all_proxy -u ALL_PROXY HF_HUB_OFFLINE=1 \
        uv run python benchmarks/p1_hnsw_overlap.py
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from devkb.config import get_settings
from devkb.db import create_engine, create_session_factory
from devkb.embedding import MAX_SEQ_LENGTH, SentenceTransformerEmbedder
from devkb.repositories import ChunkRepo, DocumentRepo, ProjectRepo

ROOT = Path(__file__).resolve().parent.parent
DEV_SET = ROOT / "evalsets/v0/retrieval_dev.jsonl"
DEFAULT_OUTPUT_DIR = ROOT / "evalsets/reports"
TIMEZONE = ZoneInfo("Asia/Shanghai")

OVERLAP_GATE = 0.95
TOP_K = 10
# 预声明 ef_search 阶梯：40 是 pgvector 会话默认值；达标即冻结，不再继续爬
EF_SEARCH_LADDER = [40, 80, 160]
# 0002 迁移的索引构建参数，写入报告供复现（脚本不重建索引）
HNSW_INDEX_PARAMS = {"m": 16, "ef_construction": 64, "ops": "vector_cosine_ops"}


def load_dev_questions() -> list[dict[str, Any]]:
    questions = [
        json.loads(line)
        for line in DEV_SET.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(questions) != 17 or any(not item.get("answerable") for item in questions):
        raise RuntimeError("T15.3 只允许运行冻结的 17 个 retrieval dev 可答问题")
    return questions


def git_value(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def corpus_hash(documents: list[tuple[str, str]]) -> str:
    payload = json.dumps(documents, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def render_markdown(report: dict[str, Any]) -> str:
    corpus = report["corpus"]
    decision = report["decision"]
    attempt_sections: list[str] = []
    for attempt in report["attempts"]:
        rows = [
            f"| {q['id']} | {q['overlap']:.2f} | {q['exact_only_count']} |"
            for q in attempt["questions"]
        ]
        attempt_sections += [
            f"### ef_search={attempt['ef_search']}（平均 overlap="
            f"{attempt['avg_overlap']:.4f}，Gate {'通过' if attempt['passed'] else '未过'}）",
            "",
            "| ID | top-10 overlap | exact 独有块数 |",
            "|---|---:|---:|",
            *rows,
            "",
        ]
    return "\n".join(
        [
            "# P1 HNSW/exact top-10 overlap 对照（T15.3）",
            "",
            f"> 运行：{report['run']['started_at']} ｜ devkb commit："
            f"`{report['run']['devkb_commit']}`",
            f"> 语料：P1 全量 {corpus['document_count']} 文档 / "
            f"{corpus['chunk_count']} chunks ｜ corpus sha256：`{corpus['sha256']}`",
            f"> 索引：HNSW {json.dumps(HNSW_INDEX_PARAMS)}（0002 迁移）｜ "
            f"top_k={TOP_K}，`{report['config']['embedding_model_id']}` / "
            f"{report['config']['embedding_device']}",
            "",
            "## 结论",
            "",
            f"- Gate（平均 overlap ≥ {OVERLAP_GATE}）：**"
            f"{'通过' if decision['gate_passed'] else '未通过'}**，"
            f"冻结口径：**{decision['frozen_vector_mode']}**"
            + (
                f"（ef_search={decision['frozen_ef_search']}）"
                if decision["frozen_ef_search"] is not None
                else ""
            ),
            f"- 每题 overlap = |exact top-10 ∩ hnsw top-10| / {TOP_K}，按 chunk_id 集合比较；"
            "两种模式使用同一查询向量（每题只嵌入一次）。",
            "- exact/hnsw 计划形状由 `vector_search` 的 GUC 强制（T12.4 已验证真实走 "
            "HNSW 索引/顺扫），本报告不重复 EXPLAIN。",
            "",
            "## 逐 ef_search 尝试",
            "",
            *attempt_sections,
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

            embedder = SentenceTransformerEmbedder(
                settings.embedding_model_id,
                device=settings.embedding_device,
                batch_size=settings.embedding_batch_size,
            )
            embeddings = [(q, embedder.embed_query(q["question"])) for q in questions]

            attempts: list[dict[str, Any]] = []
            frozen_ef: int | None = None
            for ef_search in EF_SEARCH_LADDER:
                question_rows: list[dict[str, Any]] = []
                overlaps: list[float] = []
                for question, embedding in embeddings:
                    exact_rows = await chunk_repo.vector_search(embedding, TOP_K, mode="exact")
                    hnsw_rows = await chunk_repo.vector_search(
                        embedding, TOP_K, mode="hnsw", ef_search=ef_search
                    )
                    exact_ids = [str(chunk.id) for chunk, _, _ in exact_rows]
                    hnsw_ids = [str(chunk.id) for chunk, _, _ in hnsw_rows]
                    overlap = len(set(exact_ids) & set(hnsw_ids)) / TOP_K
                    overlaps.append(overlap)
                    question_rows.append(
                        {
                            "id": question["id"],
                            "question": question["question"],
                            "overlap": overlap,
                            "exact_only_count": len(set(exact_ids) - set(hnsw_ids)),
                            "exact_top10": exact_ids,
                            "hnsw_top10": hnsw_ids,
                        }
                    )
                avg_overlap = sum(overlaps) / len(overlaps)
                passed = avg_overlap >= OVERLAP_GATE
                attempts.append(
                    {
                        "ef_search": ef_search,
                        "avg_overlap": avg_overlap,
                        "passed": passed,
                        "questions": question_rows,
                    }
                )
                if passed:
                    frozen_ef = ef_search
                    break
    finally:
        await engine.dispose()

    now = datetime.now(TIMEZONE)
    document_snapshot = sorted((doc.rel_path, doc.content_hash) for doc in documents)
    command = (
        "env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY "
        "-u all_proxy -u ALL_PROXY HF_HUB_OFFLINE=1 "
        "uv run python benchmarks/p1_hnsw_overlap.py"
    )
    gate_passed = frozen_ef is not None
    report = {
        "schema_version": "p1-dev-hnsw-overlap-v1",
        "run": {
            "started_at": now.isoformat(timespec="seconds"),
            "devkb_commit": git_value(ROOT, "rev-parse", "HEAD"),
            "devkb_worktree_dirty": bool(git_value(ROOT, "status", "--short")),
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
            "top_k": TOP_K,
            "overlap_gate": OVERLAP_GATE,
            "ef_search_ladder": EF_SEARCH_LADDER,
            "hnsw_index_params": HNSW_INDEX_PARAMS,
            "distance": "cosine",
            "embedding_model_id": settings.embedding_model_id,
            "embedding_device": settings.embedding_device,
            "embedding_max_seq_length": MAX_SEQ_LENGTH,
            "query_prompt_name": "query",
        },
        "attempts": attempts,
        "decision": {
            "gate_passed": gate_passed,
            "frozen_vector_mode": "hnsw" if gate_passed else "exact",
            "frozen_ef_search": frozen_ef,
        },
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"p1-dev-hnsw-overlap-{now.strftime('%Y%m%dT%H%M%S%z')}"
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
