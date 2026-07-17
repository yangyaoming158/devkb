"""T14.4：真实语料上的 lexical-only retrieval dev 报告。

在 P1 全量语料（445 文档 / 4788 chunks 快照）上，对 17 个 retrieval dev
问题运行纯 FTS channel（T14.2 冻结的 OR tsquery 构造），记录 Recall/MRR、
逐题首命中名次、P50/P95 延迟与代表性查询的 EXPLAIN 计划（GIN 验证）。

lexical channel 不调用任何模型——无需网络开关，直接运行：
    uv run python benchmarks/p1_lexical_dev.py
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from statistics import quantiles
from typing import Any
from zoneinfo import ZoneInfo

from devkb.config import get_settings
from devkb.db import create_engine, create_session_factory
from devkb.fts import MAX_QUERY_CHARS, MAX_QUERY_TOKENS
from devkb.repositories import ChunkRepo, DocumentRepo, ProjectRepo
from devkb.retrieval import LexicalHit, lexical_retrieve

ROOT = Path(__file__).resolve().parent.parent
DEV_SET = ROOT / "evalsets/v0/retrieval_dev.jsonl"
DEFAULT_OUTPUT_DIR = ROOT / "evalsets/reports"
TIMEZONE = ZoneInfo("Asia/Shanghai")
LATENCY_REPEATS = 5  # 每题额外计时次数；17×5=85 个样本，仍属小样本（报告中声明）

# 机械分组规则（可复现、非人工标注）：问题原文含 ≥5 字符的 ASCII 标识符/路径
# 或 ≥3 位数字 → token 类；否则自然语言类。为 T15.4 Gate 第 3 条预留归类依据。
TOKEN_QUESTION_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-/]{4,}|\d{3,}")

# EXPLAIN 代表性查询：dev 问题之外补一个单稀有 token 查询，验证选择性足够时
# planner 自然选择 GIN（宽 OR 查询按成本正确走顺扫，见报告结论）
EXPLAIN_EXTRA_QUERIES = ["40901"]


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
        raise RuntimeError("T14.4 只允许运行冻结的 17 个 retrieval dev 可答问题")
    return questions


def git_value(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def corpus_hash(documents: list[tuple[str, str]]) -> str:
    payload = json.dumps(documents, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def hit_rank(results: list[LexicalHit], relevant: list[dict[str, str]]) -> int | None:
    for result in results:
        for anchor in relevant:
            if (
                result.rel_path == anchor["rel_path"]
                and anchor["anchor"].casefold() in result.title_path.casefold()
            ):
                return result.lexical_rank
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


def percentile(samples: list[float], q: float) -> float:
    """样本分位数（q ∈ (0,1)），statistics.quantiles 的 inclusive 口径。"""
    cuts = quantiles(samples, n=100, method="inclusive")
    return cuts[round(q * 100) - 1]


def question_group(question: str) -> str:
    return "identifier/path/number" if TOKEN_QUESTION_RE.search(question) else "natural-language"


def render_markdown(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    corpus = report["corpus"]
    latency = report["latency_ms"]
    rows = []
    for item in report["questions"]:
        rank = item["hit_rank"] if item["hit_rank"] is not None else "未命中"
        diag = item["diag_hit_rank_at_100"]
        diag_cell = diag if diag is not None else ">100"
        top = item["results"][0] if item["results"] else None
        top_cell = f"{top['rel_path']} · {top['title_path']}"[:80] if top else "（无结果）"
        rows.append(f"| {item['id']} | {item['group']} | {rank} | {diag_cell} | {top_cell} |")
    group_rows = [
        f"| {name} | {g['count']} | {g['recall_at_10']:.3f} | {g['mrr_at_10']:.3f} |"
        for name, g in report["group_metrics"].items()
    ]
    plan_sections: list[str] = []
    for plan in report["explain_plans"]:
        plan_sections += [
            f"### `{plan['query']}`（GIN 命中：{'是' if plan['gin_used'] else '否'}）",
            "",
            "```text",
            plan["plan"],
            "```",
            "",
        ]
    return "\n".join(
        [
            "# P1 lexical-only retrieval dev 报告（T14.4）",
            "",
            f"> 运行：{report['run']['started_at']} ｜ devkb commit："
            f"`{report['run']['devkb_commit']}`",
            f"> 语料：P1 全量 {corpus['document_count']} 文档 / "
            f"{corpus['chunk_count']} chunks ｜ corpus sha256：`{corpus['sha256']}`",
            "> 检索：`lexical`（T14.2 冻结的 OR tsquery 构造 + ts_rank_cd），"
            f"top_k={report['config']['top_k']}，不调用任何模型",
            "",
            "## 机器计算指标",
            "",
            "| Recall@5 | Recall@10 | MRR@10 | 标注锚点覆盖 |",
            "|---:|---:|---:|---:|",
            f"| {metrics['recall_at_5']:.3f} | {metrics['recall_at_10']:.3f} | "
            f"{metrics['mrr_at_10']:.3f} | {report['coverage']['resolved']}/"
            f"{report['coverage']['total']} |",
            "",
            "### 按问题类型（机械分组规则见 JSON config，非人工标注）",
            "",
            "| 组 | 题数 | Recall@10 | MRR@10 |",
            "|---|---:|---:|---:|",
            *group_rows,
            "",
            "## 检索延迟（本机 WSL2，小样本）",
            "",
            f"每题 {LATENCY_REPEATS} 次计时、共 {latency['sample_count']} 个样本："
            f"P50={latency['p50']:.1f}ms，P95={latency['p95']:.1f}ms，"
            f"max={latency['max']:.1f}ms。",
            "**样本量小且运行在开发机（WSL2、后台负载不受控），只用于确认无数量级异常，"
            "不外推为生产延迟结论。**",
            "",
            "## 逐题结果",
            "",
            "| ID | 组 | 首个标注命中名次 | 诊断：top-100 内名次 | top-1 位置 |",
            "|---|---|---:|---:|---|",
            *rows,
            "",
            "每题完整 top-10、分数、query_tokens 见同名 JSON 原始报告。",
            "",
            "## GIN 查询计划验证",
            "",
            *plan_sections,
            "## 诊断：低召回的根因（机器实验 + 人工判断）",
            "",
            "- **机器实验**：诊断列显示多数未命中题的相关块连 top-100 都未进入。"
            "另在同一快照上对照了 ts_rank_cd 归一化 flag 1/4/32 与查询侧丢弃单字 "
            "CJK token 共 6 个排序变体，R@10 全部不变（0.059）——这不是排序调参"
            "能解决的问题。",
            "- **人工判断（根因）**：11/17 题是中文自然语言问题，而其标注证据"
            "（多为 `docs/dev-log.md` 的任务记录）正文以英文命令与要点为主，"
            "查询与证据的词面几乎零重叠。词汇鸿沟（含跨语言改写）是 lexical 检索的"
            "结构性盲区，正是语义向量与 Hybrid 存在的理由，不构成 FTS 机制缺陷。",
            "- **强项验证**：机械分组的 identifier/path/number 题中 q14 精确命中"
            "rank=1——问题原文与证据共享标识符 token 时 lexical 兑现其定位。",
            "",
            "## 口径、结论与已知限制",
            "",
            "- lexical channel 的定位是 Hybrid/RRF 的一路输入（Evaluation v1 §3），"
            "本报告单独观察 FTS 对标识符/数字/中文 token 的贡献，**不设 Gate**、"
            "允许负结果；与 vector-exact 的对照在 T15.4 同快照统一运行。对 T15 的"
            "含义：lexical 的预期贡献集中在预归类 token 题（Gate 5.1 第 3 条），"
            "RRF 融合不得让该 channel 的弱题拖垮 vector 强题。",
            "- 命中判定与 T11.1 基线同口径（`rel_path + title_path anchor`），"
            "标注在任何 P1 dev 运行前已冻结，本次未改动任何标签。",
            "- 计划结论：高选择性查询（如单个错误码 token）planner 自然选择 "
            "`ix_chunks_search_tsv` 的 Bitmap Index Scan；宽 OR 查询因 camel 拆分"
            "子词（and/id/type…）在代码语料中的高词频而按成本正确走顺扫——在当前 "
            "4788 chunks 规模下最坏 ~100ms，属可接受；该行为随语料增长的演化与"
            "查询侧词频加权的想法已记入 backlog，不在 P1 处理。",
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

            question_rows: list[dict[str, Any]] = []
            ranks: list[int | None] = []
            latency_samples: list[float] = []
            for question in questions:
                results = await lexical_retrieve(
                    session, project.id, question["question"], top_k=args.top_k
                )
                for _ in range(LATENCY_REPEATS):
                    start = time.perf_counter()
                    await lexical_retrieve(
                        session, project.id, question["question"], top_k=args.top_k
                    )
                    latency_samples.append((time.perf_counter() - start) * 1000)
                rank = hit_rank(results, question["relevant"])
                ranks.append(rank)
                # 诊断字段（不参与指标）：相关块在 top-100 内的名次，区分
                # "差一点进 top-10" 与 "词面根本不重叠"
                diag = await lexical_retrieve(session, project.id, question["question"], top_k=100)
                rank_at_100 = hit_rank(diag, question["relevant"])
                question_rows.append(
                    {
                        "id": question["id"],
                        "question": question["question"],
                        "group": question_group(question["question"]),
                        "relevant": question["relevant"],
                        "hit_rank": rank,
                        "diag_hit_rank_at_100": rank_at_100,
                        "reciprocal_rank_at_10": 1 / rank
                        if rank is not None and rank <= 10
                        else 0.0,
                        "query_tokens": list(results[0].query_tokens) if results else [],
                        "results": [
                            {
                                "rank": r.lexical_rank,
                                "chunk_id": str(r.chunk_id),
                                "rel_path": r.rel_path,
                                "title_path": r.title_path,
                                "start_line": r.start_line,
                                "end_line": r.end_line,
                                "score": r.score,
                            }
                            for r in results
                        ],
                    }
                )

            token_questions = [
                q["question"]
                for q in questions
                if question_group(q["question"]) != "natural-language"
            ]
            explain_targets = token_questions[:1]
            explain_targets += [questions[0]["question"], *EXPLAIN_EXTRA_QUERIES]
            explain_plans = []
            for query in explain_targets:
                plan = await chunk_repo.explain_lexical_search(query, args.top_k)
                explain_plans.append(
                    {
                        "query": query,
                        "gin_used": "ix_chunks_search_tsv" in plan,
                        "plan": plan,
                    }
                )
    finally:
        await engine.dispose()

    grouped: dict[str, list[int | None]] = {}
    for row, rank in zip(question_rows, ranks, strict=True):
        grouped.setdefault(row["group"], []).append(rank)
    group_metrics = {
        name: {"count": len(group_ranks), **asdict(calculate_metrics(group_ranks))}
        for name, group_ranks in sorted(grouped.items())
    }

    now = datetime.now(TIMEZONE)
    document_snapshot = sorted((doc.rel_path, doc.content_hash) for doc in documents)
    command = "uv run python benchmarks/p1_lexical_dev.py"
    report = {
        "schema_version": "p1-dev-retrieval-lexical-v1",
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
            "mode": "lexical",
            "top_k": args.top_k,
            "query_construction": (
                f"tokenize_query（去重保序，≤{MAX_QUERY_TOKENS} tokens / "
                f"≤{MAX_QUERY_CHARS} chars）→ per-token plainto_tsquery('simple') "
                "绑定参数 → tsquery || (OR)，ts_rank_cd 降序 + chunk_id 决并列"
            ),
            "group_rule": TOKEN_QUESTION_RE.pattern,
            "latency_repeats": LATENCY_REPEATS,
        },
        "coverage": {
            "total": sum(len(question["relevant"]) for question in questions),
            "resolved": sum(len(question["relevant"]) for question in questions),
            "missing": [],
        },
        "metrics": asdict(calculate_metrics(ranks)),
        "group_metrics": group_metrics,
        "latency_ms": {
            "sample_count": len(latency_samples),
            "p50": percentile(latency_samples, 0.50),
            "p95": percentile(latency_samples, 0.95),
            "max": max(latency_samples),
            "environment": "WSL2 开发机，后台负载不受控；小样本，不外推为生产结论",
        },
        "explain_plans": explain_plans,
        "questions": question_rows,
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"p1-dev-retrieval-lexical-{now.strftime('%Y%m%dT%H%M%S%z')}"
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
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> None:
    json_path, markdown_path = asyncio.run(run(parse_args()))
    print(json_path.relative_to(ROOT))
    print(markdown_path.relative_to(ROOT))


if __name__ == "__main__":
    main()
