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

# v1 为 T20.4 期间历史 schema（citation Gate 字段名经 2026-07-19 裁决更名，三份已提交
# 报告冻结不改写）；v1.1 起 Gate 键集合 split-aware 且固定、逐题带完整原始 Answer、
# holdout 另带 P0 基线对照与访问序号，见 test_eval_reports_schema（尚无 v1.1 报告落库，
# 故 2026-07-20 复评补充的字段直接并入 v1.1，不再另起版本）
REPORT_SCHEMA_VERSION = "p1-eval-v1.1"
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

# P0 holdout 历史基线（evalsets/reports/p0-holdout.md，p0 分支 commit 7fdf540，2026-07-15）：
# §7 第 3 条要求 P1 holdout 与之同口径对照，同时明确标注语料规模已变化——只作历史参照，不作硬 Gate
P0_HOLDOUT_BASELINE: dict[str, Any] = {
    "source": "evalsets/reports/p0-holdout.md",
    "devkb_commit": "7fdf540",
    "run_date": "2026-07-15",
    "corpus": {"document_count": 39, "chunk_count": 1370, "scope": "P0 冻结 Markdown 范围"},
    "retrieval": {
        "mode": "vector-exact",
        "top_k": 10,
        "question_count": 8,
        "recall_at_5": 0.625,
        "recall_at_10": 0.875,
        "mrr_at_10": 0.440,
    },
}

# holdout 一次性访问台账：每次尝试在读题前落盘，中断/失败也留痕（§7 不得静默重跑）
HOLDOUT_LEDGER_NAME = "holdout-access-log.jsonl"


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
    embedder: Embedder | None,
    top_k: int,
) -> list[Any]:
    if mode == "lexical":
        return await lexical_retrieve(session, project_id, question, top_k=top_k)
    if embedder is None:
        raise InvalidInputError(f"检索模式 {mode} 需要 embedding 模型，当前未提供")
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
    embedder: Embedder | None,
    top_k: int = EVAL_TOP_K,
) -> dict[str, Any]:
    """同一快照上按 mode 逐题检索；返回逐题名次/结果与聚合指标。

    embedder 只有向量/hybrid 模式需要；纯 lexical 评测传 None 即可。
    """
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
    split: Split,
    embedder: Embedder,
    llm: LLMClient,
    top_k: int = 8,
) -> dict[str, Any]:
    """agentic 逐题运行 + 最终 Answer 确定性终检 + split 对应 Gate 聚合。"""
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
            # §7 逐题原始结果：完整 Answer JSON 原样落盘（answer_text/claims/quotes/
            # citations/not_found/limitations/trace_summary）；不可答题的"理由"即在其中
            answer=answer,
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
    return {"questions": rows, "aggregates": aggregate_agentic(rows, split=split)}


def aggregate_agentic(rows: list[dict[str, Any]], *, split: Split) -> dict[str, Any]:
    """split 对应回答 Gate 的聚合判定（纯函数，T20.2 手算单测）。

    dev 按 §5.2（2026-07-19 修订）：拒答/误拒/L0/L1/预算/终态六项硬 Gate；
    holdout 按 §7：硬 Gate 仅 L0/L1/预算/终态，拒答/误拒只记录不设阈值。
    两个 split 都要求"无失败 run"：失败 run 不进 L0/L1/预算统计，
    不计入硬判定会让 Gate 在有失败题时静默通过。
    """
    answerable = [r for r in rows if r["answerable"]]
    unanswerable = [r for r in rows if not r["answerable"]]
    succeeded = [r for r in rows if r.get("status") == "succeeded"]
    no_failed_runs = len(succeeded) == len(rows)
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
        "note": (
            "§5.2 硬 Gate：拒答/误拒/L0/L1/预算/终态/无失败 run；"
            "citation proxy 为记录义务（2026-07-19 裁决）"
            if split == "dev"
            else "§7 硬 Gate：L0/L1/预算/终态/无失败 run；拒答/误拒/citation proxy 为记录义务"
        ),
        "correct_unanswerable": correct_unanswerable,
        "correct_unanswerable_gate": (
            (correct_unanswerable >= 3 if unanswerable else None) if split == "dev" else None
        ),
        "false_refusals": false_refusals,
        "false_refusal_gate": (
            (false_refusals <= 1 if answerable else None) if split == "dev" else None
        ),
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
        "no_failed_runs": no_failed_runs,
    }
    hard_items = [
        gates["correct_unanswerable_gate"],
        gates["false_refusal_gate"],
        l0_pass,
        l1_pass,
        within_budget,
        terminal_complete,
        no_failed_runs,
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


def p0_baseline_comparison(
    retrieval: dict[str, Any] | None, corpus: dict[str, Any]
) -> dict[str, Any]:
    """§7 第 3 条：与 P0 39 文档/1370 chunks、R@10=0.875 的历史同口径对照。

    同口径指同为 holdout 8 问、vector-exact、top-10；语料规模不同，故绝对差值
    必须与"规模已变化"声明一起呈现，不得单独归因为检索退化，也不作硬 Gate。
    """
    baseline_retrieval = P0_HOLDOUT_BASELINE["retrieval"]
    baseline_corpus = P0_HOLDOUT_BASELINE["corpus"]
    metrics = ("recall_at_5", "recall_at_10", "mrr_at_10")
    current = ((retrieval or {}).get("metrics") or {}).get("vector-exact")
    return {
        "baseline": P0_HOLDOUT_BASELINE,
        "current_vector_exact": (
            {metric: current[metric] for metric in metrics} if current is not None else None
        ),
        "delta_vs_p0": (
            {metric: current[metric] - baseline_retrieval[metric] for metric in metrics}
            if current is not None
            else None
        ),
        "corpus_scale": {
            "p0": f"{baseline_corpus['document_count']} 文档 / "
            f"{baseline_corpus['chunk_count']} chunks",
            "p1": f"{corpus['document_count']} 文档 / {corpus['chunk_count']} chunks",
            "changed": (
                corpus["document_count"] != baseline_corpus["document_count"]
                or corpus["chunk_count"] != baseline_corpus["chunk_count"]
            ),
        },
        "note": (
            "P0 绝对值只作历史参照，不作 P1 硬 Gate（Evaluation-v1 §7）：P1 候选语料新增 "
            "Java 与 config 文件后，候选 chunks 规模与干扰分布已经改变，"
            "绝对召回下降不能单独归因为检索退化。"
        ),
    }


def retrieval_gates(retrieval: dict[str, Any], *, split: Split) -> dict[str, Any]:
    """检索 Gate（纯函数）：dev 按 §5.1、holdout 按 §7（均为 2026-07-18 修订口径）。

    dev 硬项 = HNSW↔exact overlap ≥ 0.95；holdout 硬项 = 在线默认 vector-hnsw 的
    Recall@10 不低于同快照 vector-exact。hybrid 对照两个 split 均只记录不判定。
    键集合按 split 固定（不可计算时为 None），保证同 schema 版本报告形状稳定。
    """
    metrics = retrieval["metrics"]
    gates: dict[str, Any] = {
        "note": (
            "§5.1 第 1–3 条为记录义务（T15.4 裁决），仅第 4 条 overlap 硬 Gate"
            if split == "dev"
            else "§7 硬 Gate：vector-hnsw Recall@10 ≥ vector-exact（T15.4 裁决修订）；"
            "hybrid 对照与 overlap 为记录义务"
        ),
        "record_recall_delta": None,
        "record_mrr_delta": None,
        "record_token_question_improved": None,
        "hnsw_overlap_mean": None,
    }
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
    if split == "dev":
        gates["hnsw_overlap_gate"] = (
            overlap["mean"] >= HNSW_OVERLAP_GATE if overlap is not None else None
        )
        # 未同时运行 exact+hnsw：本报告不判定硬 Gate
        gates["gate_passed"] = gates["hnsw_overlap_gate"]
        return gates
    # holdout：run_eval 已强制 --mode all，exact/hnsw 必在；None 口径仅防御纯函数误用
    if "vector-hnsw" in metrics and "vector-exact" in metrics:
        gates["hnsw_recall_at_10"] = metrics["vector-hnsw"]["recall_at_10"]
        gates["exact_recall_at_10"] = metrics["vector-exact"]["recall_at_10"]
        gates["hnsw_recall_ge_exact"] = bool(
            gates["hnsw_recall_at_10"] >= gates["exact_recall_at_10"]
        )
    else:
        gates["hnsw_recall_at_10"] = None
        gates["exact_recall_at_10"] = None
        gates["hnsw_recall_ge_exact"] = None
    gates["gate_passed"] = gates["hnsw_recall_ge_exact"]
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


def read_holdout_ledger(output_dir: Path) -> list[dict[str, Any]]:
    """读取 holdout 一次性访问台账（不存在即空）。"""
    path = output_dir / HOLDOUT_LEDGER_NAME
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def append_holdout_ledger(output_dir: Path, record: dict[str, Any]) -> Path:
    """追加一条访问事件；append-only，既有记录不可改写。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / HOLDOUT_LEDGER_NAME
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return path


def _fmt_rank(rank: int | None) -> str:
    return str(rank) if rank is not None else "未命中"


def _fmt_hit(hit: bool | None) -> str:
    return "-" if hit is None else ("是" if hit else "否")


def _fmt_cell(text: str, limit: int = 120) -> str:
    """表格单元格：折行与竖线会破表，节选后转义；完整原文在同名 JSON。"""
    flat = " ".join(text.split()).replace("|", "\\|")
    return (flat[:limit] + "…") if len(flat) > limit else (flat or "-")


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
    attempt = run.get("holdout_attempt")
    if attempt is not None and attempt > 1:
        # §7：重跑必须经用户裁决并在报告中显著标记，不得静默重跑挑最好成绩
        lines += [
            f"> ⚠️ **本报告是第 {attempt} 次 holdout 运行**，不是首次一次性访问。",
            f"> 重跑裁决理由：{run.get('holdout_rerun_acknowledged')}",
            f"> 历次访问（含中断/失败尝试）见 `{HOLDOUT_LEDGER_NAME}`；"
            "依《Evaluation-v1》§7 不得静默重跑挑最好成绩。",
            "",
        ]
    baseline = report.get("p0_baseline")
    if baseline:
        current = baseline["current_vector_exact"]
        delta = baseline["delta_vs_p0"]
        base = baseline["baseline"]["retrieval"]
        lines += [
            "## 与 P0 holdout 历史基线对照（§7 第 3 条）",
            "",
            f"> P0 基线：`{baseline['baseline']['source']}`（commit "
            f"`{baseline['baseline']['devkb_commit']}`，{baseline['baseline']['run_date']}）",
            f"> 语料规模：P0 {baseline['corpus_scale']['p0']} → P1 "
            f"{baseline['corpus_scale']['p1']}"
            + ("（**已变化**）" if baseline["corpus_scale"]["changed"] else "（未变化）"),
            "",
            "| 指标 | P0 基线 | P1 本次 vector-exact | 差值 |",
            "|---|---:|---:|---:|",
        ]
        for key, label in (
            ("recall_at_5", "Recall@5"),
            ("recall_at_10", "Recall@10"),
            ("mrr_at_10", "MRR@10"),
        ):
            lines.append(
                f"| {label} | {base[key]:.3f} | "
                + (f"{current[key]:.3f} | {delta[key]:+.3f} |" if current else "- | - |")
            )
        lines += ["", baseline["note"], ""]
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
            "### 检索 Gate（§7 holdout 口径，2026-07-18 修订）"
            if run["split"] == "holdout"
            else "### 检索 Gate（§5.1，2026-07-18 修订口径）",
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
            "## Agentic 回答指标（§7 holdout 口径）"
            if run["split"] == "holdout"
            else "## Agentic 回答指标（§5.2）",
            "",
            f"- 题数：{agg['question_count']}（成功 {agg['succeeded']} / 失败 {agg['failed']}）",
            f"- mode 分布：{agg['mode_counts']}",
            f"- 正确拒答/边界：{agg['gates']['correct_unanswerable']}；"
            f"误拒：{agg['gates']['false_refusals']}",
            f"- 最终 L0 通过：{agg['gates']['l0_pass']}；最终 L1 通过：{agg['gates']['l1_pass']}",
            f"- citation-to-anchor proxy：{agg['gates']['citation_proxy']}",
            f"- 预算内（轮次≤{MAX_RETRIEVAL_ROUNDS}/请求≤{MAX_LLM_REQUESTS}）："
            f"{agg['gates']['within_budget']}；"
            f"终态完整：{agg['gates']['terminal_complete']}；"
            f"无失败 run：{agg['gates']['no_failed_runs']}",
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
        unanswerable = [row for row in agentic["questions"] if not row["answerable"]]
        if unanswerable:
            # §7：不可答题必须报告 mode 和理由（完整 Answer 原文见同名 JSON）
            lines += [
                f"### 不可答题判定与理由（{len(unanswerable)} 题）",
                "",
                "| ID | 期望 | 实际 | not_found | 回答/拒答理由（节选） |",
                "|---|---|---|---|---|",
            ]
            for row in unanswerable:
                answer = row.get("answer") or {}
                not_found = "；".join(answer.get("not_found", [])) or "-"
                lines.append(
                    f"| {row['id']} | {row.get('expected_mode') or '-'} | "
                    f"{row.get('mode') or row.get('status')} | {_fmt_cell(not_found)} | "
                    f"{_fmt_cell(answer.get('answer_text', '') or row.get('error', ''))} |"
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
    acknowledge_rerun: str | None = None,
    enforce_counts: bool = True,
    command: str = "devkb eval run",
) -> list[tuple[Path, Path]]:
    """按 split/modes 运行评测并落盘报告；返回 (json, md) 路径列表。

    holdout 只在 P1 最终验收运行一次（§6/§7）：未显式 confirm 一律拒绝；必须完整
    运行全部模式（部分模式会白白消耗一次性访问）；每次尝试在读题前写入访问台账，
    中断/失败同样留痕；台账已有记录时必须带用户裁决理由才允许重跑，且报告显著标记。
    """
    if split == "holdout" and not confirm_holdout:
        raise InvalidInputError("holdout 只在最终验收运行一次：必须显式 --confirm-holdout")
    if split == "holdout" and modes != list(EVAL_MODES):
        raise InvalidInputError(
            "holdout 一次性访问必须完整运行 --mode all（Evaluation-v1 §7），"
            f"不得只跑部分模式或重复模式（当前 {modes}）"
        )
    devkb_commit = _git_value(repo_root or Path(), "rev-parse", "HEAD")
    worktree_dirty = bool(_git_value(repo_root or Path(), "status", "--short"))

    holdout_attempt: int | None = None
    if split == "holdout":
        prior_attempts = [r for r in read_holdout_ledger(output_dir) if r.get("event") == "started"]
        if prior_attempts and not acknowledge_rerun:
            raise InvalidInputError(
                f"holdout 此前已被访问 {len(prior_attempts)} 次"
                f"（台账 {output_dir / HOLDOUT_LEDGER_NAME}，含中断/失败尝试）："
                "§7 不得静默重跑挑最好成绩；再次运行须经用户裁决，"
                "并以 --acknowledge-rerun '<裁决理由>' 显式标记"
            )
        holdout_attempt = len(prior_attempts) + 1
        # 读题即算一次访问：先落盘再动数据（进程被打断也留痕）
        append_holdout_ledger(
            output_dir,
            {
                "event": "started",
                "attempt": holdout_attempt,
                "at": datetime.now(TIMEZONE).isoformat(timespec="seconds"),
                "devkb_commit": devkb_commit,
                "devkb_worktree_dirty": worktree_dirty,
                "project": project_slug,
                "modes": list(modes),
                "command": command,
                "acknowledge_rerun": acknowledge_rerun,
            },
        )
    try:
        written = await _run_eval_body(
            settings,
            split=split,
            modes=modes,
            project_slug=project_slug,
            output_dir=output_dir,
            evalsets_dir=evalsets_dir,
            embedder=embedder,
            llm=llm,
            enforce_counts=enforce_counts,
            command=command,
            devkb_commit=devkb_commit,
            worktree_dirty=worktree_dirty,
            holdout_attempt=holdout_attempt,
            acknowledge_rerun=acknowledge_rerun,
        )
    except Exception as exc:
        if holdout_attempt is not None:
            append_holdout_ledger(
                output_dir,
                {
                    "event": "failed",
                    "attempt": holdout_attempt,
                    "at": datetime.now(TIMEZONE).isoformat(timespec="seconds"),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        raise
    if holdout_attempt is not None:
        append_holdout_ledger(
            output_dir,
            {
                "event": "completed",
                "attempt": holdout_attempt,
                "at": datetime.now(TIMEZONE).isoformat(timespec="seconds"),
                "reports": [str(json_path) for json_path, _ in written],
            },
        )
    return written


async def _run_eval_body(
    settings: Settings,
    *,
    split: Split,
    modes: list[EvalMode],
    project_slug: str,
    output_dir: Path,
    evalsets_dir: Path,
    embedder: Embedder | None,
    llm: LLMClient | None,
    enforce_counts: bool,
    command: str,
    devkb_commit: str,
    worktree_dirty: bool,
    holdout_attempt: int | None,
    acknowledge_rerun: str | None,
) -> list[tuple[Path, Path]]:
    """实际评测与报告装配；holdout 的一次性访问护栏由 run_eval 负责。"""
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

            # 纯 lexical 评测不加载真实 embedding 模型（轻量路径）
            needs_embedder = run_agentic or any(m != "lexical" for m in retrieval_modes)
            if embedder is None and needs_embedder:
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
                if retrieval_modes
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
                    session, project.id, questions, split=split, embedder=embedder, llm=llm
                )
    finally:
        await engine.dispose()

    now = datetime.now(TIMEZONE)
    snapshot = sorted((doc.rel_path, doc.content_hash) for doc in documents)
    corpus = {
        "project": project_slug,
        "document_count": len(snapshot),
        "chunk_count": chunk_count,
        "sha256": corpus_hash(snapshot),
        "manifest": [{"rel_path": p, "content_hash": h} for p, h in snapshot],
    }
    base_report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "run": {
            "started_at": now.isoformat(timespec="seconds"),
            "split": split,
            "modes": modes,
            "devkb_commit": devkb_commit,
            "devkb_worktree_dirty": worktree_dirty,
            "command": command,
            "holdout_accessed": split == "holdout",
            "holdout_attempt": holdout_attempt,
            "holdout_rerun_acknowledged": acknowledge_rerun,
        },
        "corpus": corpus,
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
            # §7 第 3 条：与 P0 历史基线同口径对照 + 语料规模变化声明（非硬 Gate）
            "p0_baseline": p0_baseline_comparison(retrieval_result, corpus),
            "gates": {
                "retrieval": (
                    retrieval_gates(retrieval_result, split="holdout") if retrieval_result else None
                ),
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
            "gates": {"retrieval": retrieval_gates(retrieval_result, split="dev")},
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
