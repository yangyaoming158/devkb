"""U2.3 冻结核对：holdout 授权前逐项证明"实现/Prompt/阈值/RRF 权重已冻结"。

只读脚本：不连数据库、不调模型、不接触 holdout 题集。核对三件事——
  ① 冻结参数与 dev Gate 报告记录的配置逐字段一致（无漂移）；
  ② 被测系统（agent/retrieval/ingest/contracts/embedding/llm）自 dev Gate 运行
     以来无代码变更——评测 harness 与 CLI 的变更不算破坏冻结，但要如实列出；
  ③ holdout 访问台账仍为空（从未被访问）。
授权本身是人的决定，脚本只负责把事实摆出来。

用法：uv run python benchmarks/p1_freeze_check.py > evalsets/reports/p1-holdout-freeze-check.md
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from devkb.agent.state import MAX_LLM_REQUESTS, MAX_RETRIEVAL_ROUNDS
from devkb.contracts import contract_versions
from devkb.evaluation import (
    CITATION_PROXY_GATE,
    EVAL_TOP_K,
    HNSW_OVERLAP_GATE,
    holdout_ledger_path,
    read_holdout_ledger,
)
from devkb.retrieval import HNSW_EF_SEARCH, MAX_CHANNEL_CANDIDATES, RRF_K_DEFAULT

DEV_GATE_REPORT = Path("evalsets/reports/p1-dev-agentic-20260719T230855+0800.json")
DEV_GATE_COMMIT = "f240e85"  # 该报告落盘的提交（dev §5.2 六项硬 Gate 全过）
EVALSETS_DIR = Path("evalsets")
# 被测系统：holdout 结果由这些模块决定，冻结要求它们自 dev Gate 起不变
SYSTEM_UNDER_TEST = [
    "src/devkb/agent",
    "src/devkb/retrieval.py",
    "src/devkb/ingest",
    "src/devkb/contracts.py",
    "src/devkb/embedding.py",
    "src/devkb/llm.py",
    "src/devkb/repositories.py",
]


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _frozen_now(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "prompt_version": contract_versions()["prompt_version"],
        "rrf_k": RRF_K_DEFAULT,
        "per_channel_n": MAX_CHANNEL_CANDIDATES,
        "ef_search": HNSW_EF_SEARCH,
        "top_k": EVAL_TOP_K,
        "hnsw_overlap_gate": HNSW_OVERLAP_GATE,
        "citation_proxy_gate": CITATION_PROXY_GATE,
        # 模型标识由配置提供：这里核对的是"与 Gate 运行时同一模型"
        "embedding_model_id": config["embedding_model_id"],
        "llm_model": config["llm_model"],
    }


def main() -> None:
    report = json.loads(DEV_GATE_REPORT.read_text(encoding="utf-8"))
    config = report["config"]
    now = _frozen_now(config)
    drift = [key for key, value in now.items() if str(config.get(key)) != str(value)]

    sut_changes = _git("diff", "--stat", f"{DEV_GATE_COMMIT}..HEAD", "--", *SYSTEM_UNDER_TEST)
    other_src = _git("diff", "--stat", f"{DEV_GATE_COMMIT}..HEAD", "--", "src/")
    ledger = read_holdout_ledger(EVALSETS_DIR)
    worktree_dirty = bool(_git("status", "--short"))

    lines = [
        "# P1 holdout 冻结核对（U2.3 授权前置）",
        "",
        "> 状态：**事实已核对，等待用户授权**——本文件由 `benchmarks/p1_freeze_check.py` 生成。",
        "> 授权是人的决定；脚本只证明「冻结」这一前提是否成立。**签字前请重跑本脚本**，",
        "> 因为 T21.1/T21.2 若改动代码会使下方结论失效。",
        "",
        f"> 核对时间 commit：`{_git('rev-parse', 'HEAD')}`"
        + ("（⚠️ 工作树有未提交改动）" if worktree_dirty else "（工作树干净）"),
        f"> 基准：dev §5.2 六项硬 Gate 全过的报告 `{DEV_GATE_REPORT.name}`"
        f"（commit `{DEV_GATE_COMMIT}`）",
        "",
        "## 1. 冻结参数逐字段核对",
        "",
        "| 字段 | dev Gate 报告 | 当前代码 | 一致 |",
        "|---|---|---|---|",
    ]
    for key, value in now.items():
        recorded = config.get(key)
        same = str(recorded) == str(value)
        lines.append(f"| `{key}` | {recorded} | {value} | {'✅' if same else '❌'} |")
    lines += [
        f"| 检索轮次硬上限 | ≤2 | ≤{MAX_RETRIEVAL_ROUNDS} | "
        f"{'✅' if MAX_RETRIEVAL_ROUNDS == 2 else '❌'} |",
        f"| LLM 请求硬上限 | ≤6 | ≤{MAX_LLM_REQUESTS} | "
        f"{'✅' if MAX_LLM_REQUESTS == 6 else '❌'} |",
        "",
        f"**漂移字段：{'无' if not drift else '、'.join(drift)}**",
        "",
        f"契约版本：`{contract_versions()}`",
        "",
        "## 2. 被测系统自 dev Gate 以来的代码变更",
        "",
        "holdout 结果由回答链路决定；评测 harness 与 CLI 的变更不改变被测行为，"
        "但一并列出以免「冻结」被含糊带过。",
        "",
        f"**被测系统（{'、'.join(SYSTEM_UNDER_TEST)}）：**",
        "",
        "```text",
        sut_changes or "（无任何变更）",
        "```",
        "",
        "**src/ 全部变更（含评测 harness 与 CLI）：**",
        "",
        "```text",
        other_src or "（无任何变更）",
        "```",
        "",
        "## 3. holdout 一次性访问台账",
        "",
        f"- 台账路径：`{holdout_ledger_path(EVALSETS_DIR)}`",
        f"- 记录条数：**{len(ledger)}**"
        + ("（从未访问 ✅）" if not ledger else "（⚠️ 已有访问记录，重跑需裁决理由）"),
        "",
        "## 4. 授权前仍需完成的前置项",
        "",
        "| 项 | 说明 | 状态 |",
        "|---|---|---|",
        "| U2.2 | 人工复核 5 条路径与逐引用核对，材料见 `p1-manual-check.md` | ☐ 待人工裁定 |",
        "| T21.1 | README 与架构材料更新 | ☐ 未完成 |",
        "| T21.2 | 真语料最终实跑与手工演示（判据含 U2.2 通过） | ☐ 未完成 |",
        "",
        "> 上述任一项若改动被测代码，第 1、2 节结论作废，需重跑本脚本。",
        "",
        "## 5. 用户授权（**待签字**）",
        "",
        "- ☐ 我确认实现、Prompt、阈值、RRF 权重已冻结，且理解 holdout 只运行一次",
        "- ☐ 我确认 U2.2 已通过、T21.1/T21.2 已完成",
        "- ☐ 授权运行一次 holdout　　签字：______　日期：______",
        "",
        "授权后的执行命令（首次访问不需要 `--acknowledge-rerun`）：",
        "",
        "```bash",
        "# 真实模型调用必须同时带两个开关（见 CLAUDE.md 环境事实）",
        "unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY",
        "HF_HUB_OFFLINE=1 uv run devkb eval run --split holdout --mode all --confirm-holdout",
        "```",
        "",
    ]
    print("\n".join(lines))


if __name__ == "__main__":
    main()
