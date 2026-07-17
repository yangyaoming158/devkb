"""T12.2 search_text backfill：填充正确、幂等、可回滚、项目隔离。"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from devkb.repositories import ChunkDraft, ChunkRepo, DocumentRepo, ProjectRepo
from devkb.retrieval import build_search_text


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


async def test_backfill_fills_tsv_idempotently_and_stays_project_scoped(
    session: AsyncSession,
) -> None:
    own_id = await _seed_two_chunks(session, "bf-own")
    other_id = await _seed_two_chunks(session, "bf-other")
    await session.commit()
    assert await _count_where(session, own_id, "search_text = ''") == 2, "摄取后未回填应为空"

    total, updated = await ChunkRepo(session, own_id).backfill_search_text(build_search_text)
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
    total2, updated2 = await ChunkRepo(session, own_id).backfill_search_text(build_search_text)
    assert (total2, updated2) == (2, 0)

    # 项目隔离：other 项目不受影响
    assert await _count_where(session, other_id, "search_text = ''") == 2


async def test_backfill_is_transactional_and_rollbackable(session: AsyncSession) -> None:
    project_id = await _seed_two_chunks(session, "bf-rb")
    await session.commit()

    total, updated = await ChunkRepo(session, project_id).backfill_search_text(build_search_text)
    assert (total, updated) == (2, 2)
    await session.rollback()

    assert await _count_where(session, project_id, "search_text = ''") == 2, (
        "未 commit 的 backfill 回滚后不留任何痕迹"
    )
