# AGENTS

本文件是 Claude、Codex/GPT、人类开发者共同的流程入口。开工前依次读取：

1. [CLAUDE.md](./CLAUDE.md)：当前阶段、业务和技术硬边界；
2. [开发工作流](./docs/development-workflow.md)：任务合同、计划前审、实现、审查和收敛规则；
3. 当前任务对应的 Task Packet、冻结规格和测试依据。

当前执行状态以 [P1.5 任务清单](./docs/P1.5任务清单.md) 为准。业务/架构冲突按 `CLAUDE.md` 文档优先级裁决，流程冲突以《开发工作流》为准。

## 每个任务都必须遵守

- **先检查工作区**：记录分支、HEAD、`git status` 和 base SHA；旧改动未隔离前不得开工。
- **先合同后代码**：行为代码任务必须从 `docs/templates/task-packet.md` 建立 Task Packet；未冻结目标、非目标、允许文件、验收标准和测试前不得实现。
- **先审计划**：中/高风险任务，以及涉及 Agent 状态、RAG、权限、数据库、Prompt、外部调用的任务，必须获得 GPT 的 `PLAN_APPROVED`。低风险文档任务可走简版快速路径。
- **控制粒度**：通常不超过 5 个核心源文件、8 个总文件；超限必须拆分，或在 Task Packet 中记录用户批准的 `size_exception`。
- **最小补丁**：只改允许路径；禁止顺手重构、计划外依赖/schema/API 变化、大面积格式化、吞异常、`if testing`、降低断言或新增 skip/xfail。
- **测试前置**：实现前冻结正常、边界、失败和至少一条组合/变形不变量；修 bug 时先复现再修。
- **执行者自审**：提交审查前核对合同、计划外文件、状态/权限/幂等、异常、debug/TODO、测试命令和 exact HEAD。
- **限定审查**：GPT 只读取 Task Packet、已批准计划、本次 diff、测试日志和必要上下文；历史问题与 P3 进入 backlog，不阻塞当前任务。
- **一次性 findings**：首审一次性给出带 ID、P0–P3、证据和最小边界的问题账本。正常任务最多一次定向修复。
- **冻结复审**：第二轮只复核既有 issue、修复直接引入的 P0/P1，以及有明确合同条款的修复直接引入 P2。出现两个新 P1、同一不变量再次失败或需扩大核心模块时，停止补丁并 `RETURN_TO_DESIGN`。
- **完成定义**：P0=0、P1=0、当前合同内 P2=0、范围无越界、exact-head 门禁通过；P3 记入 `docs/backlog.md`。

常用入口：

```text
make preflight
make preflight PACKET=docs/tasks/<task>.md
make verify-task PACKET=docs/tasks/<task>.md
make verify-full PACKET=docs/tasks/<task>.md
```
