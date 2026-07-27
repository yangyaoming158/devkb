"""T11.4 P1 Prompt/State/Answer/API 版本口径。"""

from __future__ import annotations

import json

from devkb.contracts import (
    AGENT_STATE_SCHEMA_VERSION,
    ANSWER_SCHEMA_VERSION,
    API_VERSION,
    PROMPT_VERSION,
    contract_versions,
)

EXPECTED_VERSIONS = {
    "prompt_version": "p1.5-agent-v4",
    "agent_state_schema_version": "p1.5-agent-state-v9",
    "answer_schema_version": "p1.5-answer-v2",
    "api_version": "p1-api-v1",
}


def test_p1_contract_version_names_are_frozen() -> None:
    assert EXPECTED_VERSIONS["prompt_version"] == PROMPT_VERSION
    assert EXPECTED_VERSIONS["agent_state_schema_version"] == AGENT_STATE_SCHEMA_VERSION
    assert EXPECTED_VERSIONS["answer_schema_version"] == ANSWER_SCHEMA_VERSION
    assert EXPECTED_VERSIONS["api_version"] == API_VERSION


def test_contract_versions_are_report_ready_json() -> None:
    payload = contract_versions()

    assert payload == EXPECTED_VERSIONS
    assert json.loads(json.dumps(payload)) == EXPECTED_VERSIONS

    payload["prompt_version"] = "mutated"
    assert contract_versions() == EXPECTED_VERSIONS
