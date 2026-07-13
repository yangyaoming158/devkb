# ADR-0004：LLM 供应商与接入方式

- 状态：已接受（2026-07-12，用户确认供应商；smoke 数据同日实测）
- 依据：《范围冻结与架构决策》D4 + `benchmarks/results/deepseek_smoke.json`

## 决策

LLM 供应商 = **DeepSeek**，经 OpenAI 兼容端点（`https://api.deepseek.com`）接入；代码侧通过薄 `LLMClient` Protocol + openai SDK 适配器调用，配套 FakeLLM 供测试。模型名配置 `deepseek-chat`（服务端当前实际派发 `deepseek-v4-flash`，以响应 `model` 字段为准记录在 run 数据中）。

## Smoke 实测（2026-07-12，各 5 次，temperature=0）

| 场景 | P50 | 最大 | 备注 |
|---|---|---|---|
| 规划型 + JSON mode | 1080 ms | 1614 ms | **JSON 解析成功 5/5**（含 subqueries 字段校验） |
| 生成型（带证据引用） | 1150 ms | 1707 ms | — |

结论：延迟与结构化输出稳定性满足 P0/P1 预算（复杂查询 ≤6 次 LLM 调用 → LLM 部分 ~4–7s）。

## 约束与备注

- **单价未核实**：定价以官网为准（标记待核查）；实际成本按 run 落库跟踪（agent_runs.tokens/cost），不引用记忆中的价格数字。
- **本机网络注意**：系统 SOCKS 代理（10808）会让 httpx 报 socksio 缺失；调用 DeepSeek / hf-mirror / tuna 等国内端点时 unset 代理直连（更快且绕开该问题）。适配层不做代理特判，由部署环境变量控制。
- Key 只经 `.env`（已 gitignore）注入 `DEVKB_LLM_API_KEY`；日志与 trace 不落 key。
- 替补方案：任何 OpenAI 兼容供应商（Qwen/GLM/Moonshot）改 `DEVKB_LLM_BASE_URL/MODEL` 即切，无代码改动；Claude 档（演示画质）如需引入走 Anthropic 适配器，P2 再议。
