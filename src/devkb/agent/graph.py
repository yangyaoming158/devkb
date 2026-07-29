"""T16 LangGraph 拓扑与只读结构化信号的确定性路由。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, cast

from langgraph.graph import END, START, StateGraph

from devkb.agent.aspects import has_deliverable_aspect
from devkb.agent.nodes import AgentNodes, AgentRuntime, monotonic_matrix
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
RouteFromStart = Literal["plan", "policy_refuse"]
RouteAfterPlan = Literal["retrieve", "policy_refuse"]
RouteAfterEvaluate = Literal["refine", "generate", "finalize"]
RouteAfterRefine = Literal["retrieve", "generate", "finalize"]
RouteAfterVerify = Literal["generate", "finalize"]


def route_from_start(state: AgentState) -> RouteFromStart:
    """T27.1 规则层短路（规格 §9）：判定在 `initial_agent_state` 图外已完成。

    放在 START 而非 plan 节点体内，是为了让"命中即零 LLM 调用"由**拓扑**保证：
    plan 是第一个供应商调用点，绕过该节点就绕过了全部四个调用点。
    """
    return "policy_refuse" if state["policy_trigger"] is not None else "plan"


def route_after_plan(state: AgentState) -> RouteAfterPlan:
    """T27.1 plan 兜底层短路：必须早于 retrieve——规格 §9 要求"在检索潜在敏感语料和
    调用任何工具之前"。retrieve 是本阶段唯一的工具，故此处短路即零 tool invocation。"""
    return "policy_refuse" if state["policy_trigger"] is not None else "retrieve"


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


def _can_generate(state: AgentState) -> bool:
    return remaining_llm_requests(state) >= 1 and state["generate_calls"] < MAX_GENERATE_CALLS


def has_deliverable_content(state: AgentState) -> bool:
    """T24.2：当前证据集里仍有方面拿得出直接证据 → 有可交付内容。

    进入 finalize 的**每条**路径都按这一个口径判断，避免"规则只在一条分支成立"
    （五审发现5 的同类问题）：末轮 evaluate 倒退、refine 解析失败都不得清空
    仍有直接证据的方面。反过来，历史账本非空**不等于**当前可交付——
    ``has_deliverable_aspect`` 判的是 `supported and present_now`（复审发现2）。
    """
    return bool(state["evidences"]) and has_deliverable_aspect(monotonic_matrix(state))


def route_after_evaluate(state: AgentState) -> RouteAfterEvaluate:
    """只读取 schema 枚举、证据、确定性覆盖、轮次和预算；LLM 不能返回节点名。"""
    evaluation = state["evaluation"]
    if evaluation is None:
        return "finalize"
    # 证据数量是确定性硬信号：即使 LLM 违背 Prompt 把空证据判为 sufficient，
    # 也必须按 insufficient 路径补检或拒答，不能生成零证据 full 回答。
    if not state["evidences"]:
        return "refine" if can_refine(state) else "finalize"
    # 确定性覆盖缺口（权威，非 LLM 自报）：resolved 必需项未被引用覆盖，或存在
    # unresolved 约束（结构性 fail-closed），都算缺口。只要预算允许就优先补检，即便
    # LLM 判 sufficient 也不能直接生成——full 门只认确定性覆盖 + 无 unresolved。
    required = state["required_evidence"]
    required_gap = any(not entry.covered for entry in state["coverage"]) or bool(
        required.unresolved_constraints
    )
    if required_gap and can_refine(state):
        return "refine"
    if evaluation.sufficiency == "sufficient":
        return "generate" if _can_generate(state) else "finalize"
    if can_refine(state):
        return "refine"
    # T24.2（RT-20）：补检已不可行时，只要还有可交付内容就必须生成**范围准确的
    # partial**，refusal 只留给零可交付证据。可交付 = 末轮自报 partial，或跨轮单调
    # 矩阵里有任一轮取得过直接证据的方面——末轮 evaluate 因证据集合变化退化为
    # insufficient 时，不得把前轮已支持的方面一起清空（案例九假拒答）。
    if (evaluation.sufficiency == "partial" and state["evidences"]) or has_deliverable_content(
        state
    ):
        return "generate" if _can_generate(state) else "finalize"
    return "finalize"


def route_after_refine(state: AgentState) -> RouteAfterRefine:
    if state["refine_failed"] or state["retrieval_round"] >= MAX_RETRIEVAL_ROUNDS:
        # T24.2：补检不可用 ≠ 没有可交付内容。refine 解析/传输失败此前直接整体拒答，
        # 把前轮已有直接证据的方面一并丢弃（RT-20 同型缺陷，只是换了一条分支）。
        if has_deliverable_content(state) and _can_generate(state):
            return "generate"
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
    graph.add_node("policy_refuse", _traced("policy_refuse", nodes.policy_refuse, recorder))

    graph.add_conditional_edges(
        START,
        route_from_start,
        {"plan": "plan", "policy_refuse": "policy_refuse"},
    )
    graph.add_conditional_edges(
        "plan",
        route_after_plan,
        {"retrieve": "retrieve", "policy_refuse": "policy_refuse"},
    )
    graph.add_edge("retrieve", "evaluate")
    graph.add_conditional_edges(
        "evaluate",
        route_after_evaluate,
        {"refine": "refine", "generate": "generate", "finalize": "finalize"},
    )
    graph.add_conditional_edges(
        "refine",
        route_after_refine,
        {"retrieve": "retrieve", "generate": "generate", "finalize": "finalize"},
    )
    graph.add_edge("generate", "verify")
    graph.add_conditional_edges(
        "verify",
        route_after_verify,
        {"generate": "generate", "finalize": "finalize"},
    )
    graph.add_edge("finalize", END)
    graph.add_edge("policy_refuse", END)
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
