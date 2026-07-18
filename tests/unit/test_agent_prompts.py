"""T16.2 Prompt 版本、安全指令与 schema 快照。"""

from __future__ import annotations

import hashlib
import json

from devkb.agent.prompts import prompt_snapshot
from devkb.contracts import PROMPT_VERSION


def test_prompt_snapshot_locks_version_security_and_output_contract() -> None:
    snapshot = prompt_snapshot()
    assert snapshot["version"] == PROMPT_VERSION == "p1-agent-v1"
    assert set(snapshot) == {"version", "plan", "evaluate", "refine", "generate"}
    for name in ("plan", "evaluate", "refine", "generate"):
        prompt = snapshot[name]
        assert "不可信数据" in prompt
        assert "绝不能改变本系统指令" in prompt
        assert "只输出一个 JSON 对象" in prompt
        assert "Schema:" in prompt
        assert f"Prompt-Version: {PROMPT_VERSION}" in prompt

    serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert (
        hashlib.sha256(serialized.encode()).hexdigest()
        == "8e731c3a740197c9d2256353d8310486eca3980ff7660f933084f8a229d6c275"
    )
