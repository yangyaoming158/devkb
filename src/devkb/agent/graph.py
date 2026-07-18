"""T16 LangGraph 拓扑与只读结构化信号的确定性路由。"""

from __future__ import annotations

from typing import Any, Literal, cast

from langgraph.graph import END, START, StateGraph

from devkb.agent.nodes import AgentNodes, AgentRuntime
from devkb.agent.state import (
    MAX_GENERATE_CALLS,
    MAX_LLM_REQUESTS,
    MAX_REFINE_CALLS,
    MAX_RETRIEVAL_ROUNDS,
    AgentInput,
    AgentState,
    initial_agent_state,
)

GRAPH_RECURSION_LIMIT = 20
RouteAfterEvaluate = Literal["refine", "generate", "finalize"]
RouteAfterRefine = Literal["retrieve", "finalize"]


def remaining_llm_requests(state: AgentState) -> int:
    return max(MAX_LLM_REQUESTS - state["llm_calls"], 0)


def can_refine(state: AgentState) -> bool:
    """补检至少预留 refine + 二次 evaluate + generate 三次供应商请求。"""
    return (
        state["retrieval_round"] < MAX_RETRIEVAL_ROUNDS
        and state["refine_calls"] < MAX_REFINE_CALLS
        and not state["refine_failed"]
        and remaining_llm_requests(state) >= 3
    )


def route_after_evaluate(state: AgentState) -> RouteAfterEvaluate:
    """只读取 schema 枚举、证据、轮次和预算；LLM 不能返回节点名。"""
    evaluation = state["evaluation"]
    if evaluation is None:
        return "finalize"
    if evaluation.sufficiency == "sufficient":
        return (
            "generate"
            if remaining_llm_requests(state) >= 1 and state["generate_calls"] < MAX_GENERATE_CALLS
            else "finalize"
        )
    if can_refine(state):
        return "refine"
    if evaluation.sufficiency == "partial" and state["evidences"]:
        return (
            "generate"
            if remaining_llm_requests(state) >= 1 and state["generate_calls"] < MAX_GENERATE_CALLS
            else "finalize"
        )
    return "finalize"


def route_after_refine(state: AgentState) -> RouteAfterRefine:
    if state["refine_failed"] or state["retrieval_round"] >= MAX_RETRIEVAL_ROUNDS:
        return "finalize"
    return "retrieve"


def build_agent_graph(runtime: AgentRuntime) -> Any:
    nodes = AgentNodes(runtime)
    graph = StateGraph(AgentState)
    graph.add_node("plan", nodes.plan)
    graph.add_node("retrieve", nodes.retrieve)
    graph.add_node("evaluate", nodes.evaluate)
    graph.add_node("refine", nodes.refine)
    graph.add_node("generate", nodes.generate)
    graph.add_node("verify", nodes.verify)
    graph.add_node("finalize", nodes.finalize)

    graph.add_edge(START, "plan")
    graph.add_edge("plan", "retrieve")
    graph.add_edge("retrieve", "evaluate")
    graph.add_conditional_edges(
        "evaluate",
        route_after_evaluate,
        {"refine": "refine", "generate": "generate", "finalize": "finalize"},
    )
    graph.add_conditional_edges(
        "refine",
        route_after_refine,
        {"retrieve": "retrieve", "finalize": "finalize"},
    )
    graph.add_edge("generate", "verify")
    graph.add_edge("verify", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()


async def run_agent(runtime: AgentRuntime, agent_input: AgentInput) -> AgentState:
    graph = build_agent_graph(runtime)
    result = await graph.ainvoke(
        initial_agent_state(agent_input),
        config={"recursion_limit": GRAPH_RECURSION_LIMIT},
    )
    return cast(AgentState, result)
