# mini-mall P1 真语料摄取记录（T13.5）

> 运行：2026-07-17 ｜ devkb commit：`ac53459`（T13.4 后）
> 语料视图：`/tmp/devkb-corpus/mini-mall-p1-v1-20260717T093638Z-2348d8`（T13.3 工具构建）
> manifest sha256：`058a57635d4cc80963245e565d99ae559717c63ec9101788037ee2543af5da89`
> 源仓库 commit：`cfb49f5090befe62f024f1781144fe77e5b905c7`（dirty 仅 `.taskmaster/state.json`，在排除范围内）

## 摄取前断言

指令文件（CLAUDE/AGENTS/TASKMASTER.md）、依赖/生成物目录（node_modules/target/build/dist）、
隐藏路径在视图内均为 0（脚本断言通过）；未直接 ingest 仓库根目录。

## 第一次运行（真实 Qwen3-Embedding-0.6B / cuda）

| 指标 | 值 |
|---|---|
| 成功 / 跳过 / 失败 | 406 / 39 / 0 |
| 新增 chunks | 3418（java 3387 + config 31） |
| 耗时 | 95 s |
| GPU 峰值（nvidia-smi 3s 采样） | 7103 MB（8GB 上限内） |

39 个 P0 Markdown 因 content_hash 未变全部跳过——**P0 chunk 零漂移**（1370 块原样保留，
不因 Java parser 改动重摄或重嵌）。

## 第二次运行（幂等）

成功 0 / 跳过 445 / 失败 0，新增 chunks = 0。

## 终态（project=mini-mall）

| doc_type | documents | chunks |
|---|---:|---:|
| markdown | 39 | 1370 |
| java | 392 | 3387 |
| config | 14 | 31 |
| 合计 | 445 | 4788 |

全部 4788 chunks 的 `search_text` 非空（插入点生成，无需 backfill）。

## Java symbol 随机抽查（seed=20260717，6/6 通过）

逐字对照原始仓库 `rel_path` 的 `start_line..end_line` 行切片，含拆分子块前缀规则：

1. `inventory-service/.../InventoryRecordRepository.java:L25-L26` · existsBySourceTypeAndRequestIdAndProductId
2. `inventory-service/.../AiOperationSuggestion.java:L1-L15` · package/imports
3. `inventory-service/.../MockAiProvider.java:L108-L113` · finalAnswerEnvelope
4. `inventory-service/.../InboundOrderRepositoryTest.java:L75-L92` · persistsConfirmationStateAndFindsByRequestId
5. `inventory-service/.../AiInvocationLogWriteRequest.java:L10-L25` · 类型头
6. `inventory-service/.../AiPromptTemplateId.java:L15-L17` · resourcePath

## 复现命令

```bash
uv run python tools/build_corpus_view.py --config corpora/mini-mall-p1.toml
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY \
  HF_HUB_OFFLINE=1 uv run devkb ingest <视图目录> --project mini-mall
```
