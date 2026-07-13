# 《Evaluation v0》

> 冻结日期：2026-07-12 ｜ 适用：P0/P1 ｜ 规模上限：**31 问**（扩充是 P2 的事）
> 语料：mini-mall-order（docs/ 54 个 Markdown + P1 起的 Java 代码）。

## 1. 构成

- 检索问题 25：dev 17 / holdout 8；
- 不可回答问题 6：dev 4 / holdout 2。

## 2. 来源纪律（防 LLM 自产自评循环）

- **≥60% 真实问题**：开发 mini-mall 时实际遇到的疑问；`docs/dev-log.md`、各 `phase*-acceptance.md`、`architecture-ai-review-*.md`、pressure 报告与 git 历史中的问题转写；PRD/验收条目逐条转成"是否实现/如何实现"问句。
- **≤40% LLM 辅助**：仅做措辞变体，必须用**与系统答题 LLM（DeepSeek）不同的模型**生成，且逐条人工审定。
- 相关性标注 **100% 人工**完成。
- 不可答问题须是语料确实无答案的（生产环境配置、未实现功能、外部依赖内部细节）。

## 3. 标注格式（锚点而非 chunk_id——chunk_id 随重摄取漂移）

JSONL 存 `evalsets/v0/`：

```
{ "id": "q001", "question": "…", "answerable": true,
  "relevant": [ { "rel_path": "docs/architecture.md", "anchor": "库存模块 > 并发控制" } ],
  "source": "real|llm_assisted", "notes": "…" }
```

harness 运行时将 anchor（标题路径或符号名）解析到当前 chunk。

## 4. 指标

- 检索：Recall@5、Recall@10、MRR@10；
- 回答：引用存在率（L0 通过率）、引用正确率（引用 chunk 是否命中标注锚，proxy 指标，阶段验收时人工复核）；
- P1 起：拒答率（不可答子集上正确拒答比例）、误拒率（可答子集上错误拒答比例）。

## 5. holdout 纪律

- holdout 10 问**只在阶段验收时运行**（P0 完成、P1 完成各一次），结果留档 `evalsets/reports/`；
- 一切 prompt 调整、阈值调参、检索参数迭代**只允许看 dev 集**；
- 每次 holdout 运行记录日期、代码版本、指标快照。

## 6. 使用节奏

| 阶段 | 用法 |
|---|---|
| P0 期间 | dev 集 15 问子集用于《模型与环境基准方案》质量小测（共用）；无 harness 代码，脚本级即可；数据集撰写与 P0 开发并行（纯人工任务） |
| P1 | 正式 harness（`devkb eval run`）；dev 检索 17 问进 CI 回归门（Recall@10 回退超阈值 fail）；holdout 仅 P1 验收跑一次 |
| P2 | 扩充至原《项目初步规划报告》§17 规模，增加 groundedness(NLI) 离线指标 |
