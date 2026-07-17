# P1 lexical-only retrieval dev 报告（T14.4）

> 运行：2026-07-18T00:35:16+08:00 ｜ devkb commit：`3a2de50332684cf878934584506242df4a6ac5ec`
> 语料：P1 全量 445 文档 / 4788 chunks ｜ corpus sha256：`0b8af698f960489ae335633a488a7a8cde0cb76b44d1c78e0878c2b760194670`
> 检索：`lexical`（T14.2 冻结的 OR tsquery 构造 + ts_rank_cd），top_k=10，不调用任何模型

## 机器计算指标

| Recall@5 | Recall@10 | MRR@10 | 标注锚点覆盖 |
|---:|---:|---:|---:|
| 0.059 | 0.059 | 0.059 | 25/25 |

### 按问题类型（机械分组规则见 JSON config，非人工标注）

| 组 | 题数 | Recall@10 | MRR@10 |
|---|---:|---:|---:|
| identifier/path/number | 6 | 0.167 | 0.167 |
| natural-language | 11 | 0.000 | 0.000 |

## 检索延迟（本机 WSL2，小样本）

每题 5 次计时、共 85 个样本：P50=18.5ms，P95=79.1ms，max=120.2ms。
**样本量小且运行在开发机（WSL2、后台负载不受控），只用于确认无数量级异常，不外推为生产延迟结论。**

## 逐题结果

| ID | 组 | 首个标注命中名次 | 诊断：top-100 内名次 | top-1 位置 |
|---|---|---:|---:|---|
| q01 | natural-language | 未命中 | >100 | docs/architecture-ai-review-2026-06-10.md · MiniMall 全仓架构体检 + AI 库存助手中期审查报告 > 第二 |
| q02 | natural-language | 未命中 | >100 | docs/audit/architecture-review-2026-07-03.md · minimall 代码架构体检报告 > 4. P1 级问题 > P |
| q03 | natural-language | 未命中 | >100 | docs/history/phase1-frontend-design.md · Phase 1 前端 UI 设计方案（MiniMall 用户前台） > 7.  |
| q04 | natural-language | 未命中 | >100 | README.zh-CN.md · MiniMall Order > 已知限制 |
| q05 | natural-language | 未命中 | >100 | docs/audit/architecture-review-2026-07-03.md · minimall 代码架构体检报告 > 13. 不建议现在做的事情 |
| q06 | identifier/path/number | 未命中 | >100 | README.zh-CN.md · MiniMall Order > 已知限制 |
| q07 | identifier/path/number | 未命中 | >100 | order-service/src/test/java/com/minimall/order/repository/OrderEventRepositoryTe |
| q08 | natural-language | 未命中 | >100 | docs/audit/architecture-review-2026-07-03.md · minimall 代码架构体检报告 > 5. P2 级问题 > P |
| q09 | identifier/path/number | 未命中 | 68 | docs/phase2-5-ai-inventory-readiness-acceptance.md · Phase 2.5 AI Inventory Read |
| q10 | identifier/path/number | 未命中 | >100 | docs/audit/full-repo-audit.md · 全仓库审计报告 > P1 问题 > P1-01：Docker build context 可能包 |
| q11 | natural-language | 未命中 | >100 | docs/architecture-ai-review-2026-06-10.md · MiniMall 全仓架构体检 + AI 库存助手中期审查报告 > 最终 |
| q12 | natural-language | 未命中 | >100 | docs/audit/architecture-review-2026-07-03.md · minimall 代码架构体检报告 > 4. P1 级问题 > P |
| q13 | natural-language | 未命中 | >100 | docs/architecture-ai-review-2026-06-10.md · MiniMall 全仓架构体检 + AI 库存助手中期审查报告 > 第四 |
| q14 | identifier/path/number | 1 | 1 | docs/observability.md · Observability > Docker Desktop And WSL |
| q15 | natural-language | 未命中 | >100 | docs/audit/architecture-review-2026-07-03.md · minimall 代码架构体检报告 > 1. 总体结论 |
| q21 | natural-language | 未命中 | >100 | docs/audit/fix-plans/phase-a-plan.md · Phase A 修复规划 —— 消除订单取消/支付并发竞争 > 1. 背景与目标 |
| q22 | identifier/path/number | 未命中 | 29 | docs/phase2-5-ai-inventory-readiness-acceptance.md · Phase 2.5 AI Inventory Read |

每题完整 top-10、分数、query_tokens 见同名 JSON 原始报告。

## GIN 查询计划验证

### `支付成功事件用的 exchange、routing key 和两个消费队列分别是什么？`（GIN 命中：否）

```text
Limit  (cost=982.72..982.74 rows=10 width=1440) (actual time=20.774..20.776 rows=10 loops=1)
  Buffers: shared hit=9251
  ->  Sort  (cost=982.72..985.25 rows=1013 width=1440) (actual time=20.773..20.775 rows=10 loops=1)
        Sort Key: (ts_rank_cd(chunks.search_tsv, '''支付'' | ''成功'' | ''事件'' | ''用'' | ''的'' | ''exchange'' | ''routing'' | ''key'' | ''和'' | ''两个'' | ''消费'' | ''队列'' | ''分别'' | ''是'' | ''什么'''::tsquery)) DESC, chunks.id
        Sort Method: top-N heapsort  Memory: 56kB
        Buffers: shared hit=9251
        ->  Hash Join  (cost=27.42..960.83 rows=1013 width=1440) (actual time=0.186..20.419 rows=636 loops=1)
              Hash Cond: (chunks.document_id = documents.id)
              Buffers: shared hit=9251
              ->  Seq Scan on chunks  (cost=0.00..928.19 rows=1013 width=1356) (actual time=0.059..9.640 rows=636 loops=1)
                    Filter: ((search_tsv @@ '''支付'' | ''成功'' | ''事件'' | ''用'' | ''的'' | ''exchange'' | ''routing'' | ''key'' | ''和'' | ''两个'' | ''消费'' | ''队列'' | ''分别'' | ''是'' | ''什么'''::tsquery) AND (project_id = '46eebe95-e041-4f2f-85bc-4b2ed0d8bceb'::uuid))
                    Rows Removed by Filter: 4168
                    Buffers: shared hit=7604
              ->  Hash  (cost=21.71..21.71 rows=457 width=96) (actual time=0.078..0.079 rows=450 loops=1)
                    Buckets: 1024  Batches: 1  Memory Usage: 65kB
                    Buffers: shared hit=16
                    ->  Seq Scan on documents  (cost=0.00..21.71 rows=457 width=96) (actual time=0.003..0.048 rows=450 loops=1)
                          Filter: ((status)::text = 'active'::text)
                          Buffers: shared hit=16
Planning:
  Buffers: shared hit=7
Planning Time: 0.243 ms
Execution Time: 20.790 ms
```

### `创建订单接口是怎么防止用户重复提交的？`（GIN 命中：否）

```text
Limit  (cost=970.70..970.73 rows=10 width=1440) (actual time=11.249..11.252 rows=10 loops=1)
  Buffers: shared hit=8387
  ->  Sort  (cost=970.70..972.11 rows=564 width=1440) (actual time=11.248..11.250 rows=10 loops=1)
        Sort Key: (ts_rank_cd(chunks.search_tsv, '''创建'' | ''订单'' | ''接口'' | ''是'' | ''怎么'' | ''防止'' | ''用户'' | ''重复'' | ''提交'' | ''的'''::tsquery)) DESC, chunks.id
        Sort Method: top-N heapsort  Memory: 61kB
        Buffers: shared hit=8387
        ->  Hash Join  (cost=27.42..958.52 rows=564 width=1440) (actual time=0.616..11.070 rows=284 loops=1)
              Hash Cond: (chunks.document_id = documents.id)
              Buffers: shared hit=8387
              ->  Seq Scan on chunks  (cost=0.00..928.19 rows=564 width=1356) (actual time=0.519..7.838 rows=284 loops=1)
                    Filter: ((search_tsv @@ '''创建'' | ''订单'' | ''接口'' | ''是'' | ''怎么'' | ''防止'' | ''用户'' | ''重复'' | ''提交'' | ''的'''::tsquery) AND (project_id = '46eebe95-e041-4f2f-85bc-4b2ed0d8bceb'::uuid))
                    Rows Removed by Filter: 4520
                    Buffers: shared hit=7604
              ->  Hash  (cost=21.71..21.71 rows=457 width=96) (actual time=0.073..0.074 rows=450 loops=1)
                    Buckets: 1024  Batches: 1  Memory Usage: 65kB
                    Buffers: shared hit=16
                    ->  Seq Scan on documents  (cost=0.00..21.71 rows=457 width=96) (actual time=0.003..0.044 rows=450 loops=1)
                          Filter: ((status)::text = 'active'::text)
                          Buffers: shared hit=16
Planning:
  Buffers: shared hit=7
Planning Time: 0.190 ms
Execution Time: 11.274 ms
```

### `40901`（GIN 命中：是）

```text
Limit  (cost=401.58..401.60 rows=10 width=1440) (actual time=0.252..0.254 rows=10 loops=1)
  Buffers: shared hit=132
  ->  Sort  (cost=401.58..401.64 rows=24 width=1440) (actual time=0.252..0.253 rows=10 loops=1)
        Sort Key: (ts_rank_cd(chunks.search_tsv, '''40901'''::tsquery)) DESC, chunks.id
        Sort Method: quicksort  Memory: 53kB
        Buffers: shared hit=132
        ->  Hash Join  (cost=316.63..401.06 rows=24 width=1440) (actual time=0.185..0.240 rows=13 loops=1)
              Hash Cond: (chunks.document_id = documents.id)
              Buffers: shared hit=132
              ->  Bitmap Heap Scan on chunks  (cost=289.20..373.51 rows=24 width=1356) (actual time=0.104..0.118 rows=13 loops=1)
                    Recheck Cond: (search_tsv @@ '''40901'''::tsquery)
                    Filter: (project_id = '46eebe95-e041-4f2f-85bc-4b2ed0d8bceb'::uuid)
                    Heap Blocks: exact=13
                    Buffers: shared hit=81
                    ->  Bitmap Index Scan on ix_chunks_search_tsv  (cost=0.00..289.20 rows=24 width=0) (actual time=0.101..0.101 rows=13 loops=1)
                          Index Cond: (search_tsv @@ '''40901'''::tsquery)
                          Buffers: shared hit=68
              ->  Hash  (cost=21.71..21.71 rows=457 width=96) (actual time=0.070..0.070 rows=450 loops=1)
                    Buckets: 1024  Batches: 1  Memory Usage: 65kB
                    Buffers: shared hit=16
                    ->  Seq Scan on documents  (cost=0.00..21.71 rows=457 width=96) (actual time=0.003..0.041 rows=450 loops=1)
                          Filter: ((status)::text = 'active'::text)
                          Buffers: shared hit=16
Planning:
  Buffers: shared hit=7
Planning Time: 0.171 ms
Execution Time: 0.269 ms
```

## 诊断：低召回的根因（机器实验 + 人工判断）

- **机器实验**：诊断列显示多数未命中题的相关块连 top-100 都未进入。另在同一快照上对照了 ts_rank_cd 归一化 flag 1/4/32 与查询侧丢弃单字 CJK token 共 6 个排序变体，R@10 全部不变（0.059）——这不是排序调参能解决的问题。变体诊断可复现：`benchmarks/p1_lexical_variants_diag.py`，原始输出归档于 `benchmarks/results/lexical_variants_diag.txt`。
- **人工判断（根因）**：11/17 题是中文自然语言问题，而其标注证据（多为 `docs/dev-log.md` 的任务记录）正文以英文命令与要点为主，查询与证据的词面几乎零重叠。词汇鸿沟（含跨语言改写）是 lexical 检索的结构性盲区，正是语义向量与 Hybrid 存在的理由，不构成 FTS 机制缺陷。
- **强项验证**：机械分组的 identifier/path/number 题中 q14 精确命中rank=1——问题原文与证据共享标识符 token 时 lexical 兑现其定位。

## 口径、结论与已知限制

- lexical channel 的定位是 Hybrid/RRF 的一路输入（Evaluation v1 §3），本报告单独观察 FTS 对标识符/数字/中文 token 的贡献，**不设 Gate**、允许负结果；与 vector-exact 的对照在 T15.4 同快照统一运行。对 T15 的含义：lexical 的预期贡献集中在预归类 token 题（Gate 5.1 第 3 条），RRF 融合不得让该 channel 的弱题拖垮 vector 强题。
- 命中判定与 T11.1 基线同口径（`rel_path + title_path anchor`），标注在任何 P1 dev 运行前已冻结，本次未改动任何标签。
- 计划结论：高选择性查询（如单个错误码 token）planner 自然选择 `ix_chunks_search_tsv` 的 Bitmap Index Scan；宽 OR 查询因 camel 拆分子词（and/id/type…）在代码语料中的高词频而按成本正确走顺扫——在当前 4788 chunks 规模下最坏 ~100ms，属可接受；该行为随语料增长的演化与查询侧词频加权的想法已记入 backlog，不在 P1 处理。

## 复现命令

```bash
uv run python benchmarks/p1_lexical_dev.py
```
