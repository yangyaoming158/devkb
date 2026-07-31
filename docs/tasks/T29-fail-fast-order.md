+++
task_id = "T29"
status = "accepted"
risk = "medium"
base_sha = "f51417d82624be3d63e1eb03dc1af8f57caa1d4d"
max_changed_files = 8
allowed_paths = [
  "src/devkb/service.py",
  "tests/integration/test_fail_fast_order.py",
  "Makefile",
  "docs/tasks/T29-fail-fast-order.md",
  "docs/P1.5任务清单.md",
  "docs/dev-log.md",
  "docs/backlog.md",
]
allow_dependency_changes = false
allow_migration_changes = false
allow_golden_changes = false
size_exception = ""
+++

# Task Packet：T29 unknown project fail-fast 顺序（RT-23）

> **本 packet 同时承载《P1.5任务清单》T29.1 与 T29.2**，理由见「范围 · 为什么不拆」。

## 任务目标

**一句可观察结果**：对一个**冷** `AppService`（未注入 embedder/LLM）调用 `ask()` 时，若 project slug 不存在，则在 `SentenceTransformerEmbedder` 与 `OpenAICompatLLM` **两个生产工厂都未被构造过**的前提下抛出 `NotFoundError`（HTTP 404 + 稳定脱敏错误体），且 `projects` / `agent_runs` / `agent_steps` / `tool_invocations` **四表**零新增行（m17 断言 3 逐字要求 project 也在内）——其中"两个工厂 0 次"由**装在真实构造接缝上的 spy** 断言，spy 的非空洞性由同一冷 service 的成功路径（各 1 次）正向证明。

## 背景与当前问题

- **当前行为**：`src/devkb/service.py:164-168`——
  ```python
  async with map_errors():
      embedder = await self._get_embedder()   # ← 先加载模型
      llm = self._get_llm()                   # ← 再构造 LLM client
      async with self._session_factory() as session:
          project = await self._resolve_project(session, project_slug)   # ← 最后才知道 404
  ```
  输入校验（pipeline / top_k / question 长度，`:153-163`）确实已在最前，但 **project 解析在两个模型工厂之后**。
- **复现与仓库证据**：
  - 《P1后真实仓库可用性测试问题记录》RT-23（第 618 行）：实跑 404、统一错误体、零 run/LLM 请求均正确，但"`AppService.ask()` 先 `_get_embedder/_get_llm`、后 `_resolve_project`，冷服务会加载 SentenceTransformer，**加载失败还会把本应 404 的请求提前变成 embedding 错误**"。
  - 同文件第 663 行（"已确认事实"）："现有 unknown-project service/API 测试注入的是已实例化 `FakeEmbedder/FakeLLM`，没有对 loader/client factory 调用次数做断言，**无法捕获 RT-23**"。仓库实证：`tests/integration/test_service.py:82` 与 `tests/integration/test_api.py:94` 两条 unknown-project 用例都经 `_service()` helper 注入 `FakeEmbedder()/FakeLLM()`，`_get_embedder` 在 `self._embedder is not None` 时直接返回（`service.py:111`），生产工厂根本不在调用图上——两条用例在缺陷存在时**恒绿**。
  - `_load_embedder`（`service.py:93-104`）函数体内 `import torch` + 构造 `SentenceTransformer`：CI 不装 `ml` 依赖组（`pyproject.toml:42-44`），冷服务在 CI 上必然 `ImportError → EmbeddingError`；本机装了 ml（CLAUDE.md 环境事实 T1），冷服务则会真的去加载数秒级模型。**两种环境下"unknown project"都不会得到干净的 404**——CI 得到 503 EMBEDDING_ERROR，本机得到"慢 404"。
- **用户影响**：一个纯粹的拼写错误（`devkb ask --project mini-mal ...`）在 CLI 上要等模型加载完才报"项目不存在"；在 API 上，模型加载失败时错误码从 `NOT_FOUND` 变成 `EMBEDDING_ERROR`，调用方据错误码分支即被误导。这也是《P1.5实现规格》§15 第 8 条 Gate 项与《P1.5任务清单》"验收总清单"第 8 条。

## 范围

- **允许修改模块**：`devkb.service`（唯一核心源文件）。
- **允许修改文件**：见 frontmatter `allowed_paths`（7 个：1 源 + 1 新测试 + `Makefile` + 4 文档）。
- **最大核心文件数**：1（预算 8 总文件，实际 7）。
- **预计 diff**：`src/devkb/service.py` 净增约 8 行（一处语句重排 + 一段注释）；`tests/integration/test_fail_fast_order.py` 新增约 260 行；`Makefile` 改 1 行（`eval-ci` 的 integration 组增列新测试文件，见 PG-T29-02）；其余为文档。
- **为什么不拆（T29.1 + T29.2 同 packet）**：T29.2 的判据（"新增冷 AppService/API 测试，以 loader/factory spy 断言 unknown project 时 embedder/LLM factory 均 0 次调用"）**就是** T29.1 的验收证据，二者共用同一组 spy 与同一条代码路径。按《开发工作流》§5「实现前冻结测试」，先做 T29.1 再补 T29.2 意味着实现时没有能证明它的测试；反过来先做 T29.2 则要求测试在实现前**全部通过**，与"先红后绿"冲突。合并后仍只有 **1 个核心源文件**，远低于「小任务 ≤3 核心文件」上限，不触发拆分条件。

## 非目标

- **不实现**：`ingest()` 的顺序调整。仓库证据：`service.py:216-221` 在 `get_by_slug` 返回 `None` 时走 `ProjectRepo.create`（`repositories.py:49`），**`ingest` 路径不存在 unknown-project 404**，T29 判据（"`_resolve_project()` 先于工厂"）在该路径上无对象。`ingest` 仍会在任何输入校验之前加载 tokenizer+embedder，这是**另一个**形态，登记 backlog（`T29-P3-01`）而非在本合同内修。**实现期更正**：计划初稿写它「缺目录经 `OSError` 被映射为 `DatabaseError`」——实测 `scan_files(Path('/tmp/definitely-not-here-t29'))` 返回 `[]`（`pipeline.py:53` 用 `root.rglob`，对不存在的目录不抛），真实形态是**先加载模型、再建项目、最后交付一个空 report**，backlog 按实测措辞登记。
- **不实现**：`_get_count_tokens()` 的顺序或异常映射调整（只被 `ingest` 使用）。
- **不重构**：`map_errors` 的异常分类、`_get_embedder` 的 `asyncio.Lock` 懒加载协议、`_load_embedder` 的 `EmbeddingError` 包装、`AppService.__init__` 的装配方式。
- **不修改**：`answer.py` / `agent/service.py` / `api.py` / `cli.py` / `repositories.py` / 任何 Prompt、schema、迁移、依赖。
- **不修改契约版本**：`PROMPT_VERSION`、`AGENT_STATE_SCHEMA_VERSION`、`ANSWER_SCHEMA_VERSION` 均不变（本任务不触碰 Agent 图、Prompt 与 Answer 形状）。
- **不改既有测试**：`tests/integration/test_service.py`、`test_api.py` 不在 `allowed_paths`——它们必须在不修改的前提下继续全绿（向后兼容硬证据）。

## 仓库事实调查

- **入口**：`AppService.ask()`（`src/devkb/service.py:144`）。上游两个调用方——`api.py:122`（`POST /ask`，经 `asyncio.shield`）与 `cli.py:390`（每条命令新建一个 `AppService(get_settings())`，**永远是冷 service**）。
- **调用链（当前）**：`ask` → 输入校验（`:153-163`）→ `map_errors()` → `_get_embedder()`（`:165`，`asyncio.to_thread(_load_embedder)`）→ `_get_llm()`（`:166`，构造 `OpenAICompatLLM`）→ `session_factory()` → `_resolve_project()`（`:168`，`ProjectRepo.get_by_slug`）→ `answer_question` / `agentic_answer_question`。
- **两个工厂的真实构造点**（spy 的挂点，均为**函数体内 import**，因此 monkeypatch 模块属性可拦截）：
  - `service.py:95` `from devkb.embedding import SentenceTransformerEmbedder`；构造体内 `import torch`（`embedding.py:77`）。
  - `service.py:119` `from devkb.llm import OpenAICompatLLM`；构造体内 `from openai import AsyncOpenAI`（`llm.py:67`）。
- **数据模型**：`agent_runs.project_id` 为 `ForeignKey("projects.id", ondelete="CASCADE") NOT NULL`（`models.py:100-102`）；`agent_steps`/`tool_invocations` 经复合 FK 挂到 run（`models.py:123`、`:159`）。unknown project 下不存在 `projects` 行，故后三表**结构上**无法产生关联行——但本任务仍以**四表**全局计数实测（`projects` 亦须零新增：fail-fast 路径不得像 `ingest` 那样顺手建项目），不以 FK 推理代替断言。
- **当前状态流**：`ask` 无 Agent 状态；run 状态机自 `agentic_answer_question:166`（`run_repo.create` + `commit`）才开始。`_resolve_project` 抛 `NotFoundError` 时 run 尚未创建 → 零持久化是**顺序事实**。
- **可复用能力**：`map_errors()`（`:52`）已保证 `DevKbError` 原样透传、其余脱敏；`NotFoundError` → 404 由 `api.py:36` `_STATUS_BY_CODE` 映射；`tests/integration/conftest.py` 已有 `migrated_db_url`（session 级临时库 + alembic upgrade）与 `session` 两个夹具，并已有 `text()` 直查先例（`conftest.py:_admin_exec`）。
- **当前测试**：
  - `tests/integration/test_service.py:82` `test_unknown_project_raises_not_found`（注入 Fake，只断言异常类型）；
  - `tests/integration/test_api.py:94` `test_ask_unknown_project_returns_404_with_stable_error_body`（注入 Fake，断言 404 + code + error_id）；
  - `tests/unit/test_service_errors.py:61` `test_ask_rejects_invalid_pipeline_top_k_and_question_length`（**冷 service** + 不可达端口 9 的 DSN，证明输入校验先于一切 I/O——但不观察工厂）。
  - 以上三条均**不含**任何 loader/factory 计数断言，与 RT-23 记录第 663 行一致。
- **尚未证实的假设**：无。两个工厂的 monkeypatch 可拦截性由"函数体内 import"这一仓库事实直接支撑；实现阶段由 c2（成功路径各 1 次）**正向证明**，不靠假设。

## 关键不变量

- **业务**：unknown project → `NotFoundError`（code `NOT_FOUND`，HTTP 404），文案沿用既有 `项目 '<slug>' 不存在（先执行 devkb ingest）`，不新增用户可见措辞。
- **状态**：该路径不创建 run，故不存在 run 状态转换；`AppService._embedder` 在 fail-fast 后仍为 `None`（懒加载语义不被污染），后续合法请求仍能正常加载一次。
- **权限/项目隔离**：不放宽任何隔离——本任务只把"项目是否存在"的判定**提前**，判定逻辑（`ProjectRepo.get_by_slug`，D7 内）零改动；跨项目 run 可见性不受影响（`run_trace`/`list_runs` 未改）。
- **幂等/重试**：重复 N 次 unknown-project 请求 → 每次都是 404、工厂计数恒为 0、四表零新增（无累积副作用）。
- **事务/部分成功**：project 解析在一个**只读、随即关闭**的 session 内完成，不 commit；后续问答在**新 session** 内进行。因此不存在"模型加载期间占着连接与打开的事务"（见「实现计划 · 数据流」的取舍说明）。TOCTOU：两个 session 之间 project 被删除的窗口在本仓库**无代码路径可达**（`ProjectRepo` 只有 `create`/`get_by_slug`，无 delete）；即便库外手工删除，`agent_runs` 插入会因 FK 失败为 `DatabaseError`，不产生跨项目数据。
- **LLM/Tool/RAG**：D6 边界不变（仍 plan/evaluate/refine/generate 四调用点、单 run ≤6 次）；本任务**减少**而非增加外部副作用（unknown project 时 LLM client 根本不被构造）。检索机制、Prompt、证据选择零改动。
- **失败行为**：模型工厂抛异常时，unknown project 仍必须得到 `NotFoundError` 而不是 `EmbeddingError`（RT-23 的第二个症状）；DB 不可达时仍为 `DatabaseError`（`map_errors` 未改）。
- **向后兼容**：`ask()` 签名、返回形状、错误码集合、`api.py` 错误体形状全部不变；`tests/integration/test_service.py`、`test_api.py`、`tests/unit/test_service_errors.py` 在**零修改**下继续全绿。
- **判据可证边界**（本任务新增确定性判据 = "两个工厂 spy 计数为 0"）：
  | 判据能证明什么 | 推不出什么 | 对应 fail-closed 负例 |
  |---|---|---|
  | 冷 service 下 `ask(unknown)` 未构造 `SentenceTransformerEmbedder` 与 `OpenAICompatLLM` | 推不出"计数 0 = 顺序正确"——**注入 Fake 的 service 上计数恒为 0**，与顺序无关 | **c8**：注入 Fake + 已知 project + 成功返回，两 spy 仍 0 次 → 证明既有测试形态对 RT-23 空洞，本任务的 0 次断言只在冷 service 上有效 |
  | 该路径不会把 404 变成 embedding 错误 | 推不出"工厂不可能失败"；若实现回退为旧顺序，工厂抛错就会改写错误码 | **c3**：两个工厂都换成必抛 `RuntimeError` 的桩，unknown project 仍须 `NotFoundError`（旧顺序下这里会是 `EmbeddingError`） |
  | `agentic` 路径 fail-fast | 推不出 `fixed-rag` 对照路径同样 fail-fast（两者共用 `ask` 但分支在 `answer_fn`） | **c1 参数化**两个 pipeline 值 |
  | 单次请求零持久化 | 推不出"重复失败无累积"，也推不出"失败后懒加载仍正常" | **c5**（**四表**全局计数差 0，含 `projects`）+ **c7**（3 次 unknown 后计数仍 0，随后 known 请求各恰好 1 次，再一次 known 请求 0 次新增） |
  | **首次查询 session 在两个工厂被构造前已退出**（两-session 契约本身） | 推不出"工厂计数 0 ⇒ 没有持有中的事务"——`c1/c3/c6/c7` 的计数断言对单 session 实现**同样通过**（PG-T29-01） | **c2 的事件账本**：`session` 进出与工厂构造记为同一条有序事件流，断言**逐项相等**于 `[session enter, session exit, embedder@active=0, llm@active=0, session enter, session exit]`；单 session 实现会得到 `[session enter, embedder@active=1, …]` 即红 |
  | AppService 层 fail-fast | 推不出 HTTP 层同样 fail-fast（`api.py` 另有 Pydantic 校验与异常处理器） | **c6**（API 层 404 + 三键错误体 + spy 0）与 **c6b**（非法 slug 422 时同样 0 次） |
  | 两个构造函数未被调用 | **推不出"本进程零外部网络调用"**——spy 只钉这两个构造点 | 由 c8 的空洞性演示 + 本行显式声明共同约束；README/dev-log 口径只允许写"两个生产工厂零构造"，**不得**写成"零网络调用" |

## 实现计划

| 顺序 | 文件 | 必要性 | 最小修改 | 对应测试 |
|---:|---|---|---|---|
| 1 | `tests/integration/test_fail_fast_order.py` | T29.2 判据要求的冷服务 loader spy 回归；先落红测试 | 新增文件：spy 工厂（计数 + 返回 Fake / 必抛桩）、记录 session 进出的 factory 包装、c1–c8 共 9 个测试函数 | 自身 |
| 2 | `src/devkb/service.py` | 唯一缺陷点：`ask()` 内 project 解析晚于两个工厂 | `ask()` 内语句重排：先开只读 session 取 `project_id` 并关闭，再 `_get_embedder()`/`_get_llm()`，最后在新 session 内调用 `answer_fn`；附一段说明顺序契约与 session 取舍的注释 | c1–c8 |
| 3 | `Makefile` | m17 是 §5.2 冻结机制检查，但 `eval-ci` 现有两条 pytest 调用**收集不到**新文件——不接入则 T29 回归缺席时 `eval-ci` 仍绿（PG-T29-02） | `eval-ci` 的 integration 组行尾增列 `tests/integration/test_fail_fast_order.py`（**只加这一个文件名**，不动 unit 组、不动 `ci`/`verify-full`） | 由 `make eval-ci` 收集数 19→30 自证 |
| 4 | `docs/backlog.md` | 记录调查中发现、但不在合同内的既有问题 | 新增 `T29-P3-01`（`ingest()` 在任何输入校验前加载 tokenizer+embedder；缺目录不报错而是静默产出空 report——形态见「非目标」的实现期更正） | 不适用（不改代码） |
| 5 | `docs/P1.5任务清单.md` | 勾选纪律 | 验收后勾 T29.1、T29.2 并附结果与证据 commit | 不适用 |
| 6 | `docs/dev-log.md` | 过程叙事 | 追加本任务条目 | 不适用 |
| 7 | `docs/tasks/T29-fail-fast-order.md` | 本合同 | 前审结论、自审、ledger、最终验收回填 | 不适用 |

### 数据流

改动后 `ask()` 的顺序（唯一改动点，其余保持逐字不变）：

1. `pipeline` / `top_k` / `question` 校验（**位置不变**，仍在 `map_errors()` 之外）；
2. `map_errors()` 内：`session_factory()` → `_resolve_project(session, slug)` → 取出 `project.id` → **退出该 session**（只读、不 commit）；
3. `_get_embedder()` / `_get_llm()`；
4. `session_factory()`（新 session）→ `answer_fn(session, project_id, question, ...)`。

**两个 session 而非一个（2026-07-31 会话裁决：采用两 session，不再是开放选项）**：单 session 方案（在同一 session 内先解析再加载模型）diff 更小，但 `_resolve_project` 执行 SELECT 后连接已被 checkout 且隐式事务已开启，**整个数秒级模型加载都会占着连接并保持 idle-in-transaction**；CLI 每条命令都是冷进程（`cli.py:390`），等于**每次 `devkb ask` 都发生一次**。两 session 方案的代价是**多一次 session 创建与连接 checkout**——两种方案的 project SELECT 都**只有一次**，此前写成"多一次 SELECT 往返"不准确，按 PG-T29-01 更正。TOCTOU 窗口在本仓库无删除路径可达（见「关键不变量 · 事务」），裁决明确不要求二次查询。

**该裁决必须由测试锁住，否则等于没定**（PG-T29-01）：c1/c3/c6/c7 的工厂计数断言对单 session 实现**同样通过**，故计数无法证明"首次 session 已在工厂构造前退出"。为此 c2 在测试侧把 `AppService._session_factory` 换成**记录进出的包装**（只在测试内替换实例属性，`src/` 零改动），并让两个 spy 在被调用时记下当时的**活动 session 数**，形成一条有序事件流；c2 对该事件流做**逐项相等**断言：

```text
[("session", "enter"), ("session", "exit"),
 ("factory", "embedder", 0), ("factory", "llm", 0),
 ("session", "enter"), ("session", "exit")]
```

单 session 实现会得到 `[("session","enter"), ("factory","embedder",1), …]`，旧顺序会得到 `[("factory","embedder",0), ("factory","llm",0), ("session","enter"), …]`——两者都与冻结账本不等，即红。

### 状态流

不涉及 Agent run 状态：fail-fast 发生在 `run_repo.create()` 之前（`agent/service.py:166`）。`AppService` 进程内状态只有 `_embedder` 懒加载缓存——fail-fast 路径不写它（c7 正向验证：失败后 known 请求仍恰好加载一次）。

### 异常与恢复流

| 情形 | 期望 | 依据 |
|---|---|---|
| project 不存在 | `NotFoundError`（404），零工厂、零持久化 | c1 / c5 / c6 |
| project 不存在 **且** 工厂会抛 | 仍 `NotFoundError`，**不得**降级为 `EmbeddingError`/`InternalError` | c3 |
| project 存在但模型加载失败 | 仍是 `EmbeddingError`（既有行为，不改） | 由 `_load_embedder` 的 `except Exception` 保持；c3 的对照支路断言 |
| DB 不可达 | `DatabaseError`（既有行为） | `map_errors`；既有 `test_database_unavailable_maps_to_stable_database_error` 零修改继续绿 |
| 输入非法 | `InvalidInputError`，且**先于** project 查询与工厂 | c4 |

### 风险和停止条件

- **如果需要计划外文件**：停止并回到本门禁。特别是若 `tests/integration/test_service.py` 或 `test_api.py` 因重排转红——那说明改动破坏了既有契约，属设计问题，不得靠改既有测试消化。
- **如果规格与代码事实冲突**：停止并写入《P1.5任务清单》文末偏差记录，等用户裁决。
- **如果超过文件预算**：预算 8、计划 7；触碰第 8 个文件即停。
- **如果核心不变量无法证明**：若 c2 的计数断言不通过（spy 未被调用），则挂点无效、全部 0 次断言空洞——此时停止，不得以"计数为 0"宣称通过；若 c2 的事件账本在实现后仍不等，说明两-session 契约未成立，同样停止。

## 冻结测试

全部落在新文件 `tests/integration/test_fail_fast_order.py`（需真实测试 PG，用 `migrated_db_url` / `session` 夹具；`ml` 依赖不参与——两个生产工厂在测试中总是被 spy 替换）。

**spy 设计**：`monkeypatch.setattr(devkb.embedding, "SentenceTransformerEmbedder", spy)` 与 `monkeypatch.setattr(devkb.llm, "OpenAICompatLLM", spy)`；两个 spy 均记录调用次数，`_CountingFactory` 返回可用的 `FakeEmbedder()`/`FakeLLM(script)`（使成功路径可跑通），`_ExplodingFactory` 直接抛 `RuntimeError("factory must not be called")`。

**session 事件包装（仅 c2 使用，PG-T29-01）**：测试内把实例属性 `service._session_factory` 换成一个记录 `enter`/`exit` 并委托真实 factory 的 async context manager，同时维护活动 session 计数；两个 spy 在被调用时把当时的活动计数一并写入同一条事件流。`src/` 不为此增加任何钩子。

| 类别 | 输入/前置状态 | 预期结果 | 测试位置 |
|---|---|---|---|
| 正常 | 冷 service（零注入）+ 已 ingest 的 project + 计数 spy + session 事件包装 | **两组独立断言**：①`embedder_calls == 1` 且 `llm_calls == 1`——**spy 确实挂在生产接缝上**（全部 0 次断言的非空洞性前提，对顺序不敏感）；②事件流**逐项相等**于「实现计划 · 数据流」冻结的六元组账本——首次 session 已 `exit` 且活动数为 0 时才构造两个工厂，随后另开第二个 session（两-session 契约的**唯一**判别测试） | `test_t29_c2_cold_service_resolves_and_closes_first_session_before_constructing_factories` |
| 边界 | 冷 service + 计数 spy + 非法输入（`pipeline="agent-x"` / `top_k=0` / `top_k=13` / 空白 question / 超长 question） | 每条均 `InvalidInputError`，且 `embedder_calls == llm_calls == 0`（校验先于 project 查询与工厂） | `test_t29_c4_input_validation_precedes_project_lookup_and_factories` |
| 失败 | 冷 service + 计数 spy + 不存在的 slug；`pipeline` 参数化 `agentic` / `fixed-rag` | `NotFoundError`（`code == "NOT_FOUND"`）且两 spy 均 0 次 | `test_t29_c1_cold_service_unknown_project_constructs_neither_factory` |
| 失败 | 冷 service + **必抛** spy + 不存在的 slug | 仍 `NotFoundError`；异常**不是** `EmbeddingError`/`InternalError`，且异常串不含 `RuntimeError`/`factory` 字样（旧顺序在此处必红） | `test_t29_c3_failing_factories_do_not_turn_404_into_embedding_error` |
| 重复/重试 | 冷 service + 计数 spy：连续 3 次 unknown `ask` → 再 1 次 known `ask` → 再 1 次 known `ask` | 前 3 次均 404 且计数恒 0；第 4 次成功且计数变为各 1；第 5 次成功且计数**仍为各 1**（懒加载未被 fail-fast 污染，也未重复加载） | `test_t29_c7_repeated_fail_fast_keeps_zero_then_lazy_init_still_happens_once` |
| 越权/非法状态 | API 层（`create_app(cold_service)`）：① `{"project": "no-such-project"}`；② 参数化两条非法输入——`{"project": "Bad Slug!"}` 与 `{"project": <合法 slug>, "top_k": 13}` | ① 404 + 错误体键集合恰为 `{"error": {"code","message","error_id"}}` + `code == "NOT_FOUND"`；② 均 422 + `code == "INVALID_INPUT"`（《Evaluation-v1.5》§5.2 第 8 条把"非法 `top_k` = 422"与 unknown project 写在同一条 Gate 上）；三种情形两 spy 均 0 次 | `test_t29_c6_api_unknown_project_returns_404_without_constructing_factories`、`test_t29_c6b_api_invalid_input_returns_422_without_constructing_factories` |
| 组合/变形不变量 | 同一冷 service 先 unknown 后 known（c7）+ unknown 请求前后对 `projects`/`agent_runs`/`agent_steps`/`tool_invocations` **四表**做**全局** `count(*)`（`text()` 直查，沿用 `conftest._admin_exec` 先例） | 四表计数差均为 0（m17 断言 3 的"零 project/run/step/tool 持久化"逐字满足；由实测而非 FK 推理支撑，`projects` 一栏另防"fail-fast 路径像 `ingest` 那样顺手建项目"） | `test_t29_c5_unknown_project_persists_no_project_run_step_or_tool_row` |
| 诚实边界 | c1 的异常消息与 c6 的 HTTP 响应全文 | 禁用词表零命中：`postgresql`、`asyncpg`、DSN 口令字面量、`Traceback`、`/home/`、`SentenceTransformer`、`torch`、`openai`、`AsyncOpenAI`；且**正文与错误体两处**都扫。本任务不新增任何用户可见文案（404 文案为既有串，测试对其逐字断言以锁定"未新增/未改写"） | `test_t29_c1_...`（异常侧）、`test_t29_c6_...`（HTTP 侧） |
| 判据 fail-closed 负例 | ① **注入** `FakeEmbedder/FakeLLM` 的 service + 计数 spy + **已知 project 成功路径**；② c3 的必抛 spy；③ c1 的 pipeline 参数化 | ① 成功返回且两 spy **仍 0 次** → 断言"计数 0"在注入形态下恒真、对顺序零判别力（既有 `test_service.py:82` / `test_api.py:94` 无法捕获 RT-23 的机器证明）；②③ 见上 | `test_t29_c8_injected_fakes_make_zero_count_assertion_vacuous` |

> 「诚实边界」与「判据 fail-closed 负例」两行均适用，已逐条填写。

**规模**：c1–c8 共 **9 个测试函数**（c6 拆 `c6`/`c6b` 两条）、**11 条收集用例**（c1 按 pipeline 参数化 ×2、c6b 按非法输入参数化 ×2）。基线实测 `pytest --collect-only -q` = **1007**、`eval-ci` unit 组 = **199**、integration 组 = **19**；实现后应为 `make ci` **1018**、`eval-ci` **199 + 30**。

**实现前的红绿预测（逐条，不含糊）**——在 `f51417d`（未改 `service.py`）上：

| 预测 | 用例 | 理由 |
|---|---|---|
| **红（5 函数 / 6 用例）** | `c1`（×2 pipeline）、`c2`、`c3`、`c6`、`c7` | 旧顺序下两个工厂在 project 解析前被构造：c1/c6/c7 的计数断言由 0 变 1、c3 的必抛桩把 404 改写成 `EmbeddingError`、**c2 的事件账本首项由 `("session","enter")` 变成 `("factory","embedder",0)`** |
| **绿（4 函数 / 5 用例）** | `c4`、`c5`、`c6b`（×2）、`c8` | 均**非**缺陷复现：c4 断言既有正确行为（输入校验已在最前）；**c5 是保护性断言——零持久化在旧顺序下同样成立**（fail-fast 仍早于 `run_repo.create`），它防的是将来把 project 解析挪到建 run 之后；c6b（Pydantic 在 `ask` 之前拒绝非法输入）与 c8（注入形态）是负向对照，本就应恒绿 |

红绿分布在实现阶段逐条记录进「执行者自审」；**任何一条与本表不符即停**。两条专门的停止条件：

- **c2 在 base 上的红必须落在事件账本断言**。若它红在计数断言（`1 != 1` 之外的形态，即 spy 根本没被调用），说明 monkeypatch 没挂到真实构造接缝，**全部 0 次断言随之空洞**——此时停止，不得继续。
- **若 c5 意外转红**，说明零持久化并非我理解的顺序事实，回到本门禁重新调查。

## 验收标准

- [x] `src/devkb/service.py` 的 `ask()` 中，`_resolve_project` 早于 `_get_embedder()`/`_get_llm()`（源码顺序可读、有注释说明契约）
- [x] c1–c8 全部通过（9 个测试函数 / 11 条用例），且实现前的红绿分布与「冻结测试 · 红绿预测」表逐条一致
- [x] c2 的计数断言证明两个 spy 均能被真实调用（非空洞）；c2 的事件账本证明首次 session 已在两个工厂构造前退出（两-session 契约）；c8 证明注入形态下计数断言空洞
- [x] `tests/integration/test_service.py`、`test_api.py`、`tests/unit/test_service_errors.py` **零修改**且全绿
- [x] unknown project 路径**四表**（含 `projects`）零新增行（c5 实测，m17 断言 3）
- [x] HTTP 404 错误体形状与既有契约逐字一致；禁用词表零命中
- [x] 未新增依赖/迁移/schema/API/golden；`PROMPT_VERSION`、`AGENT_STATE_SCHEMA_VERSION`、`ANSWER_SCHEMA_VERSION` 均未变
- [x] 新测试文件已接入 `make eval-ci`（m17 属《Evaluation-v1.5》§5.2 冻结机制检查，不接入则本回归缺席时 eval-ci 仍绿）
- [x] `make verify-task PACKET=docs/tasks/T29-fail-fast-order.md` 通过；`make ci` 与 `make eval-ci` 全绿（数值见「冻结测试 · 规模」：`ci` 1018、`eval-ci` 199 + 30）
- [x] 《P1.5任务清单》T29.1 / T29.2 勾选并附结果与证据 commit；`docs/dev-log.md` 追加条目；`T29-P3-01` 登记 backlog

## GPT 前置计划审查

- 结论：**第一轮 `PLAN_REVISION_REQUIRED`（3 条 P2）→ 按最小边界修订一次 → 第二轮 `PLAN_APPROVED`（2026-07-31）**。审查者逐条判定：PG-T29-01 已关闭（c2 六元组事件账本可区分旧顺序、单 session 与批准的两-session 顺序，红绿预测/停止条件/验收项已同步）；PG-T29-02 已关闭（c5 四表、`Makefile` 已纳入 7 个允许文件与实施计划、新测试明确接入 eval-ci，11 条新增用例对应 `ci`=1018 与 `eval-ci`=199+30「算术自洽」）；PG-T29-03 已关闭（工作区记录与独立检查一致）。另注：审查者独立复核 `make eval-ci` 基线确为 **199 + 19**，与本 packet 记载一致
- 审查基准：base `f51417d82624be3d63e1eb03dc1af8f57caa1d4d`（分支 `task/T29-fail-fast-order`）。**工作区事实（PG-T29-03 更正）**：`git status` 为 `?? docs/tasks/T29-fail-fast-order.md`，即**仅本 packet 未跟踪，`src/`、`tests/`、`Makefile` 零改动**；此前写作"工作区 clean"不准确。
- **会话裁决（2026-07-31）**：采用本 packet 的**两 session** 方案。理由（审查者原话要点）：单 session 会在冷模型加载期间持有已开启事务；`ProjectRepo` 无删除入口，后续 run 又受 project FK 约束，故现有 TOCTOU 分析足够，**不要求二次查询**。
- 阻塞问题与修订（**每条只做最小边界，未借机扩大范围；`src/` 允许改动面仍是 `service.py` 一个文件**）：

| Issue | 严重度 | 审查者指出的实质 | 本轮修订 |
|---|---|---|---|
| `PG-T29-01` | P2 | 两-session 不变量缺少判别测试——packet 自己写明 c1–c8 对单/双 session 等价，故单 session 实现也会全绿，证不出"首次查询 session 已在工厂调用前退出" | c2 改为**两组独立断言**：计数（非空洞性，顺序不敏感）+ **session/工厂有序事件账本**（逐项相等于六元组，单 session 与旧顺序都会红）；事件包装只在测试内替换实例属性，`src/` 不加钩子。同步改红绿预测（c2 由绿转红，5 函数 / 6 用例）、停止条件（区分"红在计数"与"红在账本"两种含义）与验收项。另按指正把"多一次 SELECT 往返"改为准确的**"多一次 session 创建与连接 checkout"**——两方案的 project SELECT 都只有一次 |
| `PG-T29-02` | P2 | 冻结的 m17 Gate 未完整落地：①m17 断言 3 要求 **project**/run/step/tool 四类零持久化，c5 只统计后三表；②`Makefile:47` 的 `eval-ci` 收集不到新文件，T29 回归缺席时仍会绿（实际增列点在 `Makefile:52` 的 integration 组一行） | ①c5 扩为**四表**全局计数（新增 `projects`，另防 fail-fast 路径像 `ingest` 那样顺手建项目），任务目标/不变量/验收项同步改口径；②`Makefile` 进 `allowed_paths` 与文件计划（第 3 行），`eval-ci` 的 integration 组增列新测试文件；文件数 6→**7**（预算 8 未动），收集数同步为 `ci` 1018、`eval-ci` 199 + 30。另按 §5.2 第 8 条把"非法 `top_k` = 422"补进 c6b（与 unknown project 同属该条 Gate），c6b 参数化 ×2、用例数 10→**11** |
| `PG-T29-03` | P2 | 工作区事实记录不准确（写了 clean，实际有未跟踪的 packet） | 见上"审查基准"一行，已按建议措辞更正 |

只有结论为 `PLAN_APPROVED` 后，metadata 的 `status` 才能改为 `approved`。

**本轮不再有开放待裁决项**：两 session 已由会话裁决冻结，并已有唯一判别测试（c2 事件账本）锁住。

## 执行者自审

- [x] 实际文件全部在 allowed_paths —— `make verify-task` 报 **7/7 文件在 `allowed_paths`**（`src/devkb/service.py`、`tests/integration/test_fail_fast_order.py`、`Makefile`、4 份文档），预算 8 未触顶
- [x] 没有未批准依赖、迁移、schema、API、golden/snapshot 变化 —— `pyproject.toml`/`uv.lock`/`alembic/` 零改动；`Makefile` 改动只是 `eval-ci` 增列一个测试文件名（不是依赖变更，`DEPENDENCY_FILES` 不含 Makefile）；`PROMPT_VERSION`/`AGENT_STATE_SCHEMA_VERSION`/`ANSWER_SCHEMA_VERSION` 与 Answer JSON 字段集合均未动
- [x] 没有大面积格式化、debug、TODO/FIXME、`if testing` —— `src/` 的 diff 仅 `ask()` 一处（`git diff --numstat` = **9 增 / 3 删**，其中 5 行是注释；`Makefile` 为 **2 增 / 1 删**，只是把一行 pytest 调用折行并增列文件名）；全仓 `if testing` 命中仍是 `tests/unit/test_workflow_guard.py` 里守卫自身的检测夹具（不在本次 diff 内）
- [x] 没有吞异常、降低断言、skip/xfail —— 新文件零 `skip`/`xfail`；既有测试**零修改**（`git diff --stat` 对 `tests/integration/test_service.py`、`test_api.py`、`tests/unit/test_service_errors.py` 为空）且继续全绿
- [x] 状态、权限、幂等、失败行为符合合同 —— c7（重复 fail-fast 无累积 + 懒加载仍只一次）、c5（四表零持久化）、c3（工厂失败不改写错误码）、c1（两个 pipeline）
- [x] 冻结测试真实运行（**逐行对表勾掉**）：正常=`test_t29_c2_cold_service_closes_first_session_before_constructing_factories`；边界=`..._c4_input_validation_precedes_project_lookup_and_factories`；失败=`..._c1_cold_service_unknown_project_constructs_neither_factory[agentic|fixed-rag]` 与 `..._c3_failing_factories_do_not_turn_404_into_embedding_error`；重复/重试=`..._c7_repeated_fail_fast_keeps_zero_then_lazy_init_still_happens_once`；越权/非法状态=`..._c6_api_unknown_project_returns_404_without_constructing_factories` 与 `..._c6b_api_invalid_input_returns_422_without_constructing_factories[2 参数]`；组合/变形=`..._c5_unknown_project_persists_no_project_run_step_or_tool_row`；诚实边界=c1（异常侧）+ c6（HTTP 侧）；fail-closed 负例=`..._c8_injected_fakes_make_zero_count_assertion_vacuous`。**9 函数 / 11 用例，与「规模」一致**
- [x] 新增用户可见文案/warning 均有禁用措辞否定断言；新增确定性判据均已写明"推不出什么"且各有 fail-closed 负例 —— 本任务**不新增任何用户可见文案**，c1 反而对既有 404 文案做**逐字相等**断言以锁定"未改写"；禁用词表（含从 `migrated_db_url` 现取的口令）在异常消息与 HTTP 响应两处都扫
- [x] `make verify-task PACKET=docs/tasks/T29-fail-fast-order.md` 通过
- [x] 已创建或指定 candidate commit，工作区干净
- [x] 日志记录 candidate HEAD 和对应命令结果 —— candidate HEAD `af1394f`，逐条见「最终验收 · 候选提交与门禁日志」
- [x] 提交前一致性扫描（从改动反推，不凭记忆）：本轮改动的数字/结论为「7 文件、9 函数、11 用例、`ci` 1018、`eval-ci` 199+30、6 红/5 绿、新增 ID `T29-P3-01`」。命令 `grep -n "6 个\|实际 6\|计划 6\|10 条\|1017\|三表\|工作区 clean\|SELECT 往返\|c9\|OSError" docs/tasks/T29-fail-fast-order.md` → 残留命中**仅在**「计划前审修订表」与「非目标 · 实现期更正」两处，均为**刻意引用旧措辞以说明改了什么**；旧测试函数名 grep 零命中

**实现期的红绿实测（与合同「红绿预测」表逐条比对）**：在 `f51417d`（`service.py` 未改）上跑新文件 → **6 failed / 5 passed**，失败集合恰为 `c1[agentic]`、`c1[fixed-rag]`、`c2`、`c3`、`c6`、`c7`，与预测**逐条相同**。两条停止条件均已核验：

- **c2 的红落在事件账本**（`assert [('factory','embedder',0), …] == [('session','enter'), …]`，`At index 0 diff`），其上一行 `assert spies.calls == (1, 1)` **先通过**——spy 确实挂在真实构造接缝上，0 次断言非空洞；
- **c5 未意外转红**（零持久化在旧顺序下同样成立，符合合同对它"保护性断言"的定性）。

另记 c3 在 base 上的实际异常为 `EmbeddingError('嵌入模型加载失败：RuntimeError')`——RT-23 记录的第二症状"加载失败把本应 404 的请求变成 embedding 错误"**原样复现**，不是推测。

**如实记录一处计划更正**：合同「非目标」初稿称 `ingest()` 缺目录会 `OSError → DatabaseError`；实测 `scan_files` 对不存在的目录返回 `[]`（不抛），真实形态是"先加载模型 → 建项目 → 交付空 report"。已在「非目标」标注更正而非悄悄改写，`T29-P3-01` 按实测措辞登记。

结论：`READY_FOR_REVIEW`

## Review Ledger

| Issue ID | 严重度 | 合同条款 | 文件/位置 | 证据与实际结果 | 最小修复边界 | 状态 |
|---|---|---|---|---|---|---|

## 定向修复记录

| Issue ID | 修复文件 | 回归测试 | 测试结果 |
|---|---|---|---|

## 最终验收

- Base SHA：`f51417d82624be3d63e1eb03dc1af8f57caa1d4d`
- Head SHA：`af1394f8d5e9a74744442db60007ddf149ef23ac`（**唯一代码提交**；首审即终审，零 finding 故无定向修复提交）
- P0：**0**
- P1：**0**
- 当前合同内 P2：**0**（首审 issue ledger 为空）
- P3/backlog：**1 条新增**（`T29-P3-01` `ingest()` 在任何输入校验前加载 tokenizer+embedder；缺目录静默产出空 report。既有代码、不在本次 diff 内，`ingest` 路径无 `_resolve_project` 可提前）
- `make verify-full`：**PASS，exit=0**，`status: clean`，changed files **7 / 8** 全在 `allowed_paths`（详见下表）
- 远端 exact-head CI：待 T32.4 统一在 P1.5 头提交上核验（沿用 T27.1/T27.2/T28.1 口径，本任务不单独推送）
- Reviewer 结论：**`PASS`**（2026-07-31 限定代码审查，**首审即终审、issue ledger 为空**）。审查者独立复核四点：①`service.py:164` 符合批准的双-session 顺序（解析并关闭首个 session → 构造两个工厂 → 第二个 session 执行问答）；②c2 的冻结事件账本能区分旧顺序、单 session 与批准实现，c1/c3/c5/c6/c7/c8 分别覆盖零构造、错误优先级、四表零持久化、HTTP 契约、重复调用与 spy 非空洞性；③`Makefile:47` 已把新测试接入 `eval-ci`；④exact-HEAD `make verify-full` exit 0、运行后工作区仍 clean

### 候选提交与门禁日志

| 项 | 结果 |
|---|---|
| 基线（base SHA `f51417d`） | `pytest --collect-only` **1007**；`eval-ci` **199 + 19** |
| 实现前红绿（新文件跑在未改的 `service.py` 上） | **6 red / 5 green**，失败集合 = `c1[agentic]`、`c1[fixed-rag]`、`c2`、`c3`、`c6`、`c7`——与合同「红绿预测」表**逐条相同** |
| 停止条件①（c2 的红必须在账本） | 通过：红在 `assert spies.ledger.events == FROZEN_SESSION_LEDGER`（`At index 0 diff: ('factory','embedder',0) != ('session','enter')`），其上一行 `assert spies.calls == (1, 1)` **先通过** → spy 挂在真实构造接缝上，0 次断言非空洞 |
| 停止条件②（c5 不得意外转红） | 通过：c5 在 base 上即绿，零持久化本就是 `run_repo.create` 之前的顺序事实 |
| RT-23 第二症状复现 | c3 在 base 上的实际异常为 `EmbeddingError('嵌入模型加载失败：RuntimeError')`，改后为 `NotFoundError`/`NOT_FOUND` |
| `make ci`（exact HEAD `af1394f`） | **1018 passed**（1007 + 11）；ruff format/check、pyright 均 0 |
| `make eval-ci`（exact HEAD） | **199 + 30**（integration 组 19 + 11） |
| `make verify-task` / `make verify-full` | 均 **PASS**；后者 `--require-clean` 下 `status: clean`、exit=0 |
| `git diff --numstat`（base→head） | `src/devkb/service.py` **9 / 3**；`Makefile` **2 / 1**；`tests/integration/test_fail_fast_order.py` 新增；4 份文档 |
| 既有测试影响 | `test_service.py` / `test_api.py` / `test_service_errors.py` **零改动**（`git diff --stat` 为空）且继续全绿 |
| 契约版本 | `PROMPT_VERSION`、`AGENT_STATE_SCHEMA_VERSION`、`ANSWER_SCHEMA_VERSION` 均**未变**；Answer JSON 字段集合、错误码集合、D6 四调用点与单 run ≤6 预算均未变 |
