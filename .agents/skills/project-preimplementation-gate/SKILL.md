---
name: project-preimplementation-gate
description: Freeze a project task contract, repository facts, implementation plan, test matrix, and invariants before behavior code is edited. Use for new features, bug fixes, refactors, migrations, Agent/RAG/tool-calling changes, or any task that needs Claude execution and GPT plan review; also use when the user asks for 前置审查、任务合同、实现计划审查、测试设计、任务拆分, or wants to reduce review rework.
---

# Project Preimplementation Gate

在修改行为代码前，把用户需求转换为同一份可审查、可执行的 Task Packet。执行者和审查者只共享这份合同，不各自补充隐含需求。

## 读取顺序

1. 完整阅读仓库根目录 `AGENTS.md`。
2. 按其文档地图阅读 `CLAUDE.md`、`docs/development-workflow.md` 和当前阶段冻结文档。
3. 使用 `docs/templates/task-packet.md` 创建或更新 `docs/tasks/<TASK-ID>-<slug>.md`。
4. 运行 `make preflight PACKET=docs/tasks/<TASK-ID>-<slug>.md`。
5. 若任务涉及 Agent、RAG、Tool Calling、状态机、异步任务或外部副作用，完整阅读 `references/agent-rag-invariants.md`。

## 执行者：调查与计划

这一阶段只读业务代码；允许创建或更新 Task Packet，不得实现功能。

1. 记录分支、HEAD、工作区状态和基线测试事实。
2. 将目标、范围、非目标、允许/禁止修改路径、最大文件数和验收标准写入 Task Packet。
3. 用仓库证据还原入口、调用链、数据模型、现有测试和可复用能力；不确定项标记“需要进一步验证”。
4. 写出逐文件最小计划，包含正常流、状态流、异常流、权限流、数据一致性和回滚/补偿。
5. 在编码前冻结测试矩阵：正常、边界、失败、越权、重复请求，以及任务适用的不变量。
6. 若预计修改超过 5 个核心文件或 8 个总文件、跨越 3 个以上核心层，或同时改公共 API、数据库 schema 与 Agent 编排，输出“任务需要拆分”。
7. 将 Task Packet 状态设为 `plan-review`，停止并交给 GPT 审查。

低风险、单模块、机械性任务可按 `docs/development-workflow.md` 的快速路径由同一模型完成计划检查；不得跳过 Task Packet 和测试定义。

## 审查者：前置计划审查

只审查 Task Packet、必要仓库事实和现有测试，不审代码 diff，也不提出合同外升级。

逐项验证：

- 需求、非目标和验收标准是否可判定；
- 每个计划文件是否必要且在允许范围；
- 状态、身份、权限、幂等、重试和部分成功是否有明确行为；
- 测试是否能证明关键不变量，而非仅覆盖成功路径；
- 当前设计是否沿用现有架构，任务是否应拆分。

结果只能是：

- `PLAN_APPROVED`
- `PLAN_REVISION_REQUIRED`
- `TASK_SPLIT_REQUIRED`
- `MORE_REPOSITORY_EVIDENCE_REQUIRED`

所有问题一次性列出，包含条款、仓库证据、风险和最小修订边界。审查者只输出结论；执行者收到 `PLAN_APPROVED` 后将结论写入 Task Packet，并把状态设为 `approved`。

## 输出纪律

- 不在计划审查阶段修改业务代码。
- 不把历史问题写进当前合同；非阻塞问题登记到 `docs/backlog.md`。
- 不使用“加强测试”“注意幂等”等空泛措辞；每项都要对应具体输入、期望结果和验证方式。
- 计划批准后，执行者只能按批准范围实现；若事实推翻计划，停止并重新进入本门禁。
