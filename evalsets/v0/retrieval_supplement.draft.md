# Evaluation v0 — U1.3 补充检索题草稿（q21–q25，待人工核验）

> 状态：**已核验（2026-07-15 用户确认全部 5 条，放弃盲写路线）**。q21–q22 已合入 `retrieval_dev.jsonl`，q23–q25 已合入 `retrieval_holdout.jsonl`；本文件退役为核验底稿存档。
> 由 Claude（非系统答题模型 DeepSeek）基于 mini-mall-order 真实文档转写（2026-07-15），主题按清单候选：支付超时取消、通知服务职责、审计日志、demo seed 数据、压测阈值。
> 建议分配：q21–q22 → dev（补至 17）；q23–q25 → holdout（补至 8）。

| ID | 问题 | 预标锚点（rel_path ➜ 章节/标题） | 证据摘录（核验用） | 来源 | 状态 |
|---|---|---|---|---|---|
| q21 | 订单超时未支付是怎么被自动取消的？库存在什么时机释放？ | docs/dev-log.md ➜ "Task 17.1 - Order timeout cancellation service foundation"；"Task 17.2 - Order timeout scheduler wiring" | 定时任务查过期 PENDING_PAYMENT，条件更新仍 pending 的订单为 CANCELLED；条件更新成功后才调 InventoryClient.release；调度参数 minimall.order.timeout（enabled/fixed-delay/batch-size）环境驱动 | dev-log 转写 | 待核验 |
| q22 | notification-service 负责什么？浏览器能通过网关直接访问它吗？ | docs/architecture.md ➜ "Runtime Topology"；"Module Responsibilities" | 消费 payment-success 事件并写 notification_logs；:8106 无公开网关路由（consumes payment events without a public gateway route） | 文档转写 | 待核验 |
| q23 | 管理员审计日志查询接口挂在哪个服务？支持哪些筛选条件？ | docs/api-gateway-contract.md ➜ "4.3 Admin Core APIs" | GET /api/admin/audit-logs → user-service，ADMIN 权限；筛选：admin user、action、resource、request id、source、reference、created time range | 文档转写 | 待核验 |
| q24 | 演示用的 demo seed 数据在什么条件下才会写入？重复启动服务会不会重复插入？ | docs/dev-quickstart.md ➜ "Phase 3 AI 演示数据" | 仅当 inventory-service 为 dev/test profile 且 MINIMALL_DEMO_DATA_ENABLED=true，拒绝 prod/production；确定性业务键（PH3-AI-LOW-TEA 等）；重启只刷新不重复 | 文档转写 | 待核验 |
| q25 | k6 网关压测脚本定义了哪些阈值断言？ | docs/dev-log.md ➜ "Task 19.3 - Verify pressure assets and complete Task 19"；"Task 19.1 - Create k6 gateway load script" | 阈值：p95 延迟、传输错误率、ApiResponse 契约失败率、期望结果失败率、negative_inventory_observed rate==0；自定义 summary 含 QPS/均值/P95/relogins | dev-log 转写 | 待核验 |

核验后合并动作（AI 执行）：q21–q22 追加进 `retrieval_dev.jsonl`（source=real，notes 记核验日期）；q23–q25 追加进 `retrieval_holdout.jsonl`；本文件退役存档；勾选 U1.3。
