"""向量检索（规格 §8）：查询嵌入 → project 范围内检索 top-k → chunk + 余弦分数。

无融合、无重排、无阈值门（P1 T15 加）。查询构造在 repositories.py（D7），
FTS token 化在 fts.py，本模块只做嵌入调用与结果组装。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from devkb.embedding import Embedder
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
