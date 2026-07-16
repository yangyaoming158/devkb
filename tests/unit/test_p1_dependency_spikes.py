"""T11.2 P1 新依赖的最小兼容性契约。

这些 spike 已收敛为正式测试：只验证当前需求所依赖的最窄 API，
不提前实现 Agent、API、Java chunker 或 FTS token 化。
"""

from __future__ import annotations

from typing import TypedDict

import jieba
import tree_sitter_java
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from langgraph.graph import END, START, StateGraph
from tree_sitter import Language, Node, Parser


class _SpikeState(TypedDict):
    value: int
    visited: list[str]


def _increment(state: _SpikeState) -> _SpikeState:
    return {"value": state["value"] + 1, "visited": [*state["visited"], "increment"]}


def test_langgraph_compiles_and_runs_a_bounded_state_graph() -> None:
    graph = StateGraph(_SpikeState)
    graph.add_node("increment", _increment)
    graph.add_edge(START, "increment")
    graph.add_edge("increment", END)

    result = graph.compile().invoke({"value": 1, "visited": []})

    assert result == {"value": 2, "visited": ["increment"]}


async def test_fastapi_supports_in_process_async_httpx_requests() -> None:
    app = FastAPI()

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/ping")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def _node_types(node: Node) -> set[str]:
    return {node.type, *(kind for child in node.children for kind in _node_types(child))}


def test_tree_sitter_java_parses_structure_without_executing_source() -> None:
    source = b"""\
package demo;

public class OrderService {
    private int count;

    public OrderService() {}

    public void createOrder() {
        count++;
    }
}
"""
    language = Language(tree_sitter_java.language())
    tree = Parser(language).parse(source)

    assert not tree.root_node.has_error
    assert {
        "package_declaration",
        "class_declaration",
        "field_declaration",
        "constructor_declaration",
        "method_declaration",
    } <= _node_types(tree.root_node)


def test_jieba_tokenization_is_deterministic_with_hmm_disabled() -> None:
    tokenizer = jieba.Tokenizer()
    text = "订单状态机支持自动取消"
    expected = ["订单", "状态机", "支持", "自动", "取消"]

    assert list(tokenizer.cut(text, HMM=False)) == expected
    assert list(tokenizer.cut(text, HMM=False)) == expected
