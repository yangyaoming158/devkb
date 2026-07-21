# P1 holdout 冻结核对（U2.3 授权前置）

> 状态：**事实已核对，等待用户授权**——本文件由 `benchmarks/p1_freeze_check.py` 生成。
> 授权是人的决定；脚本只证明「冻结」这一前提是否成立。**签字前请重跑本脚本**，
> 因为 T21.1/T21.2 若改动代码会使下方结论失效。

> 核对时间 commit：`31ecf771922f7a32044323676c055e044f88bb65`（⚠️ 工作树有未提交改动）
> 基准：dev §5.2 六项硬 Gate 全过的报告 `p1-dev-agentic-20260719T230855+0800.json`（commit `f240e85`）

## 1. 冻结参数逐字段核对

| 字段 | dev Gate 报告 | 当前代码 | 一致 |
|---|---|---|---|
| `prompt_version` | p1-agent-v3 | p1-agent-v3 | ✅ |
| `rrf_k` | 60 | 60 | ✅ |
| `per_channel_n` | 50 | 50 | ✅ |
| `ef_search` | 40 | 40 | ✅ |
| `top_k` | 10 | 10 | ✅ |
| `hnsw_overlap_gate` | 0.95 | 0.95 | ✅ |
| `citation_proxy_gate` | 0.85 | 0.85 | ✅ |
| `embedding_model_id` | Qwen/Qwen3-Embedding-0.6B | Qwen/Qwen3-Embedding-0.6B | ✅ |
| `llm_model` | deepseek-v4-flash | deepseek-v4-flash | ✅ |
| 检索轮次硬上限 | ≤2 | ≤2 | ✅ |
| LLM 请求硬上限 | ≤6 | ≤6 | ✅ |

**漂移字段：无**

契约版本：`{'prompt_version': 'p1-agent-v3', 'agent_state_schema_version': 'p1-agent-state-v2', 'answer_schema_version': 'p1-answer-v1', 'api_version': 'p1-api-v1'}`

## 2. 被测系统自 dev Gate 以来的代码变更

holdout 结果由回答链路决定；评测 harness 与 CLI 的变更不改变被测行为，但一并列出以免「冻结」被含糊带过。

**被测系统（src/devkb/agent、src/devkb/retrieval.py、src/devkb/ingest、src/devkb/contracts.py、src/devkb/embedding.py、src/devkb/llm.py、src/devkb/repositories.py）：**

```text
（无任何变更）
```

**src/ 全部变更（含评测 harness 与 CLI）：**

```text
src/devkb/cli.py        |  54 +++++-
 src/devkb/evaluation.py | 440 ++++++++++++++++++++++++++++++++++++++++++++----
 2 files changed, 451 insertions(+), 43 deletions(-)
```

## 3. holdout 一次性访问台账

- 台账路径：`evalsets/holdout-access-log.jsonl`
- 记录条数：**0**（从未访问 ✅）

## 4. 授权前仍需完成的前置项

| 项 | 说明 | 状态 |
|---|---|---|
| U2.2 | 人工复核 5 条路径与逐引用核对，材料见 `p1-manual-check.md` | ✅ 已复核通过（randy，2026-07-21，引用正确率 14/14=100%） |
| T21.1 | README 与架构材料更新 | ☐ 未完成 |
| T21.2 | 真语料最终实跑与手工演示（判据含 U2.2 通过） | ☐ 未完成 |

> 上述任一项若改动被测代码，第 1、2 节结论作废，需重跑本脚本。

## 5. 用户授权（**已签字**）

- ☑ 我确认实现、Prompt、阈值、RRF 权重已冻结，且理解 holdout 只运行一次
- ☑ 我确认 U2.2 已通过、T21.1/T21.2 已完成
- ☑ 授权运行一次 holdout　　签字：**randy**　日期：**2026-07-21**

授权后的执行命令（首次访问不需要 `--acknowledge-rerun`）：

```bash
# 真实模型调用必须同时带两个开关（见 CLAUDE.md 环境事实）
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
HF_HUB_OFFLINE=1 uv run devkb eval run --split holdout --mode all --confirm-holdout
```

