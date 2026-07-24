# v1.5 机制检查（固定复现 14–18）

> 归属：《Evaluation-v1.5》§2 与 §5.2 确定性 Graph/CI 层。
> 这五条**不是真实 DeepSeek 问答**，依赖脚本化 LLM（FakeLLM）、loader/factory spy 或只读 API，
> 不进入 §5.1"真实 DeepSeek dev 运行"口径；逐条按《P1后真实仓库可用性测试问题记录》§21.1 原判据断言。
> 数据集配套：`contract_dev.jsonl`（1–13 真实问答）、`contract_dev_extended.jsonl`、`contract_holdout.jsonl`。
> 状态：已冻结（2026-07-24 用户逐题确认，与三份 JSONL 一并冻结，`confirmed_at=2026-07-24`）。

| id | 主题 | 执行方式 | 对应任务 |
|---|---|---|---|
| m14 | L1 负例（篡改引文重生成） | FakeLLM 脚本 + 真实测试 PG | T25.2 / verification |
| m15 | 轨迹可判定性 | FakeLLM + 断言持久化元数据 | T30.1 |
| m16 | Run 回放只读一致性 | 只读 GET + 前后计数 | 既有 get_run_trace 回归护栏 |
| m17 | unknown project fail-fast | 冷 AppService/API + loader/factory spy | T29.1/T29.2 |
| m18 | 非法 top_k 输入层拒绝 | API 输入校验 | 既有 Pydantic 回归护栏 |

## m14 · L1 负例（源 §21.1 第 14 条）

- **构造**：给 generate 脚本注入一个 quote，使其规范化后不是任一绑定证据的逐字子串（篡改一字/跨 chunk 拼接各一例）。
- **断言**：
  1. L1 检出该 claim，`route_after_verify` 触发**恰好一次**重生成（受 D6 预算约束）；
  2. 第二稿最终 quote 全部通过 L0/L1；
  3. 重生成**不得新增没有事实依据的 `not_found`**（RT-08：第二稿须重做覆盖度/一致性，不得把错误缺口交付）；
  4. warning 区分 `resolved`（首稿 L1 失败已解决）与 final active，并带 attempt（RT-10）。

## m15 · 轨迹可判定性（源 §21.1 第 15 条）

- **构造**：脚本化两轮检索，制造"目标 chunk 未召回 / 低排名被截断 / 被融合淘汰 / 已给模型但未被采用"四种情形各一。
- **断言**：
  1. 每轮持久化的 per-query 候选元数据（chunk_id/rel_path/line range/rank/score/融合·截断原因）足以事后区分上述四种情形；
  2. 每个 refine term 可追踪来源（用户问题 / 已检证据 / 默认）；
  3. finalize 注入的每条 missing 记录来源与事实校验结果；
  4. 候选条数不超过冻结常量上限（有界，不得无界）；轨迹中无文档正文/secret（D8）。

## m16 · Run 回放只读一致性（源 §21.1 第 16 条）

- **构造**：对已有 run 执行 `GET /runs/{id}`（复用 P1 已实现的 `get_run_trace`）。
- **断言**：
  1. 请求前后数据库 run/step/tool 总数**不变**（不重新执行节点/检索/模型）；
  2. 问题、Answer、steps 顺序与 `trace_summary` 与原记录一致；
  3. 响应不含完整系统 Prompt、证据正文、secret 或 chain-of-thought；
  4. 跨 project 与不存在 run 均 NotFound（对应 `contract_dev_extended.jsonl` e08）。

## m17 · unknown project fail-fast（源 §21.1 第 17 条）

- **构造**：冷 AppService/API（未预热）对不存在 project 调 `/ask`，以 loader/factory **spy** 记录 `_get_embedder`/`_get_llm` 调用次数。
- **断言**（T29 的验收判据）：
  1. `_resolve_project` 先于 `_get_embedder`/`_get_llm`；两个 factory 调用次数**均为 0**；
  2. 返回结构化 404、统一脱敏错误体（无 traceback/DSN/密码）；
  3. 零 project/run/step/tool 持久化、零外部调用；
  4. 现有注入已实例化 Fake 的测试无法覆盖此顺序——必须新增 spy 型回归。

## m18 · 非法 top_k 输入层拒绝（源 §21.1 第 18 条）

- **构造**：`/ask` 传 `top_k` 越界（如 99）。
- **断言**：
  1. API 输入层（Pydantic `ge=1,le=12`）返回结构化 **422**，只报告字段位置 `body.top_k`；
  2. 不回显问题正文/请求体，无 traceback/DSN/secret；
  3. service、模型 factory、数据库 run 路径均**不进入**；数据库计数不变。

## 冻结说明

- 上述断言以《P1后真实仓库可用性测试问题记录》§21.1 第 14–18 条原判据为准，本文件只固化执行方式与到任务的映射。
- m14/m15 是 P1.5 新增能力的确定性回归；m16/m18 是既有 P1 能力的回归护栏（防止 P1.5 改动破坏）；m17 是 RT-23 修复的验收。
