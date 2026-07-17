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


async def test_failed_update_excludes_stale_chunks_from_search(
    session: AsyncSession, tmp_path: Path
) -> None:
    """active 文档更新失败 → 文档标 failed，保留的旧 chunks 必须退出向量检索。

    否则会引用与当前文件行号不符的陈旧内容（2026-07-15 外部审查发现的规格缺失）。
    """
    embedder = FakeEmbedder()
    doc = tmp_path / "guide.md"
    doc.write_text("# 部署指南\n\n库存服务先于订单服务启动。\n", encoding="utf-8")
    project_id = await _new_project(session)

    report = await ingest_directory(
        session, project_id, tmp_path, embedder=embedder, count_tokens=approx_token_counter
    )
    assert report.count("ingested") == 1

    chunk_repo = ChunkRepo(session, project_id)
    query = embedder.embed_query("库存服务先于订单服务启动。")
    assert any(
        rel_path == "guide.md" for _, rel_path, _ in await chunk_repo.vector_search(query, 5)
    )

    # 同一文件更新为不可解码内容：重摄取失败，旧 chunks 因事务回滚保留
    doc.write_bytes(UNDECODABLE)
    report = await ingest_directory(
        session, project_id, tmp_path, embedder=embedder, count_tokens=approx_token_counter
    )
    assert report.count("failed") == 1

    document = await DocumentRepo(session, project_id).get_by_rel_path("guide.md")
    assert document is not None and document.status == "failed"
    assert await chunk_repo.count() > 0  # 旧 chunks 仍在库（保留上一版是刻意行为）
    assert not any(  # 但绝不能再被检索到
        rel_path == "guide.md" for _, rel_path, _ in await chunk_repo.vector_search(query, 5)
    )


async def test_java_dispatch_and_broken_isolation(session: AsyncSession, tmp_path: Path) -> None:
    """T13.2：.java 走结构分块入库；坏 Java 隔离为 failed 且批次继续；重摄取幂等。"""
    good = tmp_path / "src" / "OrderService.java"
    good.parent.mkdir()
    good.write_text(
        (Path(__file__).parents[1] / "fixtures" / "corpus_java" / "order_service.java").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    (tmp_path / "src" / "Garbage.java").write_text("%%% 完全不是 Java %%%", encoding="utf-8")
    (tmp_path / "note.md").write_text("# 说明\n\nJava 摄取测试。\n", encoding="utf-8")
    project_id = await _new_project(session)

    report = await ingest_directory(
        session, project_id, tmp_path, embedder=FakeEmbedder(), count_tokens=approx_token_counter
    )
    assert report.count("ingested") == 2 and report.count("failed") == 1

    doc_repo = DocumentRepo(session, project_id)
    java_doc = await doc_repo.get_by_rel_path("src/OrderService.java")
    assert java_doc is not None and java_doc.doc_type == "java" and java_doc.status == "active"
    garbage = await doc_repo.get_by_rel_path("src/Garbage.java")
    assert garbage is not None and garbage.status == "failed" and garbage.doc_type == "java"
    assert garbage.parse_error is not None and "无法" in garbage.parse_error

    # Java symbol 词面可检索（search_text 在插入点生成）且引用元数据真实
    hits = await ChunkRepo(session, project_id).lexical_search("createorder", top_k=5)
    assert hits, "Java 方法名应可词面命中"
    chunk, rel_path, _ = hits[0]
    assert rel_path == "src/OrderService.java"
    assert "createOrder" in chunk.title_path and chunk.start_line >= 1

    second = await ingest_directory(
        session, project_id, tmp_path, embedder=FakeEmbedder(), count_tokens=approx_token_counter
    )
    assert second.count("skipped") == 2 and second.count("ingested") == 0


async def test_config_files_ingested_as_plaintext(session: AsyncSession, tmp_path: Path) -> None:
    """T13.4：yml/yaml/properties 纯文本摄取；占位符不展开；隐藏目录与 5MB 限制生效。"""
    (tmp_path / "application.yml").write_text(
        "# 服务配置\nspring:\n  rabbitmq:\n    password: ${RABBIT_PASSWORD}\n"
        "\nserver:\n  port: 8080\n",
        encoding="utf-8",
    )
    (tmp_path / "app.properties").write_text("jwt.secret=${JWT_SECRET}\n", encoding="utf-8")
    (tmp_path / "compose.yaml").write_text("services:\n  db:\n    image: pg\n", encoding="utf-8")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "secret.yml").write_text("nope: 1\n", encoding="utf-8")
    (tmp_path / "huge.properties").write_bytes(b"k=v\n" * (MAX_FILE_BYTES // 4 + 1))
    project_id = await _new_project(session)

    report = await ingest_directory(
        session, project_id, tmp_path, embedder=FakeEmbedder(), count_tokens=approx_token_counter
    )
    assert report.count("ingested") == 3 and report.count("failed") == 1  # huge 超限
    assert ".hidden/secret.yml" not in {o.rel_path for o in report.outcomes}

    doc_repo = DocumentRepo(session, project_id)
    yml = await doc_repo.get_by_rel_path("application.yml")
    assert yml is not None and yml.doc_type == "config" and yml.title == "application.yml"
    huge = await doc_repo.get_by_rel_path("huge.properties")
    assert huge is not None and huge.status == "failed" and huge.doc_type == "config"

    # 占位符逐字保留 + 行号真实 + 词面可检索
    hits = await ChunkRepo(session, project_id).lexical_search("rabbit_password", top_k=5)
    assert len(hits) == 1
    chunk, rel_path, _ = hits[0]
    assert rel_path == "application.yml" and "${RABBIT_PASSWORD}" in chunk.content
    source_lines = (tmp_path / "application.yml").read_text(encoding="utf-8").splitlines()
    assert chunk.content == "\n".join(source_lines[chunk.start_line - 1 : chunk.end_line])

    second = await ingest_directory(
        session, project_id, tmp_path, embedder=FakeEmbedder(), count_tokens=approx_token_counter
    )
    assert second.count("skipped") == 3 and second.count("ingested") == 0
