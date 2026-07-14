# ADR-0009：求职导向 P2/P3 路线重排与可投递线

- 状态：已接受（2026-07-14，用户批准《规划加强修复计划》并要求开始执行）
- 依据：`docs/规划加强修复计划.md`、目标岗位能力审计、Fable 5 审查意见
- 影响范围：P2/P3 方向与后续规划治理；**P0/P1 不变**

## 编号审计

创建本 ADR 时，`docs/adr/` 中已存在 `0002/0004`；《范围冻结与架构决策》已登记 `0001` 至 `0008`，其中 `0001/0003/0007` 还被 P0 T10.0 明确预留。因此 `0009` 是第一个既未落文件、也未登记或预留的编号。

## 背景

原路线能形成扎实的 Python Agentic RAG 与软件工程闭环，但 P2/P3 优先级偏向解析类型、缓存、权限和进阶检索，缺少目标岗位直接要求的 LLM observability、MCP、低代码工作流、云部署、Multi-Agent 与 GraphRAG。若等待原 P3 全部完成后再投递，会把求职启动绑定到数月级扩展。

同时，单纯为简历关键词把 LangChain/LlamaIndex 塞入核心接口，或无问题证据地引入 Neo4j/Multi-Agent，会破坏现有薄接缝、确定性控制流和评测驱动原则。

## 决策

### 1. P0/P1 保持冻结

P0 和 P1 的目标、范围、契约、任务与 Gate 全部不变。P0 期间不得提前实现本 ADR 的 P2/P3 内容；P1 规格仍须等 P0 验收后才能创建。

### 2. 建立可投递线

以下三项全部验收后开始投递，不等待云部署、Multi-Agent 或 GraphRAG：

1. P1 Agentic RAG MVP；
2. P2-A LangFuse 最小闭环；
3. P2-B MCP 只读 Server。

达线时可以陈述“提示词设计与上下文管理实践”，但只有 P2-C 的对照实验完成后才能陈述“Context Engineering（含对照实验）”。

### 3. P2 重排为可投递与 LLMOps 展示阶段

- **P2-A（投递 Gate）**：LangFuse trace、Prompt 版本、dataset/evaluation、cost/latency/quality 闭环；观测失败不得拖垮主链路。
- **P2-B（投递 Gate）**：MCP 只读 Server，复用 project_id 隔离与 Repository，先完成本地 stdio/Streamable HTTP；远程鉴权不作为投递前置。
- **P2-C（投递后逐项裁决）**：教育演示语料、单实例云部署、n8n、Context Engineering/多模型对照、LlamaIndex 隔离实验，以及原 P2 中经评测证明必要的能力。

P2 选择 n8n，不同时引入 Dify。n8n 只编排外部事件与 devkb API，不复制 RAG、Agent 或评测核心逻辑。

### 4. LangChain/LlamaIndex 有界使用

- 保留 `LLMClient`、`Embedder`、Repository 与检索主链路；
- LangGraph 可自然使用 `langchain-core` 的消息、工具或 Runnable 基础类型；
- 禁止用 LangChain ChatModel/VectorStore/Retriever 替换现有接缝；
- LlamaIndex 只进入隔离的 benchmark/experiment，并必须比较质量、延迟、可控性和迁移成本。

### 5. 云部署范围封顶

云部署只做一个平台、一个区域、单实例演示部署与基本监控，包括健康检查、结构化日志、CPU/内存/磁盘/可用性、Secret 注入和最小网络暴露。

明确排除 Kubernetes、集群、高可用、多区域、自动扩缩容、正式备份体系、恢复演练、完整灾备与生产 SLA。

### 6. 教育场景是适配演示

mini-mall 继续作为主语料与主评测基线。P2-C 可在独立 `project_id` 摄取一套小型、自编或许可清晰的课程语料，复用既有链路并复验隔离；不增加教育专用业务代码，不使用真实学生 PII，不宣称教育行业商业经验。

### 7. P3 以收益为门槛

- GraphRAG/Neo4j 只有在 Hybrid RAG 失败问题集成立且对照评测显示稳定收益时才可进入；Neo4j 仅为 sidecar，PostgreSQL 保持真相源。
- Multi-Agent 必须存在职责、工具集或上下文明显不同的专业 Agent，并证明相对单 Agent 工作流的收益；普通 LangGraph 节点不得改名冒充 Agent。
- 原 P3 的 Issue/git history、code_refs、checkpoint 等能力保留为后置可选项，不得抢占上述验证 Gate。

## 后果

### 正面

- 从“完成所有进阶项再投递”改为“P1 + 两个高收益闭环即投递”；
- 保住现有薄接缝、确定性骨架和单 Postgres 主架构；
- 每个新增关键词都有代码、测试、评测或真实客户端证据；
- GraphRAG/Multi-Agent 的负实验也能形成诚实的技术决策材料。

### 代价与风险

- P2 将增加外部服务/协议的集成面，需严格处理脱敏、降级和版本锁定；
- P2-C/P3 仍可能产生范围膨胀，必须逐 Gate 裁决并按路线图裁剪；
- 教育演示只能补场景叙事，不能替代真实行业经验；
- LangChain/LlamaIndex 的有界使用可能无法满足极端关键词筛选，但避免了为框架返工主链路。

## 延后到阶段规格的选择

以下问题不影响本 ADR 接受，在对应阶段开始前裁决：

1. LangFuse 使用云端开发实例还是独立本地部署；
2. 单实例演示部署选择 AWS、GCP 或 Azure 中哪个平台；
3. 教育语料使用自编材料还是许可明确的公开材料。

## 被取代内容

本 ADR 与 `docs/路线图-v2.md` 取代《项目初步规划报告》和原《范围冻结与架构决策》中 P2/P3 的旧方向排序；P0/P1 以及 D1–D10 在其当前阶段的行为边界不被取代。
