# Evaluation v1 盲标注审计 worksheet（已确认）

> 状态：**source-only 审计与用户最终人工确认均已完成；U2.1 通过**
> 审计日期：2026-07-16
> 用户确认：2026-07-16，全部同意
> mini-mall 源 HEAD：`cfb49f5090befe62f024f1781144fe77e5b905c7`
> 源工作区：仅 `.taskmaster/state.json` 有改动；该隐藏目录不属于 P1 445 文件语料视图
> 审计约束：未运行 `devkb ask`、`devkb eval run` 或 holdout 脚本；未读取 P1 输出；只检查 Evaluation v0 JSONL 与 P1 允许的 Markdown/Java/config 原文

## 1. 结论摘要

- 原可答题：25/25 的 `answerable=true` 判断成立。
- 路径与 anchor：35/35 均存在，所在段落能直接支持问题；不建议修改 retrieval JSONL。
- 原边界题：不建议移动文件或改变 `answerable=false`，继续保留 17+4、8+2 分组。
- expected mode 已冻结：4 个 `refusal`、2 个 `partial`。
- 最重要的新发现：P1 加入 Java 后，`u06` 已能直接证明 JWT 算法为 `HMAC256`，但没有轮换机制，因此应为 `partial`，不能再按纯拒答处理。
- 本轮没有依据系统输出修标，因此没有 holdout 泄漏。

## 2. 25 个可答题批量核验

| ID | Split | 建议 mode | 标注结论 | 源语料核验证据 |
|---|---|---|---|---|
| q01 | dev | full | 通过 | `docs/dev-log.md:L378-L382` 说明 userId+idempotencyKey、Redis SETNX/TTL、DB 唯一键；`docs/api-gateway-contract.md:L130-L141` 要求 create-order idempotencyKey |
| q02 | dev | full | 通过 | `docs/dev-log.md:L276-L280` 明确 orderNo+changeType，重复调用返回已有记录快照且不改库存/不增记录 |
| q03 | dev | full | 通过 | `docs/dev-log.md:L306-L310` 列出 PENDING_PAYMENT→PAID/CANCELLED、PAID→CLOSED/REFUNDED |
| q04 | dev | full | 通过 | `docs/api-gateway-contract.md:L57-L84` 列出被剥离请求头并说明由网关注入可信身份/审计头，防浏览器伪造 |
| q05 | dev | full | 通过 | `docs/api-gateway-contract.md:L57-L72` 完整列出 register/login/admin login、商品/库存读、OPTIONS 等公开接口 |
| q06 | dev | full | 通过 | `docs/architecture.md:L90-L106`、`docs/deployment.md:L143-L172` 给出 exchange、routing key 与两个队列 |
| q07 | dev | full | 通过 | `docs/architecture.md:L90-L109` 明确 RabbitMQ at-least-once，因此消费者按 eventId 去重 |
| q08 | dev | full | 通过 | `docs/api-gateway-contract.md:L103-L114` 与 `L130-L144` 明确重复成功支付返回 200 和原 paymentNo |
| q09 | dev | full | 通过 | `docs/deployment.md:L46-L88` 给出完整启动顺序及 order-service 的 product/inventory/Redis/RabbitMQ/MySQL 依赖 |
| q10 | dev | full | 通过 | `docs/deployment.md:L119-L135`、`docs/architecture.md:L67-L75` 给出迁移路径和 MySQL 执行方式 |
| q11 | dev | full | 通过 | `docs/architecture.md:L67-L87` 列出 8 张核心表 |
| q12 | dev | full | 通过 | `docs/dev-log.md:L408-L414`、`L428-L434` 证明重复取消幂等成功且 release 仅调用一次 |
| q13 | dev | full | 通过 | `docs/api-gateway-contract.md:L248-L260` 给出 Redis token-bucket、user ID/IP 维度、HTTP 429/TOO_MANY_REQUESTS |
| q14 | dev | full | 通过 | `docs/observability.md:L68-L115` 给出 Compose service-name 抓取和 host.docker.internal 在 WSL 下的解析陷阱 |
| q15 | dev | full | 通过 | `docs/api-gateway-contract.md:L161-L189` 明确 inbound confirm 是正式入库入口，manual adjust 仅 ADMIN 且 AI 不调用 |
| q21 | dev | full | 通过 | `docs/dev-log.md:L648-L664` 说明定时扫描过期 PENDING_PAYMENT，CAS 成功后才释放库存及调度参数 |
| q22 | dev | full | 通过 | `docs/architecture.md:L7-L50` 说明 notification-service 消费 payment-success 写通知日志，且没有公开网关路由 |
| q16 | holdout | full | 通过 | `docs/api-gateway-contract.md:L201-L225` 明确 AI 只读/生成待审建议，不得改库存、调 internal 或 manual adjust |
| q17 | holdout | full | 通过 | `docs/api-gateway-contract.md:L233-L246`、`docs/architecture.md:L7-L31` 明确 internal 仅服务间使用并列出三个接口 |
| q18 | holdout | full | 通过 | `docs/api-gateway-contract.md:L87-L111` 定义 ApiResponse 与 0-based Pageable |
| q19 | holdout | full | 通过 | `docs/dev-quickstart.md:L12-L25` 给出 `scripts/bootstrap-demo.sh --env-file .env.demo` 推荐路径 |
| q20 | holdout | full | 通过 | `docs/api-gateway-contract.md:L103-L113` 明确 40901=订单已取消、40902=状态不允许支付 |
| q23 | holdout | full | 通过 | `docs/api-gateway-contract.md:L161-L168` 明确接口属于 user-service 及全部筛选条件 |
| q24 | holdout | full | 通过 | `docs/dev-quickstart.md:L204-L225` 明确 dev/test + 开关条件、拒绝 prod、确定性业务键和重复刷新不重复 |
| q25 | holdout | full | 通过 | `docs/dev-log.md:L718-L724`、`L738-L744` 列出 p95、传输/API/预期结果失败率和负库存阈值 |

说明：这里只证明现有 relevant anchor 合法，不把 Java 中出现的更多同义证据追加进标注。Evaluation 的 relevant 集不要求穷举全部支持文档，盲审也不应为了未来检索结果扩大锚点。

## 3. 6 个不可答/边界题逐题建议

### u01 · 生产环境 MySQL 连接池最大连接数

- 建议：`refusal`。
- source-only 搜索：`maximum-pool-size`、`maximumPoolSize`、`Hikari`、`connection pool`、`连接池`、`max connections`。
- 结果：允许语料中只有 `docs/dev-log.md:L704` 提到 Hikari metrics，没有生产连接池最大值；14 个配置文件也未定义该参数。
- 正确回答边界：说明语料没有生产值，不得用 Hikari 默认值、开发值或推测值代替。

### u02 · 线上事故复盘记录

- 建议：`refusal`。
- source-only 搜索：`事故`、`复盘`、`postmortem`、`incident review`、`production incident`、`outage`、`root cause analysis`。
- 结果：445 文件候选范围内没有匹配的线上事故复盘；审计/修复计划不是生产事故记录。
- 正确回答边界：说明项目没有上线事故材料，不能把代码审计文档包装成事故复盘。

### u03 · 大促/秒杀容量规划文档

- 建议：`refusal`，允许在拒答后提示“存在压测材料，但它不等于容量规划”。
- source-only 搜索：`容量规划`、`大促`、`秒杀`、`capacity planning`、`QPS`、`throughput`、`pressure/load test`、`压测`、`峰值`。
- 结果：`docs/dev-log.md:L718-L770`、`docs/architecture.md:L131-L146`、`docs/deployment.md:L193-L214` 证明有 k6 脚本、阈值和实跑数据，但没有流量预测、容量模型、扩容目标或大促方案。
- 正确回答边界：不得用约 40 QPS 的一次压测结果冒充容量规划。

### u04 · 用户退款接口和处理流程

- 建议：`partial`。
- 已支持部分：`order-service/src/main/java/com/minimall/order/domain/OrderStateMachine.java:L16-L21` 存在 PAID→REFUNDED 状态转换。
- 缺失核心：`docs/phase1-frontend-acceptance.md:L141-L153` 明确无 refund flow；`docs/phase2-admin-acceptance.md:L36-L38` 明确支付后台无 refund/reconciliation。
- 正确回答边界：可以说明“状态机预留 REFUNDED”，但必须同时说明没有退款 API、退款服务流程、资金冲正或对账实现。

### u05 · 各服务可用性 SLA 目标

- 建议：`refusal`。
- source-only 搜索：`SLA`、`service level`、`availability target`、`可用性目标/可用率`、`uptime target`、`error budget`、`99.9/99.99`。
- 结果：没有 SLA/可用性目标；`199.90` 等金额属于正则数字假阳性，health/Prometheus 说明也不等于 SLA。
- 正确回答边界：可以指出存在健康检查和监控，不得从它们推导可用率目标。

### u06 · JWT 签名算法和密钥轮换

- 建议：`partial`。
- 已支持部分：`common-auth/src/main/java/com/minimall/common/auth/jwt/JwtUtils.java:L35-L65` 明确 `Algorithm.HMAC256(secret)`，即 HS256/HMAC-SHA256，并用于签名与验证。
- 缺失核心：`JwtProperties.java:L5-L25` 只有单个 secret 与过期时间；`api-gateway/src/main/resources/application.yml:L37-L43` 同样只有单个 secret，没有 kid、旧/新 key 集、轮换周期或双 key 验证窗口。
- 正确回答边界：回答算法为 HMAC256；明确“语料未实现/未记录轮换策略”，不得推测云 KMS、定时轮换或多 key 兼容。

## 4. 已冻结的 expected mode

| ID | Split | expected_mode | 是否修改 v0 answerable/relevant |
|---|---|---|---|
| u01 | dev | refusal | 否 |
| u02 | dev | refusal | 否 |
| u03 | dev | refusal | 否 |
| u04 | dev | partial | 否 |
| u05 | holdout | refusal | 否 |
| u06 | holdout | partial | 否 |

## 5. 用户最终确认

请用户只确认以下三项：

- [x] A. 同意 25 个原可答题和 35 个路径/anchor 整体通过，不修改 retrieval JSONL。
- [x] B. 同意 mode 冻结为：u01/u02/u03/u05=`refusal`，u04/u06=`partial`。
- [x] C. 确认本次结论只依据源 commit `cfb49f5` 的允许语料，没有参考 P1 输出。

三项已由用户全部确认。`answer-expectations.jsonl` 已转正；v0 JSONL 无需修改，记录见 `label-change-log.md`。
