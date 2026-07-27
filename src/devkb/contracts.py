"""P1 对外契约的机器可读版本标识（T11.4）。

这些常量与包版本 ``devkb.__version__`` 独立：包可以发布修复而不改契约，
某一契约变更时也只递增对应标识。P1 不在此建立兼容性框架。
"""

from __future__ import annotations

PROMPT_VERSION = "p1.5-agent-v4"
AGENT_STATE_SCHEMA_VERSION = "p1.5-agent-state-v9"
ANSWER_SCHEMA_VERSION = "p1.5-answer-v2"
API_VERSION = "p1-api-v1"


def contract_versions() -> dict[str, str]:
    """返回可直接写入 JSON 报告和轨迹摘要的契约版本。"""
    return {
        "prompt_version": PROMPT_VERSION,
        "agent_state_schema_version": AGENT_STATE_SCHEMA_VERSION,
        "answer_schema_version": ANSWER_SCHEMA_VERSION,
        "api_version": API_VERSION,
    }
