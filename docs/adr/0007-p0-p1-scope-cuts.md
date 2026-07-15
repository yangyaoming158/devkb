# ADR-0007：P0/P1 裁剪清单

- 状态：已接受（2026-07-12 冻结，2026-07-14 P2/P3 部分经 ADR-0009 修订；本文件为 T10.0 转写，内容以《范围冻结与架构决策》第二、三节为准）
- 依据：《范围冻结与架构决策》审查结论（11 项质询成立 + 7 处自查）与修订后阶段边界

## 决策

对《项目初步规划报告》做如下裁剪，P0/P1 按任务边界（而非周数）划界：

### P0 冻结范围（可运行基线，不是 MVP）

工程骨架；PG+pgvector；Alembic；Markdown/TXT 摄取+标题树分块；基准冻结的单一 embedding 模型；纯向量检索（**精确扫描**，S4：召回=100%，消除近似变量）；固定 RAG 管道（1 次 LLM 调用）；L0 引用存在性校验；引用含真实路径+行号+标题路径；CLI ingest/ask/runs；agent_runs 轨迹；单元+集成测试；CI。

### P0 明确不做（推入 P1/P2）

Agent 环、LLM 规划、关键词/混合/RRF/rerank、代码/PDF/OpenAPI 解析、Redis、FastAPI、HNSW、拒答策略、eval harness、增量索引、多用户。

### P1 收缩（C3：原 12 项单人不可验收 → 7 项核心 + 1 可裁项）

① Java 代码解析（tree-sitter）；② Postgres FTS；③ 混合检索+RRF；④ LangGraph 小图（refine≤1、regen≤1）；⑤ 确定性拒答/部分回答；⑥ agent_steps 轨迹+回放；⑦ Evaluation v0 harness+CI 回归门；⑧ 最小 FastAPI（已确认包含）。

### 主要推迟项（源文档 diff 节）

Redis→P2；Reranker→P2；PDF→P2；OpenAPI→P2；Python 解析→P2；在线 NLI→离线 P2（C7）；增量索引/revision→P2；SSE/HTML→P2；HNSW→P1；FastAPI→P1；ingest_jobs→P2（S6，P0 失败处理=逐文档隔离）；LangGraph checkpoint→P3；冲突检测→P2。

### 删除项

"无 project_id 编译不过"表述（C8）；未验证的显存/延迟断言（C6）；PyMuPDF 许可结论（C11）；所有周数估算（S3）；80/50/20 重型评测集（S7，收缩为 31 问）；P0 的 HNSW 与 API 服务。

## 后果

- 每项裁剪都有编号处置记录（C1–C11、S1–S7），翻案需指向对应编号重审。
- P2/P3 的排序此后由 ADR-0009 管辖（可投递线 = P1 + LangFuse + MCP），本 ADR 不再约束 P2+。
