# P1 真语料最终实跑与手工演示（T21.2）

> 日期：2026-07-21　｜　语料：mini-mall（已审计视图）　｜　模型：embedding=`Qwen/Qwen3-Embedding-0.6B` · llm=`deepseek-v4-flash` · prompt=`p1-agent-v3`
> 网络开关：全程 `unset` 代理 + `HF_HUB_OFFLINE=1`（CLAUDE.md 环境事实）

## 1. 已审计语料视图 · 摄取与幂等

- 视图由 `tools/build_corpus_view.py` 从只读源仓库 `/home/oslab/projects/mini-mall-order` 构建：**445 文件 = 39 markdown + 392 java + 14 config**，自检通过（无指令文件/隐藏路径/依赖生成物混入）。
- **与 dev §5.2 Gate 报告语料逐文件核对：445/445 rel_path 一致、0 处 sha256 不符 → 字节级同一语料**（源仓库自 dev Gate 以来未变）。
- 落库规模：**mini-mall = 445 文档 / 4788 chunks**（与 dev 报告 corpus 完全一致）。
- **幂等**：对同一视图二次 `devkb ingest` → 成功 0 / 跳过（未变更）445 / 失败 0；落库计数不变（445 docs / 4788 chunks）。content_hash 命中即跳过，不重嵌。

## 2. 五条路径各有可回放 run（dev §5.2 真实全量跑，U2.2 已复核）

| 路径 | 题 | run_id | mode | 回放 |
|---|---|---|---|---|
| full（首轮充分） | q09 服务启动依赖顺序 | `f01900dc-9f71-4abe-90fb-a1784da19570` | full | ✅ `devkb runs replay` 可回放 |
| refine 后 full | q10 迁移文件在哪/怎么应用 | `f56db5bd-01ae-44ec-a904-fc2da19515e1` | full | ✅ |
| partial | q07 为什么按 eventId 去重 | `f15a6d61-6dba-48e7-9b35-01bf25026571` | partial | ✅ |
| refusal（正确拒答） | u01 生产 MySQL 连接池最大连接数 | `779772dc-f88f-4bef-97bc-66ad3bf60969` | refusal | ✅ |
| L0/L1 失败→重生成后通过 | q12 库存释放是否重复执行 | `4a82736e-b1bd-422b-8016-860e89a38dcb` | full | ✅ |

> 五条 run 均 `status=succeeded`、节点轨迹完整、可只读回放（`devkb runs replay <id> --project mini-mall`）。人工引用复核见 `evalsets/reports/p1-manual-check.md`（randy 2026-07-21，引用正确率 14/14=100%）。

## 3. 手工演示：真实 DeepSeek 新提问（T21.2 现场实跑）

三条全新问题，实时调 DeepSeek + 本地 Qwen，验证在线链路端到端可用与三态/跨类型引用：

| # | 问题 | run_id | mode | 亮点 |
|---|---|---|---|---|
| D1 | 订单状态机允许哪些状态流转？ | `8a4445ca-8f46-4e97-9a04-a39f6131f7fe` | partial | 跨类型引用：`docs/dev-log.md` + Java 测试 `OrderStateMachineTest`；答 PENDING_PAYMENT→PAID/CANCELLED、PAID→CLOSED/REFUNDED，其余流转入 not_found |
| D2 | 项目部署在哪个云厂商的 K8s 集群？ | `8afc9847-3804-4b26-a4de-28726700540c` | refusal | 语料无此信息：先 refine 补检一轮仍无证据，确定性拒答并列出缺什么，未编造 |
| D3 | api-gateway 的限流怎么实现？什么算法？ | `647a6ad1-b46c-4852-8d94-b1942a9ac6d4` | partial | Java 方法级检索：`GatewayRateLimitFilter` / `RedisGatewayRateLimiter` / `GatewayRateLimitProperties` + dev-log；答 Redis 令牌桶、X-User-Id 键、HTTP 429 |

> 三条 run 已落库，可 `devkb runs replay` 回放完整节点轨迹与引用。演示覆盖 partial×2 / refusal×1、refine 路径、Markdown+Java 跨类型引用与 Java 方法级 chunk 检索。
> 说明：本次三条现场演示未出现 full（两条 partial 属已记录的「not_found 占位偏保守」倾向，见 `docs/backlog.md`）；full 路径的证据由第 2 节 q09/q10/q12 三条 dev run 覆盖。

## 4. 结论

T21.2 判据逐项满足：① 已审计语料视图完成 Markdown+Java+config 摄取，二次运行零新增（幂等）；② full/refine/partial/refusal/L1 五路径各有可回放 run；③ U2.2 人工复核通过。全程未接触 holdout。
