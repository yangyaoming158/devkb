"""摄取管道（规格 §7：同步、逐文档隔离失败）。

扫描（仅 .md/.txt，单文件 ≤5MB，跳过隐藏目录/文件）→ 编码规范化
（charset-normalizer → UTF-8）→ content_hash 未变跳过 → 标题树分块 →
批量嵌入（batch 由 Embedder 内部按基准值控制，OOM 减半退避）→
单文档事务内删旧插新。单文档失败记 status='failed' + parse_error 后
继续下一文档；数据库级故障不属于单文档问题，照常抛出中断批次。

数据访问全部经 Repository（D7：本模块不构造任何 sqlalchemy 查询）。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from devkb.embedding import Embedder
from devkb.errors import EmbeddingError, ParseError, UnsupportedFileError
from devkb.ingest.markdown import TokenCounter, chunk_markdown
from devkb.repositories import ChunkDraft, ChunkRepo, DocumentRepo

logger = structlog.get_logger(__name__)

MAX_FILE_BYTES = 5 * 1024 * 1024
SUPPORTED_SUFFIXES = frozenset({".md", ".txt"})


@dataclass(frozen=True)
class FileOutcome:
    rel_path: str
    status: str  # ingested | skipped | failed
    chunks: int = 0
    error: str | None = None


@dataclass(frozen=True)
class IngestReport:
    outcomes: list[FileOutcome] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for o in self.outcomes if o.status == status)


def scan_files(root: Path) -> list[Path]:
    """确定性排序的候选文件列表；路径中任一段以 . 开头即跳过。"""
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix.lower() in SUPPORTED_SUFFIXES
        and not any(part.startswith(".") for part in p.relative_to(root).parts)
    )


def _normalize_text(raw: bytes) -> str:
    """字节 → UTF-8 文本。规格 §7：charset-normalizer 规范化；不可解码即解析失败。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    from charset_normalizer import from_bytes

    best = from_bytes(raw).best()
    if best is None:
        raise ParseError("无法识别文件编码")
    return str(best)


def _doc_title(source: str, path: Path) -> str:
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or path.stem
        if stripped:
            break
    return path.stem


@retry(
    retry=retry_if_exception_type(EmbeddingError),
    stop=stop_after_attempt(2),
    wait=wait_fixed(1),
    reraise=True,
)
def _embed_with_retry(embedder: Embedder, texts: list[str]) -> list[list[float]]:
    """瞬态嵌入故障重试 1 次（OOM 退避在 Embedder 内部）；仍失败按单文档隔离。"""
    return embedder.embed_documents(texts)


async def ingest_directory(
    session: AsyncSession,
    project_id: uuid.UUID,
    root: Path,
    *,
    embedder: Embedder,
    count_tokens: TokenCounter,
    target_tokens: int = 400,
) -> IngestReport:
    doc_repo = DocumentRepo(session, project_id)
    chunk_repo = ChunkRepo(session, project_id)
    report = IngestReport()

    for path in scan_files(root):
        rel_path = path.relative_to(root).as_posix()
        try:
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                raise UnsupportedFileError(f"文件 {size} 字节，超过 {MAX_FILE_BYTES} 上限")
            source = _normalize_text(path.read_bytes())
            content_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()

            existing = await doc_repo.get_by_rel_path(rel_path)
            if (
                existing is not None
                and existing.status == "active"
                and existing.content_hash == content_hash
            ):
                report.outcomes.append(FileOutcome(rel_path, "skipped"))
                continue

            try:
                chunks = chunk_markdown(
                    source, count_tokens=count_tokens, target_tokens=target_tokens
                )
            except Exception as exc:  # 分块器对任意文本的未知崩溃也按单文档失败隔离
                raise ParseError(f"分块失败：{type(exc).__name__}: {exc}") from exc

            embeddings = _embed_with_retry(embedder, [c.content for c in chunks]) if chunks else []
            document = await doc_repo.upsert(
                rel_path=rel_path,
                title=_doc_title(source, path),
                doc_type="markdown" if path.suffix.lower() == ".md" else "text",
                content_hash=content_hash,
            )
            n = await chunk_repo.replace_for_document(
                document.id,
                [
                    ChunkDraft(
                        ordinal=c.ordinal,
                        title_path=c.title_path,
                        content=c.content,
                        content_hash=c.content_hash,
                        token_count=c.token_count,
                        start_line=c.start_line,
                        end_line=c.end_line,
                        embedding=vec,
                    )
                    for c, vec in zip(chunks, embeddings, strict=True)
                ],
            )
            await session.commit()
            report.outcomes.append(FileOutcome(rel_path, "ingested", chunks=n))
            logger.info("document_ingested", rel_path=rel_path, chunks=n)
        except (UnsupportedFileError, ParseError, EmbeddingError, OSError) as exc:
            await session.rollback()
            reason = f"{type(exc).__name__}: {exc}"
            await doc_repo.mark_failed(rel_path, reason)
            await session.commit()
            report.outcomes.append(FileOutcome(rel_path, "failed", error=reason))
            logger.warning("document_failed", rel_path=rel_path, error=reason)

    return report
