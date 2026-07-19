"""Evaluation v1 harness（T20，《Evaluation-v1》）。

- 指标为纯函数（§4），T20.2 以手算微数据集单测对齐；
- `run_eval` 在同一语料快照上按 mode 运行检索/agentic 评测，产出 JSON + Markdown
  时间戳报告（同名拒绝覆盖）；holdout 必须显式 confirm（§6）；
- CI 分层（§6）：本模块不在顶层 import 真实模型；embedder/llm 可注入
  （FakeEmbedder/FakeLLM + fixture 语料 + 真实测试 PG 即 `make eval-ci` 路径）。

指标口径沿用 T14.4/T15.4 基准脚本：命中按 rel_path + anchor（casefold 包含）
解析到当前快照 chunk；token 题分组用预声明机械正则，非事后挑选。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import time
import uuid as uuid_mod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from devkb.agent.service import agentic_answer_question, get_run_trace
from devkb.agent.state import MAX_LLM_REQUESTS, MAX_RETRIEVAL_ROUNDS
from devkb.agent.verification import quote_matches_evidence
from devkb.config import Settings
from devkb.contracts import PROMPT_VERSION
from devkb.db import create_engine, create_session_factory
from devkb.embedding import Embedder
from devkb.errors import InvalidInputError, NotFoundError
from devkb.llm import LLMClient
from devkb.repositories import ChunkRepo, DocumentRepo, ProjectRepo, RunRepo
from devkb.retrieval import (
    HNSW_EF_SEARCH,
    MAX_CHANNEL_CANDIDATES,
    RRF_K_DEFAULT,
    hybrid_retrieve,
    lexical_retrieve,
    retrieve,
)

logger = structlog.get_logger(__name__)

EvalMode = Literal["vector-exact", "vector-hnsw", "lexical", "hybrid-rrf", "agentic"]
RETRIEVAL_MODES: tuple[EvalMode, ...] = ("vector-exact", "vector-hnsw", "lexical", "hybrid-rrf")
EVAL_MODES: tuple[EvalMode, ...] = (*RETRIEVAL_MODES, "agentic")
Split = Literal["dev", "holdout"]

REPORT_SCHEMA_VERSION = "p1-eval-v1"
EVAL_TOP_K = 10
TIMEZONE = ZoneInfo("Asia/Shanghai")
# 冻结题量（Evaluation v0 的 31 问上限；v1 不扩题）
EXPECTED_COUNTS: dict[str, tuple[int, int]] = {"dev": (17, 4), "holdout": (8, 2)}

# 机械分组规则（T14.4 预声明）：≥5 字符 ASCII 标识符/路径或 ≥3 位数字 → token 题
TOKEN_QUESTION_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-/]{4,}|\d{3,}")
_EVIDENCE_MARK_RE = re.compile(r"\[E(\d+)\]")

# §5 预冻结阈值；轮次/调用硬上限直接取 agent.state 冻结常量（单一来源不漂移）
MRR_MAX_DROP = 0.02
HNSW_OVERLAP_GATE = 0.95
CITATION_PROXY_GATE = 0.85


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuestionSets:
    answerable: list[dict[str, Any]]
    unanswerable: list[dict[str, Any]]


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise NotFoundError(f"评测集文件不存在：{path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def load_questions(
    evalsets_dir: Path, split: Split, *, enforce_counts: bool = True
) -> QuestionSets:
    """加载冻结题集并合并 v1 answer-expectations 的 expected_mode。"""
    answerable = _load_jsonl(evalsets_dir / "v0" / f"retrieval_{split}.jsonl")
    unanswerable = _load_jsonl(evalsets_dir / "v0" / f"unanswerable_{split}.jsonl")
    expectations = {
        row["id"]: row
        for row in _load_jsonl(evalsets_dir / "v1" / "answer-expectations.jsonl")
        if row.get("split") == split
    }
    for row in unanswerable:
        expected = expectations.get(row["id"])
        if expected is None:
            raise InvalidInputError(f"不可答题 {row['id']} 缺少 answer-expectations 冻结标注")
        row["expected_mode"] = expected["expected_mode"]
    if enforce_counts:
        want = EXPECTED_COUNTS[split]
        got = (len(answerable), len(unanswerable))
        if got != want:
            raise InvalidInputError(f"{split} 题量 {got} 与冻结题集 {want} 不符，拒绝评测")
        if any(not q.get("answerable") for q in answerable):
            raise InvalidInputError("retrieval 题集混入不可答题，拒绝评测")
    return QuestionSets(answerable=answerable, unanswerable=unanswerable)


# ---------------------------------------------------------------------------
# 检索指标（纯函数，T20.2 手算单测）
# ---------------------------------------------------------------------------


def hit_rank(results: list[Any], relevant: list[dict[str, str]]) -> int | None:
    """首个命中任一 relevant anchor 的名次（1-based）；未命中 None。

    命中口径：rel_path 相等且 anchor casefold 后为 title_path 子串（与 T14.4 一致）。
    """
    for rank, result in enumerate(results, start=1):
        for anchor in relevant:
            if (
                result.rel_path == anchor["rel_path"]
                and anchor["anchor"].casefold() in result.title_path.casefold()
            ):
                return rank
    return None


def recall_at(ranks: list[int | None], k: int) -> float:
    if not ranks:
        raise ValueError("不能对空问题集计算 Recall")
    return sum(rank is not None and rank <= k for rank in ranks) / len(ranks)


def mrr_at(ranks: list[int | None], k: int) -> float:
    if not ranks:
        raise ValueError("不能对空问题集计算 MRR")
    return sum(1 / rank for rank in ranks if rank is not None and rank <= k) / len(ranks)


def top_overlap(ids_a: list[Any], ids_b: list[Any], k: int) -> float:
    """两个排名列表截断到 k 后的集合重合率；分母取截断后较长者。"""
    head_a, head_b = set(ids_a[:k]), set(ids_b[:k])
    denominator = max(len(head_a), len(head_b))
    if denominator == 0:
        raise ValueError("不能对两个空结果列表计算 overlap")
    return len(head_a & head_b) / denominator


def percentile(values: list[float], p: float) -> float:
    """nearest-rank 分位数（小样本口径，报告中声明）。"""
    if not values:
        raise ValueError("不能对空样本计算分位数")
    ordered = sorted(values)
    index = max(math.ceil(p / 100 * len(ordered)) - 1, 0)
    return ordered[index]


def question_group(text: str) -> str:
    return "token" if TOKEN_QUESTION_RE.search(text) else "natural-language"


def rank_improved(candidate: int | None, baseline: int | None) -> bool:
    """首命中名次严格优于 baseline；未命中视为无穷大。"""
    if candidate is None:
        return False
    return baseline is None or candidate < baseline


# ---------------------------------------------------------------------------
# 回答与引用指标（对最终 Answer JSON 的确定性终检）
# ---------------------------------------------------------------------------


def l0_final_errors(answer: dict[str, Any]) -> list[str]:
    """最终 Answer 的 L0：answer_text 的 [E#] 与 claims.evidence_ids 都必须指向 citations。"""
    known = {c["evidence_id"] for c in answer.get("citations", [])}
    errors = []
    for mark in dict.fromkeys(_EVIDENCE_MARK_RE.findall(answer.get("answer_text", ""))):
        if f"E{int(mark)}" not in known:
            errors.append(f"L0:answer_text:unknown_mark:E{int(mark)}")
    for index, claim in enumerate(answer.get("claims", [])):
        for evidence_id in claim.get("evidence_ids", []):
            if evidence_id not in known:
                errors.append(f"L0:claim[{index}]:unknown_evidence:{evidence_id}")
    return errors


def l1_final_errors(answer: dict[str, Any], content_by_evidence_id: dict[str, str]) -> list[str]:
    """最终 Answer 的 L1：每条 quote 须逐字（规范化后）落在其绑定的任一证据 chunk 内。"""
    errors = []
    for claim_index, claim in enumerate(answer.get("claims", [])):
        contents = [
            content_by_evidence_id[evidence_id]
            for evidence_id in claim.get("evidence_ids", [])
            if evidence_id in content_by_evidence_id
        ]
        for quote_index, quote in enumerate(claim.get("quotes", [])):
            if not any(quote_matches_evidence(quote, content) for content in contents):
                errors.append(f"L1:claim[{claim_index}]:quote[{quote_index}]:no_verbatim_match")
    return errors


def citation_hits_anchor(citations: list[dict[str, Any]], relevant: list[dict[str, str]]) -> bool:
    """citation-to-anchor proxy：至少一条最终引用命中任一人工 relevant anchor。"""
    return any(
        citation["rel_path"] == anchor["rel_path"]
        and anchor["anchor"].casefold() in citation["title_path"].casefold()
        for citation in citations
        for anchor in relevant
    )


# ---------------------------------------------------------------------------
# 运行器
# ---------------------------------------------------------------------------


async def _retrieve_by_mode(
    session: AsyncSession,
    project_id: uuid_mod.UUID,
    mode: EvalMode,
    question: str,
    *,
    embedder: Embedder,
    top_k: int,
) -> list[Any]:
    if mode == "vector-exact":
        return await retrieve(
            session, project_id, question, embedder=embedder, top_k=top_k, mode="exact"
        )
    if mode == "vector-hnsw":
        return await retrieve(
            session,
            project_id,
            question,
            embedder=embedder,
            top_k=top_k,
            mode="hnsw",
            ef_search=HNSW_EF_SEARCH,
        )
    if mode == "lexical":
        return await lexical_retrieve(session, project_id, question, top_k=top_k)
    if mode == "hybrid-rrf":
        return await hybrid_retrieve(
            session,
            project_id,
            [question],
            embedder=embedder,
            top_k=top_k,
            vector_mode="hnsw",
            ef_search=HNSW_EF_SEARCH,
        )
    raise InvalidInputError(f"未知检索模式：{mode}")


async def evaluate_retrieval(
    session: AsyncSession,
    project_id: uuid_mod.UUID,
    questions: list[dict[str, Any]],
    modes: Sequence[EvalMode],
    *,
    embedder: Embedder,
    top_k: int = EVAL_TOP_K,
) -> dict[str, Any]:
    """同一快照上按 mode 逐题检索；返回逐题名次/结果与聚合指标。"""
    ranks: dict[str, list[int | None]] = {mode: [] for mode in modes}
    latencies: dict[str, list[float]] = {mode: [] for mode in modes}
    question_rows: list[dict[str, Any]] = []
    for question in questions:
        text = question["question"]
        row: dict[str, Any] = {
            "id": question["id"],
            "question": text,
            "group": question_group(text),
            "relevant": question["relevant"],
            "ranks": {},
            "results": {},
        }
        for mode in modes:
            started = time.perf_counter()
            results = await _retrieve_by_mode(
                session, project_id, mode, text, embedder=embedder, top_k=top_k
            )
            latencies[mode].append((time.perf_counter() - started) * 1000)
            rank = hit_rank(results, question["relevant"])
            ranks[mode].append(rank)
            row["ranks"][mode] = rank
            row["results"][mode] = [
                {
                    "rank": index,
                    "chunk_id": str(result.chunk_id),
                    "rel_path": result.rel_path,
                    "title_path": result.title_path,
                }
                for index, result in enumerate(results, start=1)
            ]
        question_rows.append(row)

    metrics = {
        mode: {
            "recall_at_5": recall_at(ranks[mode], 5),
            "recall_at_10": recall_at(ranks[mode], top_k),
            "mrr_at_10": mrr_at(ranks[mode], top_k),
            "latency_ms_p50": percentile(latencies[mode], 50),
            "latency_ms_p95": percentile(latencies[mode], 95),
            "group_first_hits": {
                group: [row["ranks"][mode] for row in question_rows if row["group"] == group]
                for group in ("natural-language", "token")
            },
        }
        for mode in modes
    }
    overlap = None
    if "vector-exact" in modes and "vector-hnsw" in modes:
        per_question = [
            top_overlap(
                [r["chunk_id"] for r in row["results"]["vector-exact"]],
                [r["chunk_id"] for r in row["results"]["vector-hnsw"]],
                top_k,
            )
            for row in question_rows
        ]
        overlap = {"per_question": per_question, "mean": sum(per_question) / len(per_question)}
    return {"metrics": metrics, "overlap_exact_hnsw": overlap, "questions": question_rows}


async def evaluate_agentic(
    session: AsyncSession,
    project_id: uuid_mod.UUID,
    questions: QuestionSets,
    *,
    embedder: Embedder,
    llm: LLMClient,
    top_k: int = 8,
) -> dict[str, Any]:
    """21 题 agentic 逐题运行 + 最终 Answer 确定性终检 + §5.2 聚合。"""
    chunk_repo = ChunkRepo(session, project_id)
    rows: list[dict[str, Any]] = []
    for question in [*questions.answerable, *questions.unanswerable]:
        answerable = bool(question.get("answerable"))
        row: dict[str, Any] = {
            "id": question["id"],
            "question": question.get("question", ""),
            "answerable": answerable,
            "expected_mode": question.get("expected_mode"),
        }
        try:
            answer = await agentic_answer_question(
                session,
                project_id,
                question["question"],
                embedder=embedder,
                llm=llm,
                top_k=top_k,
            )
        except Exception as exc:
            # 失败 run 也必须有终态：取该项目最新 run 检查落库状态
            latest = await RunRepo(session, project_id).list_recent(1)
            row.update(
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                run_terminal=bool(latest) and latest[0].status in ("succeeded", "failed"),
            )
            rows.append(row)
            continue

        trace = await get_run_trace(session, project_id, uuid_mod.UUID(answer["run_id"]))
        retrieval_rounds = sum(1 for step in trace["steps"] if step["node"] == "retrieve")
        usage = trace["run"]["usage"] or {}
        content_by_evidence_id: dict[str, str] = {}
        chunk_ids = {c["evidence_id"]: uuid_mod.UUID(c["chunk_id"]) for c in answer["citations"]}
        contents = await chunk_repo.get_contents(list(chunk_ids.values()))
        for evidence_id, chunk_id in chunk_ids.items():
            if chunk_id in contents:
                content_by_evidence_id[evidence_id] = contents[chunk_id]
        l0 = l0_final_errors(answer)
        l1 = l1_final_errors(answer, content_by_evidence_id)
        row.update(
            status="succeeded",
            run_id=answer["run_id"],
            mode=answer["mode"],
            l0_errors=l0,
            l1_errors=l1,
            citation_hit=(
                citation_hits_anchor(answer["citations"], question.get("relevant", []))
                if answerable
                else None
            ),
            warnings=answer["warnings"],
            retrieval_rounds=retrieval_rounds,
            llm_calls=int(usage.get("llm_calls", 0)),
            llm_retries=int(usage.get("llm_retries", 0)),
            tokens_in=answer["stats"]["tokens_in"],
            tokens_out=answer["stats"]["tokens_out"],
            cost=trace["run"]["cost"],
            latency_ms=answer["stats"]["latency_ms"],
            run_terminal=trace["run"]["status"] in ("succeeded", "failed"),
            within_budget=(
                retrieval_rounds <= MAX_RETRIEVAL_ROUNDS
                and int(usage.get("llm_calls", 0)) <= MAX_LLM_REQUESTS
            ),
        )
        rows.append(row)
    return {"questions": rows, "aggregates": aggregate_agentic(rows)}


def aggregate_agentic(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """§5.2 聚合与 Gate 判定（纯函数，T20.2 手算单测）。"""
    answerable = [r for r in rows if r["answerable"]]
    unanswerable = [r for r in rows if not r["answerable"]]
    succeeded = [r for r in rows if r.get("status") == "succeeded"]
    correct_unanswerable = sum(1 for r in unanswerable if r.get("mode") == r.get("expected_mode"))
    false_refusals = sum(1 for r in answerable if r.get("mode") == "refusal")
    l0_pass = all(not r.get("l0_errors") for r in succeeded)
    l1_pass = all(not r.get("l1_errors") for r in succeeded)
    citation_rows = [r for r in answerable if r.get("status") == "succeeded"]
    citation_proxy = (
        sum(1 for r in citation_rows if r.get("citation_hit")) / len(citation_rows)
        if citation_rows
        else None
    )
    terminal_complete = all(r.get("run_terminal") for r in rows)
    within_budget = all(r.get("within_budget", False) for r in succeeded)
    latencies = [float(r["latency_ms"]) for r in succeeded]
    gates = {
        "correct_unanswerable": correct_unanswerable,
        "correct_unanswerable_gate": (correct_unanswerable >= 3 if unanswerable else None),
        "false_refusals": false_refusals,
        "false_refusal_gate": false_refusals <= 1 if answerable else None,
        "l0_pass": l0_pass,
        "l1_pass": l1_pass,
        # 2026-07-19 修订（T20.4 偏差裁决）：proxy 为记录义务——值与对冻结阈值
        # 85% 的对照必须落盘，但不进硬 Gate；硬性兜底改由 U2.2 人工抽查承担
        "citation_proxy": citation_proxy,
        "citation_proxy_meets_frozen_threshold": (
            citation_proxy >= CITATION_PROXY_GATE if citation_proxy is not None else None
        ),
        "within_budget": within_budget,
        "terminal_complete": terminal_complete,
    }
    hard_items = [
        gates["correct_unanswerable_gate"],
        gates["false_refusal_gate"],
        l0_pass,
        l1_pass,
        within_budget,
        terminal_complete,
    ]
    gates["gate_passed"] = all(item is not False for item in hard_items) and rows != []
    return {
        "question_count": len(rows),
        "succeeded": len(succeeded),
        "failed": len(rows) - len(succeeded),
        "mode_counts": {
            mode: sum(1 for r in succeeded if r.get("mode") == mode)
            for mode in ("full", "partial", "refusal")
        },
        "total_llm_calls": sum(int(r.get("llm_calls", 0)) for r in succeeded),
        "total_llm_retries": sum(int(r.get("llm_retries", 0)) for r in succeeded),
        "total_tokens_in": sum(int(r.get("tokens_in", 0)) for r in succeeded),
        "total_tokens_out": sum(int(r.get("tokens_out", 0)) for r in succeeded),
        "latency_ms_p50": percentile(latencies, 50) if latencies else None,
        "latency_ms_p95": percentile(latencies, 95) if latencies else None,
        "gates": gates,
    }


def retrieval_gates(retrieval: dict[str, Any]) -> dict[str, Any]:
    """§5.1（2026-07-18 修订）：第 1–3 条为对照记录义务，第 4 条 overlap 硬 Gate。"""
    metrics = retrieval["metrics"]
    gates: dict[str, Any] = {"note": "§5.1 第 1–3 条为记录义务（T15.4 裁决），仅第 4 条硬 Gate"}
    if "hybrid-rrf" in metrics and "vector-exact" in metrics:
        gates["record_recall_delta"] = (
            metrics["hybrid-rrf"]["recall_at_10"] - metrics["vector-exact"]["recall_at_10"]
        )
        gates["record_mrr_delta"] = (
            metrics["hybrid-rrf"]["mrr_at_10"] - metrics["vector-exact"]["mrr_at_10"]
        )
        gates["record_token_question_improved"] = any(
            row["group"] == "token"
            and rank_improved(row["ranks"].get("hybrid-rrf"), row["ranks"].get("vector-exact"))
            for row in retrieval["questions"]
        )
    overlap = retrieval.get("overlap_exact_hnsw")
    if overlap is not None:
        gates["hnsw_overlap_mean"] = overlap["mean"]
        gates["hnsw_overlap_gate"] = overlap["mean"] >= HNSW_OVERLAP_GATE
        gates["gate_passed"] = gates["hnsw_overlap_gate"]
    else:
        gates["gate_passed"] = None  # 未同时运行 exact+hnsw：本报告不判定硬 Gate
    return gates


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def _git_value(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def corpus_hash(documents: list[tuple[str, str]]) -> str:
    payload = json.dumps(documents, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def write_report(
    report: dict[str, Any], markdown: str, output_dir: Path, stem: str
) -> tuple[Path, Path]:
    """JSON + Markdown 落盘；同名文件存在即拒绝（不覆盖历史报告）。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{stem}.json"
    markdown_path = output_dir / f"{stem}.md"
    if json_path.exists() or markdown_path.exists():
        raise InvalidInputError(f"报告已存在，拒绝覆盖：{stem}")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return json_path, markdown_path


def _fmt_rank(rank: int | None) -> str:
    return str(rank) if rank is not None else "未命中"


def _fmt_hit(hit: bool | None) -> str:
    return "-" if hit is None else ("是" if hit else "否")


def render_markdown(report: dict[str, Any]) -> str:
    run = report["run"]
    corpus = report["corpus"]
    lines = [
        f"# Evaluation v1 报告：{run['split']} · {'+'.join(run['modes'])}",
        "",
        f"> 运行：{run['started_at']} ｜ commit：`{run['devkb_commit']}`"
        + ("（工作树有未提交改动）" if run["devkb_worktree_dirty"] else ""),
        f"> 语料：{corpus['project']} · {corpus['document_count']} 文档 / "
        f"{corpus['chunk_count']} chunks ｜ manifest sha256：`{corpus['sha256']}`",
        f"> 配置：embedding=`{report['config']['embedding_model_id']}` "
        f"llm=`{report['config']['llm_model']}` prompt=`{report['config']['prompt_version']}` "
        f"rrf_k={report['config']['rrf_k']} per_channel_n={report['config']['per_channel_n']} "
        f"ef_search={report['config']['ef_search']} top_k={report['config']['top_k']}",
        "",
    ]
    retrieval = report.get("retrieval")
    if retrieval:
        lines += [
            "## 检索指标（同一快照）",
            "",
            "| mode | Recall@5 | Recall@10 | MRR@10 | P50ms | P95ms |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for mode, m in retrieval["metrics"].items():
            lines.append(
                f"| {mode} | {m['recall_at_5']:.3f} | {m['recall_at_10']:.3f} | "
                f"{m['mrr_at_10']:.3f} | {m['latency_ms_p50']:.0f} | {m['latency_ms_p95']:.0f} |"
            )
        overlap = retrieval.get("overlap_exact_hnsw")
        if overlap:
            lines.append("")
            lines.append(
                f"exact↔hnsw 平均 top-{report['config']['top_k']} overlap：{overlap['mean']:.3f}"
            )
        modes = list(retrieval["metrics"])
        lines += [
            "",
            "## 逐题首命中名次",
            "",
            "| ID | 组 | " + " | ".join(modes) + " |",
            "|---|---|" + "---:|" * len(modes),
        ]
        for row in retrieval["questions"]:
            lines.append(
                f"| {row['id']} | {row['group']} | "
                + " | ".join(_fmt_rank(row["ranks"][mode]) for mode in modes)
                + " |"
            )
        lines += [
            "",
            "### 检索 Gate（§5.1，2026-07-18 修订口径）",
            "",
            "```json",
            json.dumps(report["gates"]["retrieval"], ensure_ascii=False, indent=2),
            "```",
            "",
        ]
    agentic = report.get("agentic")
    if agentic:
        agg = agentic["aggregates"]
        lines += [
            "## Agentic 回答指标（§5.2）",
            "",
            f"- 题数：{agg['question_count']}（成功 {agg['succeeded']} / 失败 {agg['failed']}）",
            f"- mode 分布：{agg['mode_counts']}",
            f"- 正确拒答/边界：{agg['gates']['correct_unanswerable']}；"
            f"误拒：{agg['gates']['false_refusals']}",
            f"- 最终 L0 通过：{agg['gates']['l0_pass']}；最终 L1 通过：{agg['gates']['l1_pass']}",
            f"- citation-to-anchor proxy：{agg['gates']['citation_proxy']}",
            f"- 预算内（轮次≤{MAX_RETRIEVAL_ROUNDS}/请求≤{MAX_LLM_REQUESTS}）："
            f"{agg['gates']['within_budget']}；"
            f"终态完整：{agg['gates']['terminal_complete']}",
            f"- tokens：{agg['total_tokens_in']}+{agg['total_tokens_out']}；"
            f"latency P50/P95：{agg['latency_ms_p50']}/{agg['latency_ms_p95']} ms",
            f"- **Gate：{'通过' if agg['gates']['gate_passed'] else '未通过'}**",
            "",
            "| ID | 可答 | 期望 | 实际 | L0 | L1 | 引用命中 | 轮次 | 调用 | latency |",
            "|---|---|---|---|---|---|---|---:|---:|---:|",
        ]
        for row in agentic["questions"]:
            lines.append(
                f"| {row['id']} | {'是' if row['answerable'] else '否'} | "
                f"{row.get('expected_mode') or '-'} | {row.get('mode') or row.get('status')} | "
                f"{'过' if not row.get('l0_errors') else '未过'} | "
                f"{'过' if not row.get('l1_errors') else '未过'} | "
                f"{_fmt_hit(row.get('citation_hit'))} | "
                f"{row.get('retrieval_rounds', '-')} | {row.get('llm_calls', '-')} | "
                f"{row.get('latency_ms', '-')} |"
            )
        lines.append("")
    lines += ["逐题完整原始结果见同名 JSON。", "", "```bash", run["command"], "```", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def expand_modes(mode: str) -> list[EvalMode]:
    if mode == "all":
        return list(EVAL_MODES)
    if mode not in EVAL_MODES:
        raise InvalidInputError(f"mode 只支持 {'|'.join(EVAL_MODES)}|all（当前 {mode}）")
    return [mode]  # type: ignore[list-item]


async def run_eval(
    settings: Settings,
    *,
    split: Split,
    modes: list[EvalMode],
    project_slug: str,
    output_dir: Path,
    evalsets_dir: Path = Path("evalsets"),
    repo_root: Path = Path(),
    embedder: Embedder | None = None,
    llm: LLMClient | None = None,
    confirm_holdout: bool = False,
    enforce_counts: bool = True,
    command: str = "devkb eval run",
) -> list[tuple[Path, Path]]:
    """按 split/modes 运行评测并落盘报告；返回 (json, md) 路径列表。

    holdout 只在 P1 最终验收运行一次：未显式 confirm 一律拒绝（§6）。
    """
    if split == "holdout" and not confirm_holdout:
        raise InvalidInputError("holdout 只在最终验收运行一次：必须显式 --confirm-holdout")
    questions = load_questions(evalsets_dir, split, enforce_counts=enforce_counts)
    retrieval_modes: list[EvalMode] = [m for m in modes if m != "agentic"]
    run_agentic = "agentic" in modes

    engine = create_engine(settings.database_url)
    try:
        async with create_session_factory(engine)() as session:
            project = await ProjectRepo(session).get_by_slug(project_slug)
            if project is None:
                raise NotFoundError(f"项目 '{project_slug}' 不存在")
            documents = await DocumentRepo(session, project.id).list_active()
            chunk_repo = ChunkRepo(session, project.id)
            chunk_count = await chunk_repo.count()
            anchors = await chunk_repo.list_reference_anchors()
            missing = [
                {"question_id": q["id"], **anchor}
                for q in questions.answerable
                for anchor in q["relevant"]
                if not any(
                    rel_path == anchor["rel_path"]
                    and anchor["anchor"].casefold() in title_path.casefold()
                    for rel_path, title_path in anchors
                )
            ]
            if missing:
                raise InvalidInputError(
                    "当前语料快照无法解析全部标注，停止评测："
                    + json.dumps(missing, ensure_ascii=False)
                )

            if embedder is None and (retrieval_modes or run_agentic):
                from devkb.embedding import SentenceTransformerEmbedder

                embedder = SentenceTransformerEmbedder(
                    settings.embedding_model_id,
                    device=settings.embedding_device,
                    batch_size=settings.embedding_batch_size,
                )
            retrieval_result = (
                await evaluate_retrieval(
                    session, project.id, questions.answerable, retrieval_modes, embedder=embedder
                )
                if retrieval_modes and embedder is not None
                else None
            )
            agentic_result = None
            if run_agentic:
                if llm is None:
                    from devkb.llm import OpenAICompatLLM

                    llm = OpenAICompatLLM(
                        api_key=settings.llm_api_key.get_secret_value(),
                        base_url=settings.llm_base_url,
                        model=settings.llm_model,
                    )
                assert embedder is not None
                agentic_result = await evaluate_agentic(
                    session, project.id, questions, embedder=embedder, llm=llm
                )
    finally:
        await engine.dispose()

    now = datetime.now(TIMEZONE)
    snapshot = sorted((doc.rel_path, doc.content_hash) for doc in documents)
    base_report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "run": {
            "started_at": now.isoformat(timespec="seconds"),
            "split": split,
            "modes": modes,
            "devkb_commit": _git_value(repo_root or Path(), "rev-parse", "HEAD"),
            "devkb_worktree_dirty": bool(_git_value(repo_root or Path(), "status", "--short")),
            "command": command,
            "holdout_accessed": split == "holdout",
        },
        "corpus": {
            "project": project_slug,
            "document_count": len(snapshot),
            "chunk_count": chunk_count,
            "sha256": corpus_hash(snapshot),
            "manifest": [{"rel_path": p, "content_hash": h} for p, h in snapshot],
        },
        "dataset": {
            "answerable": len(questions.answerable),
            "unanswerable": len(questions.unanswerable),
            "evalsets_dir": str(evalsets_dir),
        },
        "config": {
            "top_k": EVAL_TOP_K,
            "agentic_top_k": 8,
            "rrf_k": RRF_K_DEFAULT,
            "per_channel_n": MAX_CHANNEL_CANDIDATES,
            "ef_search": HNSW_EF_SEARCH,
            "embedding_model_id": settings.embedding_model_id,
            "llm_model": settings.llm_model,
            "prompt_version": PROMPT_VERSION,
            "mrr_max_drop": MRR_MAX_DROP,
            "hnsw_overlap_gate": HNSW_OVERLAP_GATE,
            "citation_proxy_gate": CITATION_PROXY_GATE,
        },
    }

    written: list[tuple[Path, Path]] = []
    timestamp = now.strftime("%Y%m%dT%H%M%S%z")
    if split == "holdout":
        report = {
            **base_report,
            "retrieval": retrieval_result,
            "agentic": agentic_result,
            "gates": {
                "retrieval": retrieval_gates(retrieval_result) if retrieval_result else None,
                "agentic": (agentic_result or {}).get("aggregates", {}).get("gates"),
            },
        }
        written.append(
            write_report(report, render_markdown(report), output_dir, f"p1-holdout-{timestamp}")
        )
        return written
    if retrieval_result:
        report = {
            **base_report,
            "retrieval": retrieval_result,
            "gates": {"retrieval": retrieval_gates(retrieval_result)},
        }
        written.append(
            write_report(
                report, render_markdown(report), output_dir, f"p1-dev-retrieval-{timestamp}"
            )
        )
    if agentic_result:
        report = {
            **base_report,
            "agentic": agentic_result,
            "gates": {"agentic": agentic_result["aggregates"]["gates"]},
        }
        written.append(
            write_report(report, render_markdown(report), output_dir, f"p1-dev-agentic-{timestamp}")
        )
    return written
