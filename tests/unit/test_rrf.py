"""T15.1 RRF 纯函数 golden 全集与行为断言（规格 §8 第 3–5 条）。

golden 快照锁定 `rrf_fuse` 对判据列举场景（并列、缺失 channel、重复 chunk、
空结果、多 query 融合）的**完整输出**（含 fused_score 浮点值与排序）。
任何输出变化都会打破快照，必须人工审阅 DEVKB_UPDATE_GOLDEN=1 的 diff——
RRF 是 T15.4 Gate 与 holdout 的融合口径，变更即产生新评测报告。
"""

from __future__ import annotations

import itertools
import uuid

import pytest
from conftest import FIXTURES_DIR, GoldenCheck

from devkb.retrieval import RRF_K_DEFAULT, ChannelRanking, FusedChunk, rrf_fuse

GOLDEN_PATH = FIXTURES_DIR / "golden_rrf" / "fuse_cases.json"


def cid(n: int) -> uuid.UUID:
    """确定性 chunk_id：整数 n 的 UUID 表示，golden 中可读且大小序与 n 一致。"""
    return uuid.UUID(int=n)


# case 名 → (rankings, k)。覆盖 T15.1 判据列举的全部场景。
CASES: dict[str, tuple[list[ChannelRanking], int]] = {
    # 双 channel 基本融合：chunk 2 两路命中，去重合并贡献并保留两个 channel rank
    "two_channels_overlap": (
        [
            ChannelRanking("订单如何取消", "vector", (cid(1), cid(2), cid(3))),
            ChannelRanking("订单如何取消", "lexical", (cid(2), cid(4))),
        ],
        RRF_K_DEFAULT,
    ),
    # 并列：两 chunk 各在一路同名次，fused_score 相等，按 chunk_id 升序决并列
    "tie_broken_by_chunk_id": (
        [
            ChannelRanking("q", "vector", (cid(9),)),
            ChannelRanking("q", "lexical", (cid(3),)),
        ],
        RRF_K_DEFAULT,
    ),
    # 缺失 channel：lexical 空结果不贡献分数，chunk 只有 vector rank
    "missing_channel": (
        [
            ChannelRanking("q", "vector", (cid(1), cid(2))),
            ChannelRanking("q", "lexical", ()),
        ],
        RRF_K_DEFAULT,
    ),
    # 同一 ranking 内重复 chunk：只计首个（最优）名次，rank 3 的重复出现被忽略
    "duplicate_within_ranking": (
        [ChannelRanking("q", "vector", (cid(7), cid(8), cid(7)))],
        RRF_K_DEFAULT,
    ),
    # 空输入：无 ranking → 空输出
    "empty_rankings": ([], RRF_K_DEFAULT),
    # 多 query 同 channel：chunk 1 两个 query 贡献相加、hit_queries 首见序、
    # channel rank 取跨 query 最优
    "multi_query_same_channel": (
        [
            ChannelRanking("原查询", "vector", (cid(2), cid(1))),
            ChannelRanking("改写查询", "vector", (cid(1),)),
            ChannelRanking("原查询", "lexical", (cid(1), cid(3))),
        ],
        RRF_K_DEFAULT,
    ),
}


def fused_to_jsonable(rankings: list[ChannelRanking], fused: list[FusedChunk]) -> dict[str, object]:
    return {
        "rankings": [
            {"query": r.query, "channel": r.channel, "chunk_ids": [str(c) for c in r.chunk_ids]}
            for r in rankings
        ],
        "fused": [
            {
                "chunk_id": str(f.chunk_id),
                "fused_score": f.fused_score,
                "channel_ranks": [[channel, rank] for channel, rank in f.channel_ranks],
                "hit_queries": list(f.hit_queries),
            }
            for f in fused
        ],
    }


def test_rrf_golden_full_set(golden_check: GoldenCheck) -> None:
    actual = [
        {"case": name, "k": k, **fused_to_jsonable(rankings, rrf_fuse(rankings, k=k))}
        for name, (rankings, k) in CASES.items()
    ]
    golden_check(GOLDEN_PATH, actual)


def test_default_k_is_60_and_score_formula() -> None:
    [only] = rrf_fuse([ChannelRanking("q", "vector", (cid(1),))])
    assert only.fused_score == 1.0 / (60 + 1)
    assert RRF_K_DEFAULT == 60


def test_two_channel_contributions_sum_not_raw_scores() -> None:
    """融合分只能来自 1/(k+rank) 求和——输入类型不携带原始 score，量纲混加不可表达。"""
    fused = rrf_fuse(
        [
            ChannelRanking("q", "vector", (cid(1),)),
            ChannelRanking("q", "lexical", (cid(2), cid(1))),
        ]
    )
    by_id = {f.chunk_id: f for f in fused}
    assert by_id[cid(1)].fused_score == pytest.approx(1 / 61 + 1 / 62)
    assert by_id[cid(1)].channel_ranks == (("lexical", 2), ("vector", 1))
    assert by_id[cid(2)].fused_score == pytest.approx(1 / 61)
    assert by_id[cid(2)].rank_for("vector") is None, "缺失 channel 的 rank 必须为 None"


def test_stable_output_across_repeated_calls() -> None:
    for rankings, k in CASES.values():
        first = rrf_fuse(rankings, k=k)
        assert first == rrf_fuse(rankings, k=k)
        scores = [f.fused_score for f in first]
        assert scores == sorted(scores, reverse=True), "fused_score 必须降序"
        for left, right in itertools.pairwise(first):
            if left.fused_score == right.fused_score:
                assert left.chunk_id < right.chunk_id, "并列必须按 chunk_id 升序"


def test_invalid_k_rejected() -> None:
    with pytest.raises(ValueError):
        rrf_fuse([], k=0)
