"""T13.1 Java 分块 golden：锁 chunk 边界/行号/title_path。

外加机制不变量：内容逐字对应源行（不 strip——引用必须与真实文件一致）、
hash 稳定、行号单调不重叠、语法错误容错、无结构输入抛 ParseError。
"""

from __future__ import annotations

import hashlib
import itertools

import pytest

from conftest import FIXTURES_DIR, GoldenCheck, chunks_to_jsonable
from devkb.errors import ParseError
from devkb.ingest.java import JavaChunk, chunk_java
from devkb.ingest.markdown import approx_token_counter

CORPUS_JAVA_DIR = FIXTURES_DIR / "corpus_java"
GOLDEN_DIR = FIXTURES_DIR / "golden_java"
FIXTURE_NAMES = ["order_service", "contracts", "long_method", "broken"]


def _chunk_fixture(name: str) -> tuple[str, list[JavaChunk]]:
    source = (CORPUS_JAVA_DIR / f"{name}.java").read_text(encoding="utf-8")
    return source, chunk_java(source, count_tokens=approx_token_counter)


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_golden_snapshot(name: str, golden_check: GoldenCheck) -> None:
    _, chunks = _chunk_fixture(name)
    assert chunks, f"fixture {name}.java 不应产出空 chunk 列表"
    golden_check(GOLDEN_DIR / f"{name}.json", chunks_to_jsonable(chunks))


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_content_is_verbatim_line_slice(name: str) -> None:
    source, chunks = _chunk_fixture(name)
    lines = source.splitlines()
    for c in chunks:
        cited = "\n".join(lines[c.start_line - 1 : c.end_line])
        if c.content != cited:
            # 超长拆分子块：内容 = 签名上下文 + 引用区间；两段都必须逐字来自源文件
            prefix, sep, rest = c.content.partition("\n" + cited)
            assert sep and not rest, f"chunk 内容必须以引用区间逐字结尾：{c.title_path}"
            assert prefix in source, f"签名上下文必须逐字来自源文件：{c.title_path}"
        assert c.content_hash == hashlib.sha256(c.content.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_line_ranges_monotonic_and_disjoint(name: str) -> None:
    _, chunks = _chunk_fixture(name)
    for prev, cur in itertools.pairwise(chunks):
        assert cur.ordinal == prev.ordinal + 1
        assert prev.start_line <= prev.end_line
        assert cur.start_line > prev.end_line, "chunk 行号区间不得重叠"


def test_title_paths_follow_fqn_breadcrumbs() -> None:
    _, chunks = _chunk_fixture("order_service")
    titles = [c.title_path for c in chunks]
    root = "com.example.order.OrderService"
    assert f"{root} > package/imports" in titles
    assert f"{root} > OrderService" in titles, "类型头 chunk（注解 + 签名）"
    assert f"{root} > OrderService > createOrder" in titles, "方法 chunk（规格 §6.3 示例格式）"
    assert f"{root} > OrderService > OrderService" in titles, "构造器 chunk"
    assert f"{root} > OrderService > fields" in titles, "field group chunk"
    assert f"{root} > OrderService > static {{}}" in titles, "static 块 chunk"
    assert f"{root} > OrderService > RetryPolicy" in titles, "嵌套类类型头"
    assert f"{root} > OrderService > RetryPolicy > shouldRetry" in titles, "嵌套类方法"


def test_javadoc_annotations_and_generics_stay_with_member() -> None:
    _, chunks = _chunk_fixture("order_service")
    by_title = {c.title_path: c for c in chunks}
    root = "com.example.order.OrderService"

    header = by_title[f"{root} > OrderService"]
    assert "@Service" in header.content and "@Transactional" in header.content
    assert "订单应用服务" in header.content, "类型头应吸附紧邻 Javadoc"

    create = by_title[f"{root} > OrderService > createOrder"]
    assert "客户端幂等键" in create.content, "方法应吸附紧邻 Javadoc"
    assert create.content.rstrip().endswith("}"), "方法体完整保留"

    group_by = by_title[f"{root} > OrderService > groupBy"]
    assert "<K, V>" in group_by.content, "泛型签名保留"
    assert "// 泛型辅助：按键聚合" in group_by.content, "行注释也应吸附"

    fields = by_title[f"{root} > OrderService > fields"]
    assert "idempotencyCache" in fields.content and "MAX_RETRY" in fields.content
    assert "幂等键缓存" in fields.content, "字段 Javadoc 属于 field group"

    imports = by_title[f"{root} > package/imports"]
    assert "License header" in imports.content, "文件头注释归 package/imports 区"
    assert "import java.util.Map;" in imports.content


def test_interface_enum_record_recognized() -> None:
    _, chunks = _chunk_fixture("contracts")
    titles = [c.title_path for c in chunks]
    pkg = "com.example.contract"
    # 同文件多个顶层类型：各用自己的 FQN 作根，不误挂到主类型下
    assert f"{pkg}.PaymentGateway > PaymentGateway > fields" in titles, (
        "接口常量按 field group 识别"
    )
    assert f"{pkg}.PaymentGateway > PaymentGateway > isDuplicate" in titles, "default 方法识别"
    assert f"{pkg}.OrderStatus > OrderStatus > constants" in titles, "枚举常量区识别"
    assert f"{pkg}.OrderStatus > OrderStatus > isTerminal" in titles, "枚举方法识别"
    assert f"{pkg}.PaymentResult > PaymentResult" in titles, "record 类型头识别"
    assert f"{pkg}.PaymentResult > PaymentResult > PaymentResult" in titles, "紧凑构造器识别"

    by_title = {c.title_path: c for c in chunks}
    assert "(String orderId, int code, String message)" in (
        by_title[f"{pkg}.PaymentResult > PaymentResult"].content
    ), "record 组件在类型头签名内"
    assert "PENDING_PAYMENT" in by_title[f"{pkg}.OrderStatus > OrderStatus > constants"].content


def test_long_method_split_with_signature_context() -> None:
    source, chunks = _chunk_fixture("long_method")
    lines = source.splitlines()
    title = "com.example.batch.InventoryReconcileJob > InventoryReconcileJob > reconcile"
    parts = [c for c in chunks if c.title_path == title]
    assert len(parts) >= 2, "超目标方法必须被拆分为多个子块"

    assert parts[0].content.lstrip().startswith("public void reconcile()"), "首块从签名开始"
    for sub in parts[1:]:
        assert sub.content.startswith("    public void reconcile() {"), (
            "后续子块必须重复最小签名上下文"
        )
        cited = "\n".join(lines[sub.start_line - 1 : sub.end_line])
        assert sub.content == "    public void reconcile() {\n" + cited, (
            "签名之外的内容必须逐字等于引用区间"
        )
        assert cited.lstrip().startswith(("int drift", "audit(")), "拆分落在真实语句边界"

    # 全部子块拼起来覆盖整个方法体（区间连续无缝、无重叠）
    for prev, cur in itertools.pairwise(parts):
        assert cur.start_line == prev.end_line + 1, "子块引用区间必须连续"
    assert "step-0" in parts[0].content and "step-29" in parts[-1].content
    assert all(c.token_count <= 400 + 60 for c in parts), "子块规模受目标 token 约束"


def test_broken_file_recovers_parseable_members() -> None:
    _, chunks = _chunk_fixture("broken")
    titles = [c.title_path for c in chunks]
    assert "com.example.broken.HalfBroken > HalfBroken > increment" in titles, (
        "语法错误文件中完好的方法应被容错恢复"
    )


def test_unparseable_source_raises_parse_error() -> None:
    with pytest.raises(ParseError):
        chunk_java("%%% 完全不是 Java %%%", count_tokens=approx_token_counter)
