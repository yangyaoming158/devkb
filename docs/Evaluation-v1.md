# 《Evaluation v1：P1 Agentic RAG 评测与 Gate》

> 冻结日期：2026-07-16
> 适用阶段：P1
> 问题集继承：Evaluation v0 的 31 问，不扩题、不改 holdout
> 原则：dev 用于开发；holdout 只在 P1 最终验收运行一次。

## 1. 评测目标

P1 不只回答“效果好不好”，还要分别证明：

1. Hybrid/RRF 相对 P0 vector baseline 的收益或代价；
2. HNSW 相对精确扫描没有不可接受的召回损失；
3. Agent 会在有限预算内补检、部分回答或拒答；
4. L0/L1 能发现并处理无效引用和篡改引文；
5. 图、工具和故障路径可确定性回归；
6. 质量提升没有用失控的 token、成本或延迟换取。

## 2. 数据与不可泄漏规则

继续使用：

- `retrieval_dev.jsonl`：17 个可答问题；
- `unanswerable_dev.jsonl`：4 个不可答/边界问题；
- `retrieval_holdout.jsonl`：8 个可答问题；
- `unanswerable_holdout.jsonl`：2 个不可答问题。

P1 不新增问题，以遵守 Evaluation v0 的 31 问上限。Java 解析质量使用 parser contract/golden suite，不伪装成真实问答集。

允许在 P1 开工前做一次**盲标注审计**：审计者只看原语料和题目，不看 P1 检索/回答结果；可修正明显错误的路径、锚点或 expected mode，并必须写 `label-change-log.md`。一旦运行任何 P1 dev 结果，标签冻结。holdout 不因 P0/P1 的检索结果修改；若发现争议，只在报告中并列“原始指标”和“人工复核解释”。

## 3. 评测模式

同一语料快照、同一问题、同一 embedding 模型下运行：

| 模式 | 用途 |
|---|---|
| `vector-exact` | P0 纯向量精确扫描基线 |
| `vector-hnsw` | 测 HNSW 对精确结果的近似损失；**P1 在线默认检索**（2026-07-18 T15.4 裁决，见 §5.1 修订） |
| `lexical` | 单独观察 FTS 对中文/标识符/数字 token 的贡献 |
| `hybrid-rrf` | 显式调用与评测路径，对照前三者（裁决前曾定位为 P1 默认检索，2026-07-18 起非在线默认，见 §5.1 修订） |
| `agentic` | 完整 plan→retrieve→evaluate→refine→generate→verify |

CLI 的统一评测枚举为 `vector-exact|vector-hnsw|lexical|hybrid-rrf|agentic|all`；`all` 不是第六种算法，而是按表中五种模式依次运行并产出同快照对照。报告、dev 和 holdout 命令均不得再使用 `vector`、`hybrid`、`vector-fixed`、`agentic-hybrid` 等别名。

每次真实评测记录：日期、commit、数据库语料 hash/文档数/chunk 数、模型名、Prompt 版本、配置、RRF k、每路候选数、top_k、命令和环境开关。

## 4. 指标

### 4.1 检索

- Recall@5、Recall@10、MRR@10；
- exact 与 HNSW 的 top-10 overlap；
- Vector-only / Lexical-only / Hybrid 的首个命中名次；
- 按问题类型分组：自然语言、英文、标识符/路径/数字 token；
- P50/P95 检索延迟。

命中仍按 `rel_path + anchor` 解析到当前 chunk，不把漂移的 chunk_id 写入标注。

### 4.2 回答与引用

- final mode：full / partial / refusal；
- 正确拒答或正确边界回答率；
- 误拒率：可答题被错误 refusal 的比例；
- L0 最终通过率；
- L1 最终通过率；
- citation-to-anchor proxy：可答问题中，至少一个最终引用命中任一人工 relevant anchor 的问题占比；
- 人工引用正确率：抽查回答中的 claim 是否被对应原文支持。

`u04` 这类“存在状态但缺少接口/流程”的题可以标为 `partial`，不能只用“有没有拒答”二元计分。expected mode 由开工前盲审冻结。

### 4.3 Agent 与可靠性

- 检索轮次、发往 LLM 供应商的总请求次数（含格式/传输重试）及重试次数；
- 无效循环率；
- 工具选择/参数合法率；
- 注入检索故障、LLM 超时、格式错误后的正确降级率；
- run/step/tool 终态完整率；
- 单问 tokens、cost、端到端 latency P50/P95。

## 5. 预冻结 Gate

### 5.1 Dev 检索 Gate（真实 Qwen，本地运行）

在 17 个 retrieval dev 问题上：

1. `hybrid-rrf Recall@10 >= vector-exact Recall@10`；
2. `hybrid-rrf MRR@10` 不得比 vector-exact 下降超过 `0.02`；
3. 至少一个预先归类的标识符/路径/数字 token 问题，其首命中名次优于 vector-exact；
4. HNSW 对 exact 的平均 top-10 overlap ≥ `0.95`；若未达标，调高 `ef_search`，仍不达标则默认路径保留精确扫描并把 HNSW 作为负实验记录；
5. 所有模式使用同一 chunk 快照和 query，不允许为 Hybrid 单独改 relevant 标注。

本 Gate 要求证明“Hybrid 至少不退化且解决一个预声明弱点”，不要求为了漂亮数字引入 reranker。

> **2026-07-18 修订（T15.4 偏差裁决，见《P1任务清单》偏差记录）**：第 1–3 条在冻结机制
> （RRF k=60、每路 top-N ≤50、冻结 token 化与标注）下经证明不可满足——官方同快照对照
> `evalsets/reports/p1-dev-hybrid-gate-20260718T122552+0800` Gate 未过；28 组代表性网格
> 加穷举 n_vector×n_lexical=1..50×1..50 共 2500 组规格内组合均 0 组通过、其中 MRR 条
> （G2）单项通过数为 0（`benchmarks/results/hybrid_grid_diag.{txt,json}`），
> 根因是 lexical channel 对中文自然语言题系统性噪声使 RRF 共识假设失效。
> 依《P1实现规格》§8"无稳定评测收益时允许记录负结果"（文档优先级 2 > 3），用户裁决：
> - 第 1–3 条由硬 Gate 改为**同快照对照记录义务**：三模式（vector-exact/lexical/hybrid-rrf）
>   指标与逐题名次如实落盘，负结果允许，不在 17 题 dev 上继续调参；
> - **P1 在线默认检索 = vector channel（HNSW，ef_search=40，T15.3 冻结）**；
>   hybrid-rrf 保留为可工作实现、评测模式与显式配置路径，不作在线默认；
> - 第 4、5 条维持硬性不变（第 4 条已由 T15.3 通过，overlap=1.0）。

### 5.2 Dev 回答 Gate（真实 DeepSeek，本地运行）

在 17 个可答 + 4 个不可答/边界问题上：

- 正确 refusal/partial ≥ `3/4`；
- 可答题误 refusal ≤ `1/17`；
- 最终 L0 通过率 = `100%`；
- 最终 L1 通过率 = `100%`；
- citation-to-anchor proxy ≥ `85%`；
- 每个 run 检索轮次 ≤2、供应商总请求（含重试）≤6；
- 失败或降级 run 的终态记录完整率 = `100%`。

LLM 输出存在随机性：固定 model、temperature、Prompt 版本后运行一次完整 dev；失败题可用于开发，但每次改动必须新建报告，不覆盖旧结果。

### 5.3 确定性 Graph/CI Gate

使用 FakeLLM/FakeEmbedder/真实测试 PostgreSQL，至少覆盖：

1. 首轮充分直接生成；
2. 首轮不足 → refine → 二次充分；
3. 二次仍不足 → refusal；
4. 部分证据 → partial + not_found；
5. L0 失败 → 重生成一次；
6. L1 篡改引文 → 重生成一次；
7. 第二次验证仍失败 → 确定性降级；
8. evaluate/refine 结构化解析失败默认路径；
9. 检索工具异常、LLM 超时、数据库异常；
10. 跨项目 run/chunk/tool invocation 越权不可见；
11. API 并发边界和事件循环不被同步 embedding 阻塞；
12. 所有路径满足轮次/调用/重试硬上限。

断言必须针对节点序列、调用次数、终态、错误码和持久化记录，不能只断言“返回了字符串”。

## 6. CI 与真实评测分层

CI 禁止下载 Qwen、调用 DeepSeek 或依赖 mini-mall 外部仓库，因此分两层：

### `make eval-ci`

- 使用仓库内最小 Markdown/Java/config fixture；
- FakeEmbedder + 真实 PostgreSQL FTS/HNSW 机制测试；
- 固定 channel ranks 的 RRF golden；
- FakeLLM graph 路径矩阵；
- 解析已提交评测报告，校验 schema、commit、配置和 Gate 字段完整。

### `devkb eval run`

- 本地真实 mini-mall + Qwen/DeepSeek；
- `--split dev` 可重复运行，每次产生不可覆盖的时间戳报告；
- `--split holdout` 默认拒绝，必须加 `--confirm-holdout`；
- 输出 JSON 原始结果和 Markdown 摘要，报告不得手工改写指标。

CI Gate 防机制回归；真实 dev Gate 证明系统质量。不得用 Fake 指标冒充真实召回率。

## 7. P1 holdout

P1 完成所有实现、Prompt、阈值和权重冻结后，才运行一次 8+2 holdout：

```bash
devkb eval run --split holdout --mode all --confirm-holdout
```

必须同时报告：

- vector-exact、vector-hnsw、lexical、hybrid-rrf 的 Recall@5/10、MRR@10，以及 agentic 的回答/引用/轨迹指标；
- P1 同一 445 文件候选语料视图/同一 chunk 快照下，vector-exact 与 hybrid-rrf 的相对对照；
- 与 P0 39 文档/1370 chunks、Recall@10=`0.875` 的历史同口径对照，但明确标注语料规模已经变化；
- 2 个不可答题的 mode 和理由；
- L0/L1、引用 proxy、轮次、调用、token、cost、latency；
- 逐题原始结果与争议标签说明。

Holdout 硬 Gate：

- P1 同一语料/chunk 快照上 `hybrid-rrf Recall@10 >= vector-exact Recall@10`；
  > **2026-07-18 修订（T15.4 偏差裁决）**：随 §5.1 修订同步调整——本条改为
  > **在线默认检索模式（vector-hnsw）的 Recall@10 不低于同快照 vector-exact**
  > （近似损失约束，与 T15.3 overlap Gate 同向）；hybrid-rrf 与 vector-exact 的
  > 相对对照仍必须完整报告，作为记录义务不设硬阈值。
- 最终 L0/L1 均为 100%；
- 0 个 run 超出硬预算或无终态。

P0 的绝对值 `0.875` 只作历史参照，不作 P1 硬 Gate：P1 新增 392 个 Java 和配置文件后，候选 chunks 的规模与干扰分布已经改变，绝对召回下降不能单独归因为 Hybrid 退化。其余指标如实报告，不在看过 holdout 后继续调参。若硬 Gate 失败，P1 不勾选；修复必须基于新增 dev 证据或机制缺陷，之后是否允许再次运行 holdout 由用户裁决并在报告中显著标记，不得静默重跑挑最好成绩。

## 8. 报告文件

建议产物：

```text
evalsets/reports/
├── p1-dev-retrieval-<timestamp>.json
├── p1-dev-retrieval-<timestamp>.md
├── p1-dev-agentic-<timestamp>.json
├── p1-dev-agentic-<timestamp>.md
├── p1-holdout-<timestamp>.json
└── p1-holdout-<timestamp>.md
```

报告必须区分：机器计算结果、人工判断、推断和已知限制。负结果可以通过工程验收，但只有满足本文件硬 Gate 才能通过 P1 阶段验收。
