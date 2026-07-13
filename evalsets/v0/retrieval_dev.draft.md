# Evaluation v0 — dev 检索集草稿（待人工核验）

> 状态：**已核验（2026-07-12 用户确认全部 26 条）**。分配：q01–q15 + u01–u04 → dev；q16–q20 + u05–u06 → holdout。
> 正式文件：`retrieval_dev.jsonl`、`retrieval_holdout.jsonl`、`unanswerable_dev.jsonl`、`unanswerable_holdout.jsonl`（本文件退役为核验底稿存档）。
> 由 Claude（非系统答题模型 DeepSeek，符合 Evaluation-v0 来源纪律）基于 mini-mall-order 真实文档与 dev-log 开发记录转写。
> 你的核验动作（每条 1 分钟）：① 这个问题像不像你/新人真的会问的；② 打开锚点位置确认答案确实在那里；③ 状态列改 `✅`/`✏️改写`/`❌删`。核验完成后由 AI 转为 `retrieval_dev.jsonl`，届时才可勾选任务清单 U1.1。
> 目标：核验后保留 ≥15 条可答 + 挑 4 条不可答进 dev；其余进 holdout 池。

## A. 可答检索问题（20 条草稿）

| ID | 问题 | 预标锚点（rel_path ➜ 章节/标题） | 证据摘录（核验用） | 来源 | 状态 |
|---|---|---|---|---|---|
| q01 | 创建订单接口是怎么防止用户重复提交的？ | docs/dev-log.md ➜ "Task 10.5 - Duplicate submit idempotency"；docs/api-gateway-contract.md ➜ "4.1 Customer And Public APIs" | userId+idempotencyKey 幂等；Redis SETNX+TTL 拦在途重复；DB 唯一键兜底 race；请求体必须带 idempotencyKey | dev-log 转写 | ✅ |
| q02 | 库存扣减的幂等键是什么？同一订单重复扣减会发生什么？ | docs/dev-log.md ➜ "Task 8.3 - Inventory deduct/release records and idempotency" | orderNo+changeType 作幂等键；重放返回已有记录的库存快照，不改库存不加记录 | dev-log 转写 | ✅ |
| q03 | 订单状态机允许哪些状态流转？ | docs/dev-log.md ➜ "Task 9.2 - OrderStateMachine status transitions" | PENDING_PAYMENT→PAID/CANCELLED；PAID→CLOSED/REFUNDED；非法流转抛 BusinessException | dev-log 转写 | ✅ |
| q04 | 网关会剥掉浏览器传来的哪些请求头？为什么要剥？ | docs/api-gateway-contract.md ➜ "2. Authentication And Trust Boundary" | X-User-Id/X-Username/X-User-Role/X-Internal-Token 等始终移除；网关按 JWT 注入可信头 | 文档转写 | ✅ |
| q05 | 哪些接口是公开的、不需要带 JWT？ | docs/api-gateway-contract.md ➜ "2. Authentication And Trust Boundary" | register/login/admin login/商品读/库存读/CORS preflight | 文档转写 | ✅ |
| q06 | 支付成功事件用的 exchange、routing key 和两个消费队列分别是什么？ | docs/architecture.md ➜ "Event Flow"；docs/deployment.md ➜ "RabbitMQ Operations" | minimall.payment.exchange / payment.success / order 与 notification 两个队列 | 文档转写 | ✅ |
| q07 | 消费支付事件为什么必须按 eventId 去重？ | docs/architecture.md ➜ "Event Flow" | RabbitMQ 投递语义是 at-least-once | 文档转写 | ✅ |
| q08 | 对一个已支付成功的订单再次调支付接口，会返回什么？ | docs/api-gateway-contract.md ➜ "3. Response And Pagination Rules"（40903 行）与 "4.1"（pay 行） | 重复成功支付回放 200 + 原 paymentNo；若订单仍 PENDING_PAYMENT 会重发 payment.success 用于恢复 | 文档转写 | ✅ |
| q09 | 服务启动的依赖顺序是什么？order-service 要等哪些依赖健康才能起？ | docs/deployment.md ➜ "Startup Order" | 基础设施→prometheus→grafana→五个服务→order-service→gateway；order 等 product/inventory/Redis/RabbitMQ/MySQL | 文档转写 | ✅ |
| q10 | 数据库初始 schema 的迁移文件在哪？怎么应用到本地库？ | docs/deployment.md ➜ "Database Operations"；docs/architecture.md ➜ "Data Model" | docs/sql/migrations/V1__initial_schema.sql + compose exec mysql 命令 | 文档转写 | ✅ |
| q11 | 这个项目一共有哪些核心数据表？ | docs/architecture.md ➜ "Data Model" | users/products/inventory/inventory_records/orders/order_events/payments/notification_logs | 文档转写 | ✅ |
| q12 | 取消订单时库存释放会不会被重复执行？已取消的订单再取消一次会怎样？ | docs/dev-log.md ➜ "Task 11.2 - Order cancellation command" 与 "Task 11.4 - Cancellation chain regression tests" | 已 CANCELLED 视为幂等成功、不再释放；回归测试断言 release 只调一次 | dev-log 转写 | ✅ |
| q13 | 网关限流是什么算法？按什么维度限流？被限后返回什么？ | docs/api-gateway-contract.md ➜ "6. Browser Support, Rate Limiting, And Logging" | Redis token-bucket；认证请求按用户 ID、公开请求按客户端 IP；429 + TOO_MANY_REQUESTS | 文档转写 | ✅ |
| q14 | Prometheus 是怎么抓到各个 Java 服务的？在 WSL 环境下用 host.docker.internal 有什么坑？ | docs/observability.md ➜ "Docker Desktop And WSL" 与 "Service Metrics" | 按 Compose 服务名抓容器；host.docker.internal 可能解析到 Windows 宿主导致 target down；WSL IP 会变不要提交 | 真实踩坑记录转写 | ✅ |
| q15 | 正式增加库存的唯一入口是哪个接口？管理员手动调整接口谁可以调？ | docs/api-gateway-contract.md ➜ "4.3 Admin Core APIs" | inbound-orders/{inboundNo}/confirm 是唯一正式入库增加库存 API；manual adjust 永不被 AI 调用 | 文档转写 | ✅ |
| q16 | AI 库存助手的权限边界是什么？它能直接改库存吗？ | docs/api-gateway-contract.md ➜ "4.4 Admin AI Inventory Assistant APIs" | 只能读结构化数据和创建待审建议；不得改 inventory/inventory_records、不得调 /internal/**、不得确认入库 | 文档转写 | ✅ |
| q17 | /internal/** 的接口能通过网关访问吗？都有哪些、给谁用？ | docs/api-gateway-contract.md ➜ "5. Non-Browser Contracts"；docs/architecture.md ➜ "Runtime Topology" | 网关不路由 /internal/**；仅服务间调用；例：internal products 查询、inventories deduct/release | 文档转写 | ✅ |
| q18 | What is the unified API response envelope, and how does pagination work? | docs/api-gateway-contract.md ➜ "3. Response And Pagination Rules" | ApiResponse{success,code,message,data}；PageResponse；page 0 起，size/sort | 文档转写（英文题） | ✅ |
| q19 | 本地想一键起一套带演示数据的完整环境，推荐的路径是什么？ | docs/dev-quickstart.md ➜ "1. 启动后端 / 推荐：可复现演示路径" | scripts/bootstrap-demo.sh --env-file .env.demo；含 admin seed/migration/smoke check | 文档转写 | ✅ |
| q20 | 错误码 40901 和 40902 分别代表什么业务含义？ | docs/api-gateway-contract.md ➜ "3. Response And Pagination Rules"（错误码表） | 40901=订单已取消；40902=订单状态不允许支付 | 文档转写（精确 token 困难题） | ✅ |

## B. 不可答问题草稿（6 条，核验点=确认语料确实无答案）

| ID | 问题 | 为什么应拒答 | 风险提示（需你确认） | 状态 |
|---|---|---|---|---|
| u01 | 生产环境 MySQL 连接池的最大连接数配置是多少？ | docs 只有环境变量名与本地部署说明，无生产值 | 确认 docs/ 下没有任何生产配置值 | ✅ |
| u02 | 项目上线后的线上事故复盘记录在哪里？ | 本地 MVP 项目，无生产运行历史 | — | ✅ |
| u03 | 大促/秒杀场景的容量规划文档在哪里？ | 语料中不存在容量规划内容 | pressure/ 有压测报告，注意区分"压测结果"≠"容量规划"，可能被误检回 | ✅ |
| u04 | 用户退款的接口和处理流程是怎样的？ | 状态机存在 REFUNDED 状态，但网关契约中无退款 API | 陷阱题：存在部分证据。期望行为=指出"有 REFUNDED 状态但无退款接口文档"。若认为对 P0/P1 太难可移 holdout 或删 | ✅ |
| u05 | 各服务的可用性 SLA 目标是多少？ | 语料无 SLA 内容 | — | ✅ |
| u06 | JWT 用的什么签名算法？密钥轮换策略是什么？ | 已读文档只提 JWT secret 环境变量，未见算法与轮换策略 | 需确认 docs/history/ 等未读文档里确实没有 | ✅ |

## C. 核验后的分配建议

- A 表保留的条目：前 17 条进 dev（`retrieval_dev.jsonl`），其余进 holdout 池；
- B 表保留的条目：4 条进 dev（`unanswerable_dev.jsonl`），2 条进 holdout；
- holdout 还差的名额（检索 8 条），建议由你**亲自写**（不看本草稿的盲写最好——holdout 越独立越可信），主题可选：支付超时取消、通知服务职责、审计日志、demo seed 数据、压测阈值。
