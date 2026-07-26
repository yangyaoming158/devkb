---
name: project-scoped-review
description: Perform a contract-bounded code review or frozen repair re-review using an approved Task Packet, approved plan, current diff, and exact-HEAD test evidence. Use when reviewing Claude implementation, conducting 限定范围审查、终局验收、定向修复复审, classifying P0-P3 findings, or deciding PASS, TARGETED_FIX, or RETURN_TO_DESIGN without expanding requirements.
---

# Project Scoped Review

让审查在一轮发现、一次定向修复内收敛。此 skill 只读：不得修改代码、任务合同或测试。

## 必需输入

开始前确认并记录：

1. 当前分支、exact HEAD 和 `git status`；
2. 已批准 Task Packet 及批准的实现计划；
3. Task Packet 的 `base_sha` 到当前状态的 diff；
4. 执行者自审和测试日志，且日志能够对应 exact HEAD；
5. 若为复审，上一轮冻结的 issue 列表和修复前基准提交。

缺少 Task Packet、未批准计划、不可识别 base、测试证据不对应 HEAD 时，不开展开放式审查；返回 `RETURN_TO_DESIGN` 或要求补齐证据。

## 首轮限定审查

只读取 Task Packet、批准计划、本次 diff、测试日志和证明具体发现所必需的相邻代码。不得重新审查整个仓库。

一次性检查：

- 合同验收条款是否全部满足；
- diff 是否越过允许路径、引入依赖/迁移/公共 API 等未批准变化；
- 当前变更是否引入 P0、P1 或合同内 P2；
- 测试是否真实执行并能证明关键正常、边界和失败行为；
- 是否吞异常、降低断言、跳过测试、留下 debug 分支，或破坏 Agent/RAG 适用不变量。

同一根因合并为一个 issue；不要用多个场景重复计算。历史问题、普通优化和未来架构建议只能作为 P3/技术债，且不得阻塞。

## 发现格式

每个阻塞发现必须包含：

- `Issue ID`
- 严重程度 `P0` / `P1` / `P2`
- 对应任务合同条款
- 文件和代码位置
- 可复现输入或逻辑证据
- 期望结果与实际错误结果
- 是否由本次 diff 或修复直接引入
- 一个回归测试能够证明的最小修复边界

没有以上证据，不得列为阻塞 issue。命名、局部重复、未来扩展和非合同内优化归为 P3。

## 定向修复复审

第二轮只允许检查：

1. 冻结 issue 是否逐一关闭；
2. 修复是否直接引入新的 P0/P1；
3. 有明确合同条款、且由修复直接引入的阻塞 P2；
4. 修复测试和 exact-HEAD 回归是否通过。

不得新增普通场景、历史问题或未来优化。若第二轮出现两个及以上新 P1、同一核心不变量再次失败、或修复需要计划外核心模块，停止补丁并返回设计阶段。

## 最终裁决

- `PASS`：P0=0、P1=0、合同内阻塞 P2=0、所有冻结 issue 关闭、exact-HEAD 测试通过、无越界修改。
- `TARGETED_FIX`：只剩一个局部且可证明的问题，设计仍成立，可由一个回归测试和一个最小补丁解决。
- `RETURN_TO_DESIGN`：设计/身份/状态/算法结构无法证明合同，出现多个新 P1，同一不变量反复失败，或修复必须扩散。

输出 findings 优先；无 findings 时明确写“未发现阻塞问题”。P3 可登记到 `docs/backlog.md`，但不影响裁决。
