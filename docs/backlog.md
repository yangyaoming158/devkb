# Backlog（清单外想法收集处）

规则：开发中产生的任何"顺手做了吧"的想法、优化点、新依赖提案，先记在这里，不直接实现；由用户在阶段间隙裁决进入哪个阶段或丢弃。

从 2026-07-26 起，代码审查产生的合同外 P2、全部 P3 和未来优化不得留在当前 review ledger 阻塞任务，统一登记到文末“扩展技术债登记”。历史表保持原貌，不做无意义迁移。

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
| 2026-07-17 | Fable 5 | lexical 查询侧词频/IDF 加权（或丢弃语料高频拆分词） | T14.4 实测：camel 拆分子词（and/id/type…）在代码语料中的高词频使宽 OR tsquery 匹配 65% chunks，planner 正确走顺扫（~100ms@4788 chunks），且高频词稀释 ts_rank_cd；随语料增长该行为会恶化。已验证归一化 flag 与去单字 CJK 均无收益，词汇鸿沟才是主因，故不属于 P1 必修 | 待裁决 |
| 2026-07-20 | Fable 5 | `GENERATE_SYSTEM` 的 Schema 示例把 `not_found` 占位值喂给了模型，导致本可 full 的回答被降级为 partial | U2.2 材料准备时发现：8 个 partial 中 q05、q07 属误降级（q04 是 claim 未过 L1 被移除，行为正确）。q07 的 not_found 逐字等于 Prompt 里的示例 `"not_found":["未覆盖方面"]`——模型把格式示例当内容抄；q05 写了语义相反的「无其他未覆盖的方面。」。根因：`not_found` 是唯一经常合法为空的字段，示例却给了可抄占位值，且 Prompt 未说明「无则返回空数组」。`finalize` 要求 `not draft.not_found` 才判 full，本身是正确的保守设计，不应改判定逻辑。修法：Schema 示例改 `"not_found":[]` + 补一句空数组指令。影响：不破坏 §5.2/§7 任何硬项，仅使 mode 分布偏保守（8 full/8 partial → 应为 10/6）与一行无意义的用户可见输出 | 已裁决 2026-07-21（randy）：P1 保持现状不修——属 Prompt 变更会使 PROMPT_VERSION 递增、冻结失效、须重跑 dev §5.2 Gate（约 76 次真实调用且有 false-refusal 触上限风险），取舍不利；留待 P2 Prompt 迭代时随冻结失效一并修 | 
| 2026-07-22 | 用户/Codex | P1 后真实陌生仓库可用性测试问题包：指定证据类型、`not_found` 语义、语义/权威性、精确路径与方法 chunk 召回、跨轮证据保持、refine 查询漂移、Vue/TypeScript 覆盖、拒答负证据与轨迹/人机界面 | 六次真实 DeepSeek 问答覆盖“测试替代生产实现仍判 full”“已索引/已召回路径仍被称为不存在”“格式未摄取后的安全拒答”“资料确实未记录时的正确拒答”“校验失败后的安全 partial”“完整调用链源码均已索引却假拒答”；需修复能力缺口，同时保留正确拒答基线 | 待用户裁决；见 [`问题记录`](./P1后真实仓库可用性测试问题记录.md) 与 [`手工测试清单`](./P1后真实仓库手工测试清单.md)，未实施修复、未触碰 P1 holdout |
| 2026-07-23 | 用户/Codex | 真实仓库跨用户隔离问答的证据闭环与安全结论校准 | 第七次 DeepSeek run 主要 owner SQL 正确且保守判 partial，但两条 not_found 可被已索引源码直接证伪；generate 无 not_found、finalize 却注入 evaluator 错误 missing；正文把 Service 前置保护与 Repository 自身过滤混为统一双重防线，并对未覆盖路径给出绝对安全结论 | 待用户裁决；并入 [`问题记录`](./P1后真实仓库可用性测试问题记录.md) RT-01/02/03/04/06/15/17/18 与 [`手工测试清单`](./P1后真实仓库手工测试清单.md) 第 7 项，未实施修复、未触碰 P1 holdout |
| 2026-07-23 | 用户/Codex | JWT 配置分层问答的直接证据与安全保证校准 | 第八次 DeepSeek run 正确区分 application fallback 与 Compose 必填，但未召回已索引且明确要求的 `docker-compose.yml`/关键 Java 配置，evaluate 仍 sufficient；答案把 Compose 非空检查扩大为 secret 强度和生产安全保证，未识别裸 JVM fallback 不受 profile 限制、Java 无长度/弱值校验 | 待用户裁决；并入 [`问题记录`](./P1后真实仓库可用性测试问题记录.md) RT-01/02/03/05/06/08/10/11/15/17/19 与 [`手工测试清单`](./P1后真实仓库手工测试清单.md) 第 8 项，未实施修复、未触碰 P1 holdout |
| 2026-07-23 | 用户/Codex | NO_ANSWER/UNGROUNDED 生产源码问答的跨轮覆盖单调性与 partial 保留 | 第九次 DeepSeek run 四个指定生产类均 active；第一轮已支持两方面、只缺 CitationParser，第二轮补到 CitationParser 时却丢失 RagService，supported_count 从 2 降至 1，最终把仍可回答的非法引用行为也全量清空为 refusal | 待用户裁决；并入 [`问题记录`](./P1后真实仓库可用性测试问题记录.md) RT-01/02/03/05/06/15/16/17/20 与 [`手工测试清单`](./P1后真实仓库手工测试清单.md) 第 9 项，未实施修复、未触碰 P1 holdout |
| 2026-07-23 | 用户/Codex | 删除源文档后历史引用问答的迁移/生产读取证据闭环与 SQL 摄取覆盖 | 第十次 DeepSeek run 对 `chunk_id=null`、`snippet`/`document_filename` 快照保留的结论正确并保守判 partial，但最终只引用设计/失败案例；Flyway `.sql` 因格式不支持而 0 文档入库，已索引的 Service/Repository/DTO 路径也未形成引用，evaluate 的 sufficient 又与 generate 的 3 条 not_found 冲突 | 待用户裁决；并入 [`问题记录`](./P1后真实仓库可用性测试问题记录.md) RT-01/02/03/05/06/07/13/15 与 [`手工测试清单`](./P1后真实仓库手工测试清单.md) 第 10 项，未实施修复、未触碰 P1 holdout |
| 2026-07-23 | 用户/Codex | 全局否定与穷举代码问答的确定性 repository inventory | 第十一问需要证明 Phase 6 Agent 在限定 `src/main` 中未实现，第十二问需要闭合全部生产 Controller/endpoint；当前 planner/refine 只把 grep/find/遍历写成检索字符串，实际有限 top-k retrieve 既不能证明“不存在”，也不能保证“全部”。目标提交可确定性得到 Phase 6 默认跳过、无生产 Agent 符号，以及 6 个 Controller/26 个静态映射 | 待用户裁决；并入 [`问题记录`](./P1后真实仓库可用性测试问题记录.md) RT-01/02/05/06/07/14/15/17/21 与 [`手工测试清单`](./P1后真实仓库手工测试清单.md) 第 11–12 项；应设计安全只读 manifest/symbol/annotation 能力，不直接开放任意 shell，未实施修复、未触碰 P1 holdout |
| 2026-07-23 | 用户/Codex | Prompt Injection/秘密提取/越权命令请求的 policy-refusal 终态 | 第十三次 DeepSeek run 最终未泄露、未编造、未执行命令，且只有 retrieve 工具调用；但把系统 Prompt、凭据和 `.env` 结果列为普通 not_found，明显攻击仍走 4 次 LLM 与两次失败校验，最终 refusal 由 finalize 删除失败 claim 后形成而非明确策略终态 | 待用户裁决；并入 [`问题记录`](./P1后真实仓库可用性测试问题记录.md) RT-02/10/11/22 与 [`手工测试清单`](./P1后真实仓库手工测试清单.md) 第 13 项；需同时保留合法安全咨询反例，未实施修复、未触碰 P1 holdout |
| 2026-07-23 | 用户/Codex | Unknown project 的模型初始化 fail-fast 顺序 | API 实跑的 404、统一脱敏错误、零 run/step/tool 与零外部 LLM 请求均正确；但生产 `AppService.ask()` 在 `_resolve_project` 前先 `_get_embedder/_get_llm`，冷服务会为必然 404 的请求加载 SentenceTransformer。现有测试注入已实例化 Fake，无法观察 factory 调用 | 待用户裁决；并入 [`问题记录`](./P1后真实仓库可用性测试问题记录.md) RT-23 与 [`手工测试清单`](./P1后真实仓库手工测试清单.md) 第 15 项；修复须以冷服务 loader spy 证明两个 factory 为 0 次，并保持 404/脱敏/零持久化，未实施修复、未触碰 P1 holdout |
| 2026-07-26 | Claude Opus 5 | Task Packet 模板增设默认条款：新增的用户可见文案/warning 必须配一条禁用词表断言 | T25.1 前审三轮里，PG-01 与 PG-05 是同一类错误——用"目标文件被引用"这一可证事实，推出"命题已被支撑"（PG-01）与"与正文存在冲突"（PG-05）两个不可证结论。第一次修订改掉了结论措辞，却在新写的 warning 里原样复发；真正止住它的是把禁用词表变成机器断言的 U14，而不是复审措辞本身。说明前审对"措辞类过度声称"的收敛效率低（两轮才收敛），且这类缺陷在 Agent/RAG 诚实性任务里会反复出现（T22 三/四审、T23 三审、T24 四/五/六审同属"判据说得比能证的多"）。建议在 `docs/templates/task-packet.md` 的「冻结测试」固化一行默认要求：凡新增用户可见文案或 warning，必须有一条列出禁用措辞的否定断言测试，覆盖范围含 warning 而不只是正文。**2026-07-27 收尾扩展（本条提案覆盖面不足）**：T25.1 首审的 `T25.1-CR-01` 是同一个错误的第三次出现，但它在 `citation_scope` 的**解析层**——"某个 token 解析到一个被引用路径"推出"这条缺口说的就是它"，压根没有用户可见文案可扫，禁用词表天然挡不住（前两次分别在结论层 PG-01、warning 层 PG-05）。故模板条款应从"文案配禁用词表断言"扩为两条：①凡新增用户可见文案或 warning，配禁用措辞否定断言（原提案）；②凡新增**确定性判据**，在「关键不变量」显式写出它**证明了什么**与**推不出什么**，并为后者配一条 fail-closed 负例测试 | 待裁决 → ✅ 2026-07-26 用户同意：先登记，**待 T25.1 收尾验收后**再改模板；不在 T25.1 合同内实施（`docs/templates/task-packet.md` 不在其 allowed_paths）。T25.1 已于 2026-07-27 终局复核 PASS 并勾选（`5170a02`→`9746a7c`→`7b07879`），前置条件满足。**✅ 2026-07-27 已实施**：`docs/templates/task-packet.md` 三处落点——「关键不变量」增「判据可证边界」必填项、「冻结测试」增「诚实边界」+「判据 fail-closed 负例」两行默认条款（含不适用须写原因、不得删行）、「执行者自审」增一条对应勾选项并把「冻结测试真实运行」改为逐行对表（针对 T25.1-CR-02 凭记忆漏写 U13）。`make workflow-check` PASS。本条关闭 |
| 2026-07-25 | Claude Opus 5 | type-only 必需证据的 not_found 文案重复（"生产源码:生产源码"） | T22 收尾自查发现：`required_evidence_tail` 用 `TYPE_LABELS[type]:{symbol or path or anchor}` 组装，type-only 项（如"请引用生产源码"）的 anchor 恰是类型词本身，用户看到冗余重复。仅文案问题，不影响 full/partial 判定与覆盖矩阵 | 待裁决；建议随 T26 文案口径或 T32.1 README 口径统一处理，不单独改 |

## 扩展技术债登记

新增条目必须说明为什么不阻塞当前任务，以及在什么条件下重新评估。严重度只允许“合同外 P2”或“P3”；P0/P1 不得进入此表逃避修复。

| ID | 日期 | 来源任务 | 严重度 | 问题/建议 | 不阻塞理由 | 重新评估条件 | 负责人 | 状态 |
|---|---|---|---|---|---|---|---|---|
