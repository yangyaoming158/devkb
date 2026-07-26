+++
task_id = "REPLACE-ME"
status = "draft"
risk = "medium"
base_sha = "REPLACE-WITH-40-CHAR-SHA"
max_changed_files = 8
allowed_paths = []
allow_dependency_changes = false
allow_migration_changes = false
allow_golden_changes = false
size_exception = ""
+++

# Task Packet：<TASK-ID> <标题>

> 复制为 `docs/tasks/<TASK-ID>-<slug>.md` 后填写。行为代码任务不得直接修改本模板。

## 任务目标

用一句可观察结果描述任务完成后的行为。

## 背景与当前问题

- 当前行为：
- 复现或仓库证据：
- 用户影响：

## 范围

- 允许修改模块：
- 允许修改文件：
- 最大核心文件数：
- 预计 diff：

## 非目标

- 不实现：
- 不重构：
- 不修改：

## 仓库事实调查

- 入口：
- 调用链：
- 数据模型：
- 当前状态流：
- 可复用能力：
- 当前测试：
- 尚未证实的假设：

## 关键不变量

- 业务：
- 状态：
- 权限/项目隔离：
- 幂等/重试：
- 事务/部分成功：
- LLM/Tool/RAG：
- 失败行为：
- 向后兼容：

不适用项写“不适用 + 原因”，不得留空。

## 实现计划

| 顺序 | 文件 | 必要性 | 最小修改 | 对应测试 |
|---:|---|---|---|---|
| 1 |  |  |  |  |

### 数据流

### 状态流

### 异常与恢复流

### 风险和停止条件

- 如果需要计划外文件：
- 如果规格与代码事实冲突：
- 如果超过文件预算：
- 如果核心不变量无法证明：

## 冻结测试

| 类别 | 输入/前置状态 | 预期结果 | 测试位置 |
|---|---|---|---|
| 正常 |  |  |  |
| 边界 |  |  |  |
| 失败 |  |  |  |
| 重复/重试 |  |  |  |
| 越权/非法状态 |  |  |  |
| 组合/变形不变量 |  |  |  |

## 验收标准

- [ ] <验收项>
- [ ] <验收项>

## GPT 前置计划审查

- 结论：`PENDING`
- 审查基准：
- 阻塞问题：
- 批准范围：

只有结论为 `PLAN_APPROVED` 后，metadata 的 `status` 才能改为 `approved`。

## 执行者自审

- [ ] 实际文件全部在 allowed_paths
- [ ] 没有未批准依赖、迁移、schema、API、golden/snapshot 变化
- [ ] 没有大面积格式化、debug、TODO/FIXME、`if testing`
- [ ] 没有吞异常、降低断言、skip/xfail
- [ ] 状态、权限、幂等、失败行为符合合同
- [ ] 冻结测试真实运行
- [ ] `make verify-task PACKET=<本文件>` 通过
- [ ] 已创建或指定 candidate commit，工作区干净
- [ ] 日志记录 candidate HEAD 和对应命令结果

结论：`NOT_READY`

## Review Ledger

| Issue ID | 严重度 | 合同条款 | 文件/位置 | 证据与实际结果 | 最小修复边界 | 状态 |
|---|---|---|---|---|---|---|

## 定向修复记录

| Issue ID | 修复文件 | 回归测试 | 测试结果 |
|---|---|---|---|

## 最终验收

- Base SHA：
- Head SHA：
- P0：
- P1：
- 当前合同内 P2：
- P3/backlog：
- `make verify-full`：
- 远端 exact-head CI：
- Reviewer 结论：`PENDING`
