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
        assert c.content == "\n".join(lines[c.start_line - 1 : c.end_line]), (
            f"chunk 内容必须逐字对应源行：{c.title_path}"
        )
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


def test_long_method_kept_whole_until_t13_2() -> None:
    _, chunks = _chunk_fixture("long_method")
    by_title = {c.title_path: c for c in chunks}
    reconcile = by_title[
        "com.example.batch.InventoryReconcileJob > InventoryReconcileJob > reconcile"
    ]
    assert "step-0" in reconcile.content and "step-29" in reconcile.content
    assert reconcile.token_count > 400, "该 fixture 必须超目标（T13.2 拆分的对象）"


def test_broken_file_recovers_parseable_members() -> None:
    _, chunks = _chunk_fixture("broken")
    titles = [c.title_path for c in chunks]
    assert "com.example.broken.HalfBroken > HalfBroken > increment" in titles, (
        "语法错误文件中完好的方法应被容错恢复"
    )


def test_unparseable_source_raises_parse_error() -> None:
    with pytest.raises(ParseError):
        chunk_java("%%% 完全不是 Java %%%", count_tokens=approx_token_counter)
