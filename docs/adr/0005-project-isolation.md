# ADR-0005：project_id 项目隔离策略

- 状态：已接受（2026-07-12 冻结；本文件为 T11.3 转写，内容以《范围冻结与架构决策》D7/C8 为准）
- 依据：《范围冻结与架构决策》D7、《P1 Agentic RAG MVP 实现规格》§2/§5/§9/§12

## 背景

Python 不能通过“没有 `project_id` 就编译不过”保证隔离。项目隔离必须同时作用于仓储 API、数据库约束、查询组织、测试和静态检查，不能只依赖调用方记得加 `WHERE project_id = ...`。

## 决策

P0/P1 采用下列多层项目隔离：

1. **Repository 绑定项目**：除管理项目实体本身的 `ProjectRepo` 外，所有业务 Repository 都以 `project_id` 实例化；查询方法不接受外部传入的 `project_id`。跨项目查询不应在该 API 中可表达。
2. **项目归属落库**：`documents/chunks` 的 `project_id` 为 `NOT NULL` 且受外键约束；P1 的 `agent_steps/tool_invocations` 同样保存 `project_id` 并同时关联所属 run/step。所有读取均同时受 Repository 已绑定的项目约束。
3. **项目来源可信**：应用服务根据外部 `project` slug 解析 `project_id` 后注入状态与 Repository；LLM 输出、AgentState 更新或工具参数不得覆盖 `project_id/run_id`。
4. **真实双项目测试**：CI 在同一 PostgreSQL 中创建两个项目，验证 document/chunk/run 以及 P1 新增的 step/tool/retrieval 读取均不能跨项目可见。不用“相似度较低”代替“查不到”的隔离断言。
5. **SQL 构造收口**：`src/devkb/` 中的 SQLAlchemy 查询构造只允许出现在 `repositories.py`，CI 用 AST 静态扫描强制，防止 service/API/Agent 节点绕开 scoped Repository 自行查库。

## RLS 推迟

PostgreSQL Row-Level Security 在 P0/P1 不启用，作为 P2 可选的纵深防御。当前已有仓储绑定、数据库约束、双项目测试和静态扫描四层机制；P1 尚无多用户鉴权，此时引入 RLS 会增加迁移、连接上下文和测试复杂度，却不会使 P1 变成完整的多租户系统。

## 后果与边界

- 新增任何项目归属表或查询时，必须同步扩展 Repository 隔离和双项目测试。
- 外部请求中的项目不存在，或 run 不属于该项目时，对调用方均表现为 NotFound，不泄露其他项目是否存在该 ID。
- 项目隔离不等于用户鉴权。P1 API 无鉴权，只允许本地绑定，不得直接公网暴露。
- 应用连接仍拥有整库权限；RLS 未启用时，此策略降低业务查询误用风险，但不是对数据库凭证泄露的安全边界。
