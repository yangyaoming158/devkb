# ADR-0004：LLM 供应商与接入方式

- 状态：已接受（2026-07-12，用户确认供应商；smoke 数据同日实测）
- 依据：《范围冻结与架构决策》D4 + `benchmarks/results/deepseek_smoke.json`

## 决策

LLM 供应商 = **DeepSeek**，经 OpenAI 兼容端点（`https://api.deepseek.com`）接入；代码侧通过薄 `LLMClient` Protocol + openai SDK 适配器调用，配套 FakeLLM 供测试。模型名固定为显式的 **`deepseek-v4-flash`**（2026-07-13 修订：官方公告 `deepseek-chat`/`deepseek-reasoner` 别名将于北京时间 2026-07-24 23:59 弃用，分别对应 v4-flash 的非思考/思考模式；smoke 实测该别名当时已派发 v4-flash）。

## Smoke 实测（2026-07-12，各 5 次，temperature=0）

| 场景 | P50 | 最大 | 备注 |
|---|---|---|---|
| 规划型 + JSON mode | 1080 ms | 1614 ms | **JSON 解析成功 5/5**（含 subqueries 字段校验） |
| 生成型（带证据引用） | 1150 ms | 1707 ms | — |

结论：延迟与结构化输出稳定性满足 P0/P1 预算（复杂查询 ≤6 次 LLM 调用 → LLM 部分 ~4–7s）。

## 约束与备注

- **单价（2026-07-13 官网核实，来源：https://api-docs.deepseek.com/zh-cn/quick_start/pricing ）**：deepseek-v4-flash 三档计价——输入缓存命中 0.02 元/百万 tokens、输入缓存未命中 1 元/百万 tokens、输出 2 元/百万 tokens。**计价必须按三档分别计算**（API usage 返回 prompt_cache_hit_tokens/prompt_cache_miss_tokens），agent_runs.usage JSONB 存供应商原始 usage 供复算；P0 验收当日以官网复核价格是否变动。
- **本机网络注意**：系统 SOCKS 代理（10808）会让 httpx 报 socksio 缺失；调用 DeepSeek / hf-mirror / tuna 等国内端点时 unset 代理直连（更快且绕开该问题）。适配层不做代理特判，由部署环境变量控制。
- Key 只经 `.env`（已 gitignore）注入 `DEVKB_LLM_API_KEY`；日志与 trace 不落 key。
- 替补方案：任何 OpenAI 兼容供应商（Qwen/GLM/Moonshot）改 `DEVKB_LLM_BASE_URL/MODEL` 即切，无代码改动；Claude 档（演示画质）如需引入走 Anthropic 适配器，P2 再议。
