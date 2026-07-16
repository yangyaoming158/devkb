# Backlog（清单外想法收集处）

规则：开发中产生的任何"顺手做了吧"的想法、优化点、新依赖提案，先记在这里，不直接实现；由用户在阶段间隙裁决进入哪个阶段或丢弃。

| 日期 | 提出者 | 想法 | 动机 | 裁决 |
|---|---|---|---|---|
| 2026-07-14 | 用户/Fable 5/Codex | LangFuse trace、Prompt 版本、dataset/evaluation 闭环 | 形成可观测、可对照、可数据驱动迭代的项目证据 | ✅ 纳入 P2-A，作为可投递 Gate；具体部署方式到阶段规格裁决 |
| 2026-07-14 | 用户/Fable 5/Codex | MCP 只读 Server | 用标准协议开放检索、chunk 与 run trace，展示互操作和安全边界 | ✅ 纳入 P2-B，作为可投递 Gate；本地传输先行，远程鉴权非前置 |
| 2026-07-14 | 用户/Fable 5/Codex | 教育场景演示语料 | 低成本补充教育岗位叙事并复验项目隔离 | ✅ 纳入 P2-C；独立 project_id、小型许可清晰语料，不宣称行业经验 |
| 2026-07-14 | 用户/Fable 5/Codex | 单实例云演示部署 | 补充实际云平台部署证据 | ✅ 纳入 P2-C；限单平台/单区域/单实例与基本监控，排除 HA/灾备/K8s |
| 2026-07-14 | 用户/Fable 5/Codex | n8n 外部集成工作流 | 展示低代码工作流编排和系统集成 | ✅ 纳入 P2-C；只做一个真实工作流，不同时引入 Dify，不复制 RAG 核心 |
| 2026-07-14 | 用户/Fable 5/Codex | Context Engineering 与多模型对照报告 | 把提示词/上下文实践升级为有基线、有变量控制的实验能力 | ✅ 纳入 P2-C；完成前简历只写提示词设计与上下文管理实践 |
| 2026-07-14 | 用户/Fable 5/Codex | LlamaIndex 受控对照实验 | 获得框架实际经验，同时避免返工主链路 | ✅ 纳入 P2-C 可裁项；只放 benchmark/experiment，不替换现有接缝 |
| 2026-07-14 | 用户/Fable 5/Codex | Multi-Agent | 覆盖专业 Agent 协作、上下文隔离与 supervisor 编排 | ✅ 纳入 P3，非投递前置；须证明相对单 Agent 的收益 |
| 2026-07-14 | 用户/Fable 5/Codex | GraphRAG + Neo4j sidecar | 覆盖跨文档关系、调用链和影响分析类问题 | ✅ 纳入 P3，非投递前置；Hybrid 失败问题集与对照评测是引入门槛 |
| 2026-07-15 | Fable 5 | `devkb ask --json` stdout 纯净化 | T10.3 实跑发现 structlog 的 run_finished 日志混入 stdout，`--json` 输出无法被管道直接解析（需跳过前缀）；建议日志改道 stderr | ✅ 2026-07-15 用户裁决直接修：根因是 configure_logging 从未被 CLI 调用（structlog 默认打 stdout，脱敏也未生效）；已在 CLI 回调接入 + logger_factory 指 stderr，真实 ask --json 管道解析验证通过 |
| 2026-07-16 | Fable 5 | CLI 非领域异常兜底（DB 不可达输出原始 traceback） | GPT 审查发现、复现确认（exit=1 正确，仅呈现不友好）；经 P1 规格 §13 与 T19.1 吸收，P0 不单独修 | ✅ 归入 P1 T19.1，随共用 service 异常映射一并实现 |
