# P0 人工抽查报告（T10.3）

> 执行：2026-07-15，Claude 实跑并逐引用核对原文；用户于 2026-07-15 完成人工复核并通过 T10.3。
> 方式：从 dev 集选 5 问（q01/q06/q09/q12/q13，覆盖 8 个不同文档），对 `mini-mall` 项目（39 文档/1370 chunks，T10.2 摄取）真实运行 `devkb ask`，随后打开每处引用的 `rel_path:Lstart-Lend` 原文行区间，核对内容是否支持回答中标注该 E# 的论断。
> 环境：deepseek-v4-flash + Qwen3-Embedding-0.6B(cuda)；run 明细可用 `devkb runs list --project mini-mall` 取回。

## 总结

| 问题 | 引用数 | 路径行号真实 | 内容对应 | warnings |
|---|---|---|---|---|
| q01 创建订单防重复提交 | 4 | 4/4 | 4/4 | 0 |
| q06 支付事件 exchange/routing key/队列 | 4 | 4/4 | 4/4 | 0 |
| q09 服务启动依赖顺序 | 1 | 1/1 | 1/1 | 0 |
| q12 取消订单库存释放幂等 | 4 | 4/4 | 4/4 | 0 |
| q13 网关限流算法/维度/响应 | 4 | 4/4 | 4/4 | 0 |

**17/17 引用路径行号真实且内容对应；0 条 L0 warning；无编造引用发现。**

## 逐问记录

### q01 创建订单接口怎么防止用户重复提交？
回答要点：前端 `crypto.randomUUID()` 一次性幂等键（重试不重生成）+ 后端 userId+idempotencyKey 幂等、Redis SETNX+TTL 锁、DB 唯一键兜底。
- E1 `docs/dev-log.md:L378-L382`（Task 10.5）✓ 原文含 userId+idempotencyKey、Redis SETNX with TTL
- E2 `docs/architecture-ai-review-2026-06-10.md:L144-L157`（不变量核对表）✓ 原文行"重复下单幂等 | Redis setIfAbsent 锁 + uk_orders_idempotency_key + DataIntegrityViolation 重放"
- E4 `docs/phase1-frontend-acceptance.md:L126-L139`（Idempotency contract）✓ 原文含 one crypto.randomUUID() in onMounted、not regenerated on failure
- E6 `docs/dev-log.md:L1306-L1308`（B3 Checkout rewrite）✓ 该区间讲 submitError 展示；回答引它支持"幂等键生命周期在重写中保持"——对应关系成立但较弱（该叙述在相邻行 L1300 附近更完整），属检索边界切分问题而非编造

### q06 支付成功事件的 exchange、routing key 和两个消费队列？
回答：`minimall.payment.exchange` / `payment.success` / `minimall.order.payment-success.queue` + `minimall.notification.payment-success.queue`。
- E1 `docs/architecture.md:L90-L109`（Event Flow）✓；E2 `docs/deployment.md:L143-L162`（RabbitMQ Operations）✓；E4 `docs/messaging/payment-success-event.md:L13-L29`（RabbitMQ Topology）✓；E5 `docs/history/project-acceptance-report.md:L143-L169`（Data And Event Contracts）✓——四处原文均逐字含全部四个名字

### q09 服务启动依赖顺序？order-service 等哪些依赖？
回答：基础设施→prometheus→grafana→五服务→order-service→api-gateway；order-service 等 product/inventory/Redis/RabbitMQ/MySQL 健康。
- E1 `docs/deployment.md:L46-L88`（Startup Order）✓ 原文行"order-service waits for product, inventory, Redis, RabbitMQ, and MySQL health"

### q12 取消订单库存释放会否重复执行？重复取消会怎样？
回答：释放有条件 UPDATE（locked >= :qty）+ 唯一流水键 (order_no, RELEASE) 幂等；回归测试断言 release 只调一次；已取消再取消 = 幂等成功（ORDER_CANCELLED 语义）。
- E1 `docs/audit/fix-plans/phase-b-plan.md:L89-L92` ✓ 原文"订单 CANCELLED 且无支付记录 → 仍返回 ORDER_CANCELLED（语义不变）"
- E2 `docs/dev-log.md:L428-L437`（Task 11.4 回归测试）✓ cancelling twice returns idempotent success
- E3 `docs/architecture-ai-review-2026-06-10.md:L144-L157` ✓ 原文行"释放不超锁定量 | releaseLockedStock 条件 locked >= :qty"与"(order_no, change_type) 唯一键"（change_type 涵盖 RELEASE）
- E5 `docs/audit/fix-plans/phase-a-plan.md:L51-L73`（任务 A1）✓ 原文"保留'已 CANCELLED → 幂等返回'的现有语义"

### q13 网关限流算法/维度/被限响应？
回答：Redis-backed token-bucket；认证请求按可信 X-User-Id、公开请求回退客户端 IP；429 + ApiResponse，业务码 42900 TOO_MANY_REQUESTS。
- E2 `docs/api-gateway-contract.md:L248-L261` ✓ 原文含 token-bucket、trusted user ID、client IP fallback、429+TOO_MANY_REQUESTS
- E3 `docs/dev-log.md:L628-L631`（Task 16.4）✓；E5 `docs/history/phase1-frontend-prereview.md:L59-L64` ✓ 原文含 TOO_MANY_REQUESTS=42900；E6 `docs/history/project-acceptance-report.md:L126-L131` ✓ 原文含 429 with ApiResponse

## 过程中发现（不影响本报告结论）

- `devkb ask --json` 的 stdout 混入 structlog 日志行（run_finished），管道消费需跳过非 JSON 前缀——已记 `docs/backlog.md`。
- q01-E6 的引用对应关系成立但证据强度弱（相关叙述被 chunk 边界切开），是 P0 纯向量+分块粒度的已知特性，符合 README"引用保证可溯源"的边界声明。
