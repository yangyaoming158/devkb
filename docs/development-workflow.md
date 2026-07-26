# AI 开发与审查工作流

本文件是 Claude 执行、GPT 审查和人类协调的统一流程合同。它只规定“怎样完成任务”，业务目标和架构边界仍由 `CLAUDE.md` 指向的冻结规格决定。

目标：普通任务以“一次实现、一次审查、最多一次定向修复”完成；复杂任务在写代码前拆分。

## 1. 角色和唯一事实源

| 角色 | 主要责任 | 不得做 |
|---|---|---|
| 用户 | 给出原始目标；在 Claude 与 GPT 之间传递同一 Task Packet；裁决规格冲突和范围扩张 | 在代码审查阶段静默加入普通需求 |
| Claude / 执行者 | 调查仓库、起草合同与计划、冻结测试、实现、自审、定向修复 | 未经计划前审直接实现中高风险任务 |
| GPT / 审查者 | 前审计划；按合同审查 diff；维护 issue ledger；给最终裁决 | 全仓重审、报告历史问题、把 P3 当阻塞项 |
| 脚本与 CI | 校验范围、文件预算、依赖/迁移/golden 变化、测试降级和项目门禁 | 替代语义审查 |

每个任务只维护一份 `docs/tasks/<TASK-ID>-<slug>.md`。合同、计划、测试、前审结论、review ledger 和最终验收都写在这份 Task Packet 中，不再分别创建多份计划文档。

## 2. 风险和快速路径

### 低风险

- 只改文档、注释、拼写或无行为配置；
- 不改变 API、schema、状态、Prompt、依赖和测试语义；
- 通常不超过 2 个文件。

可使用简版 Task Packet，不强制 GPT 计划前审，但仍需自审和一次限定 diff review。

### 中高风险

满足任一条件即必须完整走 Stage 0–10：

- 修改业务行为；
- 修改 Agent 状态、路由、Prompt、结构化输出或 LLM 调用；
- 修改 RAG 摄取、索引、检索、引用或评测；
- 修改数据库、迁移、事务、权限、隔离、幂等或外部调用；
- 修改公共 API、依赖、golden、snapshot；
- 预计超过 5 个核心源文件或 8 个总文件。

## 3. 标准阶段

| 阶段 | Claude / 执行者 | GPT / 审查者 | 输出与 Gate |
|---|---|---|---|
| 0 工作区 | 记录分支、HEAD、status、base SHA；识别旧改动 | 无 | 工作区可开始；否则先隔离 |
| 1 任务合同 | 写目标、范围、非目标、允许路径、不变量、验收和文件预算 | 只指出歧义 | Task Packet draft |
| 2 事实调查 | 只读确认入口、调用链、模型、现有测试和影响面 | 可要求补仓库证据 | 每个计划假设有证据 |
| 3 实现计划 | 列每个文件的必要性、状态/数据/异常流、最小实现和测试顺序 | 不写实现代码 | 文件级计划 |
| 4 计划前审 | 最多修订一次 | 只能给四类结论 | `PLAN_APPROVED` 后才能实现 |
| 5 测试冻结 | 先定义正常、边界、失败、重复/越权和组合/变形不变量 | 检查是否覆盖主要风险 | 测试矩阵冻结 |
| 6 分段实现 | 先红后绿，小 diff，禁止计划外修改 | 无 | 实现与计划一致 |
| 7 执行者自审 | 核对范围、异常、状态、断言、依赖、debug 和真实测试 | 无 | `READY_FOR_REVIEW` |
| 8 限定代码审查 | 提供 packet、base/head、diff、日志 | 首轮一次性输出 issue ledger | P0/P1/范围内 P2 为 0 |
| 9 定向修复 | 只修 ledger 中的阻塞项 | 只复核旧 issue 和修复直接引入的阻塞问题 | 最多一次普通修复 |
| 10 验收 | 提供 exact-head 证据，登记 P3 | `PASS` / `TARGETED_FIX` / `RETURN_TO_DESIGN` | Definition of Done |

计划前审只能输出：

- `PLAN_APPROVED`
- `PLAN_REVISION_REQUIRED`
- `TASK_SPLIT_REQUIRED`
- `MORE_REPOSITORY_EVIDENCE_REQUIRED`

代码终审只能输出：

- `PASS`
- `TARGETED_FIX`
- `RETURN_TO_DESIGN`

## 4. 任务粒度

推荐上限：

- 小任务：一个行为或不变量，最多 3 个核心源文件；
- 中任务：一个子系统，最多 5 个核心源文件、8 个总文件；
- 手写净增约 300 行以上时，在计划中解释为什么不能拆。

必须拆分：

- 同时修改公共 API、数据库 schema 和 Agent 工作流；
- 同时修改 Prompt/schema、解析器、状态机和持久化；
- 同时增加多个状态和外部调用；
- 新功能与大规模重构混在一起；
- 存在两个以上独立回滚点；
- 无法用一句可观察结果描述验收。

超限但不拆分只能由用户批准，并在 metadata 的 `size_exception` 写明原因。脚本不接受空白例外。

Agent 任务按“合同/schema → 状态模型 → 单节点 → 路由编排 → 持久化/trace → API → 评测”拆分。RAG 任务按“解析/manifest → 索引一致性 → 检索 → 选择 → 生成/引用 → 评测”拆分。

## 5. 测试与不变量

实现前至少冻结：

- 正常路径；
- 边界、空值和上限；
- 失败与超时；
- 重复请求、重试和幂等；
- 非法状态和越权；
- 外部调用部分成功；
- 修复前最小反例；
- 至少一个组合或变形不变量。

涉及 Agent/RAG 时，使用 `$project-preimplementation-gate` 的 `references/agent-rag-invariants.md`，只加载适用部分。

禁止：

- 降低既有断言；
- 新增 skip/xfail 逃避失败；
- 只测 mock 调用次数而不测可观察结果；
- 用更宽 fallback 或吞异常换取绿色；
- 用 line coverage 代替语义不变量。

## 6. 严重程度

| 等级 | 定义 | 当前任务处理 |
|---|---|---|
| P0 | 数据损坏、跨项目泄露、secret、RCE、高风险操作绕审批、不可恢复 | 立即阻塞；必要时独立事故任务 |
| P1 | 核心流程错误、错误 full/refusal、非法状态、重复执行、幂等/事务/权限失效 | 阻塞 |
| P2 | 当前合同内的重要边界、恢复、关键测试或轨迹缺失 | 合同内阻塞；合同外新建任务 |
| P3 | 命名、局部重复、未来扩展、非阻塞重构/文档优化 | 不阻塞，进入 backlog |

历史问题不属于当前 finding。历史 P1/P2 进入 backlog；历史 P0 单独停止并升级处理。

## 7. 审查冻结与修复上限

首审：

- 只读 Task Packet、批准计划、`base...head` diff、测试日志和必要上下文；
- 一次性列出所有已发现的阻塞项；
- 每项必须有 ID、严重程度、合同条款、文件位置、复现/逻辑证据、实际影响和最小修复边界。

定向修复：

- 只修 ledger 中的 P0/P1/合同内 P2；
- 每项先复现再加回归测试；
- 不处理 P3，不顺手重构，不扩大允许路径。

第二轮：

- 只验证既有 issue 是否关闭；
- 只允许新增“修复直接引入”的 P0/P1；
- 只有能指向明确合同条款时，才允许报告修复直接引入的阻塞 P2；
- 不新增普通场景或 P3，不重新做全仓或开放式属性探索。

满足任一条件立即 `RETURN_TO_DESIGN`：

- 第二轮出现两个及以上新 P1；
- 同一核心不变量再次失败；
- 需要计划外核心模块；
- 状态、身份或算法模型无法证明合同；
- 继续修补会增加规则堆叠。

第二次修复只允许一个局部、可证明、无需扩大模块且由上次修复直接引入的阻塞问题，并需用户明确批准。

## 8. Definition of Done

任务通过必须同时满足：

- Task Packet 状态和批准计划完整；
- 实际文件全部在允许范围；
- P0=0、P1=0、当前合同内 P2=0；
- P3 已登记 backlog；
- 无未批准依赖、迁移、API、schema、golden/snapshot 变化；
- 无 debug、`if testing`、测试降级和大面积格式化；
- 目标测试、`make ci` 和任务要求的 eval 门禁通过；
- 最终日志对应 exact HEAD；
- reviewer 输出 `PASS`。

阶段级远端 CI、真实模型评测、人工复核和 holdout 仍按当前阶段规格执行，不被本工作流替代。

## 9. 本地命令

```text
# 开始任务前
make preflight

# Task Packet 建立后
make preflight PACKET=docs/tasks/<task>.md

# 实现/修复后：范围和静态工作流门禁，可在 dirty diff 上运行
make verify-task PACKET=docs/tasks/<task>.md

# 最终验收：要求干净 exact HEAD，并运行项目完整门禁
make verify-full PACKET=docs/tasks/<task>.md
```

`make workflow-check` 校验工作流文档、skill frontmatter 和 Claude symlink，可独立运行，也由 CI 执行。
Pull Request 只要修改代码、测试、脚本、skills、CI、依赖或迁移，就必须同时且只修改一份 Task Packet；CI 会从 PR base 自动选择该 packet 并执行同一范围门禁。纯文档修订不强制新建 packet。

## 10. 用户操作顺序与提示词

### A. 把任务交给 Claude：只调查和计划

```text
使用 $project-preimplementation-gate 处理 <TASK-ID>。
先读取 AGENTS.md、CLAUDE.md、当前规格和任务清单，运行 preflight。
只读调查仓库并创建 docs/tasks/<TASK-ID>-<slug>.md。
冻结目标、非目标、允许路径、文件预算、不变量、验收标准和测试计划。
本轮不要修改业务代码。完成后输出供 GPT 前审的 Task Packet。
```

### B. 把同一 Task Packet 交给 GPT：前审计划

```text
使用 $project-preimplementation-gate，以计划审查者身份审查
docs/tasks/<TASK-ID>-<slug>.md。
只审任务合同、仓库事实、实现计划、测试和适用的不变量。
不得新增普通需求或提出任务外升级。
只输出 PLAN_APPROVED、PLAN_REVISION_REQUIRED、
TASK_SPLIT_REQUIRED 或 MORE_REPOSITORY_EVIDENCE_REQUIRED，并给出证据。
```

### C. 计划通过后交回 Claude：实现

```text
GPT 对 <TASK-ID> 的结论是 PLAN_APPROVED。
按已批准的 docs/tasks/<TASK-ID>-<slug>.md 实现。
先落冻结的失败测试，再做最小补丁；只改 allowed_paths。
完成后运行目标测试、make verify-task，并填写执行者自审。
不要处理合同外问题，不要自行宣布最终验收。
```

如果 GPT 要求修订，只把其 findings 原样交回 Claude；Claude 最多修订一次计划。若为 `TASK_SPLIT_REQUIRED`，先拆任务，不进入实现。

### D. 实现完成后交给 GPT：限定代码审查

```text
使用 $project-scoped-review 审查 <TASK-ID>。
输入仅限 Task Packet、已批准计划、BASE_SHA...HEAD diff、
执行者自审和测试日志，以及理解 diff 必需的相邻代码。
首审一次性输出 issue ledger。不得审历史问题、不得新增需求、
不得把 P3 当阻塞项。最终只给 PASS、TARGETED_FIX 或 RETURN_TO_DESIGN。
```

### E. 只有 TARGETED_FIX 时交回 Claude

```text
对 <TASK-ID> 执行定向修复，只处理以下 issue：
<粘贴完整 issue ledger>
先复现、加一个最小回归测试、做最小补丁并运行对应门禁。
禁止修复未列出问题、顺手重构或扩大 allowed_paths。
完成后输出 issue 到修改/测试的映射。
```

### F. 把修复结果交给 GPT：终局复核

```text
使用 $project-scoped-review 对 <TASK-ID> 做终局复核。
只验证既有 issue 是否关闭，以及修复是否直接引入 P0/P1
或有明确合同条款的阻塞 P2。
不得新增普通场景或 P3，不再开放式探索。
按 PASS、TARGETED_FIX、RETURN_TO_DESIGN 规则裁决。
```

## 11. 技术债与流程指标

合同外 P2、全部 P3 和未来优化写入 `docs/backlog.md` 的扩展登记表，不得留在 review ledger 中阻塞任务。

连续统计至少 10 个任务：

- 首次 diff 文件数；
- 首审 P0/P1/P2；
- 修复引入问题数；
- 审查轮数；
- 计划外文件数；
- `RETURN_TO_DESIGN` 次数。

目标：普通任务平均定向修复次数小于 1，任何任务不再以四到五轮补丁方式收敛。
