"""T6.3 摄取管道集成测试：计数正确、二次摄取幂等（零新增）、坏文件隔离不中断批次。

计数器用 approx_token_counter（CI 禁真模型）；语料复用 tests/fixtures/corpus_md。
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from devkb.embedding import FakeEmbedder
from devkb.ingest.markdown import approx_token_counter
from devkb.ingest.pipeline import MAX_FILE_BYTES, ingest_directory, scan_files
from devkb.repositories import ChunkRepo, DocumentRepo, ProjectRepo

CORPUS_MD = Path(__file__).parents[1] / "fixtures" / "corpus_md"
# charset-normalizer 确定性判定为不可解码的字节模式（本仓库实测）
UNDECODABLE = bytes([0x81, 0xFE, 0x00, 0xFF, 0x9D, 0x8F, 0x00, 0xC3]) * 128


def _build_corpus(root: Path) -> int:
    """标准测试语料：5 个 fixture md + 嵌套 md + GBK txt；外加应被忽略的文件。返回候选文件数。"""
    for f in CORPUS_MD.glob("*.md"):
        shutil.copy(f, root / f.name)
    (root / "docs").mkdir()
    (root / "docs" / "nested.md").write_text("# 嵌套文档\n\n子目录内容。\n", encoding="utf-8")
    (root / "notes.txt").write_bytes("运维笔记：数据库连接池上限 20。".encode("gbk"))
    # 以下不应出现在扫描结果中
    (root / ".hidden").mkdir()
    (root / ".hidden" / "secret.md").write_text("# 不该被摄取", encoding="utf-8")
    (root / "ignore.py").write_text("print('not a doc')", encoding="utf-8")
    return 7


async def _new_project(session: AsyncSession) -> uuid.UUID:
    project = await ProjectRepo(session).create(slug=f"t6-{uuid.uuid4().hex[:8]}", name="t6")
    await session.commit()
    return project.id


async def test_first_ingest_counts(session: AsyncSession, tmp_path: Path) -> None:
    expected = _build_corpus(tmp_path)
    project_id = await _new_project(session)

    assert [p.name for p in scan_files(tmp_path)].count("secret.md") == 0
    report = await ingest_directory(
        session, project_id, tmp_path, embedder=FakeEmbedder(), count_tokens=approx_token_counter
    )

    assert report.count("ingested") == expected
    assert report.count("skipped") == 0
    assert report.count("failed") == 0
    rel_paths = {o.rel_path for o in report.outcomes}
    assert "docs/nested.md" in rel_paths
    assert "ignore.py" not in rel_paths

    doc_repo = DocumentRepo(session, project_id)
    assert len(await doc_repo.list_active()) == expected
    notes = await doc_repo.get_by_rel_path("notes.txt")
    assert notes is not None and notes.doc_type == "text"  # GBK 已规范化摄取
    zh = await doc_repo.get_by_rel_path("zh.md")
    assert zh is not None and zh.title == "架构总览"
    chunk_repo = ChunkRepo(session, project_id)
    total = await chunk_repo.count()
    assert total == sum(o.chunks for o in report.outcomes)
    # T7 判据：摄取后全部 chunk 的 embedding 非空（vector_search 只见非空行）
    hits = await chunk_repo.vector_search(FakeEmbedder().embed_query("任意查询"), top_k=total + 1)
    assert len(hits) == total


async def test_reingest_is_idempotent(session: AsyncSession, tmp_path: Path) -> None:
    expected = _build_corpus(tmp_path)
    project_id = await _new_project(session)
    chunk_repo = ChunkRepo(session, project_id)

    await ingest_directory(
        session, project_id, tmp_path, embedder=FakeEmbedder(), count_tokens=approx_token_counter
    )
    baseline = await chunk_repo.count()

    second = await ingest_directory(
        session, project_id, tmp_path, embedder=FakeEmbedder(), count_tokens=approx_token_counter
    )
    assert second.count("skipped") == expected
    assert second.count("ingested") == 0
    assert await chunk_repo.count() == baseline  # 二次摄取零新增

    # 变更一个文件：仅该文档重摄取，chunk 删旧插新
    (tmp_path / "docs" / "nested.md").write_text(
        "# 嵌套文档\n\n改写后的内容。\n\n## 新增小节\n\n补充说明。\n", encoding="utf-8"
    )
    third = await ingest_directory(
        session, project_id, tmp_path, embedder=FakeEmbedder(), count_tokens=approx_token_counter
    )
    assert third.count("ingested") == 1
    assert third.count("skipped") == expected - 1
    docs = await DocumentRepo(session, project_id).list_active()
    assert len(docs) == expected  # 文档数不变，无重复行


async def test_bad_files_isolated_batch_continues(session: AsyncSession, tmp_path: Path) -> None:
    expected = _build_corpus(tmp_path)
    (tmp_path / "bad_encoding.md").write_bytes(UNDECODABLE)
    (tmp_path / "too_big.md").write_bytes(b"# big\n" + b"x" * (MAX_FILE_BYTES + 1))
    project_id = await _new_project(session)

    report = await ingest_directory(
        session, project_id, tmp_path, embedder=FakeEmbedder(), count_tokens=approx_token_counter
    )

    assert report.count("failed") == 2
    assert report.count("ingested") == expected  # 坏文件不中断批次
    doc_repo = DocumentRepo(session, project_id)
    bad = await doc_repo.get_by_rel_path("bad_encoding.md")
    assert bad is not None and bad.status == "failed"
    assert bad.parse_error is not None and "编码" in bad.parse_error
    big = await doc_repo.get_by_rel_path("too_big.md")
    assert big is not None and big.status == "failed"
    assert len(await doc_repo.list_active()) == expected  # failed 不算 active
