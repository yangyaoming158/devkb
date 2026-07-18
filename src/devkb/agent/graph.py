"""T16 LangGraph 拓扑与只读结构化信号的确定性路由。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
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
from devkb.agent.trace import (
    StepTimer,
    TraceRecorder,
    derive_step_status,
    summarize_input,
    summarize_output,
)

NodeFn = Callable[[AgentState], Awaitable[dict[str, Any]]]

GRAPH_RECURSION_LIMIT = 20
RouteAfterEvaluate = Literal["refine", "generate", "finalize"]
RouteAfterRefine = Literal["retrieve", "finalize"]
RouteAfterVerify = Literal["generate", "finalize"]


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
    # 证据数量是确定性硬信号：即使 LLM 违背 Prompt 把空证据判为 sufficient，
    # 也必须按 insufficient 路径补检或拒答，不能生成零证据 full 回答。
    if not state["evidences"]:
        return "refine" if can_refine(state) else "finalize"
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


def route_after_verify(state: AgentState) -> RouteAfterVerify:
    """仅 L0/L1 失败可触发一次重生成；传输/格式失败与预算耗尽走确定性降级。"""
    verification = state["verification"]
    if verification is None or verification.passed:
        return "finalize"
    if state["generate_failed"]:
        return "finalize"
    if state["generate_calls"] >= MAX_GENERATE_CALLS:
        return "finalize"
    if remaining_llm_requests(state) < 1:
        return "finalize"
    return "generate"


def _traced(node: str, fn: NodeFn, recorder: TraceRecorder | None) -> Any:
    """T18.1 节点轨迹包装：成功/降级/异常路径各写一个 step，异常原样上抛。

    返回 Any：langgraph add_node 的 StateNode 形参要求具名 state 参数，
    Callable 别名在类型系统里是 positional-only，与 build_agent_graph 同样放宽。
    """
    if recorder is None:
        return fn

    async def wrapped(state: AgentState) -> dict[str, Any]:
        timer = StepTimer()
        input_summary = summarize_input(node, cast(dict[str, Any], state))
        try:
            updates = await fn(state)
        except Exception as exc:
            recorder.finish_step(
                node=node,
                status="failed",
                input_summary=input_summary,
                output_summary=None,
                latency_ms=timer.elapsed_ms(),
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        status, error = derive_step_status(updates)
        recorder.finish_step(
            node=node,
            status=status,
            input_summary=input_summary,
            output_summary=summarize_output(node, updates),
            latency_ms=timer.elapsed_ms(),
            error=error,
        )
        return updates

    return wrapped


def build_agent_graph(runtime: AgentRuntime, recorder: TraceRecorder | None = None) -> Any:
    nodes = AgentNodes(runtime, recorder)
    graph = StateGraph(AgentState)
    graph.add_node("plan", _traced("plan", nodes.plan, recorder))
    graph.add_node("retrieve", _traced("retrieve", nodes.retrieve, recorder))
    graph.add_node("evaluate", _traced("evaluate", nodes.evaluate, recorder))
    graph.add_node("refine", _traced("refine", nodes.refine, recorder))
    graph.add_node("generate", _traced("generate", nodes.generate, recorder))
    graph.add_node("verify", _traced("verify", nodes.verify, recorder))
    graph.add_node("finalize", _traced("finalize", nodes.finalize, recorder))

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
    graph.add_conditional_edges(
        "verify",
        route_after_verify,
        {"generate": "generate", "finalize": "finalize"},
    )
    graph.add_edge("finalize", END)
    return graph.compile()


async def run_agent(
    runtime: AgentRuntime,
    agent_input: AgentInput,
    recorder: TraceRecorder | None = None,
) -> AgentState:
    graph = build_agent_graph(runtime, recorder)
    result = await graph.ainvoke(
        initial_agent_state(agent_input),
        config={"recursion_limit": GRAPH_RECURSION_LIMIT},
    )
    return cast(AgentState, result)
