"""T16.1 严格状态与结构化输出契约。"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from devkb.agent.state import (
    MAX_QUERY_CHARS,
    AgentInput,
    EvaluateOutput,
    PlanOutput,
    RefineOutput,
    initial_agent_state,
)


def test_structured_models_reject_invalid_enum_extra_missing_and_long_query() -> None:
    with pytest.raises(ValidationError):
        EvaluateOutput.model_validate(
            {"sufficiency": "next_node", "supported_aspects": [], "missing_aspects": []}
        )
    with pytest.raises(ValidationError):
        PlanOutput.model_validate(
            {"intent": "knowledge_qa", "queries": ["q"], "project_id": str(uuid.uuid4())}
        )
    with pytest.raises(ValidationError):
        PlanOutput.model_validate({"intent": "knowledge_qa"})
    with pytest.raises(ValidationError):
        RefineOutput.model_validate({"queries": ["x" * (MAX_QUERY_CHARS + 1)]})


def test_plan_queries_are_trimmed_deduplicated_and_bounded() -> None:
    plan = PlanOutput(intent="knowledge_qa", queries=["  order status  ", "order status"])
    assert plan.queries == ["order status"]
    with pytest.raises(ValidationError):
        PlanOutput(intent="knowledge_qa", queries=["a", "b", "c", "d"])


def test_agent_identity_is_strict_and_only_created_by_input() -> None:
    run_id = uuid.uuid4()
    project_id = uuid.uuid4()
    state = initial_agent_state(
        AgentInput(run_id=run_id, project_id=project_id, question="库存如何扣减？")
    )
    assert state["run_id"] == run_id and state["project_id"] == project_id
    assert state["llm_calls"] == 0 and state["retrieval_round"] == 0

    with pytest.raises(ValidationError):
        AgentInput.model_validate(
            {
                "run_id": run_id,
                "project_id": project_id,
                "question": "问题",
                "llm_project_id": uuid.uuid4(),
            }
        )
