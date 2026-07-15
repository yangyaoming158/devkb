"""固定问答管道（规格 §6/§8，D6：P0 在线路径仅此一处 LLM 调用点）。

检索 → 证据编号 E1..En → 生成 prompt（仅依据证据、中文回答保留英文术语、
证据不含答案时明确说明）→ LLM 调用（超时/故障重问 1 次，有界）→
L0 校验（越界 [E#] 从文本剔除并记 warnings）→ Answer JSON → agent_runs 落库。

诚实性边界（P0 已知限制，见 README）：仅到 prompt 级 + L0 存在性校验；
确定性拒答与引文忠实度（L1）是 P1 内容。
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from devkb.embedding import Embedder
from devkb.errors import LLMError
from devkb.llm import LLMClient, LLMResult, compute_cost
from devkb.repositories import RunRepo
from devkb.retrieval import RetrievedChunk, retrieve

logger = structlog.get_logger(__name__)

_EVIDENCE_MARK = re.compile(r"\[E(\d+)\]")

SYSTEM_PROMPT = (
    "你是软件项目知识助手。只依据用户提供的证据回答问题，禁止使用证据之外的知识。"
    "用中文回答，保留英文术语与代码原文。"
    "引用证据时在相应句子后标注 [E编号]（如 [E1]）；只允许引用给出的证据编号。"
    "如果证据不足以回答问题，明确说明“提供的资料中没有找到相关信息”，不要编造。"
)


def build_user_prompt(question: str, evidences: list[RetrievedChunk]) -> str:
    blocks = [
        f"[E{i}] {e.rel_path}:L{e.start_line}-L{e.end_line} · {e.title_path}\n{e.content}"
        for i, e in enumerate(evidences, start=1)
    ]
    return "## 证据\n\n" + "\n\n".join(blocks) + f"\n\n## 问题\n\n{question}"


def apply_l0(answer_text: str, evidence_count: int) -> tuple[str, list[int], list[str]]:
    """L0 引用存在性校验（纯函数）。

    返回 (清洗后文本, 被引用且有效的证据编号升序, warnings)。
    越界 [E#]（不在 1..evidence_count）从文本剔除并记 warning。
    """
    warnings: list[str] = []
    cited: set[int] = set()

    def _check(match: re.Match[str]) -> str:
        num = int(match.group(1))
        if 1 <= num <= evidence_count:
            cited.add(num)
            return match.group(0)
        warnings.append(f"L0: 引用 [E{num}] 不在本次证据集合（共 {evidence_count} 条），已剔除")
        return ""

    cleaned = _EVIDENCE_MARK.sub(_check, answer_text)
    return cleaned, sorted(cited), warnings


@retry(
    retry=retry_if_exception_type(LLMError),
    stop=stop_after_attempt(2),
    wait=wait_fixed(1),
    reraise=True,
)
async def _complete_with_retry(llm: LLMClient, *, system: str, user: str) -> LLMResult:
    """超时/故障重问 1 次（D6：有界，不得无限重试）。"""
    return await llm.complete(system=system, user=user)


async def answer_question(
    session: AsyncSession,
    project_id: uuid.UUID,
    question: str,
    *,
    embedder: Embedder,
    llm: LLMClient,
    top_k: int = 8,
) -> dict[str, Any]:
    run_repo = RunRepo(session, project_id)
    run = await run_repo.create(question)
    await session.commit()
    started = time.perf_counter()

    try:
        evidences = await retrieve(session, project_id, question, embedder=embedder, top_k=top_k)
        result = await _complete_with_retry(
            llm, system=SYSTEM_PROMPT, user=build_user_prompt(question, evidences)
        )
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        await run_repo.finish(
            run.id,
            answer={"error": f"{type(exc).__name__}: {exc}"},
            model="",
            tokens_in=0,
            tokens_out=0,
            usage=None,
            cost=None,
            latency_ms=latency_ms,
            status="failed",
        )
        await session.commit()
        raise

    answer_text, cited, warnings = apply_l0(result.text, len(evidences))
    latency_ms = int((time.perf_counter() - started) * 1000)
    answer: dict[str, Any] = {
        "answer_text": answer_text,
        "citations": [
            {
                "evidence_id": f"E{num}",
                "chunk_id": str(evidences[num - 1].chunk_id),
                "rel_path": evidences[num - 1].rel_path,
                "start_line": evidences[num - 1].start_line,
                "end_line": evidences[num - 1].end_line,
                "title_path": evidences[num - 1].title_path,
                "score": round(evidences[num - 1].score, 4),
            }
            for num in cited
        ],
        "warnings": warnings,
        "stats": {
            "tokens_in": int(result.usage.get("prompt_tokens", 0)),
            "tokens_out": int(result.usage.get("completion_tokens", 0)),
            "latency_ms": latency_ms,
            "model": result.model,
        },
    }
    await run_repo.finish(
        run.id,
        answer=answer,
        model=result.model,
        tokens_in=answer["stats"]["tokens_in"],
        tokens_out=answer["stats"]["tokens_out"],
        usage=result.usage,
        cost=compute_cost(result.model, result.usage),
        latency_ms=latency_ms,
        status="succeeded",
    )
    await session.commit()
    logger.info(
        "run_finished",
        run_id=str(run.id),
        citations=len(answer["citations"]),
        warnings=len(warnings),
        latency_ms=latency_ms,
    )
    answer["run_id"] = str(run.id)
    return answer
