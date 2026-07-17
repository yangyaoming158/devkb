"""T12.2 search_text：摄取即填充（复评修复回归）+ backfill 幂等/可回滚/项目隔离。"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from devkb.repositories import ChunkDraft, ChunkRepo, DocumentRepo, ProjectRepo


def _vec(hot: int) -> list[float]:
    v = [0.0] * 1024
    v[hot] = 1.0
    return v


async def _seed_two_chunks(session: AsyncSession, slug_prefix: str) -> uuid.UUID:
    project = await ProjectRepo(session).create(f"{slug_prefix}-{uuid.uuid4().hex[:6]}", "BF")
    doc = await DocumentRepo(session, project.id).upsert(
        rel_path="docs/pay.md", title="Pay", doc_type="markdown", content_hash="h"
    )
    await ChunkRepo(session, project.id).replace_for_document(
        doc.id,
        [
            ChunkDraft(0, "Pay > 支付", "PaymentService 处理支付成功事件", "c0", 8, 1, 3, _vec(0)),
            ChunkDraft(1, "Pay > 订单", "订单状态机返回错误码 40901", "c1", 8, 4, 6, _vec(1)),
        ],
    )
    return project.id


async def _count_where(session: AsyncSession, project_id: uuid.UUID, condition: str) -> int:
    result = await session.execute(
        text(f"SELECT count(*) FROM chunks WHERE project_id = :pid AND {condition}"),
        {"pid": project_id},
    )
    return result.scalar_one()


async def _blank_search_text(session: AsyncSession, project_id: uuid.UUID) -> None:
    """模拟迁移 0002 升级后、backfill 之前的存量数据形态。"""
    await session.execute(
        text("UPDATE chunks SET search_text = '' WHERE project_id = :pid"),
        {"pid": project_id},
    )
    await session.commit()


async def test_ingestion_fills_search_text_at_insert(session: AsyncSession) -> None:
    """复评修复回归：chunk 唯一写入口径（replace_for_document）必须当场生成 search_text。"""
    pid = await _seed_two_chunks(session, "bf-ingest")
    await session.commit()

    assert await _count_where(session, pid, "search_text = ''") == 0, "新摄取不得留空 search_text"
    hits = await ChunkRepo(session, pid).lexical_search("paymentservice", top_k=5)
    assert len(hits) == 1, "新摄取后无需 backfill 即可词面检索"
    # 与 backfill 使用同一函数：立即重算应为 0 更新
    total, updated = await ChunkRepo(session, pid).backfill_search_text()
    assert (total, updated) == (2, 0), "新摄取写入值必须与 backfill 重算值逐字一致"


async def test_backfill_fills_legacy_rows_idempotently_and_stays_project_scoped(
    session: AsyncSession,
) -> None:
    own_id = await _seed_two_chunks(session, "bf-own")
    other_id = await _seed_two_chunks(session, "bf-other")
    await session.commit()
    await _blank_search_text(session, own_id)
    await _blank_search_text(session, other_id)

    total, updated = await ChunkRepo(session, own_id).backfill_search_text()
    await session.commit()
    assert (total, updated) == (2, 2)

    # 生成列生效：token 化后的标识符/中文/错误码可经 tsquery 命中
    for query, expected in [("paymentservice", 1), ("支付", 1), ("40901", 1), ("不存在词", 0)]:
        hits = await _count_where(
            session, own_id, f"search_tsv @@ plainto_tsquery('simple', '{query}')"
        )
        assert hits == expected, f"tsquery {query!r} 应命中 {expected} 块"
    assert await _count_where(session, own_id, "search_text <> ''") == 2, "非空率应为 100%"

    # 幂等：重复执行 0 更新
    total2, updated2 = await ChunkRepo(session, own_id).backfill_search_text()
    assert (total2, updated2) == (2, 0)

    # 项目隔离：other 项目的存量空值不受影响
    assert await _count_where(session, other_id, "search_text = ''") == 2


async def test_backfill_is_transactional_and_rollbackable(session: AsyncSession) -> None:
    project_id = await _seed_two_chunks(session, "bf-rb")
    await session.commit()
    await _blank_search_text(session, project_id)

    total, updated = await ChunkRepo(session, project_id).backfill_search_text()
    assert (total, updated) == (2, 2)
    await session.rollback()

    assert await _count_where(session, project_id, "search_text = ''") == 2, (
        "未 commit 的 backfill 回滚后不留任何痕迹"
    )
