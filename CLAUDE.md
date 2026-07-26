# devkb — 项目开发总规则

Agentic RAG 软件项目知识助手（面向求职展示）。P0 已于 2026-07-16 复验通过；P1 已于 2026-07-22 验收关闭；当前阶段：**P1.5 真实仓库可靠性/诚实加固**（ADR-010），具体进度以 `docs/P1.5任务清单.md` 为准。
任何执行开发任务的模型/人，开工前先读 `AGENTS.md` 与 `docs/development-workflow.md`，再按下述顺序读取当前任务所需的冻结文档，并严格遵守本文件规则。

## 业务与架构文档地图（同领域冲突时序号小的赢）

1. `docs/范围冻结与架构决策.md` — 范围、阶段边界、架构决策 D1–D11、已确认决策
2. `docs/P1.5实现规格.md` — 当前阶段唯一实现依据（契约正确性/证据约束/not_found 分类/一致性/安全终态）
3. `docs/Evaluation-v1.5.md` — P1.5 数据纪律、分环节指标、真实评测与 CI Gate
4. `docs/P1.5任务清单.md` — 当前执行状态跟踪，**做一项勾一项**
5. `docs/P1实现规格.md`、`docs/Evaluation-v1.md`、`docs/P1任务清单.md` — P1 已验收基线与兼容性依据；不得为 P1.5 静默破坏
6. `docs/P0实现规格.md`、`docs/P0任务清单.md` — 已验收基线与兼容性依据；不得静默破坏
7. `docs/模型与环境基准方案.md`、`docs/Evaluation-v0.md` — 模型冻结与 31 问原始数据纪律
8. `docs/路线图-v2.md` — ADR-009/010 接受的 P1.5→P1.6→P2/P3 方向、可投递线、Gate 与粗略容量估时；**不是当前实现依据**
9. `docs/规划加强修复计划.md`、`docs/adr/0009-job-aligned-roadmap.md`、`docs/adr/0010-post-p1-reliability-gate.md`、`docs/P1后真实仓库可用性测试问题记录.md` — 路线修订依据、RT-01～RT-23 与裁决记录
10. `docs/项目初步规划报告.md` — 历史愿景归档；P2+ 优先级已被 1、8、9 取代

## 硬规则（违反 = 返工）

- **工作流门禁**：所有行为代码任务必须使用 `docs/templates/task-packet.md`，按《开发工作流》完成工作区检查、任务合同、仓库调查、计划前审、测试冻结、实现、自审、限定审查和验收。未获批准不得越过阶段；普通任务最多一次定向修复。
- **范围纪律**：只做《P1.5任务清单》中当前未勾选的任务。清单外的想法/优化写入 `docs/backlog.md`，不得直接实现。P1.5 关闭后才创建 P1.6 实现规格/任务清单与 D6 窄修订 ADR，P1.6 关闭后才创建 P2 文档；当前禁止实现或细化 P1.6/P2/P3（含多语言摄取、inventory、动态工具调用）。P0/P1 兼容性回归是 P1.5 Gate，但不得借兼容之名增加清单外功能。
- **勾选纪律**：一项任务的"完成判据"全部满足后，才在清单勾选并附一行结果说明；执行中发现与规格冲突或规格缺失，停止该项，写入清单末尾"偏差记录"，等用户裁决，不得自行改规格。
- **Embedding/迁移门禁**：ADR-0002 已冻结 `Qwen/Qwen3-Embedding-0.6B` 与 `vector(1024)`；P1 的 HNSW/迁移必须沿用该维度。未获新 ADR、全量重嵌方案和用户裁决前，不得更换模型或维度。
- **LLM 边界（D6）**：LLM 供应商 = DeepSeek（OpenAI 兼容端点）。P0 对照路径保持 1 个生成调用；P1 在线路径只有 plan/evaluate/refine/generate 四类调用点，单 run 发往供应商的总请求硬上限 6 次（含格式/传输重试）。所有控制流分支由确定性代码依据结构化信号和预算判定；单点解析/传输失败最多重问 1 次，重问消耗全局预算，预算不足即走冻结默认值或报错，不得无限重试。
- **数据隔离（D7）**：所有 sqlalchemy 查询构造只允许出现在 `src/devkb/repositories.py`；Repository 类以 project_id 实例化，查询方法不得接受外部传入的 project_id。
- **依赖门禁**：P1 仅允许按 T11 spike 引入 `fastapi`、ASGI server、`langgraph` 及其必需 `langchain-core`、`tree-sitter` Java 解析依赖、`jieba`、测试用 `httpx`。禁止引入 Redis、reranker、PyMuPDF、LlamaIndex、LangFuse/MCP SDK、Neo4j driver、Dify/n8n 运行依赖；规格外依赖须先写 backlog/ADR 并获用户同意。
- **反过度设计**：不建占位接口、不为未来预建抽象；Protocol 只允许出现在 `embedding.py` 和 `llm.py` 两个接缝。
- **测试纪律**：CI 与测试不得调用真实模型/真实 API（用 FakeEmbedder/FakeLLM）；业务代码禁止出现 `if testing` 类分支；每项任务完成时 ruff、pyright、pytest 必须零错误全绿。
- **安全**：`.env` 不入库（`.env.example` 入库）；API key 只经环境变量；日志不打印文档正文与任何 secret；解析器永不执行被解析内容。
- **诚实汇报**：测试失败/判据未达成时如实记录，不得勾选；引用/回答机制的已知限制写入 README 而非掩盖。

## 工程规范

- Python 3.12 + **uv**（依赖/虚拟环境/锁文件）；Ruff 负责 lint+format；Pyright 类型检查。
- 回答语言约定：系统生成的用户可见回答为中文、保留英文术语与代码原文。
- 提交信息用 Conventional Commits（feat/fix/test/docs/chore）；一个已验收的逻辑任务对应一个最终提交或一组明确的修复提交，不以提交数量替代验收。
- 每完成一项清单任务，在 `docs/dev-log.md` 追加过程叙事（做了什么/踩的坑与定位/证据 commit），与提交同步；不记文档正文与 secret。
- 常用命令收敛进 Makefile：`make up`（compose）、`make preflight`、`make verify-task`、`make verify-full`、`make lint`、`make test`、`make ci`。

## 环境事实（2026-07-12～07-16 实测）

- WSL2 Ubuntu：10GB 内存 / 8 核 / 8GB swap；RTX 4060 Laptop 8GB（驱动 610.62）；Docker 27.x + Compose v2 可用；T1 已安装并验证 CUDA PyTorch（`torch.cuda.is_available() == True`）。
- 演示语料：`/home/oslab/projects/mini-mall-order`（只读使用）。P1 必须按《P1实现规格》§6.2 先构建带 manifest 的 `/tmp` 语料视图再摄取，禁止直接 ingest 仓库根目录；CLAUDE.md/AGENTS.md/TASKMASTER.md、隐藏目录、依赖和生成物不得进入视图（指令文件留作 P2 注入测试样本）。
- 本仓库位于 Linux 文件系统（ext4），不得迁往 /mnt/c。
- 真实模型调用（冒烟/演示/holdout）必须同时带两个开关：unset 全部代理环境变量（SOCKS 代理干扰 httpx 调 DeepSeek）+ `HF_HUB_OFFLINE=1`（直连时 huggingface.co 遭 DNS 污染，SYN-SENT 静默挂死；嵌入模型已本地缓存）。缺一不可（2026-07-12 / 07-15 两次实测）。
