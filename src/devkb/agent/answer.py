"""P1 Answer v1 组装（规格 §10）。

在 P0 字段（answer_text/citations/warnings/stats）之上新增
run_id/mode/claims/not_found/limitations/trace_summary；全部由终态
AgentState 确定性序列化，不发起任何 LLM 调用。
"""

from __future__ import annotations

import re
from typing import Any

from devkb.agent.state import AgentState, Evidence

_EVIDENCE_MARK = re.compile(r"\[E(\d+)\]")


def _cited_evidence_ids(state: AgentState) -> list[str]:
    """按证据编号升序返回被引用的 evidence_id（claims 绑定 ∪ 文本 [E#] 标注）。"""
    known = {evidence.evidence_id for evidence in state["evidences"]}
    cited: set[str] = set()
    for claim in state["final_claims"]:
        cited.update(evidence_id for evidence_id in claim.evidence_ids if evidence_id in known)
    for match in _EVIDENCE_MARK.finditer(state["final_answer"] or ""):
        evidence_id = f"E{int(match.group(1))}"
        if evidence_id in known:
            cited.add(evidence_id)
    return sorted(cited, key=lambda evidence_id: int(evidence_id[1:]))


def _citation(evidence: Evidence) -> dict[str, Any]:
    return {
        "evidence_id": evidence.evidence_id,
        "chunk_id": str(evidence.chunk_id),
        "rel_path": evidence.rel_path,
        "start_line": evidence.start_line,
        "end_line": evidence.end_line,
        "title_path": evidence.title_path,
        "score": round(evidence.score, 4),
    }


def _limitations(state: AgentState) -> list[str]:
    mode = state["final_mode"]
    items: list[str] = []
    if mode == "partial":
        # T25.1（前审 PG-04）：partial 的真实局限有四种，措辞必须与终态实际字段相符。
        # 只看 mode 会产生两句不成立的话：`not_found` 被 T23 事实校验剔空后仍把用户
        # 指向空清单；而 `claims` 为空时"回答了现有证据支持的部分"根本没发生——且
        # 这里还要再分两种，正文可能是确定性降级文案（生成失败），也可能是模型真实
        # 输出但没给结构化 claim（`test_sufficient_without_structured_claims_...`）。
        if not state["final_claims"]:
            items.append(
                "未生成经验证的结构化断言；正文为确定性降级文案"
                if state["generate_failed"]
                else "正文未附结构化 claim，未经逐条引用验证"
            )
        elif state["final_not_found"]:
            items.append("仅回答了现有证据支持的部分，未覆盖方面见 not_found")
        else:
            items.append("仅回答了现有证据支持的部分；本次未列出具体未覆盖方面")
    if mode == "refusal":
        items.append("未找到足以回答问题的证据，未生成实质回答")
    verification = state["verification"]
    if verification is not None and verification.failed_claims:
        items.append(f"已移除 {len(verification.failed_claims)} 个未通过 L0/L1 引用验证的 claim")
    if state["generate_failed"]:
        items.append("生成调用失败，返回确定性降级文案")
    return items


def _trace_summary(state: AgentState) -> str:
    return (
        f"节点 {'>'.join(state['node_history'])}；"
        f"检索 {state['retrieval_round']} 轮、证据 {len(state['evidences'])} 条；"
        f"LLM 请求 {state['llm_calls']}（重试 {state['llm_retries']}）；"
        f"refine {state['refine_calls']}、generate {state['generate_calls']}；"
        f"模式 {state['final_mode']}"
    )


def build_answer(state: AgentState) -> dict[str, Any]:
    """终态 AgentState → Answer v1 JSON dict（可直接落库/渲染/API 返回）。"""
    evidence_by_id = {evidence.evidence_id: evidence for evidence in state["evidences"]}
    return {
        "answer_text": state["final_answer"] or "",
        "citations": [
            _citation(evidence_by_id[evidence_id]) for evidence_id in _cited_evidence_ids(state)
        ],
        "warnings": list(state["warnings"]),
        "stats": {
            "tokens_in": state["tokens_in"],
            "tokens_out": state["tokens_out"],
            "latency_ms": state["latency_ms"],
            "model": state["model"],
            "llm_calls": state["llm_calls"],
            "llm_retries": state["llm_retries"],
        },
        "run_id": str(state["run_id"]),
        "mode": state["final_mode"] or "refusal",
        "claims": [
            {
                "text": claim.text,
                "evidence_ids": list(claim.evidence_ids),
                "quotes": list(claim.quotes),
            }
            for claim in state["final_claims"]
        ],
        "not_found": list(state["final_not_found"]),
        # T23：四分类平行字段（对既有消费者纯加法，not_found 仍是 list[str]）；
        # 每项带类别、来源与事实校验依据，被改写的原文保留在 original_text
        "not_found_details": [
            {
                "text": detail.text,
                "category": detail.category,
                "source": detail.source,
                "basis": detail.basis,
                "refs": list(detail.refs),
                "original_text": detail.original_text,
            }
            for detail in state["final_not_found_details"]
        ],
        "limitations": _limitations(state),
        "trace_summary": _trace_summary(state),
    }
