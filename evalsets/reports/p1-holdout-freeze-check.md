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

## 验收复核注记（2026-07-22，T21.4 前）

> 本节为**人工复核注记**，不改写上方由 `p1_freeze_check.py` 生成的历史数值、用户签字与 holdout 前事实（§1–§5 一律保留原样）。仅澄清几处会引起误读的生成态遗留：

1. **第 7 行「⚠️ 工作树有未提交改动」不代表被测系统脏**：本报告是 Git 跟踪文件，脚本用 shell 重定向（`… > p1-holdout-freeze-check.md`）覆盖自身时，`git status --short` 会把这份正在被覆盖的报告本身识别为工作树改动。真正的判定看第 2 节——被测系统 `git diff` 为「（无任何变更）」；脏标来自报告文件自身，与冻结无关。

2. **第 4 节 T21.1/T21.2「☐ 未完成」是生成模板的陈旧静态状态**：该表由脚本以固定模板输出，不随任务进度自动更新。二者实际均已在授权前完成——T21.1 见 `README.md` 重写、T21.2 见 `evalsets/reports/p1-manual-demo.md`。第 5 节用户签字第二项「☑ 我确认 U2.2 已通过、T21.1/T21.2 已完成」即对此的明确确认，签字晚于模板行、以签字为准。

3. **第 3 节台账「0 条（从未访问）」是 holdout 前的历史快照，不是当前状态**：本报告生成于 holdout 之前，故当时台账为空、如实记 0。holdout 已于其后一次性运行，**当前** `evalsets/holdout-access-log.jsonl` 有 `started` + `completed` 两条 `attempt=1` 记录（不在此改写第 3 节以免伪装 holdout 后状态为 holdout 前状态）。

4. **台账 `started` 记录反证实跑时工作树干净**：该记录 `devkb_commit=087c73e`、`devkb_worktree_dirty=false`——即真正运行 holdout 的那次提交工作树是干净的，与第 1 点「报告文件自身导致的脏标」是两回事。

5. **dev Gate → holdout 之间被测系统代码零变更**：基准 dev Gate 报告落盘于 `f240e85`；第 2 节 `git diff f240e85..HEAD -- <被测系统>` 为空，T21.1/T21.2 只改 README 与报告、未触被测链路。冻结成立，holdout 结果可采信。
