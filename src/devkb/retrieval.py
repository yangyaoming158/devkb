"""向量 / lexical 检索与 RRF 融合（规格 §8）：project 范围内检索 top-k，带解释性字段。

查询构造在 repositories.py（D7），FTS token 化在 fts.py，
本模块只做嵌入调用、纯函数融合与结果组装。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from devkb.embedding import Embedder
from devkb.fts import tokenize_query
from devkb.repositories import ChunkRepo

RRF_K_DEFAULT = 60


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


# ---------------------------------------------------------------------------
# RRF 融合（§8 第 3–5 条）：纯函数，不触库、不触模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelRanking:
    """单次 (query, channel) 检索的有序候选，名次由位置隐含（首位 = rank 1）。

    输入按设计只携带 chunk_id 序、不携带任何原始分数——余弦相似度与
    ts_rank_cd 量纲不同，在类型层面即不可能被混加（§8"RRF 不混加原始 score"）。
    """

    query: str
    channel: str  # "vector" | "lexical"（融合本身 channel 无关，编排层约定取值）
    chunk_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class FusedChunk:
    """融合结果项：fused_score 降序，并列按 chunk_id 升序保证输出确定。"""

    chunk_id: uuid.UUID
    fused_score: float
    # (channel, 该 channel 跨 query 的最优名次)，按 channel 名排序
    channel_ranks: tuple[tuple[str, int], ...]
    hit_queries: tuple[str, ...]  # 命中该 chunk 的 query，按输入首见顺序

    def rank_for(self, channel: str) -> int | None:
        for name, rank in self.channel_ranks:
            if name == channel:
                return rank
        return None


def rrf_fuse(rankings: Sequence[ChannelRanking], *, k: int = RRF_K_DEFAULT) -> list[FusedChunk]:
    """Reciprocal Rank Fusion：fused_score(d) = Σ 1/(k + rank)，对 d 出现的每个
    (query, channel) ranking 求和。

    - 同一 ranking 内重复 chunk_id 只计首个（最优）名次；
    - 跨 ranking 按 chunk_id 去重合并贡献，各 channel 保留跨 query 最优名次；
    - 返回完整融合列表（不截断），final top-k 由编排层裁剪。
    """
    if k < 1:
        raise ValueError(f"RRF k 必须 >= 1：{k}")
    scores: dict[uuid.UUID, float] = {}
    best_ranks: dict[uuid.UUID, dict[str, int]] = {}
    hit_queries: dict[uuid.UUID, list[str]] = {}
    for ranking in rankings:
        seen: set[uuid.UUID] = set()
        for rank, chunk_id in enumerate(ranking.chunk_ids, start=1):
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
            ranks = best_ranks.setdefault(chunk_id, {})
            if ranking.channel not in ranks or rank < ranks[ranking.channel]:
                ranks[ranking.channel] = rank
            queries = hit_queries.setdefault(chunk_id, [])
            if ranking.query not in queries:
                queries.append(ranking.query)
    ordered = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))
    return [
        FusedChunk(
            chunk_id=chunk_id,
            fused_score=scores[chunk_id],
            channel_ranks=tuple(sorted(best_ranks[chunk_id].items())),
            hit_queries=tuple(hit_queries[chunk_id]),
        )
        for chunk_id in ordered
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
