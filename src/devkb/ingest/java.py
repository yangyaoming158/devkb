"""Java 结构化分块（规格 §6.3，T13.1）。

tree-sitter 纯解析——永不启动 JVM、执行构建脚本、解析外部实体或访问网络。
chunk 单位 = 结构成员：

- package/import 区（含文件头注释/license）；
- 类型头：class/interface/enum/record/@interface 的注解+签名到 ``{`` 行；
- enum 常量区、constructor、method、field group（连续字段一组）、static 块；
- 嵌套类型递归，title_path 逐层累加：
  ``com.example.OrderService > OrderService > createOrder``（根 = 主类型 FQN）。

每个成员 chunk 向上吸附紧邻注释/Javadoc（相隔 ≤1 行，不越过前一成员）；
content 为源行逐字切片，start/end_line 1-based 含端点——引用契约与 P0 一致。
超长成员的有界拆分在 T13.2；本模块当前整块保留。
语法错误容错解析可识别部分；整文件无有效 chunk 时抛 ParseError（单文件隔离）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import tree_sitter_java
from tree_sitter import Language, Node, Parser

from devkb.errors import ParseError
from devkb.ingest.markdown import TokenCounter

_LANGUAGE = Language(tree_sitter_java.language())

_TYPE_NODES = frozenset(
    {
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "record_declaration",
        "annotation_type_declaration",
    }
)
_COMMENT_NODES = frozenset({"line_comment", "block_comment"})
_FIELD_NODES = frozenset({"field_declaration", "constant_declaration"})
_NAMED_MEMBER_NODES = frozenset(
    {
        "method_declaration",
        "constructor_declaration",
        "compact_constructor_declaration",
        "annotation_type_element_declaration",
    }
)


@dataclass(frozen=True)
class JavaChunk:
    """与 MdChunk 同构：pipeline 以同一方式转 ChunkDraft。"""

    ordinal: int
    title_path: str
    content: str
    content_hash: str
    token_count: int
    start_line: int  # 1-based，含
    end_line: int  # 1-based，含


@dataclass(frozen=True)
class _Span:
    title_path: str
    start_row: int  # 0-based，含
    end_row: int  # 0-based，含


def _node_text(node: Node) -> str:
    return (node.text or b"").decode("utf-8", errors="replace")


def _type_name(node: Node) -> str:
    name = node.child_by_field_name("name")
    return _node_text(name) if name is not None else "(anonymous)"


def _package_name(root: Node) -> str:
    for child in root.children:
        if child.type == "package_declaration":
            for sub in child.children:
                if sub.type in ("scoped_identifier", "identifier"):
                    return _node_text(sub)
    return ""


def _attach_comments(siblings: list[Node], index: int, floor_row: int) -> int:
    """成员向上吸附紧邻注释/Javadoc：相隔 ≤1 行、不越过 floor_row（前一成员末行）。"""
    start_row = siblings[index].start_point.row
    for j in range(index - 1, -1, -1):
        prev = siblings[j]
        if prev.type not in _COMMENT_NODES:
            break
        if prev.end_point.row <= floor_row or start_row - prev.end_point.row > 1:
            break
        start_row = prev.start_point.row
    return start_row


def _member_title(node: Node) -> str:
    if node.type in _NAMED_MEMBER_NODES:
        return _type_name(node)
    if node.type == "static_initializer":
        return "static {}"
    return f"({node.type})"


def _walk_type(node: Node, path: list[str], spans: list[_Span], header_start: int) -> None:
    type_path = [*path, _type_name(node)]
    body = node.child_by_field_name("body")
    # 类型头：吸附的 Javadoc + 注解 + 签名（record 组件在签名行内）到 body 开括号行
    header_end = body.start_point.row if body is not None else node.end_point.row
    spans.append(_Span(" > ".join(type_path), header_start, header_end))
    if body is None:
        return

    members = body.children
    if body.type == "enum_body":
        constants = [c for c in members if c.type == "enum_constant"]
        if constants:
            spans.append(
                _Span(
                    " > ".join([*type_path, "constants"]),
                    _attach_comments(members, members.index(constants[0]), header_end),
                    constants[-1].end_point.row,
                )
            )
        declarations = [c for c in members if c.type == "enum_body_declarations"]
        members = declarations[0].children if declarations else []

    last_end_row = header_end
    field_run: list[tuple[int, int]] = []  # (吸附注释后的 start_row, end_row)

    def flush_fields() -> None:
        nonlocal last_end_row
        if field_run:
            spans.append(
                _Span(" > ".join([*type_path, "fields"]), field_run[0][0], field_run[-1][1])
            )
            last_end_row = field_run[-1][1]
            field_run.clear()

    for index, member in enumerate(members):
        if member.type in _FIELD_NODES:
            field_run.append((_attach_comments(members, index, last_end_row), member.end_point.row))
            continue
        if member.type in _COMMENT_NODES or not member.is_named:
            continue  # 注释由后续成员吸附；'{' '}' ';' 等标点跳过
        flush_fields()
        start_row = _attach_comments(members, index, last_end_row)
        if member.type in _TYPE_NODES:
            _walk_type(member, type_path, spans, start_row)
        elif member.type in _NAMED_MEMBER_NODES or member.type == "static_initializer":
            spans.append(
                _Span(
                    " > ".join([*type_path, _member_title(member)]),
                    start_row,
                    member.end_point.row,
                )
            )
        else:
            continue  # ERROR 等不可识别节点：容错跳过，不产出行号不可信的 chunk
        last_end_row = member.end_point.row
    flush_fields()


def chunk_java(
    source: str, *, count_tokens: TokenCounter, target_tokens: int = 400
) -> list[JavaChunk]:
    """结构化分块入口。target_tokens 供 T13.2 超长拆分使用，当前不切分。"""
    del target_tokens  # T13.2 接入
    tree = Parser(_LANGUAGE).parse(source.encode("utf-8"))
    root = tree.root_node
    lines = source.splitlines()

    package = _package_name(root)
    top_types = [c for c in root.children if c.type in _TYPE_NODES]

    def _fqn(type_name: str) -> str:
        if package and type_name:
            return f"{package}.{type_name}"
        return type_name or package or "(default)"

    # package/imports 区归属文件主类型（第一个顶层类型）的 FQN；
    # 其余顶层类型各用自己的 FQN 作根，面包屑不误挂到主类型下
    fqn_root = _fqn(_type_name(top_types[0]) if top_types else "")

    spans: list[_Span] = []
    top_children = list(root.children)
    header_nodes = [
        (i, c)
        for i, c in enumerate(top_children)
        if c.type in ("package_declaration", "import_declaration")
    ]
    last_top_row = -1
    if header_nodes:
        first_index = header_nodes[0][0]
        last_top_row = header_nodes[-1][1].end_point.row
        spans.append(
            _Span(
                f"{fqn_root} > package/imports",
                _attach_comments(top_children, first_index, -1),
                last_top_row,
            )
        )
    for index, child in enumerate(top_children):
        if child.type in _TYPE_NODES:
            _walk_type(
                child,
                [_fqn(_type_name(child))],
                spans,
                _attach_comments(top_children, index, last_top_row),
            )
            last_top_row = child.end_point.row

    if not spans:
        raise ParseError("无法从 Java 源码识别任何结构（package/类型/成员）")

    chunks: list[JavaChunk] = []
    for span in spans:
        content = "\n".join(lines[span.start_row : span.end_row + 1])
        if not content.strip():
            continue
        chunks.append(
            JavaChunk(
                ordinal=len(chunks),
                title_path=span.title_path,
                content=content,
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                token_count=count_tokens(content),
                start_line=span.start_row + 1,
                end_line=span.end_row + 1,
            )
        )
    if not chunks:
        raise ParseError("Java 源码解析后未产出任何非空 chunk")
    return chunks
