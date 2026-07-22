# P1 验收总清单（T21.4）

> 状态：**《P1实现规格》§15 十项 10/10 满足，P1 技术验收通过**。逐条核对 §15 十项验收标准，每项链到测试 / 报告 / run_id / 命令输出。
> 核对时间：2026-07-22　｜　验收证据基准提交：`13f8f008c0f310aa12a3d5db273f1ba8d1274624`（`docs: reconcile P1 acceptance evidence`，见文末「提交序列」）　｜　holdout：已一次性运行并通过（`p1-holdout-20260721T223756+0800`）
>
> **CI 状态注记**：验收证据基准提交 `13f8f008` 已推送；核验时本地 `HEAD` 与 `origin/p1` 均指向该完整 SHA、工作区干净。对应 GitHub Actions Run **29922618713**（head_sha `13f8f008…`）最终 **completed / success**，checks 中 pytest、`make eval-ci` 等全部成功。第 8 项据此满足——以当前基准头提交 `13f8f008` 自身的 CI 成功为准，不以较早 `1a4bb0c` 的绿灯替代。

## §15 十项逐条

| # | 验收标准 | 证据 | 状态 |
|---|---|---|---|
| 1 | 迁移可逆；P0 数据无损 backfill；GIN/HNSW 和两张轨迹表可用 | `tests/integration/test_migrations.py`（upgrade head→downgrade 0001→upgrade head）、`test_backfill.py`（P0 数据 backfill 无损）。迁移链：`0001_initial_schema.py` 建 projects/documents/chunks/**agent_runs**；`0002_p1_search_and_trace.py` 新增 chunks `search_text`/`search_tsv`（STORED 生成列）+ **GIN**(`ix_chunks_search_tsv`) + **HNSW**(`ix_chunks_embedding_hnsw`) + **agent_steps**/**tool_invocations**；`0003_trace_composite_fks.py` 加固轨迹表复合 FK（`agent_steps(run_id,project_id)`→`agent_runs(id,project_id)` 等，D7 跨项目归属一致性） | ✅ |
| 2 | mini-mall 语料视图摄取 Markdown+Java+config，manifest 不含指令文件/生成物/依赖；二次运行零新增；Java 引用行号真实 | `evalsets/reports/p1-manual-demo.md`：视图 445=39md+392java+14config，与 dev 报告逐文件 sha256 一致；二次 ingest 成功 0/跳过 445；`build_corpus_view.py` 自检断言无指令文件/隐藏/依赖。Java 行号真实见 `p1-manual-check.md`（q07/q12 Java 引用逐行核对，randy 通过） | ✅ |
| 3 | Hybrid/RRF 与 P0 vector 基线同一 dev 集受控对照，达预冻结 Gate | **§5.1 检索 Gate**：`p1-dev-retrieval-20260719T224424+0800.{json,md}`——HNSW 对 exact 平均 top-10 overlap=1.0（§5.1 第 4 条硬性）通过；hybrid/vector-exact 同快照对照按记录义务落盘（T15.4 负结果 hybrid R@10<vector，ADR 未改在线默认，在线默认=vector-hnsw） | ✅ |
| 4 | LangGraph full/partial/refusal/补检/L1 重生成确定性测试；轮次≤2、供应商总请求（含重试）≤6 | `tests/unit/test_agent_graph.py`（路径矩阵：full/partial/refusal/refine/L0L1 重生成的确定性路由）；预算硬上限 `MAX_RETRIEVAL_ROUNDS=2`、`MAX_LLM_REQUESTS=6`（`src/devkb/agent/state.py`，测试锁定）。holdout 实测 39 调用/10 run，单 run ≤6、0 重试 | ✅ |
| 5 | 4 个 dev 不可答题达拒答/边界 Gate；可答题误拒率达标 | **§5.2 回答 Gate**：`p1-dev-agentic-20260719T230855+0800.{json,md}`——correct_unanswerable 3/4（u04 期望 partial 实得 refusal，T20.4 已记录偏差并裁决）、false_refusal 1/17（q02，§5.2 允许 ≤1）、L0/L1 均 100%、预算内、终态完整（§5.2 六项全过）。holdout 记录：正确拒答 2、误拒 0 | ✅（u04 偏差已裁决） |
| 6 | agent_steps/tool_invocations 可按 run_id 回放；失败 run 也有完整终态和故障位置 | `devkb runs replay <run_id>` 实测可回放完整节点轨迹（本会话回放 q07 partial run 成功）；**失败 run 终态落库**：`tests/integration/test_agent_service.py::test_unexpected_node_failure_persists_failed_run_and_failed_last_step`（节点内未捕获异常 → run 终态 `failed`、轨迹保留且最后 step 为 `failed`）。`test_trace.py` 佐证轨迹行为、`test_datalayer.py` 佐证轨迹持久化，但失败 run 终态由前述 service 测试直接证明 | ✅ |
| 7 | CLI、`POST /ask`、`GET /runs/{id}`、`GET /healthz` 集成测试；双项目数据不可越权 | `tests/integration/test_api.py`（三端点）、`test_api_concurrency.py`；`test_isolation.py` 验证 Repository 以 project_id 实例化、跨项目不可读（D7：查询构造只在 `repositories.py`，查询方法不接受外部 project_id） | ✅ |
| 8 | 本地 `make ci && make eval-ci` 全绿；当前头提交 GitHub Actions 全绿 | 本地 2026-07-22：`ruff check --no-cache` 通过；`make ci`（ruff format/check ✓、pyright 0 错、pytest **281 passed**）与 `make eval-ci`（unit **65** + integration **19** passed）全绿。远端：验收证据基准提交 `13f8f008c0f310aa12a3d5db273f1ba8d1274624` 已推送、HEAD 与 origin/p1 一致；GitHub Actions Run **29922618713**（head_sha `13f8f008…`）**completed / success**，pytest、`make eval-ci` 等 checks 全部成功（https://github.com/yangyaoming158/devkb/actions/runs/29922618713）。以当前头提交自身 CI 成功为准，不以较早 `1a4bb0c` 绿灯替代 | ✅ |
| 9 | holdout 只运行一次并留档；不据结果回改 Prompt/阈值/权重/标注 | `p1-holdout-20260721T223756+0800.{json,md}` + 台账 `holdout-access-log.jsonl`（attempt=1 started+completed 恰一条）；U2.3 授权 `p1-holdout-freeze-check.md`（randy 签字）。两硬 Gate 全绿，dev-log 明确记「未据结果调参」 | ✅ |
| 10 | README、架构图、状态图、评测报告、演示步骤与实现一致 | `README.md`（T21.1 重写：两 mermaid 图 + 三态表 + 一页起步，所有措辞对代码核过）；`p1-manual-demo.md`（真实演示三条 + 五路径可回放 run）；`p1-dev/holdout` 报告 | ✅ |

## 偏差记录与裁决

| 偏差 | 裁决 | 出处 |
|---|---|---|
| u04 期望 partial 实得 refusal（正确拒答 3/4 而非 4/4） | 已记录，可接受（拒答内容成立、not_found 具体）；U2.2 补充样本已复核 | T20.4、`p1-manual-check.md` |
| citation-to-anchor proxy < 冻结 0.85（dev 0.53 / holdout 0.50） | 裁决采纳：proxy 由硬 Gate 降为记录义务，硬性兜底由 U2.2 人工引用抽查承担（100%） | T20.4、Evaluation-v1 §5.2 修订 |
| q05/q07 not_found 占位致 partial 偏保守 | 裁决：P1 保持现状不改 Prompt（改则冻结失效须重跑 Gate），记 backlog 留 P2 | `p1-manual-check.md`、`docs/backlog.md` |

> 无「计划中/待补」的能力被勾选；上述三处偏差均有用户裁决。

## 边界声明（§15 末）

P1 通过只证明**单 Agent Agentic workflow**。不对外宣称 Multi-Agent、MCP、LangFuse、GraphRAG 已实现——这些是 P2/P3 路线（`docs/路线图-v2.md`），当前未实现。

## 结论（**已验收通过**）

§15 **十项全部满足（10/10）**——第 8 项随验收证据基准提交 `13f8f008` 推送、HEAD 与 origin/p1 一致、GitHub Actions Run 29922618713 completed/success 而补齐。三处偏差均有用户裁决，无「计划中/待补」被勾选。**P1 单 Agent Agentic workflow 技术验收通过。**

复核/验收人：**randy**　日期：**2026-07-22**　☑ P1 验收通过　☐ 退回（原因：____）

## 提交序列（核对时）

- 历史事实（保留）：`1a4bb0c`（`fix: sort conftest imports...`）曾 GitHub Actions 全绿；其后本地一度叠加 `565b4a1`（`docs: close T21.5 ... complete §15 acceptance checklist`）等文档提交，使当时头提交与远端 CI 绿灯不一致——已在验收证据复查中如实记录并回退相关勾选。
- **验收证据基准提交**：`13f8f008c0f310aa12a3d5db273f1ba8d1274624`（`docs: reconcile P1 acceptance evidence`）——已推送；核验时本地 `HEAD` 与 `origin/p1` 均指向该完整 SHA、工作区干净。对应 GitHub Actions Run **29922618713**（head_sha `13f8f008…`）completed / success（https://github.com/yangyaoming158/devkb/actions/runs/29922618713）。第 8 项以此提交为准。
- 说明：`13f8f008` 是**验收证据基准提交**，不是承载本记录的验收关闭提交。本次「验收关闭」的文档收尾（勾选 T21.4/T21.5/T21.6、更新本文件与 dev-log）由一个基于它的**纯文档提交**承载；该关闭提交推送后须通过自身 GitHub Actions，最终状态以远端分支 CI 为准。本文不预记提交创建前无法取得的自身 SHA，也不以 `13f8f008` 的绿灯替代关闭提交的 CI。
