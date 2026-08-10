# T33.1 · function-calling 可行性实测探针（结果）

- 日期：2026-08-11 ｜ 模型：`deepseek-v4-flash`（ADR-0004 冻结）｜ 端点：DeepSeek OpenAI 兼容
- 脚本：`benchmarks/p16_function_calling_spike.py`（零仓库数据、不导入 `devkb.*`、不写库）
- 原始数据：`benchmarks/p16_function_calling_spike.json`
- 运行方式：unset 全部代理变量（CLAUDE.md 环境事实）；`max_retries=0` 关闭 SDK 隐式重试使请求可数
- **三次重复，关键观测三次完全一致**

## 结论先行

**ADR-0011 的可行性前提成立**——该模型可用 function-calling：返回结构化 `tool_calls`、`arguments` 为可解析 JSON、喂回工具结果后能继续并给出最终文本、`tool_choice="none"` 可强制不调用。

**但本探针同时推翻了《P1.6实现规格》三处已冻结的设计**，且其中一处会在生产中直接产生 HTTP 400。详见「推翻了什么」。

## O1–O6 观测

| 观测 | 结果（三次一致） |
|---|---|
| **O1** 能否返回结构化 `tool_calls` | ✅ 能。`id` / `type="function"` / `function.name` / `function.arguments`（字符串）。**`arguments` 三次全部可 `json.loads`** |
| **O2** 一次响应的 `tool_calls` **实际条数** | **多意图问题 → `4`**（三次均为 4，且 name/args 完全相同：`get_weather×2` + `get_population×2`，城市各北京/上海）；单意图问题 → `1` |
| **O3** 喂回工具结果后能否继续 | ✅ 能。`finish_reason="stop"`，正文 124–180 字，正确综合了四个工具结果 |
| **O4** `content` 与 `reasoning_content` 形态 | **两者并存，且 `content` 与 `tool_calls` 也并存**：多意图时 `content_len=20`（非 0）+ 4 个 tool_calls；单意图时 `content_len=0` + 1 个 tool_call。`reasoning_content` 恒存在（`reasoning_len` 91–321）。**T31.2R-a 的「content 为空」在本探针中未表现为故障**——带 tools 时 `content` 为空是协议常态，不是缺陷 |
| **O5** 请求数与 token | 本探针每 trial 4 次请求、总 token 1993 / 2069 / 2151。**一次真实的「1 轮工具 + 1 次收尾」= 2 次请求**；多意图那次 prompt 719 tokens（含 4 条工具结果回填） |
| **O6-a** 不回工具结果直接再问 | ❌ **`BadRequestError` 400**：`An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id'.`（三次一致） |
| **O6-b** `tool_choice="none"` | ✅ 生效。`tool_calls_n=0`，返回文本「无法获取实时天气数据…」 |

## 推翻了什么（这是本 spike 的主要价值）

### ① 「一次响应最多一个 tool_call」——**必须废弃**

《P1.6实现规格》§6.1/§6.3 与 ADR-0011 §2 冻结了「一次模型响应最多携带一个工具调用，多于一个即判为协议违规」。

**实测：多意图问题三次全部返回 4 个 `tool_calls`。** 而 P1.6 要处理的正是多部分仓库问题（「一共有几个 Controller」「哪些方法带某注解」），**恰恰是多意图**。按该规则，模型每次正常且正确的行为都会被判为协议违规。

### ② 「协议失败槽位：记 warning、本轮无工具结果、继续下一轮」——**在协议上不可实现**

这是第三轮为修 `PG-P16-04`（控制流泄漏）而设计的核心机制。实测 O6-a 证明：**assistant 消息带 `tool_calls` 后，必须为每个 `tool_call_id` 回一条 tool 消息，否则下一次请求直接 400。**

所以「不执行、记 warning、继续」这条路**走不通**——会话在协议层就断了。这不是效率问题，是可行性问题。

**实测反推出的修法**（比纸面设计更简单）：超出上限的 `tool_call` **仍然回一条 tool 消息**，内容是确定性文案（如 `{"error":"not_executed: exceeds per-round limit"}`）。协议满足、模型拿不到任何控制权、轮次照常推进。

### ③ 「`content` 与 `tool_calls` 二选一」——不成立

实测两者并存（多意图时 `content_len=20` 且 `tool_calls_n=4`）。任何按「有 tool_calls 就没有 content」写的解析分支都会丢信息。

## 对 11/5/4 的定标含义（首次有真实数据）

- 一次「模型响应 → 执行 N 个工具 → 回填 → 收尾」= **2 次供应商请求**，与工具个数 N 无关。
- 因此 `MAX_TOOL_ROUNDS = 4`（4 次模型响应）在最坏情形下是 **5 次请求**（4 轮 + 1 收尾），原推导成立。
- **但「轮次」的语义要改**：一轮 = 一次模型响应 + 其**全部**工具执行，而不是「一次工具执行」。需要另设**每轮工具执行数上限**（实测多意图为 4，建议上限取 ≥4 否则常态被截断）。
- token：带 4 条工具结果回填时 prompt 为 719 tokens，远低于原定的 6000 累计顶——该顶暂看不构成约束，但需在真实 inventory 返回值上重测。

## 稳定性

三次重复的 `tool_calls_n`、name、arguments、finish_reason、400 错误形态**完全一致**；仅 `reasoning_len`（178/227/321）与最终正文长度（128/124/180）有自然波动。**未复现 T31.2R-a 的间歇性空 content 故障**（本探针 prompt 远短于 P1.5 的在线 Prompt，不能据此认为该问题已消失）。

## 未验证 / 不在本探针范围

- 真实 inventory 工具返回值（远大于假工具）下的 token 与截断行为。
- 长 Prompt 下 `reasoning_content` 抢占 `content` 的间歇故障是否复现。
- 并发、超时、连接失败的形态（`max_retries=0` 下的重试语义）。
- 与 `json_mode` 同时开启的行为。

## 下一步

本结果**不自动修改规格**。按 spike 纪律：结果先回填《P1.6任务清单》T33.1，规格的三处修订（①②③）另行送前审。
