+++
task_id = "WF-001"
status = "accepted"
risk = "medium"
base_sha = "4955fbea7b4b7bdbd146886fca4d9e8a4ded3f40"
max_changed_files = 20
allowed_paths = [
  ".agents/skills/",
  ".claude/skills/",
  ".github/workflows/ci.yml",
  ".gitignore",
  "AGENTS.md",
  "CLAUDE.md",
  "Makefile",
  "docs/backlog.md",
  "docs/development-workflow.md",
  "docs/dev-log.md",
  "docs/tasks/WF-001-ai-review-convergence.md",
  "docs/templates/task-packet.md",
  "scripts/workflow_guard.py",
  "tests/unit/test_workflow_guard.py",
]
allow_dependency_changes = false
allow_migration_changes = false
allow_golden_changes = false
size_exception = "一次性安装跨模型规则、skills、守卫脚本与 CI 接缝；不修改业务运行代码。"
+++

# WF-001：AI 开发与审查收敛工作流

## 任务目标

把《AI 开发工作流审计报告》的最小落地方案变成仓库内可执行流程，使普通任务以“一次实现、一次审查、最多一次定向修复”为默认路径。

## 背景与当前问题

- T22、T23、T24 首次实现分别修改 11、16、11 个文件。
- 三个任务累计经历 9、4、7 个提交，并出现修复引入的新问题。
- 当前缺少任务级合同、计划前审、严重程度定义、审查冻结和最大返工次数。
- `AGENTS.md` 仍错误指向 P0 任务清单。

证据与完整分析见 `docs/AI开发工作流审计报告.md`。

## 范围

- 统一 Claude、Codex/GPT 和人类开发者共用的流程规则。
- 增加一份合并式 Task Packet 模板。
- 增加前置计划门禁和限定范围审查两个项目 skill。
- 增加无新依赖的工作流守卫、单元测试、Make 入口和 CI 校验。
- 扩充技术债登记格式。
- 给出用户协调 Claude 执行与 GPT 审查的固定提示词时序。

## 非目标

- 不修改 `src/` 下业务代码。
- 不修复或重审 T24 及其他历史任务。
- 不修改数据库、迁移、依赖或评测数据。
- 不引入 PR 平台、pre-commit 框架、覆盖率或 mutation testing。
- 除本分支内的最终工作流提交外，不创建额外 commit、不推送远程或创建 PR。

## 仓库事实

- Codex 从仓库根目录 `.agents/skills` 发现项目 skill。
- Claude Code 从仓库根目录 `.claude/skills` 发现项目 skill。
- 两者均支持 symlink，因此 skill 正文可保持单一副本。
- 当前门禁为 Ruff、Pyright、pytest 和 `make eval-ci`。
- `.agents` 与 `.claude` 当前均为空目录，`.gitignore` 未忽略它们，需要建立版本化的 canonical skill 与共享入口。

## 实现计划

1. 将 `AGENTS.md` 改为简短的跨模型硬规则，修正当前任务清单指针。
2. 在 `CLAUDE.md` 的文档地图中加入统一流程文档，保留业务技术边界。
3. 新增 `docs/development-workflow.md` 和 `docs/templates/task-packet.md`。
4. 通过 `skill-creator` 初始化两个 skill，在 `.claude/skills` 建相对 symlink。
5. 新增 `scripts/workflow_guard.py`，实现 repo 校验、preflight 和 task scope 校验。
6. 增加守卫单测，并接入 Makefile 与 CI。
7. 运行 skill 校验、工作流守卫、Ruff、Pyright、pytest 和 eval-ci。

## 测试与不变量

### 正常路径

- 合法 Task Packet 能通过解析与范围校验。
- `.agents` canonical skills 和 `.claude` symlink 均能通过 repo 校验。
- 当前分支的允许文件均被识别。

### 失败路径

- 缺少必填 metadata 时失败。
- 修改允许范围外文件时失败。
- 未授权依赖、迁移或 golden 变化时失败。
- 新增 `skip`、`xfail`、`breakpoint`、`pdb.set_trace` 或 `if testing` 时失败。
- 超过文件预算且没有 `size_exception` 时失败。

### 不变量

- 守卫只读检查 Git 和文件，不修改工作区。
- 不新增运行依赖。
- workflow 规则只存在一个 canonical 事实源，skills 只编排、不复制全文。
- 第二轮审查只能复核既有 issue、修复直接引入的 P0/P1，以及有明确合同条款的修复直接引入 P2。

## 前置计划审查

- 结论：`PLAN_APPROVED`
- 依据：用户明确要求从审计报告落地改造；本任务是新流程安装本身，故以用户授权作为 bootstrap 前审例外。
- 例外：文件数超过普通任务门槛，但所有变更属于同一流程安装包，且不触碰业务运行代码。

## 验收标准

- [x] 当前工作位于独立分支 `workflow/ai-review-convergence`。
- [x] `AGENTS.md` 不再指向 P0 清单，并包含任务合同、范围、审查冻结和修复上限。
- [x] Task Packet、P0–P3、任务拆分、DoD 和用户提示词时序可直接使用。
- [x] 两个 skill 通过 `quick_validate.py`，Claude 与 Codex 发现入口均存在。
- [x] 工作流守卫的正常与失败路径有单元测试。
- [x] `make workflow-check` 通过。
- [x] `make ci` 和 `make eval-ci` 通过。
- [x] 最终交付列出用户何时把什么提示词交给 Claude 和 GPT。

## 执行者自审

- [x] 实际变更全部位于 `allowed_paths`，18/20 个文件。
- [x] `src/`、数据库、迁移、依赖、锁文件和评测数据均未修改。
- [x] 没有大面积格式化、debug、TODO/FIXME、skip/xfail 或断言下降。
- [x] 两个模型读取同一 canonical skill；长不变量按需加载。
- [x] `make verify-task`、`make ci`、`make eval-ci` 均真实运行。

结论：`READY_FOR_REVIEW`

## Review Ledger

- 审查基准：本 Task Packet、批准计划、`4955fbea...HEAD` diff 和本地测试日志。
- `WF-001-R1`（P2，已关闭）：实现提示词只要求 dirty-worktree 可运行的 `verify-task`，而限定审查 skill 要求 exact HEAD，未明确候选提交时机会让 diff 与测试日志无法绑定同一版本。最小修复只改《开发工作流》和 Task Packet 模板，要求首审前创建 candidate commit、定向修复后创建 repair commit，并记录对应 HEAD。
- P3：无；未将历史业务问题纳入本任务。
- 裁决：`PASS`。

## 最终验收

- Base SHA：`4955fbea7b4b7bdbd146886fca4d9e8a4ded3f40`
- Head SHA：本提交
- P0：0
- P1：0
- 当前合同内 P2：0
- P3/backlog：0
- `make verify-full`：PASS（本提交 exact HEAD；命令输出见交付记录）
- 远端 exact-head CI：未推送；不属于本任务授权范围，本地等价门禁已通过
- Reviewer 结论：`PASS`
