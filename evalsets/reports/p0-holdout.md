# P0 holdout 基线报告（T10.4）

> 运行：2026-07-15 ｜ 代码版本：commit `7fdf540`（p0 分支）｜ 脚本：`benchmarks/holdout_baseline.py`
> 语料：mini-mall 项目（39 文档 / 1370 chunks，T10.2 摄取）｜ 检索：纯向量精确扫描 top_k=10（Qwen3-Embedding-0.6B, cuda）
> 纪律：Evaluation-v0 §5——holdout 仅阶段验收时运行，本次为 P0 验收运行；**看过结果后不回改标注/参数**。

## 指标（检索 holdout 8 问）

| Recall@5 | Recall@10 | MRR@10 |
|---|---|---|
| **0.625** | **0.875** | **0.440** |

参照：dev 集 15 问在 T1.4 基准（500 块采样语料）上 Recall@5=0.80。holdout 是全量 1370 chunks + 未调过的题，数字更低属预期。

## 逐问明细

| ID | 首个命中名次 | top-1 (score) |
|---|---|---|
| q16 | 10 | 0.729 audit/architecture-review · 总体结论 |
| q17 | 2 | 0.626 history/phase0-api-contract-scope · Internal Boundary |
| q18 | 1 | 0.638 api-gateway-contract · Response And Pagination Rules |
| q19 | 1 | 0.649 dev-quickstart · 推荐：可复现演示路径 |
| q20 | **未命中** | 0.566 history/phase1-frontend-prereview · 错误码/安全 |
| q23 | 6 | 0.695 history/phase2-admin-api-contract · 6.7 Audit |
| q24 | 2 | 0.661 audit/full-repo-audit · P1-04 demo seed |
| q25 | 4 | 0.668 dev-log · Task 19.2 压测文档 |

## 不可答 2 问（备档，P0 无拒答策略）

| ID | top-1 | top-3 |
|---|---|---|
| u05 | 0.430 | 0.430 / 0.420 / 0.398 |
| u06 | 0.511 | 0.511 / 0.491 / 0.488 |

可答题 top-1 分数范围 0.566–0.729，不可答题 0.430–0.511——存在可分性信号但有重叠（q20 的 0.566 与 u06 的 0.511 接近），单一阈值拒答在 P0 数据上不干净，支持 P1 采用 evaluate 节点而非纯阈值的设计。

## 观察（如实记录，不据此改动）

1. **q20（错误码 40901/40902）是纯向量的典型失败模式**：精确数字 token 无语义近邻。出题时已标注"精确 token 困难题"，本次验证了预期。P1 的 FTS/混合检索正是为这类问题设计（待验假设⑥的对照点）。
2. **q20 的 top-1 实际包含正确答案**（phase1-frontend-prereview 错误码清单列有 40901/40902 的含义），但该位置不在人工标注锚点内——按 proxy 指标计未命中。这提示标注锚点覆盖不全的可能，留待 P1 harness 时按纪律（不看 holdout 结果调标注 → 由独立复核裁决）处理。
3. q16/q23 命中名次靠后（10/6）：审计/契约类长文档的相近章节多，纯向量区分弱，rerank/混合的潜在受益点。
