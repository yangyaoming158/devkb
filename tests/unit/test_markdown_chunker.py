"""快照 A（近似计数器，CI 可跑）：锁 chunk 边界/行号/title_path/近似 token_count。

外加与计数器无关的机制不变量：内容逐字对应源行、hash 稳定、行号单调不重叠、
围栏与表格原子、超长段落被递归切分。
"""

from __future__ import annotations

import hashlib
import itertools

import pytest

from conftest import CORPUS_MD_DIR, FIXTURES_DIR, GoldenCheck, chunks_to_jsonable
from devkb.ingest.markdown import MdChunk, approx_token_counter, chunk_markdown

GOLDEN_DIR = FIXTURES_DIR / "golden"
FIXTURE_NAMES = ["zh", "en", "long_section", "table", "fenced"]


def _chunk_fixture(name: str) -> tuple[str, list[MdChunk]]:
    source = (CORPUS_MD_DIR / f"{name}.md").read_text(encoding="utf-8")
    return source, chunk_markdown(source, count_tokens=approx_token_counter)


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_golden_snapshot_a(name: str, golden_check: GoldenCheck) -> None:
    _, chunks = _chunk_fixture(name)
    assert chunks, f"fixture {name}.md 不应产出空 chunk 列表"
    golden_check(GOLDEN_DIR / f"{name}.json", chunks_to_jsonable(chunks))


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_content_is_verbatim_line_slice(name: str) -> None:
    source, chunks = _chunk_fixture(name)
    lines = source.splitlines()
    for c in chunks:
        assert c.content == "\n".join(lines[c.start_line - 1 : c.end_line]).strip("\n")
        assert c.content_hash == hashlib.sha256(c.content.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_line_ranges_monotonic_and_disjoint(name: str) -> None:
    _, chunks = _chunk_fixture(name)
    for prev, cur in itertools.pairwise(chunks):
        assert cur.ordinal == prev.ordinal + 1
        assert prev.start_line <= prev.end_line
        assert cur.start_line > prev.end_line, "chunk 行号区间不得重叠"


def test_fence_never_split() -> None:
    _, chunks = _chunk_fixture("fenced")
    fence_chunks = [c for c in chunks if "```java" in c.content]
    assert len(fence_chunks) == 1
    assert fence_chunks[0].content.count("```") == 2, "代码围栏必须开闭完整地留在同一 chunk"
    assert "InventoryService" in fence_chunks[0].content
    assert "InventoryLog.released" in fence_chunks[0].content
    # 该围栏本身即超目标：验证"原子块超目标时整块保留"而非被切
    assert fence_chunks[0].token_count > 400


def test_table_kept_atomic() -> None:
    _, chunks = _chunk_fixture("table")
    table_chunks = [c for c in chunks if "| 环境 |" in c.content]
    assert len(table_chunks) == 1
    assert "| prod |" in table_chunks[0].content, "表格必须整块保留在同一 chunk"


def test_title_path_breadcrumb() -> None:
    _, chunks = _chunk_fixture("zh")
    paths = {c.title_path for c in chunks}
    assert "架构总览 > 库存 > 并发控制" in paths
    assert "架构总览 > 库存 > 回补策略" in paths
    assert "架构总览 > 支付" in paths
    # 第二个 h1 出现后面包屑重置
    assert "附录" in paths


def test_long_paragraph_recursively_split() -> None:
    _, chunks = _chunk_fixture("long_section")
    section = [c for c in chunks if c.title_path == "订单履约全流程 > 状态机与补偿"]
    assert len(section) > 1, "超目标章节必须被切成多个 chunk"
    # 非原子块递归切分后，各 chunk 不应显著超目标（下限是单行）
    assert all(c.token_count <= 400 * 2 for c in section)


def test_approx_token_counter_deterministic() -> None:
    assert approx_token_counter("") == 0
    assert approx_token_counter("库存扣减") == 4  # CJK ≈ 1 token/字
    assert approx_token_counter("abcdefgh") == 2  # 其余 ≈ 4 字符/token
    assert approx_token_counter("库存ab") == 3
