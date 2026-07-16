# P1 开发前 vector-exact dev 基线（T11.1）

> 运行：2026-07-16T21:41:58+08:00 ｜ devkb commit：`0a75a895c037260379da1fdda560d660bd6b5f38` ｜ source commit：`cfb49f5090befe62f024f1781144fe77e5b905c7`
> 语料：P0 冻结范围全量 39 文档 / 1370 chunks ｜ corpus sha256：`12e64bbdfd05cda8699a7810a12eb10f52ccbdc88efd7f12e908a4f3cc5141a6`
> 检索：`vector-exact` top_k=10，`Qwen/Qwen3-Embedding-0.6B` / cuda

## 机器计算指标

| Recall@5 | Recall@10 | MRR@10 | 标注锚点覆盖 |
|---:|---:|---:|---:|
| 0.647 | 0.824 | 0.511 | 25/25 |

## 逐题结果

| ID | 首个标注命中名次 | top-1 score | top-1 位置 |
|---|---:|---:|---|
| q01 | 1 | 0.674 | docs/dev-log.md · Development Log > Task 10.5 - Duplicate submit idempotency for create order |
| q02 | 未命中 | 0.722 | docs/architecture-ai-review-2026-06-10.md · MiniMall 全仓架构体检 + AI 库存助手中期审查报告 > 第三部分：核心业务链路审查 > 不变量保护核对表 |
| q03 | 1 | 0.640 | docs/dev-log.md · Development Log > Task 9.2 - OrderStateMachine status transitions |
| q04 | 10 | 0.500 | docs/history/phase2-admin-api-contract.md · Phase 2 Admin API Contract > 3. Role And Authentication Contract > 3.3 Trusted Headers |
| q05 | 6 | 0.642 | docs/api-gateway-contract.md · API Gateway Contract > 4. Current Endpoint Index |
| q06 | 1 | 0.773 | docs/architecture.md · Architecture > Event Flow |
| q07 | 4 | 0.645 | docs/messaging/payment-success-event.md · Payment Success Event Contract > Consumer Rules |
| q08 | 未命中 | 0.721 | docs/audit/fix-plans/phase-b-plan.md · Phase B 修复规划 —— 增强稳定性（一致性语义补齐、测试补盲） > 2. 任务分解 > 任务 B3（P2-4）：重复支付幂等重放 |
| q09 | 1 | 0.679 | docs/deployment.md · Deployment And Operations > Startup Order |
| q10 | 1 | 0.663 | docs/deployment.md · Deployment And Operations > Database Operations |
| q11 | 2 | 0.470 | docs/architecture-ai-review-2026-06-10.md · MiniMall 全仓架构体检 + AI 库存助手中期审查报告 > 第一部分：事实盘点（不含结论） > 项目结构 |
| q12 | 2 | 0.644 | docs/audit/fix-plans/phase-b-plan.md · Phase B 修复规划 —— 增强稳定性（一致性语义补齐、测试补盲） > 2. 任务分解 > 任务 B3（P2-4）：重复支付幂等重放 |
| q13 | 2 | 0.565 | docs/architecture-ai-review-2026-06-10.md · MiniMall 全仓架构体检 + AI 库存助手中期审查报告 > 第六部分：安全风险分级 |
| q14 | 1 | 0.797 | docs/observability.md · Observability > Docker Desktop And WSL |
| q15 | 未命中 | 0.588 | docs/dev-log.md · Development Log > Task 13.3 - 库存初始化与调整（幂等写） |
| q21 | 2 | 0.631 | docs/history/phase1-frontend-prereview.md · Phase 1 前端接手前置审查（只读） > 三、风险点（按优先级） > R9（低）订单超时会变 CLOSED |
| q22 | 6 | 0.536 | docs/api-gateway-contract.md · API Gateway Contract > 1. Gateway Route Ownership |

每题完整 top-10、分数、chunk_id 和人工 relevant 标注见同名 JSON 原始报告。

## 口径与已知限制

- 这是 T13 Java/config 摄取前的 P1 开发前基线，覆盖已验收的 P0 Markdown 全量语料，不是 P1 最终 445 文件语料快照。
- 指标命中依据为 `rel_path + title_path anchor`；运行前已检查所有标注锚点在当前可检索 chunks 中可解析。
- 本报告只描述机器计算结果，未运行 holdout，不代表 Hybrid/HNSW/Agent Gate 已通过。

## 复现命令

```bash
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY HF_HUB_OFFLINE=1 uv run python benchmarks/p1_dev_baseline.py
```
