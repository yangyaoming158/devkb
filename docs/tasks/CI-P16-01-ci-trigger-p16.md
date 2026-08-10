+++
task_id = "CI-P16-01"
status = "review"
risk = "low"
base_sha = "6dfec2115b488075347f3f5ca21c432ee5411e41"
max_changed_files = 2
allowed_paths = [".github/workflows/ci.yml", "docs/tasks/CI-P16-01-ci-trigger-p16.md"]
allow_dependency_changes = false
allow_migration_changes = false
allow_golden_changes = false
size_exception = ""
+++

# Task Packet：CI-P16-01 把 `p1.6` 加入 CI 触发分支

## 任务目标

推送到 `p1.6` 分支时，GitHub Actions 的 `ci` workflow 会被触发并跑完 `make ci` 与 `make eval-ci`；当前推送到该分支**不触发任何 run**。

## 背景与当前问题

P1.5 已于 2026-08-09 以 Gate 未通过冻结，P1.6 文档已在新分支 `p1.6` 起草（`6dfec21`）。但 `.github/workflows/ci.yml:5` 的触发分支为 `[main, p0, p1, p1.5]`，**不含 `p1.6`**——`6dfec21` 推送后经 GitHub Actions API 查询确认零 run 产生。

后果：《P1.6任务清单》T39.4「当前头提交远端 CI 全绿」在触发器修复前**结构上无法满足**；且 P1.6 全程失去远端回归网。

## 范围

- `.github/workflows/ci.yml`：`on.push.branches` 列表增加 `p1.6`。**仅此一处。**
- 本 packet 文件自身。

## 非目标

- 不改 workflow 的任何 job、step、service、env 或 `pull_request` 触发条件。
- 不改 `scripts/workflow_guard.py` 的 `PROTECTED_BRANCHES`（`p1.6` 在开发期**不应**被保护；P1.6 关闭时再议，另行立项）。
- 不动 `p1.5` 分支，不改任何 P1.5 冻结产物。
- **不据本次 CI 绿勾选 T39.4**——本次只证明触发器恢复，T39.4 要求的是 P1.6 **最终头提交**全绿。

## 仓库事实调查

| # | 事实 | 证据（已执行） |
|---|---|---|
| F1 | 触发分支不含 `p1.6` | `sed -n '3,6p' .github/workflows/ci.yml` → `branches: [main, p0, p1, p1.5]`；`grep -q "p1\.6"` 返回非零 |
| F2 | `6dfec21` 推送后零 run | `curl .../actions/runs?branch=p1.6` → `total_count` 无该分支 run；`?branch=p1.5&per_page=1` 最新仍为 `138dd388`（P1.5 末次提交） |
| F3 | workflow 只有一个 job `checks`，两步业务命令为 `make ci` 与 `make eval-ci` | `cat .github/workflows/ci.yml`（本次已通读全文） |
| F4 | PR 范围检查 `if: github.event_name == 'pull_request'`，push 事件跳过 | 同 F3；`138dd38` 的 run 中该步 `skipped` |
| F5 | `p1.6` 不在 `PROTECTED_BRANCHES` | `sed -n '19,32p' scripts/workflow_guard.py` → `{"main","master","p0","p1","p1.5"}` |

## 关键不变量

- I1：`on.push.branches` 的既有四项 `main/p0/p1/p1.5` **一个不少**。
- I2：`on.pull_request` 触发条件不变。
- I3：`jobs.checks` 全部 step、service、env 逐字不变。
- I4：diff 只有一行（列表内新增 `p1.6`），`git diff --stat` 为 `1 file changed, 1 insertion(+), 1 deletion(-)`。

## 实现计划

1. 编辑 `.github/workflows/ci.yml:5`：`branches: [main, p0, p1, p1.5]` → `branches: [main, p0, p1, p1.5, p1.6]`。
2. `git diff` 逐字核对 I1–I4。
3. 提交、推送到 `p1.6`（先快进合回）。
4. **按过程纪律第 3 条**：`git ls-remote origin p1.6` 与本地 HEAD 逐字比对。
5. 轮询 Actions API 至该 SHA 的 run `completed`，记录 `conclusion` 与 `head_sha`。

## 冻结测试

本任务无可写的自动化测试——CI 触发器的行为**只能由 GitHub 侧观测**，本地无法断言。替代证据（三者齐备才算达成）：

- E1：`git diff` 满足 I1–I4（结构证据）。
- E2：推送后 `git ls-remote origin p1.6` 与本地 HEAD 逐字相同（推送真的落地）。
- E3：Actions API 返回一个 `head_sha` 等于该 HEAD 的 run，且 `status=completed`、`conclusion=success`（触发器确实恢复）。

**E3 是本任务唯一能证明"触发器已修复"的证据**；缺 E3 则任务未完成，不得以 E1+E2 替代。

## 验收标准

1. `.github/workflows/ci.yml` 的 `on.push.branches` 含 `p1.6`，且既有四项一个不少（I1）。
2. `on.pull_request` 与 `jobs.checks` 逐字不变（I2/I3）。
3. `git diff --stat` 为 1 file / 1 insertion / 1 deletion（I4）。
4. 变更文件数 ≤ 2 且全在 `allowed_paths` 内（`make verify-task` PASS）。
5. 本地 `make ci` 全绿。
6. E2 成立：远端 `p1.6` SHA 与本地 HEAD 逐字相同。
7. E3 成立：该 SHA 的 Actions run `completed / success`，`make ci` 与 `make eval-ci` 两步均 success。
8. **未勾选 T39.4**，且在《P1.6任务清单》或本 packet 中留有「本次绿只证明触发器恢复」的明示记录。

## GPT 前置计划审查

（本任务经 2026-08-10 用户直接裁决「是，现在加」并指定为独立低风险 CI-bootstrap 任务；审查者同批给出执行约束——只改 ci.yml 与一份简版 packet、推送后核对 ls-remote 与 Actions 全绿、不得提前勾选 T39.4。上述约束已逐条落入本 packet 的「非目标」与「验收标准」。）

## 执行者自审

实现后逐条核对，**每条附已执行的命令与返回值**：

- **I1 ✅**：`git diff` 显示 `-branches: [main, p0, p1, p1.5]` / `+branches: [main, p0, p1, p1.5, p1.6]`，既有四项逐字保留、顺序未变。
- **I2/I3 ✅**：`git diff --stat` 为 `1 file changed, 1 insertion(+), 1 deletion(-)`，`pull_request` 触发条件与 `jobs.checks` 全部 step/service/env **零改动**（diff 中不出现）。
- **I4 ✅**：同上，diff 恰为一行。
- **范围门禁 ✅**：`make verify-task PACKET=docs/tasks/CI-P16-01-ci-trigger-p16.md` → `changed files: 2 / 2`、**PASS**。
- **本地门禁 ✅**：`make ci` → ruff/pyright 0 错、`1351 passed`。
- **E2**：见下方最终验收（推送后回填）。
- **E3**：见下方最终验收（Actions 结果回填）。

**主动声明的两点局限**：① 本任务无自动化测试可写——CI 触发器行为只能由 GitHub 侧观测，本地断言不了，故 E3 是不可替代的证据；② 本次 Actions 绿**只证明触发器恢复**，`T39.4` 要求的是 P1.6 **最终头提交**全绿，二者不可混同（已写入「非目标」第 4 条与验收标准第 8 条）。

## Review Ledger

| 编号 | 严重度 | 描述 | 处置 |
|---|---|---|---|
| — | — | 待限定审查 | — |

## 定向修复记录

（无）

## 最终验收

（待回填）
