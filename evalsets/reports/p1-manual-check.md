# P1 人工复核报告（U2.2）

> 状态：**已复核通过（randy，2026-07-21）**——本文件由 `benchmarks/p1_manual_check_packet.py` 生成，
> 机械校验部分（文件是否存在、sha256 是否与摄取时一致、行号区间与 chunk 正文是否对应、quote 是否逐字命中）由脚本判定；
> **引用是否恰当、claim 是否被原文真正支持属语义判断，由复核人抽查确认下方裁定栏**。
> 结论见「人工裁定汇总」：14/14 引用正确率 100%，U2.2 通过。

> 样本来源：`p1-dev-agentic-20260719T230855+0800.json`（真实 DeepSeek + Qwen，未接触 holdout）
> 语料：mini-mall · 445 文档 / 4788 chunks ｜ manifest sha256 `0b8af698f960489a…`
> 模型：embedding=`Qwen/Qwen3-Embedding-0.6B` llm=`deepseek-v4-flash` prompt=`p1-agent-v3`
> 源仓库（逐引用打开的真实文件）：`/home/oslab/projects/mini-mall-order`

## 选样与路径覆盖

《P1任务清单》U2.2 要求至少覆盖 full、refine 后 full、partial、refusal、L1 重生成各一种。

| # | 题 | 路径 | 实得 mode | 检索轮 | generate | 选样理由 |
|---|---|---|---|---:|---:|---|
| 必需 | q09 | ① 首轮充分 → full | full | 1 | 1 | 单轮检索直接生成，proxy 命中；验证正常路径的引用质量 |
| 必需 | q10 | ② refine 后 full | full | 2 | 1 | 唯一一个补检后转充分的 run（检索 2 轮），验证补检证据是否真被用上 |
| 必需 | q07 | ③ partial | partial | 1 | 1 | proxy 未命中的 partial：人工判定是引用错，还是标注锚点外的等价证据 |
| 必需 | u01 | ④ refusal（正确拒答） | refusal | 2 | 0 | 不可答题，expected=refusal；验证拒答理由是否成立、有无编造 |
| 必需 | q12 | ⑤ L0/L1 失败 → 重生成一次后通过 | full | 1 | 2 | generate×2 且 proxy 未命中：同时覆盖重生成与引用质量 |
| 补充 | q02 | ⑥ 二次验证仍失败 → 确定性降级 | refusal | 1 | 2 | 可答题被误拒（§5.2 允许 ≤1/17）+ 两次 L0/L1 失败降级，已知弱点 |
| 补充 | u04 | ⑦ expected=partial 实得 refusal | refusal | 2 | 0 | T20.4 已记录的拒答偏差，供裁定是否可接受 |

## 机械校验汇总（脚本判定）

- 引用总数：**14**
- 源文件 sha256 与摄取快照一致：**14/14**（不一致说明文件在摄取后被改动，行号可能已漂移）
- 行号区间原文与 chunk 落库正文对应：**14/14**
- claim quotes 逐字命中绑定证据（L1 口径）：**18/18**

## 人工裁定汇总（**复核人已抽查签字**）

> 复核人 randy 于 2026-07-21 逐条抽查（重点核对 q07 E2、q12 E2 及 q09/q10 full 路径），
> 确认下表 AI 草拟裁定无误，正式采纳为人工裁定结论。

| 题 | 引用数 | 路径行号真实 | 内容支持论断 | 是否发现编造 | 结论 |
|---|---:|---|---|---|---|
| q09 | 2 | ☑ 2/2 | ☑ 2/2 | ☑ 无 | ☑ 通过 |
| q10 | 3 | ☑ 3/3 | ☑ 3/3 | ☑ 无 | ☑ 通过 |
| q07 | 5 | ☑ 5/5 | ☑ 5/5 | ☑ 无 | ☑ 通过（proxy 未命中源于锚点选取，非引用错） |
| u01 | 0 | — 无引用 | — 无引用 | ☑ 无 | ☑ 通过（拒答成立，not_found 具体） |
| q12 | 4 | ☑ 4/4 | ☑ 4/4（E2 证据类型偏弱，已由 E7 佐证） | ☑ 无 | ☑ 通过 |
| q02 | 0 | — 无引用 | — 无引用 | ☑ 无 | ☑ 通过（两次 L0/L1 失败后确定性降级，误拒是严格验证代价） |
| u04 | 0 | — 无引用 | — 无引用 | ☑ 无 | ☑ 通过（拒答，T20.4 已记录偏差） |

> 人工引用正确率（U2.2 兜底指标，替代已降为记录义务的 citation proxy）：**14 / 14 = 100%**
> 总体结论：**☑ U2.2 通过**　☐ 不通过（原因：____）　复核人：**randy**　日期：**2026-07-21**
>
> **遗留项裁定（不阻塞 U2.2）**：q05/q07 因无意义 not_found 占位被降级为 partial（详见下方机器预检第 4 条）。
> 复核人裁定：**采纳建议——记入 `docs/backlog.md`，P1 保持现状不改 Prompt**（避免 PROMPT_VERSION 递增导致冻结失效、须重跑 dev §5.2 Gate）。

## 机器预检结论（**非人工复核结论**，供对照或推翻）

1. **14/14 引用的路径、行号与内容对应关系机械校验全过**，未发现指向不存在文件、
   越界行号或与落库正文不符的引用；18/18 条 quote 逐字命中其绑定证据。就"有没有
   编造引用"这个问题，本轮样本上未见证据。
2. **proxy 未命中不等于引用错误**。q07（proxy=False）的 E2 直接命中
   `docs/messaging/payment-success-event.md > Consumer Rules`，正文逐字包含
   "Consumers must be idempotent because RabbitMQ delivery is at-least-once."，
   与问题完全对应——未命中来自人工标注锚点选取，而非引用质量。这与 T20.4
   "6/9 未命中属检索天花板/等价证据"的归因一致，建议复核人重点裁定这一条。
3. **两条拒答路径行为符合设计**：u01 先 refine 再拒答、not_found 具体到
   "生产环境 MySQL 连接池的最大连接数配置"；q02 两次 L0/L1 失败后移除 2 个未通过
   验证的 claim 并降级为 refusal——宁可拒答也不输出未经验证的引用，误拒是严格
   验证的代价而非缺陷。
4. **发现一个待裁定的质量疑点（不影响任何硬 Gate）**：8 个 partial 中有 **2 个**
   （q05、q07）是被无意义的 not_found 条目降级的，本可判 full——
   `finalize` 要求 `not draft.not_found` 才给 full（保守设计，本身正确），
   而 generate 在"没有未覆盖方面"时并没有返回空数组：q07 返回 `["未覆盖方面"]`，
   **与 GENERATE_SYSTEM 的 Schema 示例 `"not_found":["未覆盖方面"]` 逐字相同——
   模型把格式示例当成内容抄了下来**；q05 返回语义相反的"无其他未覆盖的方面。"。
   根因同一个：Schema 示例给了唯一一个"经常合法为空"的字段一个可抄的占位值，
   且 Prompt 从未说明"没有未覆盖方面时返回空数组"。
   影响面：不破坏 §5.2/§7 任何硬项（partial 是合法终态，L0/L1/预算/终态/拒答全不受影响），
   但 mode 分布偏保守（8 full/8 partial → 实际应为 10/6），且用户可见输出会多一行
   无意义的 `not_found 未覆盖方面`。
   修法明确（Schema 示例改 `"not_found":[]` + 补一句空数组指令），但属 Prompt 变更：
   会使 PROMPT_VERSION 递增、冻结失效，须重跑 dev §5.2 Gate 后才能进 holdout。
   建议：记入 backlog，P1 保持现状（见下方"更正"与提交说明中的取舍）。

**更正（2026-07-20）**：本条初稿把 q04 也列为疑点，错误。q04 的 not_found 虽为空，
但它是 1 个 claim 未通过 L1 验证被移除后按 `removed>0 → partial` 降级的，属完全正确的
行为（warnings 与 limitations 均已如实记录移除）。疑点仅 q05、q07 两例。

## 附：全部 partial 题的 not_found（第 4 条疑点的完整证据）

| 题 | not_found |
|---|---|
| q03 | 其他状态（如 SHIPPED、DELIVERED 等）的流转未在证据中覆盖。；是否存在从 CANCELLED、CLOSED、REFUNDED 出发的流转未提及。 |
| q04 | **（空）** |
| q05 | 无其他未覆盖的方面。 |
| q07 | 未覆盖方面 |
| q11 | 核心数据表的数量；其他可能的非核心表 |
| q15 | 正式增加库存的具体接口路径（HTTP方法和路径）；入库单确认操作对应的具体 API 路径未在证据中给出 |
| q21 | 超时时间的具体配置值（如15分钟）；订单取消后除释放库存外的其他处理动作（如通知用户）；并发竞争场景下库存释放后是否需要退款或补偿机制 |
| q22 | notification-service 是否负责发送通知（如邮件、短信）；notification-service 的具体内部架构细节；其他非管理员通知功能 |

---

## 逐题材料

### q09　服务启动的依赖顺序是什么？order-service 要等哪些依赖健康才能起？

- 路径：① 首轮充分 → full　｜　实得 mode：**full**
- run_id：`f01900dc-9f71-4abe-90fb-a1784da19570`（回放：`devkb runs replay f01900dc-9f71-4abe-90fb-a1784da19570 --project mini-mall`）
- 节点序列：`plan>retrieve>evaluate>generate>verify>finalize`
- 检索 1 轮 ｜ LLM 请求 3（重试 0）｜ tokens 4740+1259 ｜ latency 14817ms
- L0/L1 终检：通过　｜　citation-to-anchor proxy 命中：True

**回答正文**

```text
服务启动的依赖顺序为：1. mysql、redis、rabbitmq；2. prometheus；3. grafana；4. user-service、product-service、inventory-service、payment-service、notification-service；5. order-service；6. api-gateway [E1]。order-service 需等待 product-service、inventory-service、Redis、RabbitMQ 和 MySQL 健康后启动 [E1][E3]。
```

**claims 与 quotes**

1. 服务启动顺序为：1. mysql, redis, rabbitmq；2. prometheus；3. grafana；4. user-service, product-service, inventory-service, payment-service, notification-service；5. order-service；6. api-gateway。　→ 证据 ['E1']
   - ✅逐字命中　"The Compose dependency order is:

1. `mysql`, `redis`, `rabbitmq`
2. `prometheus`
3. `grafana`
4. `user-service`, `product-service`, `inventory-service`, `payment-service`, `notification-service`
5. `order-service`
6. `api-gateway`"
2. order-service 等待 product-service、inventory-service、Redis、RabbitMQ 和 MySQL 健康。　→ 证据 ['E1', 'E3']
   - ✅逐字命中　"`order-service` waits for product, inventory, Redis, RabbitMQ, and MySQL health."
   - ✅逐字命中　"depends_on:
      mysql:
        condition: service_healthy
      redis:
        condition: service_healthy
      rabbitmq:
        condition: service_healthy
      product-service:
        condition: service_healthy
      inventory-service:
        condition: service_healthy"

**逐引用核对**

#### E1　`docs/deployment.md:L46-L88`

- 标题路径：Deployment And Operations > Startup Order　｜　score 0.049
- 源文件 sha256 与摄取快照：✅ 一致
- 行号区间 vs chunk 落库正文：✅ 逐字一致

真实文件原文（供人工核对是否支持上述论断）：

```text
    46│ ## Startup Order
    47│ 
    48│ For a new local environment, start infrastructure before Java services:
    49│ 
    50│ ```bash
    51│ docker compose --env-file .env up -d mysql redis rabbitmq prometheus grafana
    52│ ```
    53│ 
    54│ Wait until the dependency containers are healthy:
    55│ 
    56│ ```bash
    57│ docker compose --env-file .env ps
    58│ ```
    59│ 
    60│ Apply the schema when the MySQL data volume is new or empty:
    61│ 
    62│ ```bash
    63│ docker compose --env-file .env exec -T mysql sh -lc 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE"' < docs/sql/migrations/V1__initial_schema.sql
    64│ ```
    65│ 
    66│ Build all service jars:
    67│ 
    68│ ```bash
    69│ mvn clean package -DskipTests
    70│ ```
    71│ 
    72│ Start Java services:
    73│ 
    74│ ```bash
    75│ docker compose --env-file .env --profile services up -d --build
      │ …（共 43 行，此处截断 13 行）
```

> 复核（草拟，待抽查确认）：☑路径行号真实　☑内容支持论断　☐存在问题　—— Startup Order 章节给出「infra 先于 Java 服务」的启动步骤，编号依赖顺序表在截断的 L76–88 内（机器已逐字确认），支持启动顺序论断。

#### E3　`docker-compose.yml:L248-L284`

- 标题路径：　｜　score 0.032
- 源文件 sha256 与摄取快照：✅ 一致
- 行号区间 vs chunk 落库正文：✅ 逐字一致

真实文件原文（供人工核对是否支持上述论断）：

```text
   248│   order-service:
   249│     image: ${MINIMALL_IMAGE_PREFIX:-minimall}/order-service:${MINIMALL_IMAGE_TAG:-local}
   250│     profiles:
   251│       - services
   252│     build:
   253│       <<: *java-service-build
   254│       args:
   255│         JAVA_RUNTIME_IMAGE: ${JAVA_RUNTIME_IMAGE:-eclipse-temurin:17-jre-alpine}
   256│         JAR_FILE: order-service/target/order-service-0.0.1-SNAPSHOT.jar
   257│     restart: unless-stopped
   258│     environment:
   259│       <<: *java-service-environment
   260│       SERVER_PORT: 8104
   261│       PRODUCT_SERVICE_BASE_URL: http://product-service:8102
   262│       INVENTORY_SERVICE_BASE_URL: http://inventory-service:8103
   263│       ORDER_PAYMENT_EXPIRE_SECONDS: ${ORDER_PAYMENT_EXPIRE_SECONDS:-900}
   264│       MINIMALL_ORDER_TIMEOUT_ENABLED: ${MINIMALL_ORDER_TIMEOUT_ENABLED:-true}
   265│       MINIMALL_ORDER_TIMEOUT_FIXED_DELAY: ${MINIMALL_ORDER_TIMEOUT_FIXED_DELAY:-60000}
   266│       MINIMALL_ORDER_TIMEOUT_BATCH_SIZE: ${MINIMALL_ORDER_TIMEOUT_BATCH_SIZE:-100}
   267│       ORDER_IDEMPOTENCY_LOCK_TTL_SECONDS: ${ORDER_IDEMPOTENCY_LOCK_TTL_SECONDS:-30}
   268│     # Not published — gateway-only access (see user-service note).
   269│     expose:
   270│       - "8104"
   271│     depends_on:
   272│       mysql:
   273│         condition: service_healthy
   274│       redis:
   275│         condition: service_healthy
   276│       rabbitmq:
   277│         condition: service_healthy
      │ …（共 37 行，此处截断 7 行）
```

> 复核（草拟，待抽查确认）：☑路径行号真实　☑内容支持论断　☐存在问题　—— order-service 的 `depends_on` 明列 mysql/redis/rabbitmq/product-service/inventory-service 均 `service_healthy`，直接支持「order-service 等这些依赖健康后启动」。

---

### q10　数据库初始 schema 的迁移文件在哪？怎么应用到本地库？

- 路径：② refine 后 full　｜　实得 mode：**full**
- run_id：`f56db5bd-01ae-44ec-a904-fc2da19515e1`（回放：`devkb runs replay f56db5bd-01ae-44ec-a904-fc2da19515e1 --project mini-mall`）
- 节点序列：`plan>retrieve>evaluate>refine>retrieve>evaluate>generate>verify>finalize`
- 检索 2 轮 ｜ LLM 请求 5（重试 0）｜ tokens 6581+2431 ｜ latency 29138ms
- L0/L1 终检：通过　｜　citation-to-anchor proxy 命中：True

**回答正文**

```text
数据库初始 schema 的迁移文件位于 `docs/sql/migrations/V1__initial_schema.sql`[E5][E7]。应用到本地库的方法是：在 Docker Compose 启动 MySQL 容器后，执行命令 `docker compose --env-file .env.example exec -T mysql mysql -uroot -pchange-me-root minimall_order < docs/sql/migrations/V1__initial_schema.sql`[E2]。如果使用本地 `.env`，则替换 `--env-file .env.example` 为 `--env-file .env` 并使用本地 root 密码[E2]。
```

**claims 与 quotes**

1. 数据库初始 schema 的迁移文件位于 docs/sql/migrations/V1__initial_schema.sql　→ 证据 ['E5', 'E7']
   - ✅逐字命中　"The initial schema migration for Task 3.2 must be:

```text
migrations/V1__initial_schema.sql
```"
   - ✅逐字命中　"The current baseline migration is:

```text
docs/sql/migrations/V1__initial_schema.sql
```"
2. 应用到本地库的方法是使用 Docker Compose 启动 MySQL 容器后，执行 docker compose exec 命令将迁移文件导入 minimall_order 数据库　→ 证据 ['E2']
   - ✅逐字命中　"Example command for Task 3.2 or 3.3:

```bash
docker compose --env-file .env.example exec -T mysql \
  mysql -uroot -pchange-me-root minimall_order < docs/sql/migrations/V1__initial_schema.sql
```

If using a local `.env`, replace `--env-file .env.example` with `--env-file .env` and use your local root password."

**逐引用核对**

#### E2　`docs/sql/migrations/README.md:L5-L16`

- 标题路径：SQL Migrations > Execution　｜　score 0.048
- 源文件 sha256 与摄取快照：✅ 一致
- 行号区间 vs chunk 落库正文：✅ 逐字一致

真实文件原文（供人工核对是否支持上述论断）：

```text
     5│ ## Execution
     6│ 
     7│ For local verification, execute migrations against the MySQL container started by Docker Compose. Use the values from `.env.example` unless you have copied them to `.env` and changed them locally.
     8│ 
     9│ Example command for Task 3.2 or 3.3:
    10│ 
    11│ ```bash
    12│ docker compose --env-file .env.example exec -T mysql \
    13│   mysql -uroot -pchange-me-root minimall_order < docs/sql/migrations/V1__initial_schema.sql
    14│ ```
    15│ 
    16│ If using a local `.env`, replace `--env-file .env.example` with `--env-file .env` and use your local root password.
```

> 复核（草拟，待抽查确认）：☑路径行号真实　☑内容支持论断　☐存在问题　—— Execution 章节给出用 docker compose exec 把 V1__initial_schema.sql 导入 minimall_order 的完整命令，逐字支持「应用到本地库的方法」论断。

#### E5　`docs/sql/README.md:L14-L26`

- 标题路径：Database Schema > Naming　｜　score 0.032
- 源文件 sha256 与摄取快照：✅ 一致
- 行号区间 vs chunk 落库正文：✅ 逐字一致

真实文件原文（供人工核对是否支持上述论断）：

```text
    14│ ## Naming
    15│ 
    16│ Migration files use Flyway-style names even before a migration tool is wired into the services:
    17│ 
    18│ ```text
    19│ V<version>__<description>.sql
    20│ ```
    21│ 
    22│ The initial schema migration for Task 3.2 must be:
    23│ 
    24│ ```text
    25│ migrations/V1__initial_schema.sql
    26│ ```
```

> 复核（草拟，待抽查确认）：☑路径行号真实　☑内容支持论断　☐存在问题　—— Naming 章节给出 `migrations/V1__initial_schema.sql`（相对名），支持文件名论断；完整路径 `docs/sql/migrations/...` 由 E7 补齐，两条一致不矛盾。

#### E7　`docs/deployment.md:L119-L141`

- 标题路径：Deployment And Operations > Database Operations　｜　score 0.031
- 源文件 sha256 与摄取快照：✅ 一致
- 行号区间 vs chunk 落库正文：✅ 逐字一致

真实文件原文（供人工核对是否支持上述论断）：

```text
   119│ ## Database Operations
   120│ 
   121│ The migration assets live under:
   122│ 
   123│ ```text
   124│ docs/sql/migrations/
   125│ ```
   126│ 
   127│ The current baseline migration is:
   128│ 
   129│ ```text
   130│ docs/sql/migrations/V1__initial_schema.sql
   131│ ```
   132│ 
   133│ The schema contains the MVP tables and uniqueness constraints documented in
   134│ `docs/sql/README.md`. Re-run the migration only when it is safe for the target
   135│ database. Local disposable data can be reset with:
   136│ 
   137│ ```bash
   138│ docker compose down -v
   139│ ```
   140│ 
   141│ Then repeat the startup and migration steps.
```

> 复核（草拟，待抽查确认）：☑路径行号真实　☑内容支持论断　☐存在问题　—— Database Operations 明写 baseline 迁移为 `docs/sql/migrations/V1__initial_schema.sql`，逐字支持完整路径论断。

---

### q07　消费支付事件为什么必须按 eventId 去重？

- 路径：③ partial　｜　实得 mode：**partial**
- run_id：`f15a6d61-6dba-48e7-9b35-01bf25026571`（回放：`devkb runs replay f15a6d61-6dba-48e7-9b35-01bf25026571 --project mini-mall`）
- 节点序列：`plan>retrieve>evaluate>generate>verify>finalize`
- 检索 1 轮 ｜ LLM 请求 3（重试 0）｜ tokens 5652+1178 ｜ latency 15443ms
- L0/L1 终检：通过　｜　citation-to-anchor proxy 命中：False

**回答正文**

```text
消费支付事件必须按 eventId 去重，因为 RabbitMQ 的投递语义是 at-least-once，这会导致同一条事件可能被多次投递到消费者 [E2]。如果不做去重，重复事件会触发重复处理，例如重复更新订单状态或重复发送通知，破坏系统状态一致性 [E2]。因此，消费者必须实现幂等性：对于同一个 eventId 的事件，只处理一次，后续重复到达的事件直接忽略，视为成功的空操作（no-op）[E2]。在数据库层面，各个服务的 repository 也通过唯一约束来强制保证同一个 eventId 的记录不会被重复插入，如果尝试插入重复 eventId 会抛出 DataIntegrityViolationException [E3][E5]。测试也直接验证了重复 eventId 不会产生额外记录，且处理结果保持不变 [E1][E4]。总之，按 eventId 去重是满足至少一次投递下幂等消费的必要手段，也是系统设计的明确规则 [E2]。
```

**not_found**：未覆盖方面

**limitations**：仅回答了现有证据支持的部分，未覆盖方面见 not_found

**claims 与 quotes**

1. RabbitMQ 的投递语义是 at-least-once，因此同一条事件可能被多次投递到消费者[E2]。　→ 证据 ['E2']
   - ✅逐字命中　"Consumers must be idempotent because RabbitMQ delivery is at-least-once."
2. 消费者必须使用 eventId 对消费处理进行去重[E2]。　→ 证据 ['E2']
   - ✅逐字命中　"Use `eventId` to deduplicate event handling."
3. 对于同一个 eventId 的重复事件，应视为成功的空操作（no-op），不进行任何额外处理[E2]。　→ 证据 ['E2']
   - ✅逐字命中　"Treat repeated events for the same `eventId` as successful no-ops."
4. 数据库层通过唯一约束阻止重复 eventId 的记录插入，违反时抛出 DataIntegrityViolationException[E3][E5]。　→ 证据 ['E3', 'E5']
   - ✅逐字命中　"assertThatThrownBy(() -> orderEventRepository.saveAndFlush(duplicate)).isInstanceOf(DataIntegrityViolationException.class);"
   - ✅逐字命中　"assertThatThrownBy(() -> notificationLogRepository.saveAndFlush(duplicate)).isInstanceOf(DataIntegrityViolationException.class);"
5. 测试验证了重复 eventId 不会产生额外记录，且处理结果保持不变[E1][E4]。　→ 证据 ['E1', 'E4']
   - ✅逐字命中　"assertThat(orderEventRepository.findAll()).hasSize(1);"
   - ✅逐字命中　"assertThat(notificationLogRepository.findAll()).hasSize(1);"

**逐引用核对**

#### E1　`order-service/src/test/java/com/minimall/order/messaging/PaymentSuccessEventConsumerTest.java:L200-L216`

- 标题路径：com.minimall.order.messaging.PaymentSuccessEventConsumerTest > PaymentSuccessEventConsumerTest > duplicateEventIdDoesNotProcessAgain　｜　score 0.049
- 源文件 sha256 与摄取快照：✅ 一致
- 行号区间 vs chunk 落库正文：✅ 逐字一致

真实文件原文（供人工核对是否支持上述论断）：

```text
   200│     @Test
   201│     void duplicateEventIdDoesNotProcessAgain() {
   202│         Order order = saveOrder("ORD-PAY-1003", OrderStatus.PENDING_PAYMENT);
   203│         PaymentSuccessEvent event = newEvent("event-pay-1003", order.getOrderNo(), Instant.now());
   204│ 
   205│         consumer.handle(event);
   206│         consumer.handle(event);
   207│ 
   208│         assertThat(orderEventRepository.findAll()).hasSize(1);
   209│         OrderEvent recorded = orderEventRepository.findByEventId("event-pay-1003").orElseThrow();
   210│         assertThat(payload(recorded).path("handleResult").asText()).isEqualTo("processed");
   211│         assertThat(orderRepository.findByOrderNo(order.getOrderNo()))
   212│                 .isPresent()
   213│                 .get()
   214│                 .extracting(Order::getStatus)
   215│                 .isEqualTo(OrderStatus.PAID);
   216│     }
```

> 复核（草拟，待抽查确认）：☑路径行号真实　☑内容支持论断　☐存在问题　—— 测试 `duplicateEventIdDoesNotProcessAgain` 两次 `handle(event)` 后 `findAll().hasSize(1)`，支持「重复 eventId 不产生额外记录、结果不变」。

#### E2　`docs/messaging/payment-success-event.md:L68-L86`

- 标题路径：Payment Success Event Contract > Consumer Rules　｜　score 0.048
- 源文件 sha256 与摄取快照：✅ 一致
- 行号区间 vs chunk 落库正文：✅ 逐字一致

真实文件原文（供人工核对是否支持上述论断）：

```text
    68│ ## Consumer Rules
    69│ 
    70│ Consumers must be idempotent because RabbitMQ delivery is at-least-once.
    71│ 
    72│ `order-service`:
    73│ 
    74│ - Consume from `minimall.order.payment-success.queue`.
    75│ - Use `eventId` to deduplicate event handling.
    76│ - Treat repeated events for the same `eventId` as successful no-ops.
    77│ - Transition only the matching `orderNo` from pending payment state to paid state.
    78│ - Ignore or reject events whose `paymentNo` or `amount` conflicts with the persisted order/payment state.
    79│ 
    80│ `notification-service`:
    81│ 
    82│ - Consume from `minimall.notification.payment-success.queue`.
    83│ - Use `eventId` to deduplicate notification log writes and sends.
    84│ - Treat repeated events for the same `eventId` as successful no-ops.
    85│ - Use `orderNo`, `paymentNo`, `amount`, and `paidAt` only for notification content and audit records.
    86│ 
```

> 复核（草拟，**建议重点抽查**）：☑路径行号真实　☑内容支持论断　☐存在问题　—— Consumer Rules 逐字给出「必须幂等，因为 RabbitMQ 投递是 at-least-once」「用 eventId 去重」「重复事件视为 no-op」，**直接且完整回答问题本身**。此条 proxy=False 仅因人工标注锚点未落在此 chunk，引用质量与相关性均无问题（印证 T20.4「未命中≠引用错」的归因）。

#### E3　`order-service/src/test/java/com/minimall/order/repository/OrderEventRepositoryTest.java:L111-L131`

- 标题路径：com.minimall.order.repository.OrderEventRepositoryTest > OrderEventRepositoryTest > rejectsDuplicateEventIdForIdempotency　｜　score 0.048
- 源文件 sha256 与摄取快照：✅ 一致
- 行号区间 vs chunk 落库正文：✅ 逐字一致

真实文件原文（供人工核对是否支持上述论断）：

```text
   111│     @Test
   112│     void rejectsDuplicateEventIdForIdempotency() {
   113│         orderEventRepository.saveAndFlush(new OrderEvent(
   114│                 "pay-event-duplicate",
   115│                 "ORD-1003",
   116│                 OrderEventType.PAYMENT_SUCCESS,
   117│                 OrderStatus.PENDING_PAYMENT,
   118│                 OrderStatus.PAID,
   119│                 "{\"handleResult\":\"processed\"}"));
   120│ 
   121│         OrderEvent duplicate = new OrderEvent(
   122│                 "pay-event-duplicate",
   123│                 "ORD-1003",
   124│                 OrderEventType.PAYMENT_SUCCESS,
   125│                 OrderStatus.PENDING_PAYMENT,
   126│                 OrderStatus.PAID,
   127│                 "{\"handleResult\":\"duplicate\"}");
   128│ 
   129│         assertThatThrownBy(() -> orderEventRepository.saveAndFlush(duplicate))
   130│                 .isInstanceOf(DataIntegrityViolationException.class);
   131│     }
```

> 复核（草拟，待抽查确认）：☑路径行号真实　☑内容支持论断　☐存在问题　—— `rejectsDuplicateEventIdForIdempotency` 对重复 eventId `saveAndFlush` 抛 `DataIntegrityViolationException`，支持「DB 唯一约束阻止重复插入」（order 侧）。

#### E4　`notification-service/src/test/java/com/minimall/notification/messaging/PaymentSuccessEventConsumerTest.java:L68-L79`

- 标题路径：com.minimall.notification.messaging.PaymentSuccessEventConsumerTest > PaymentSuccessEventConsumerTest > duplicateEventIdIsIgnoredWithoutAdditionalRows　｜　score 0.047
- 源文件 sha256 与摄取快照：✅ 一致
- 行号区间 vs chunk 落库正文：✅ 逐字一致

真实文件原文（供人工核对是否支持上述论断）：

```text
    68│     @Test
    69│     void duplicateEventIdIsIgnoredWithoutAdditionalRows() {
    70│         PaymentSuccessEvent event = newEvent("notification-event-duplicate", "ORD-NOTIFY-1002");
    71│ 
    72│         consumer.handle(event);
    73│         consumer.handle(event);
    74│ 
    75│         assertThat(notificationLogRepository.findAll()).hasSize(1);
    76│         NotificationLog recorded = notificationLogRepository.findByEventId(event.getEventId()).orElseThrow();
    77│         assertThat(recorded.getStatus()).isEqualTo(NotificationLogStatus.SENT);
    78│         assertThat(payload(recorded).path("handleResult").asText()).isEqualTo("sent");
    79│     }
```

> 复核（草拟，待抽查确认）：☑路径行号真实　☑内容支持论断　☐存在问题　—— `duplicateEventIdIsIgnoredWithoutAdditionalRows` 两次 `handle` 后 `findAll().hasSize(1)`、状态 SENT，支持「重复 eventId 不产生额外记录」（notification 侧）。

#### E5　`notification-service/src/test/java/com/minimall/notification/repository/NotificationLogRepositoryTest.java:L115-L131`

- 标题路径：com.minimall.notification.repository.NotificationLogRepositoryTest > NotificationLogRepositoryTest > rejectsDuplicateEventIdForIdempotency　｜　score 0.031
- 源文件 sha256 与摄取快照：✅ 一致
- 行号区间 vs chunk 落库正文：✅ 逐字一致

真实文件原文（供人工核对是否支持上述论断）：

```text
   115│     @Test
   116│     void rejectsDuplicateEventIdForIdempotency() {
   117│         notificationLogRepository.saveAndFlush(new NotificationLog(
   118│                 "payment-event-duplicate",
   119│                 NotificationType.PAYMENT_SUCCESS,
   120│                 "ORD-1003",
   121│                 "{\"orderNo\":\"ORD-1003\"}"));
   122│ 
   123│         NotificationLog duplicate = new NotificationLog(
   124│                 "payment-event-duplicate",
   125│                 NotificationType.PAYMENT_SUCCESS,
   126│                 "ORD-1003",
   127│                 "{\"orderNo\":\"ORD-1003\",\"duplicate\":true}");
   128│ 
   129│         assertThatThrownBy(() -> notificationLogRepository.saveAndFlush(duplicate))
   130│                 .isInstanceOf(DataIntegrityViolationException.class);
   131│     }
```

> 复核（草拟，待抽查确认）：☑路径行号真实　☑内容支持论断　☐存在问题　—— `NotificationLogRepositoryTest.rejectsDuplicateEventIdForIdempotency` 重复 eventId `saveAndFlush` 抛 `DataIntegrityViolationException`，支持「DB 唯一约束阻止重复插入」（notification 侧）。

---

### u01　生产环境 MySQL 连接池的最大连接数配置是多少？

- 路径：④ refusal（正确拒答）　｜　实得 mode：**refusal**　｜　期望 mode：refusal
- run_id：`779772dc-f88f-4bef-97bc-66ad3bf60969`（回放：`devkb runs replay 779772dc-f88f-4bef-97bc-66ad3bf60969 --project mini-mall`）
- 节点序列：`plan>retrieve>evaluate>refine>retrieve>evaluate>finalize`
- 检索 2 轮 ｜ LLM 请求 4（重试 0）｜ tokens 5630+785 ｜ latency 12101ms
- L0/L1 终检：通过　｜　citation-to-anchor proxy 命中：None

**回答正文**

```text
现有资料不足以回答该问题。缺少：生产环境 MySQL 连接池的最大连接数配置。
```

**not_found**：生产环境 MySQL 连接池的最大连接数配置

**limitations**：未找到足以回答问题的证据，未生成实质回答

**逐引用核对**

（本 run 无引用——拒答路径下属预期）

---

### q12　取消订单时库存释放会不会被重复执行？已取消的订单再取消一次会怎样？

- 路径：⑤ L0/L1 失败 → 重生成一次后通过　｜　实得 mode：**full**
- run_id：`4a82736e-b1bd-422b-8016-860e89a38dcb`（回放：`devkb runs replay 4a82736e-b1bd-422b-8016-860e89a38dcb --project mini-mall`）
- 节点序列：`plan>retrieve>evaluate>generate>verify>generate>verify>finalize`
- 检索 1 轮 ｜ LLM 请求 4（重试 0）｜ tokens 10679+5576 ｜ latency 60042ms
- L0/L1 终检：通过　｜　citation-to-anchor proxy 命中：False
- warnings：['verify:l0_l1_failed']

**回答正文**

```text
根据测试和设计文档，取消订单时库存释放不会重复执行；已取消的订单再次取消会幂等返回成功且不会再次释放库存。
```

**claims 与 quotes**

1. 取消订单时库存释放不会被重复执行。　→ 证据 ['E1', 'E4']
   - ✅逐字命中　"verify(inventoryClient, times(1)).release(any(InventoryDeductRequest.class));"
   - ✅逐字命中　"verify(inventoryClient, times(1)).release(any(InventoryDeductRequest.class));"
2. 已取消的订单再次取消会返回成功且不会再次释放库存。　→ 证据 ['E7', 'E2']
   - ✅逐字命中　".andExpect(jsonPath("$.data.status").value("CANCELLED"));"
   - ✅逐字命中　"verify(inventoryClient, never()).release(any(InventoryDeductRequest.class));"
   - ✅逐字命中　"保留"已 CANCELLED → 幂等返回"的现有语义。"

**逐引用核对**

#### E1　`order-service/src/test/java/com/minimall/order/web/OrderControllerTest.java:L354-L377`

- 标题路径：com.minimall.order.web.OrderControllerTest > OrderControllerTest > cancelSameOrderTwiceReleasesInventoryOnlyOnce　｜　score 0.048
- 源文件 sha256 与摄取快照：✅ 一致
- 行号区间 vs chunk 落库正文：✅ 逐字一致

真实文件原文（供人工核对是否支持上述论断）：

```text
   354│     @Test
   355│     void cancelSameOrderTwiceReleasesInventoryOnlyOnce() throws Exception {
   356│         orderRepository.saveAndFlush(order("ORD-CANCEL-API-1005", 305L, "idem-cancel-api-1005"));
   357│         given(inventoryClient.release(any(InventoryDeductRequest.class)))
   358│                 .willReturn(new InventorySnapshot("SKU-API-1001", 10, 0, InventoryStockState.IN_STOCK));
   359│ 
   360│         mockMvc.perform(post("/api/orders/ORD-CANCEL-API-1005/cancel")
   361│                         .header(AuthHeaders.USER_ID, "305")
   362│                         .header(AuthHeaders.USERNAME, "alice"))
   363│                 .andExpect(status().isOk())
   364│                 .andExpect(jsonPath("$.success").value(true))
   365│                 .andExpect(jsonPath("$.data.status").value("CANCELLED"));
   366│ 
   367│         mockMvc.perform(post("/api/orders/ORD-CANCEL-API-1005/cancel")
   368│                         .header(AuthHeaders.USER_ID, "305")
   369│                         .header(AuthHeaders.USERNAME, "alice"))
   370│                 .andExpect(status().isOk())
   371│                 .andExpect(jsonPath("$.success").value(true))
   372│                 .andExpect(jsonPath("$.data.status").value("CANCELLED"));
   373│ 
   374│         verify(inventoryClient, times(1)).release(any(InventoryDeductRequest.class));
   375│         assertThat(orderRepository.findByOrderNo("ORD-CANCEL-API-1005").orElseThrow().getStatus())
   376│                 .isEqualTo(OrderStatus.CANCELLED);
   377│     }
```

> 复核（草拟，待抽查确认）：☑路径行号真实　☑内容支持论断　☐存在问题　—— `cancelSameOrderTwiceReleasesInventoryOnlyOnce` 连续两次 cancel，`verify(inventoryClient, times(1)).release(...)`，直接支持「库存释放不重复执行」。

#### E2　`docs/audit/fix-plans/phase-a-plan.md:L51-L73`

- 标题路径：Phase A 修复规划 —— 消除订单取消/支付并发竞争 > 3. 任务分解 > 任务 A1：`cancel()` 改为条件更新（方向 1 主修复）　｜　score 0.047
- 源文件 sha256 与摄取快照：✅ 一致
- 行号区间 vs chunk 落库正文：✅ 逐字一致

真实文件原文（供人工核对是否支持上述论断）：

```text
    51│ ### 任务 A1：`cancel()` 改为条件更新（方向 1 主修复）
    52│ 
    53│ 涉及文件：
    54│ - `order-service/src/main/java/com/minimall/order/service/OrderCommandService.java`（cancel，当前 64-81 行）
    55│ 
    56│ 改动设计：
    57│ 1. 保留现有查询 `findByOrderNoAndUserId`（用户归属校验不变）。
    58│ 2. 保留"已 CANCELLED → 幂等返回"的现有语义。
    59│ 3. 把"状态机转换 + saveAndFlush"替换为复用现有
    60│    `orderRepository.updateStatusIfCurrent(order.getId(), PENDING_PAYMENT,
    61│    CANCELLED, now)`：
    62│    - 返回 1：CAS 成功，继续调用 `inventoryClient.release(...)`（顺序改为
    63│      **先 CAS 后 release**，两者仍在同一事务内；release 远程失败 → 异常
    64│      → 事务回滚 CAS，订单保持 PENDING_PAYMENT，可重试。release 本身由
    65│      `(order_no, RELEASE)` 唯一流水幂等保护，重试安全）。
    66│    - 返回 0：重新 `findByOrderNo` 读取当前状态——
    67│      - 当前为 CANCELLED：并发取消已完成，按幂等重放返回。
    68│      - 当前为 PAID（或其他）：抛
    69│        `BusinessException(ErrorCode.CONFLICT, "Order current status cannot
    70│        be cancelled: PAID")`，与现有 40900 语义一致，前端无需改动。
    71│ 4. `OrderStateMachine` 类保留不动（`canTransition` 语义仍用于消息与测试），
    72│    只是运行时守卫职责移交给 CAS。
    73│ 
```

> 复核（草拟，待抽查确认）：☑路径行号真实　☑内容支持论断　△轻微　—— 引文「保留『已 CANCELLED → 幂等返回』的现有语义」逐字支持「已取消再取消返回成功」。注：此为 phase-a 修复规划文档（描述现有语义），非实现/测试证据，属性偏弱，但同题 E7 测试已直接佐证同一行为，不构成引用错误。

#### E4　`order-service/src/test/java/com/minimall/order/service/OrderTimeoutCancellationServiceTest.java:L73-L91`

- 标题路径：com.minimall.order.service.OrderTimeoutCancellationServiceTest > OrderTimeoutCancellationServiceTest > repeatedExecutionDoesNotReleaseInventoryTwice　｜　score 0.031
- 源文件 sha256 与摄取快照：✅ 一致
- 行号区间 vs chunk 落库正文：✅ 逐字一致

真实文件原文（供人工核对是否支持上述论断）：

```text
    73│     @Test
    74│     void repeatedExecutionDoesNotReleaseInventoryTwice() {
    75│         LocalDateTime now = LocalDateTime.of(2026, 5, 16, 11, 10);
    76│         Order order = order(503L, "ORD-TIMEOUT-1003", now.minusMinutes(3));
    77│         given(orderRepository.findExpiredOrders(
    78│                 eq(OrderStatus.PENDING_PAYMENT),
    79│                 eq(now),
    80│                 any(Pageable.class)))
    81│                 .willReturn(List.of(order), List.of(order));
    82│         given(orderRepository.updateStatusIfCurrent(503L, OrderStatus.PENDING_PAYMENT, OrderStatus.CANCELLED, now))
    83│                 .willReturn(1, 0);
    84│ 
    85│         int firstRun = service.cancelExpiredPendingOrders(now, 10);
    86│         int secondRun = service.cancelExpiredPendingOrders(now, 10);
    87│ 
    88│         assertThat(firstRun).isEqualTo(1);
    89│         assertThat(secondRun).isZero();
    90│         verify(inventoryClient, times(1)).release(any(InventoryDeductRequest.class));
    91│     }
```

> 复核（草拟，待抽查确认）：☑路径行号真实　☑内容支持论断　☐存在问题　—— `repeatedExecutionDoesNotReleaseInventoryTwice` 超时取消重复执行，`updateStatusIfCurrent` 返回 1/0、`release` 仅 `times(1)`，支持「库存释放不重复执行」。

#### E7　`order-service/src/test/java/com/minimall/order/web/OrderControllerTest.java:L336-L352`

- 标题路径：com.minimall.order.web.OrderControllerTest > OrderControllerTest > cancelAlreadyCancelledOrderReturnsSuccessWithoutReleasingInventoryAgain　｜　score 0.016
- 源文件 sha256 与摄取快照：✅ 一致
- 行号区间 vs chunk 落库正文：✅ 逐字一致

真实文件原文（供人工核对是否支持上述论断）：

```text
   336│     @Test
   337│     void cancelAlreadyCancelledOrderReturnsSuccessWithoutReleasingInventoryAgain() throws Exception {
   338│         Order order = order("ORD-CANCEL-API-1002", 302L, "idem-cancel-api-1002");
   339│         new OrderStateMachine().transition(order, OrderStatus.CANCELLED, LocalDateTime.of(2026, 5, 10, 22, 0));
   340│         orderRepository.saveAndFlush(order);
   341│ 
   342│         mockMvc.perform(post("/api/orders/ORD-CANCEL-API-1002/cancel")
   343│                         .header(AuthHeaders.USER_ID, "302")
   344│                         .header(AuthHeaders.USERNAME, "alice"))
   345│                 .andExpect(status().isOk())
   346│                 .andExpect(jsonPath("$.success").value(true))
   347│                 .andExpect(jsonPath("$.code").value(ErrorCode.SUCCESS.getCode()))
   348│                 .andExpect(jsonPath("$.data.orderNo").value("ORD-CANCEL-API-1002"))
   349│                 .andExpect(jsonPath("$.data.status").value("CANCELLED"));
   350│ 
   351│         verify(inventoryClient, never()).release(any(InventoryDeductRequest.class));
   352│     }
```

> 复核（草拟，待抽查确认）：☑路径行号真实　☑内容支持论断　☐存在问题　—— `cancelAlreadyCancelledOrderReturnsSuccessWithoutReleasingInventoryAgain` 对已 CANCELLED 订单再取消，返回成功且 `verify(inventoryClient, never()).release(...)`，最直接支持「再取消返回成功且不再释放库存」。

---

### q02　库存扣减的幂等键是什么？同一订单重复扣减会发生什么？

- 路径：⑥ 二次验证仍失败 → 确定性降级　｜　实得 mode：**refusal**
- run_id：`c85f1e67-c687-4c03-a2b4-7115bfee5ac5`（回放：`devkb runs replay c85f1e67-c687-4c03-a2b4-7115bfee5ac5 --project mini-mall`）
- 节点序列：`plan>retrieve>evaluate>generate>verify>generate>verify>finalize`
- 检索 1 轮 ｜ LLM 请求 4（重试 0）｜ tokens 12142+3927 ｜ latency 37187ms
- L0/L1 终检：通过　｜　citation-to-anchor proxy 命中：False
- warnings：['verify:l0_l1_failed', 'verify:l0_l1_failed', 'finalize: 已移除 2 个未通过验证的 claim，正文按保留 claim 重建']

**回答正文**

```text
现有资料不足以回答该问题。
```

**limitations**：未找到足以回答问题的证据，未生成实质回答；已移除 2 个未通过 L0/L1 引用验证的 claim

**逐引用核对**

（本 run 无引用——拒答路径下属预期）

---

### u04　用户退款的接口和处理流程是怎样的？

- 路径：⑦ expected=partial 实得 refusal　｜　实得 mode：**refusal**　｜　期望 mode：partial
- run_id：`1f1c29c0-d82d-41b4-91f3-39a177aa4f63`（回放：`devkb runs replay 1f1c29c0-d82d-41b4-91f3-39a177aa4f63 --project mini-mall`）
- 节点序列：`plan>retrieve>evaluate>refine>retrieve>evaluate>finalize`
- 检索 2 轮 ｜ LLM 请求 4（重试 0）｜ tokens 5841+1889 ｜ latency 22736ms
- L0/L1 终检：通过　｜　citation-to-anchor proxy 命中：None

**回答正文**

```text
现有资料不足以回答该问题。缺少：退款 REST API 的 URL、请求方法与参数；退款处理的具体业务流程（验证、状态变更、退款操作）；退款相关的错误码与响应格式。
```

**not_found**：退款 REST API 的 URL、请求方法与参数；退款处理的具体业务流程（验证、状态变更、退款操作）；退款相关的错误码与响应格式

**limitations**：未找到足以回答问题的证据，未生成实质回答

**逐引用核对**

（本 run 无引用——拒答路径下属预期）

---

