"""向量与 lexical 检索（规格 §8）：project 范围内检索 top-k，带解释性字段。

无融合、无重排、无阈值门（P1 T15 加）。查询构造在 repositories.py（D7），
FTS token 化在 fts.py，本模块只做嵌入调用与结果组装。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from devkb.embedding import Embedder
from devkb.fts import tokenize_query
from devkb.repositories import ChunkRepo


@dataclass(frozen=True)
class RetrievedChunk:
    """检索命中项：引用渲染所需的完整元数据（rel_path:L{start}-L{end} · title_path）。"""

    chunk_id: uuid.UUID
    rel_path: str
    title_path: str
    content: str
    start_line: int
    end_line: int
    score: float


@dataclass(frozen=True)
class LexicalHit:
    """lexical channel 命中项：chunk 元数据 + 排名解释字段（§8 第 6 条，轨迹与评测用）。"""

    chunk_id: uuid.UUID
    rel_path: str
    title_path: str
    content: str
    start_line: int
    end_line: int
    lexical_rank: int  # 1-based，channel 内名次
    score: float  # ts_rank_cd 原始分——只用于 channel 内排序解释，不与余弦分数混加
    query: str  # 命中该结果的原始查询
    query_tokens: tuple[str, ...]  # 实际参与 tsquery 的 token（与 lexical_search 内部一致）


async def lexical_retrieve(
    session: AsyncSession,
    project_id: uuid.UUID,
    query: str,
    *,
    top_k: int = 8,
) -> list[LexicalHit]:
    """FTS 检索并组装解释字段。token 化是纯函数，此处复算的 query_tokens
    与 lexical_search 内部构造 tsquery 所用的完全一致（同函数同输入）。"""
    query_tokens = tuple(tokenize_query(query))
    rows = await ChunkRepo(session, project_id).lexical_search(query, top_k)
    return [
        LexicalHit(
            chunk_id=chunk.id,
            rel_path=rel_path,
            title_path=chunk.title_path,
            content=chunk.content,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            lexical_rank=rank,
            score=score,
            query=query,
            query_tokens=query_tokens,
        )
        for rank, (chunk, rel_path, score) in enumerate(rows, start=1)
    ]


async def retrieve(
    session: AsyncSession,
    project_id: uuid.UUID,
    query: str,
    *,
    embedder: Embedder,
    top_k: int = 8,
) -> list[RetrievedChunk]:
    query_embedding = embedder.embed_query(query)
    rows = await ChunkRepo(session, project_id).vector_search(query_embedding, top_k)
    return [
        RetrievedChunk(
            chunk_id=chunk.id,
            rel_path=rel_path,
            title_path=chunk.title_path,
            content=chunk.content,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            score=score,
        )
        for chunk, rel_path, score in rows
    ]
