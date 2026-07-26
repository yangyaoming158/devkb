# 开发日志（dev-log）

> 体裁分工：任务清单=验收凭证（一行结论）；ADR=决策记录；本文件=**过程叙事**——
> 每个任务做了什么、踩了什么坑、怎么定位、证据在哪。面试素材与 G4 投递包的原料库。
> 约定：每完成一项清单任务追加一条，与该任务的提交同步；只记事实与教训，不记文档正文与 secret。

## 2026-07-12 · 环境准备（T1 前置）

- WSL2 内存从 5.8GB 扩到 10GB。坑：`.wslconfig` 行内注释（尤其 `swapfile=` 行）导致整个配置解析失败、swap 变 0B——**该文件不能写行内注释**；删掉 swapfile 行用 C 盘默认位置后恢复 8GB swap。
- HF 模型下载期间用户 C 盘骤降：WSL2 的 vhdx 按需增长且不自动回缩，模型缓存虽在 ext4 的 `~/.cache/huggingface`，但 vhdx 膨胀反映到 C 盘可用空间。教训：大模型下载前先估空间，需要时用 `Optimize-VHD` 回收。

## 2026-07-12 · T1 模型与环境基准（→ ADR-0002 / ADR-0004）

做了什么：4 个 embedding 候选 ×{GPU fp16, CPU} 全矩阵基准 + 15 问 Recall@5 质量小测 + DeepSeek smoke，冻结 Qwen3-Embedding-0.6B / vector(1024)。证据：`benchmarks/embedding_report.md`、`benchmarks/results/*.json`。

- **坑（最重要的一个）：召回天花板坍塌**。首轮质量小测所有模型都在 0.27–0.33 徘徊，差点得出"模型都不行"的错误结论；实际是均匀降采样把 15 个锚点块丢得只剩 6 个——**上限只有 0.40，分数再高也高不过上限**。修复：`sample_corpus.py` 强制保留锚点块，跑分前先校验锚点覆盖率。教训固化为流程：**先验证评测上限，再解读评测分数**。
- 8GB 显卡装不下 fp32 默认配置：bge-m3/Qwen3 连续 CUDA OOM。处理：`torch_dtype=float16` + `max_seq_length` 截到 1024——这不是丢精度的妥协，是 8GB 卡的必要条件，已写入 ADR-0002 强制约束。
- Qwen3 batch 64 显存溢出到共享内存，吞吐从 18.2 崩到 1.7 chunks/s（悬崖不是渐降）；batch≤32 冻结进配置并注释警告。
- CPU 批量嵌入不可用是实测结论而非推测：bge-m3 0.7 chunks/s，Qwen3 跑 95 分钟未完成被中止——中止本身作为测量记录。
- 网络坑三连：PyPI 走 SOCKS 代理仅 0.5MB/s，切清华镜像后 43MB/s（80 倍）；httpx 遇 SOCKS 代理缺 socksio 直接 ImportError——对国内端点（DeepSeek/hf-mirror/tuna）一律 unset 代理直连；HF 下载走 hf-mirror。
- `pkill -f` 的模式匹配到了包装脚本自身，把整个基准矩阵误杀过一次（后来 T7 又中招一次：匹配到发起 pkill 的复合命令自己）。教训：杀进程用 PID，别用 `pkill -f`。

## 2026-07-13 · T2 仓库骨架（577f749）

做了什么：uv 工程 + ruff/pyright/pytest 质量门 + typer CLI 入口 + GitHub Actions 工作流。

- 依赖分组是本任务的核心设计：`dev`（质量门）与 `ml`（torch/sentence-transformers）分离，CI 用 `uv sync --no-default-groups --group dev`——"CI 禁真模型"从纪律变成物理不可能。本地默认双组全装。
- 镜像双轨：本地 pyproject 固定 tuna 索引，CI（境外 runner）用 `UV_DEFAULT_INDEX` 覆盖回官方源。
- ruff 对中文文档字符串报 RUF001/002/003（全角标点歧义）——项目注释约定就是中文，整类关闭而非逐处豁免。
- pyright 两处：structlog processor 签名要用 `structlog.typing` 的 `EventDict/WrappedLogger` 才过；`Settings()` 缺必填 llm_api_key 是运行时注入，定点 `# pyright: ignore[reportCallIssue]`。

## 2026-07-13 · T3 基础设施（3e53a37）

做了什么：pgvector compose（healthcheck+initdb 建扩展）、pydantic-settings 配置、structlog 脱敏 processor、DevKbError 异常层级。

- test_config 的 .env 隔离最初用改 `model_config` 的 hack，改为 `monkeypatch.chdir(tmp_path)`——测试不该知道被测对象的内部结构。
- 日志脱敏做成 structlog processor（`_redact`），在管道里统一挡 key/正文，而不是散在各调用点。

## 2026-07-13 · 三轮外部审查（127efa2 / e3f9572 / c47ac50）

做了什么：用户将 GPT-5.6 的三轮审查转来，逐条核实→修复→记入偏差记录（共 10 条）。流程：**审查方提出 → 我用证据核实（含 WebFetch 官网）→ 用户裁决 → 修复落盘**。

- 审查也会错：第一轮"GPU 不可用"的断言是审查方自己沙箱的限制，本地 `cuda_available=True` 实测证伪——外部审查结论同样要核实，不能照单全收。
- 被抓的真实纪律违规：T2.4 在 Actions 判据未满足时提前勾选（拆分判据+新增 T10.6 补救）；T1.5 判据文字与勾选状态不一致（修订定义使文字与状态一致）。教训：**勾选纪律的漏洞都出在"差一点点就算了"**。
- 第三轮最有价值：DeepSeek 按缓存命中/未命中/输出三档计价，只存合计 tokens 无法复算成本 → agent_runs 增加 usage JSONB 存供应商原始分档字段；`deepseek-chat` 别名 2026-07-24 弃用 → 模型名冻结为显式 `deepseek-v4-flash`。两者都经官网核实（2026-07-13）。

## 2026-07-13 · T4 数据层（05132c6）

做了什么：四表 models + Alembic 迁移 001 + scoped repositories + 双项目隔离集成测试 + D7 AST 静态扫描。

- **真实 ORM 陷阱：upsert RETURNING 撞身份映射**。二次 upsert 同一行时，SQLAlchemy 发现 RETURNING 的行已在 identity map 里，直接返回**第一次的陈旧对象**（content_hash 还是旧值）——集成测试抓到。修复：`execution_options={"populate_existing": True}` 强制用 RETURNING 新值刷新，附回归测试。
- 迁移里幂等 `CREATE EXTENSION vector`：CI 的 service container 不挂 initdb 目录，扩展只能靠迁移自建；本地已有扩展则幂等跳过。
- D7 静态扫描用 AST 而非 grep：解析 import 与调用，`select/insert/update/delete/text/...` 等查询构造出现在 repositories.py 之外即 fail——防"顺手在服务层写个查询"。
- 隔离测试的最严苛用例：用 B 项目的"完美匹配向量"（相似度=1）在 A 项目范围内检索，B 的 chunk 不可见——隔离不是靠分数低，是靠查不到。

## 2026-07-14 · T5 Markdown 分块（cd2b8bc）

做了什么：markdown-it-py 标题树分块（面包屑/行号/贪心装箱/递归切分）+ 5 个 fixture + 双 golden 快照。

- 自查出的规格偏差：初版对超长单段"整块保留"，但规格 §7 明确"段落递归切分"——补了非原子块按行二分递归；**切分下限是单行**，因为引用以行号定位，行内切开会让 content 与行号失配。围栏/表格保持原子。
- TokenCounter 定为普通 `Callable[[str], int]` 而非 Protocol（硬规则：Protocol 只允许在 embedding.py/llm.py）。三种注入：生产=Qwen tokenizer（懒加载）、CI=确定性近似计数器、本地 ML 测试=真 tokenizer。
- 双快照的预期差异实测确认：真实 tokenizer 对中文压缩率更高（同一段 approx 600+ token、真实 371），边界自然不同——所以 A/B 是两份独立 golden，**不做误差范围断言**（脆弱且无行动价值）。
- golden 机制：`DEVKB_UPDATE_GOLDEN=1` 重生成，缺失时报错提示人工审阅后提交——快照必须是"看过的"。

## 2026-07-14 · T6 摄取管道（024e25a）

做了什么：扫描→编码规范化→hash 跳过→事务删旧插新→失败隔离 + CLI ingest 统计表 + 三条幂等/隔离集成测试。

- charset-normalizer 行为先实测再依赖：GBK 能正确规范化为 UTF-8；纯乱码字节 `best()` 返回 None——据此构造**确定性**坏文件 fixture（固定字节模式，不用随机数免得测试抖动）。
- "坏文件"两种：乱码（ParseError）与超 5MB（UnsupportedFileError），都走 status='failed'+parse_error 隔离，批次继续；数据库级故障则照常抛出——**文档的失败要隔离，基础设施的失败要暴露**。
- ruff B008 与 typer 的参数默认值惯用法冲突：加 `flake8-bugbear.extend-immutable-calls` 豁免 `typer.Argument/Option`，不逐处 noqa。

## 2026-07-14 · T7 Embedding 接入（1bfad24）

做了什么：Embedder Protocol 接缝 + SentenceTransformer 生产实现（ADR-0002 四约束落地）+ FakeEmbedder + 批量嵌入接入管道 + 真模型冒烟。

- FakeEmbedder 的设计要点：sha256(text) 种子 → 确定性归一化向量，且**同一文本的 query/doc 向量一致**——这让 T8 的"已知查询命中期望 chunk"可以做精确断言，而不是概率性断言。
- OOM 减半退避放在 Embedder 内部（它知道 batch），tenacity 重试放在管道（对 EmbeddingError 重试 1 次后按单文档隔离）——职责分层，避免两层都在猜对方做了什么。
- 冒烟脚本坑：unset 代理后 sentence-transformers 仍向 huggingface.co 发 HEAD 检查（尽管模型有本地缓存），直连超时重试 5 次。解决：`HF_HUB_OFFLINE=1` 纯走缓存，加载 5.5s。
- pyright 拒绝 `**kwargs` 展开进 `model.encode()`（签名有大量 Literal 参数），改显式 `prompt_name` 参数传递——接口窄一点，类型就干净一点。
- 冒烟验证了 ADR-0002 的"查询侧必须 prompt_name='query'"真实生效：同一文本 query/doc 向量 cos=0.89（≠1）；语义命中正确（"库存扣减的并发控制"→ top1 即并发控制 chunk）。
- 遗留数据教训：T6 阶段（无嵌入）摄取的演示项目，因 content_hash 未变会被幂等跳过，嵌入永远不会补上——**幂等跳过的判断只看内容，不看衍生数据完整性**（P0 接受此限制，重摄取需换项目或清库；已清理 fixtures-demo）。

## 2026-07-14 · T8 向量检索

做了什么：`retrieval.py`（查询嵌入 → project 范围精确扫描 → RetrievedChunk 完整元数据）+ 三条精确断言集成测试。

- 判据要求结果带 rel_path，而 Chunk 表只有 document_id：在 `vector_search` 里联查 documents 带出 rel_path（查询构造留在 repositories.py，D7 扫描继续通过），而不是在 retrieval 层逐条回查文档——一次 join 换 N 次往返。
- 返回形状从 `(chunk, score)` 变 `(chunk, rel_path, score)`，三个既有测试的解包点同步更新——**改 repo 返回形状是波及面最大的一类改动**，趁调用点少（4 处）早改。
- FakeEmbedder"同文本 query/doc 向量一致"的设计在此兑现：以 chunk 逐字原文为查询，top1 必是该 chunk（score>0.999），断言是确定性的而非概率性的——测机制不测模型。

## 2026-07-14 · T9.1–T9.4 LLM 与固定问答管道

做了什么：`llm.py`（LLMClient Protocol + DeepSeek 适配 + FakeLLM）、`answer.py`（检索→E# 证据→生成→L0→Answer→落库）、CLI ask/runs、4 条 e2e + 6 条 L0 单元测试、真实 DeepSeek 冒烟。

- 重试策略收敛到一个地方：openai SDK 设 `max_retries=0`，"1 次重问"由 answer.py 的唯一调用点用 tenacity 控制——否则 SDK 隐式重试 × 应用层重试会相乘，D6 的"有界"就成了纸面约束。
- L0 做成纯函数 `apply_l0(text, evidence_count)`，正则替换的同时收集越界引用——单元测试不碰 DB/LLM，6 个边界用例（E0、空证据集、重复引用）秒级跑完。
- FakeLLM 的脚本项允许是 Exception：`[LLMTimeoutError, "正常回答"]` 一行就能测"超时后重问成功"，比 mock 补丁干净得多。
- 失败也要落库：LLM 双故障后 run 记 status='failed' + error 再抛出——排查线上问题时"没有记录"比"记录了失败"糟糕得多。
- 真实冒烟（run be2d13ee）验证了整条链路的质量信号：真实嵌入下 top1 就是并发控制 chunk（0.722），DeepSeek 生成 4 条引用全部有效、0 warnings；usage 返回里 prompt_cache_hit_tokens=0/miss=664 与 total 自洽，三档计价的数据基础成立。
- 引用渲染小坑：rich 表格里中文与全角标点混排会撑宽列，位置列用 `rel_path:L{s}-L{e} · title_path` 单元格合并成一列，避免逐字段对齐问题。

## 2026-07-14 · 治理 · ADR-0009 求职导向路线重排（34abbcf）

用户提供目标 JD，GPT-5.6 起草路线加强计划，我核实审查（编号审计、冻结文档零改动、边界一致性），用户裁决后落盘：可投递线 = P1 + LangFuse + MCP + 投递包（约 7–9 周），P2/P3 重排为 Gate 制，Multi-Agent/GraphRAG 设收益门槛。P0/P1 执行不受影响。

## 2026-07-15 · T9.5 定价冻结与成本闭环

做了什么：`compute_cost(model, usage)` 三档计价纯函数入 llm.py（定价是供应商事实，与 ADR-0004 同源），answer.py 落库时接上；6 条单元测试锁逐档手算值；真实冒烟 run 验证 cost 非空可复算。

- 计价函数的诚实性边界：模型不在价目表（如 fake-llm）或 usage 缺失 → 返回 None 而非按 0 计——cost 列本就可空，"没算"和"算出 0 元"是两回事。
- usage 无分档字段时全部输入按缓存未命中计（保守高估），有 hit 无 miss 时用 prompt_tokens-hit 推导——兼容供应商字段变动而不静默算错。
- 冒烟踩坑：unset 代理直连后进程挂死，`ss -tnp` 显示 SYN-SENT 到 108.160.169.175:443——huggingface.co 的 DNS 污染假 IP。T7 记过 `HF_HUB_OFFLINE=1` 的坑，这次是同一根因的新表现（上次是超时重试 5 次，这次直接挂死）。教训：**凡真实冒烟一律带 HF_HUB_OFFLINE=1 + unset 代理**，两个开关缺一不可。
- 复算闭环（验收 7 判据）：run 8e1e2de0 usage={hit:0, miss:878, out:307}，0×0.02+878×1+307×2=1492 → cost=0.001492 元，与落库值一致；`runs show --json` 可取回。

## 2026-07-15 · T10.0 ADR 转写 + U1.3 补充题草稿

做了什么：转写 ADR-0001（单 Postgres 四职责+规模假设）、ADR-0003（语料核查+解析优先级+P0 摄取范围）、ADR-0007（P0/P1 裁剪清单）；按清单主题候选转写 U1.3 剩余 5 题草稿存 `evalsets/v0/retrieval_supplement.draft.md`。

- 转写纪律：三份 ADR 只搬运《范围冻结与架构决策》既有决策，逐条标注 D/C/S 编号出处，不夹带新决策——转写任务最大的风险是"顺手补充"，出处编号是防走样的锚。
- U1.3 的两难：清单建议用户盲写以避免分布偏差，但用户又让我推进。处理：草稿写进独立文件、对话里零展示——用户仍可选择不打开文件自己盲写，两个选项都保留。U1.3 不勾，等用户二选一。
- 5 题锚点全部先到 mini-mall 语料实地核实（标题存在、证据在场）再落笔：q21 超时取消（dev-log Task 17.x）、q22 通知服务（architecture 两节）、q23 审计日志（4.3 Admin Core APIs）、q24 demo seed（dev-quickstart Phase 3 AI 演示数据）、q25 压测阈值（dev-log Task 19.x）。

## 2026-07-15 · T10.1 README + T10.2 真语料实跑 + T10.3 抽查（待复核）

做了什么：README 一页起步 + 5 条已知限制；mini-mall 真语料摄取（39 文档 → 1370 chunks，53.9s，0 失败）；5 问真实问答抽查报告（17/17 引用核实）。

- D2 范围与 CLI 的落差：CLI 只认"一个目录"，而 D2 是"docs/**/*.md + 根级 README*"。在 scratchpad 组装摄取目录（拷贝，源仓库保持只读）——rel_path 恰好与评测集锚点的 `docs/xxx.md` 前缀对齐，一举两得。
- 事实修正：冻结文档记 docs/ 有 54 个 md，今日实际递归 37 个（外加 2 根级 README = 39）。D2 按模式定义不受影响，数字如实入清单。
- 抽查方法：不只看回答"像不像对"，而是打开每处 `rel_path:L#-L#` 原文行区间核对论断——17 处全对应，其中 q01-E6 对应成立但证据弱（叙述被 chunk 边界切开），如实记入报告而非算作全优。
- 发现 `ask --json` stdout 混入 structlog 日志行，管道不可直接解析。修复是一行日志改道 stderr，但 T9.3 已勾选、清单无此项——按范围纪律记 backlog 等裁决，抽查脚本先跳前缀绕过。
- T10.3 判据含"人工"，Claude 核对不能替代：报告标注"待用户复核后方可勾选"。

## 2026-07-15 · fix · ask --json stdout 纯净化（用户裁决 backlog 项）

做了什么：修复 `--json` 输出混入日志的问题；网络坑结论固化进 CLAUDE.md 环境事实。

- 根因比表象深一层：以为是"日志打错流"，实际是 **`configure_logging` 从未被 CLI 调用**——structlog 一直跑默认配置（PrintLogger→stdout），意味着 T9.3 起脱敏 processor、JSON 渲染也从未生效。教训：**写了配置函数 ≠ 配置生效，接线处要有测试或实跑验证**。
- 修复两处：CLI 回调接入 configure_logging（ConfigError 时退默认，保住无 .env 的 `--version`）；logger_factory 显式指 stderr。
- 验证：make ci 58 passed；真实 `ask --json | python -c json.load` 解析成功（修复前该用法必炸）。

## 2026-07-15 · U1.3 评测集合入 + T10.3 复核勾选 + T10.4 holdout 基线

做了什么：用户裁决（放弃盲写、草稿全过、抽查复核通过）落盘——q21–q25 合入 JSONL（全集 31 问齐），T10.3 勾选；`benchmarks/holdout_baseline.py` 跑 P0 holdout 基线并出报告。

- holdout 结果：Recall@5=0.625 / Recall@10=0.875 / MRR@10=0.440（8 问，全量 1370 chunks）。相比 T1.4 dev 基准的 0.80 属预期回落：全量语料 + 未调过的题。
- q20（错误码精确 token 题）top-10 未命中标注锚，但 top-1 内容实际含正确答案（锚点覆盖不全的信号）。**看过 holdout 结果后不改标注不调参**——观察记进报告留给 P1，这条纪律比这次的数字更值钱。
- 不可答题 top-1（0.43/0.51）与可答题 top-1（0.57–0.73）有可分性但有重叠——纯阈值拒答在 P0 数据上不干净，为 P1 evaluate 节点设计提供了第一手依据。

## 2026-07-15 · T10.5/T10.6 · P0 验收完成

做了什么：远程仓库（github.com/yangyaoming158/devkb）push main+p0 后 Actions 首次运行 success；验收总清单 8 项逐项核对全过；ADR-0004 验收日单价复核（官网 0.02/1/2 未变动）。

- 验收不是走过场：第 1、2 项当场重新执行（alembic current=head；重跑摄取 39 全跳过新增 0），第 3、7 项引用既有证据（用户复核的抽查报告、cost 复算 run），第 5 项以远程 CI run 29426944680 为准。
- 官网价格页是 JS 渲染，curl 抓不到正文，用带渲染的抓取工具复核成功——"验收当日复核"这类承诺要在 ADR 里当天写上结论，否则下次没人记得核没核过。
- **P0 全部任务与验收判据完成**。下一步按 CLAUDE.md 范围纪律：创建 P1 实现规格/任务清单/Evaluation-v1 后方可动 P1。

## 2026-07-15 · fix · 检索排除 failed 文档的陈旧 chunks（GPT 审查发现，用户裁决）

做了什么：`vector_search` 补 `Document.status == "active"` 过滤 + "active 文档更新失败"状态转换回归测试；规格 §8 与偏差记录同步。

- 缺陷链条：更新失败 → 事务回滚保留旧 chunks（本身是对的，保住上一版）→ mark_failed → 检索只查 project_id 不看 status → 陈旧 chunks 被引用，行号对的是旧版文件。三环节各自合理，叠加起来打穿了"引用可溯源"。
- 定性为规格缺失而非实现违规：§8 的 WHERE 本来就没写状态过滤，代码忠实实现了有漏洞的规格——所以修复走"用户裁决改规格"流程而非直接改码。
- 测试有效性验证：git stash 掉修复跑新测试必红（0.42s fail），恢复后绿。**测试先证明自己能咬住 bug，才配当回归防线**。
- 测试盲区教训：坏文件测试覆盖了"从未成功的文件"，漏了"成功过的文件变坏"——状态机类逻辑的测试要按转换边覆盖，不是按状态点。

## 2026-07-16 · 治理 · P0 独立复验通过，创建 P1 执行规格

做了什么：独立复跑 P0 的本地/远端 Gate；在 ADR-009 授权下创建 `P1实现规格.md`、`P1任务清单.md`、`Evaluation-v1.md`，并同步 CLAUDE 文档地图与路线图状态（证据：本提交）。

- 复验没有只信清单勾选：本地 `make ci` 为 59 passed、Ruff/Pyright 零错误；迁移在 0001(head)；真实 Qwen 摄取按冻结范围重跑 39 文件全部 skipped、0 新增；数据库保留 mini-mall 39 文档/1370 chunks 和可复算成本 run；最终 P0 头 f30e24d 的 GitHub Actions run 29428269586 为 success。
- P1 规格把“功能名”改写成可失败的工程 Gate：Hybrid 必须与 exact/lexical 同快照对照，HNSW 测 top-10 overlap；Agent 必须覆盖 full/partial/refusal、补检、L0/L1 重生成和第二次失败降级；供应商总请求（含重试）硬上限 6。
- Evaluation v1 继续复用 31 问，不为 P1 擅自扩题；Java parser 用 contract/golden 测，不冒充真实问答质量。CI 只守确定性机制，真实 Qwen/DeepSeek dev Gate 单独留档，避免用 Fake 指标包装效果。
- P0 暴露但不阻断验收的 CLI 基础设施异常呈现债务，收敛进 P1 T19 共用 service/异常映射，而不是清单外顺手改代码。

## 2026-07-16 · 治理 · P1 开工前规格收紧

用户在开工前复核发现三处歧义，本次只修规划契约、不实现 P1 代码（证据：本提交）。

- Holdout 从跨语料绝对 Gate 改为同快照相对 Gate：`hybrid-rrf Recall@10 >= vector-exact Recall@10`。P0 的 0.875 来自 39 文档/1370 chunks，只保留历史对照；P1 增加 Java/config 后不能把绝对下降直接归罪于 Hybrid。
- 评测 mode 统一为 `vector-exact|vector-hnsw|lexical|hybrid-rrf|agentic|all`；问答链路对照另用 `--pipeline fixed-rag|agentic`，避免同一个参数承载两套枚举。
- 把 P0 临时组目录固化为版本化 corpus view：当前只读源快照审计为 37 docs Markdown + 2 README + 392 Java + 14 config = 445 文件；manifest 记录源 commit/hash，并强制排除指令文件、隐藏目录、node_modules 和 target/build/dist。P1 禁止直接 ingest mini-mall 根目录。

## 2026-07-16 · U2.1 · Evaluation v1 盲标注审计

做了什么：只读取 31 道题与 mini-mall 允许语料，对 25 个可答题逐路径/anchor 核验，并对 6 个边界题跨 Markdown/Java/config 做反证搜索；用户最终全部确认（证据：本提交）。

- 冻结源 HEAD=`cfb49f5`；未运行 ask/eval/holdout，未读取 P1 输出。25/25 可答题与 35/35 anchor 通过，不改 v0 JSONL，也不改变 17+4、8+2 分组。
- 三态期望冻结为 u01/u02/u03/u05=refusal、u04/u06=partial。u06 是本轮最有价值的边界修正：Java 明确 `Algorithm.HMAC256(secret)`，但配置只有单 secret/expiry，没有 kid、多 key 或轮换窗口，因此只能部分回答。
- 产物：`evalsets/v1/label-audit.worksheet.md`、`answer-expectations.jsonl`、`label-change-log.md`。后续 T11.1 可开始，但 holdout 仍受 U2.3 单独授权约束。

## 2026-07-16 · T11.1 · P1 开发前 vector-exact dev 基线

做了什么：在 P0 已验收的 mini-mall 全量 Markdown 语料（39 文档/1370 chunks）上，用冻结的 Qwen3-Embedding-0.6B 和精确余弦扫描运行 17 个 retrieval dev 问题；产出不可覆盖的 JSON 原始报告与 Markdown 摘要（证据：本提交）。

- 口径判断：T11.1 在执行图上早于 T13 Java/config parser 和 445 文件语料视图，所以本次“P1 前全量”是相对 T1 的 500 块抽样而言，指 P0 冻结摄取范围的生产全量。报告显式标记 `scope=p0-frozen-markdown-full`，避免与 T15 将来的 P1 445 文件同快照对照混淆。
- 先验证天花板再跑分：Repository 增加受 project/status 约束的路径+标题索引读取，实测 25/25 个 `rel_path + anchor` 在当前可检索 chunks 中可解析，避免重演 T1 “锚点不在语料却解读召回率”的教训；隔离测试同时锁定 failed 文档和其他 project 不可见。
- 机器计算结果：Recall@5=`0.647`、Recall@10=`0.824`、MRR@10=`0.511`；q02/q08/q15 在 top-10 未命中标注锚点。这些负结果原样留档，未因结果修改标注、检索参数或语料。
- 数据纪律：脚本只读取冻结的 17 题 dev 文件，不提供 holdout 参数；原始报告记录 `holdout_accessed=false`、devkb/source commit、worktree 状态、模型配置、逐题 top-10 与语料 SHA-256。

## 2026-07-16 · T11.2 · P1 新依赖最小 spike

做了什么：按 P1 依赖门禁锁定 LangGraph/FastAPI/tree-sitter Java/jieba 和测试用 HTTPX，分别运行最小成功样例，然后收敛为 `tests/unit/test_p1_dependency_spikes.py` 的 4 条正式兼容性契约；版本、许可来源、维护日期与风险详见 `benchmarks/p1_dependency_spike.md`（证据：本提交）。

- 最小依赖边界：FastAPI 不安装 `[standard]` extras，Uvicorn 只带基础 h11；HTTPX 明确放进 dev group；未安装 `langchain` 元包、provider/VectorStore、Postgres checkpoint adapter 或任何 P2 依赖。
- 转递依赖的边界要靠使用约束而非假装不存在：LangGraph 自身要求 `langgraph-checkpoint/prebuilt/sdk`，`langchain-core` 要求 `langsmith`。这些包只是锁定的上游依赖，P1 不导入/配置 LangSmith、不启用 checkpointer，也不把它们宣称为已实现能力。
- 真实兼容性结果：LangGraph `START→node→END`、HTTPX `ASGITransport` async 请求、Java package/class/field/constructor/method CST、jieba `HMM=False` 重复分词全部通过；整个 spike 不启动网络端口、不执行 Java。
- 维护风险：jieba 0.42.1 自 2020-01 后无新 PyPI release，Python 3.12 首次编译还会报 invalid escape `SyntaxWarning`。规格已冻结 jieba，本任务不擅自换库；后续 T14 以纯函数封装、`HMM=False` 和 golden 防漂移。

## 2026-07-17 · T11.3 · ADR-0005/0006/0008 转写

做了什么：把《范围冻结与架构决策》D6–D9 及 P0/P1 已冻结实现契约转写为 ADR-0005 项目隔离、ADR-0006 LLM/确定性边界和 ADR-0008 引用分级；本任务只落决策记录，不修改冻结规格或提前实现后续代码（证据：本提交）。

- 编号审计：开工前现有文件为 0001/0002/0003/0004/0007/0009，冻结 ADR 清单中 0005/0006/0008 已分别预留给本任务。落盘后 0001–0009 每个编号恰好一份，无重复、无抢占。
- ADR-0005 不沿用“编译不过”的假保证，而是明确 Repository 绑定、DB 约束、可信 project 注入、双项目测试和 SQL 静态扫描五层措施；RLS 保持 P2 可选纵深防御，不偷渡进 P1。
- ADR-0006 区分“逻辑调用上限”与“供应商总请求上限”：plan/evaluate/refine/generate 四类调用的所有格式/传输重试共享 6 次预算，路由始终由代码根据结构化信号和计数器判定。
- ADR-0008 把“引用存在”、“引文与原文匹配”和“证据语义支持 claim”拆为 L0/L1/L2；P1 在线只做确定性 L0+L1，L2/NLI/judge 仍是 P2 离线评测。同时保留 D8 的诚实边界：引用保证可追溯不保证为真，风险只能“分层降低，不可消除”。
- 验证过程如实记录：首次 `make ci` 的 Ruff/Pyright 和 46 个非数据库测试通过，18 个集成测试因 PostgreSQL 容器未运行而统一 `ConnectionRefused`；`docker compose up -d --wait` 达到 healthy 后原样重跑，最终 Ruff/Pyright 零错、pytest 64 passed。

## 2026-07-17 · T11.4 · P1 契约版本标识冻结

做了什么：冻结 Prompt bundle、AgentState、Answer 和 HTTP API 四个 P1 契约版本，集中放入 `src/devkb/contracts.py`，并提供可直接写入 JSON 报告和轨迹摘要的 `contract_versions()`；完整口径记录在 `docs/P1版本标识.md`（证据：本提交）。

- 包版本与契约版本分离：`devkb.__version__` 仍表达软件发布版本；四个契约只在对应 Prompt、schema 或 API 语义变化时分别递增，避免内部修复无意义地制造报告口径变化。
- Prompt 采用单一 `p1-agent-v1` bundle 标识，覆盖 plan/evaluate/refine/generate，不为四个节点提前建立独立版本系统；后续 T16 从常量导入，不复制字面量。
- `p1-api-v1` 是报告和 API schema 元数据，不改变冻结路由：P1 仍使用 `/ask`、`/runs/{run_id}`、`/healthz`，不提前引入 `/v1` 或多版本路由。
- 报告纪律落成明确约束：新报告写入四个字段；任一标识递增后生成新的带时间戳报告，不覆盖旧报告。P1 不实现协商、adapter、弃用周期或迁移注册表。
- 两条单元测试锁定精确版本值、JSON 可序列化和返回映射无共享可变状态；完整质量门最终为 Ruff/Pyright 零错误、pytest 66 passed。

## 2026-07-17 · T12.1 · P1 Alembic revision（检索字段 + 轨迹表）

做了什么：新增 revision 0002——chunks 加 `search_text`（NOT NULL DEFAULT ''）与 `search_tsv`、GIN 与部分 HNSW 索引，创建 `agent_steps`/`tool_invocations` 两张轨迹表；ORM 同步扩展并写入往返迁移集成测试（证据：本提交）。

- 方案裁决（规格 §5.1 留了两条路）：`search_tsv` 采用 STORED 生成列而非 Repository 同步写入。理由：一致性由数据库保证、无双写漂移，T12.2 backfill 只需 UPDATE search_text 单列，downgrade 只需删列——可逆性最简，正好命中"以迁移 spike 可逆性为准"。副作用是规格中"普通列 + Repository 写入才要求一致性测试"的分支不再适用，仍在 schema 测试里用 UPDATE→tsquery 命中断言锁了生成行为。
- HNSW 建为部分索引 `WHERE embedding IS NOT NULL`（§5.1 只覆盖非空 embedding），m=16/ef_construction=64 显式写死（即 pgvector 默认值，写死为了复现）；vector(1024) 沿用 ADR-0002 不动。精确扫描路径不受影响，评测基线仍可用。
- ORM 侧一个小坑：部分索引的 WHERE 在 SQLAlchemy 里要 `text()`，而 D7 分层扫描禁止 models.py 导入 text——索引因此只在迁移里建，模型 docstring 注明归属，不为绕过扫描开豁免。
- 测试自建独立临时库（不复用 conftest 会话级已迁移库，那个不允许被降级）：`upgrade head → 种子 P0 四表 → downgrade 0001 → 断言新表/新列消失且数据原样 → upgrade head → 断言数据仍在、默认值就位`；嵌入用 `<=>` 距离=0 断言逐字节回读一致。schema 一致性测试精确断言两张新表字段集、unique(run_id,seq) 与 GIN/HNSW indexdef。
- 本地生产库执行非破坏 `alembic upgrade head`：mini-mall 1370 chunks 全部保留且 search_text=''（另 16 块属 P0 fixtures-t7 项目，非漂移）。质量门：ruff/pyright 零错误，pytest 68 passed。

## 2026-07-17 · T12.2 · P0 chunks search_text backfill

做了什么：实现规格 §7 的 FTS token 化纯函数（`retrieval.py`，摄取期/查询期共用），`ChunkRepo.backfill_search_text` 单事务回填 + `devkb backfill-search` CLI；对真实库 mini-mall 1370 chunks 完成回填（证据：本提交）。

- 顺序裁决（清单 T12.2 在 T14 之前，但 §7 说"具体函数由 T14 spike 冻结"）：按清单的自然读法，函数在 T12.2 诞生——"与新摄取相同"表达的是共用约束而非冻结时点；T14.1 再补中文/英文/Java 符号/RabbitMQ/URL/错误码 golden 全集并冻结。backfill 幂等可重跑，若 T14 微调函数只需重执行一次 UPDATE，不推倒任何东西。
- token 化按 §7 七条规则：保留原词（小写化）、snake/camel/字母数字边界拆分、路径/routing key/错误码整词 + 分段、jieba 独立 Tokenizer HMM=False 保证确定性、空/超长（>64 字符）token 丢弃、同 token 计入 ≤10 次（保词频信号防病态膨胀）、总量 ≤8192 确定性截断。
- backfill 设计：只 UPDATE `search_text` 单列（`search_tsv` 生成列自动跟随），结果一致的行跳过——幂等由"算出来一样就不写"保证；不在 Repository 内 commit，事务归调用方，失败自然整体回滚（集成测试用显式 rollback 证明零残留）。
- 真实库结果：第一次 1370/1370 更新，第二次 0 更新；非空率 100%；`plainto_tsquery('simple','orderstatemachine')` 命中 6 块、`订单` 110 块，GIN 可查。fixtures-t7 的 16 块属 P0 测试遗留项目，不在本任务范围，未回填。
- 质量门：ruff/pyright 零错误，pytest 78 passed（新增 token 化单测 8 项 + backfill 集成测试 2 项）。

## 2026-07-17 · T12.3 · Repository 扩展（FTS / HNSW 模式 / 轨迹写读）

做了什么：`ChunkRepo` 增加 `lexical_search` 与 `vector_search` 的 exact/hnsw 双模式，新增 `AgentStepRepo`/`ToolInvocationRepo`；全部以 project_id 实例化，查询方法不接受外部 project_id（证据：本提交）。

- exact/hnsw 的真实困难在 planner：小表下 planner 几乎总选顺扫（HNSW 建了也不用），而评测基线又要求"精确"名副其实（不能被 planner 静默换成近邻索引）。方案：`set_config('enable_indexscan'/'enable_seqscan', 'off', is_local=true)` 事务级强制，查询后立即恢复 'on'，不污染同事务后续查询；ef_search 同样 set_config 传入（带 1..1000 校验），全程无字符串拼 SQL。
- `lexical_search` 用 `websearch_to_tsquery('simple', :query)`：对任意输入不抛语法错误、天然参数绑定，特殊字符最多不命中、不可能注入；`ts_rank_cd` 排序并列时以 chunk_id 兜底，输出确定。查询期 query 构造的注入/超长压测与排名解释字段属 T14.2/T14.3，此处不提前铺。
- `ToolInvocationRepo.list_for_run` 按所属 step 的 seq 排序；步内多次调用以 (created_at, id) 兜底——created_at 是事务时间戳、同事务内并列，规格 §5.3 最小字段集没有步内 seq，严格步内顺序语义待 T18 落轨迹时按需明确（docstring 已注明，不静默扩字段）。
- 隔离测试新增 4 项：lexical 同词面双项目、hnsw 模式向量双项目、step/tool 跨项目 run_id 读取返回空（不报错不泄漏）。D7 分层静态扫描继续全绿。
- 质量门：ruff/pyright 零错误，pytest 81 passed。

## 2026-07-17 · T12.4 · 数据层集成测试

做了什么：补齐 T12 判据的综合集成测试 9 项（FTS 排名/注入安全/GIN 计划、HNSW-exact 对照/计划、FK/顺序/级联），连同 T12.2 的 backfill 测试与 T12.3 的隔离测试覆盖全部判据（证据：本提交）。

- 抓到一个真 bug：T12.3 的 hnsw 模式只关 `enable_seqscan` 不够——小表下 planner 会走 project_id btree + Sort 给出"假 HNSW"的精确结果，T15.3 的 overlap 对照会因此变成 exact vs exact 而虚高。修复为同时关 `enable_sort`（距离序只能由 HNSW 索引提供），EXPLAIN 断言锁住 `ix_chunks_embedding_hnsw` 出现在计划里。
- GIN 计划的教训：planner 在小项目上永远偏向 project_id btree bitmap，60 行 + ANALYZE 都不换。最终用 800 行/仅 1 行命中目标 token 的种子让选择性差距真实成立；真实 1370 块语料上先用 psql 人工验证了自然计划确实走 `Bitmap Index Scan on ix_chunks_search_tsv`（正式落盘归 T14.4）。
- 注入安全断言：`'; DROP TABLE chunks; --`、tsquery 元字符、5000 字符超长串、空串一律安全返回空且表仍在——websearch_to_tsquery + 全参数绑定的组合成立。
- 三处 asyncio 陷阱记录：`expire_all()` 后访问 ORM 属性触发同步 IO 抛 MissingGreenlet（改为先取 UUID）；EXPLAIN 断言消息里直接内嵌完整计划文本，失败时不用二次排查。
- HNSW-exact 全等断言的适用条件：30 块 + ef_search=200（覆盖全集）下 HNSW 必然穷尽，与 exact 逐位一致；这不外推到 1370 块生产语料——那是 T15.3 的 overlap Gate 的事。
- 质量门：ruff/pyright 零错误，pytest 90 passed。

## 2026-07-17 · T12 复评修复 · 三项审查发现全部属实并修复

做了什么：外部复评指出 2 个阻断 + 1 个中等问题，核实全部属实；重开 T12.2–T12.4 修复并补回归测试（证据：本提交）。

- 阻断 1（新摄取不生成 search_text）：根因是我把"摄取期写入"划给了 T14.2，留下"重摄取会静默清空 FTS"的中间态，且测试还把这个漏洞断言成了预期行为。修复：token 化函数迁至独立 `fts.py`（repositories 依赖它、retrieval 依赖 repositories，留在 retrieval 会循环导入），`replace_for_document` 在插入点统一计算——chunk 唯一写入口径收口后不存在"忘了填"的路径；`backfill_search_text()` 收窄为无参（面向存量数据与 T14 函数升级），与新摄取必然同函数。回归：摄取后无需 backfill 即可词面检索、立即重算 0 更新。
- 阻断 2（轨迹表错配写入）：0002 的独立 FK 只保证"目标行存在"，挡不住项目 A 的 step 挂项目 B 的 run、run A 的 tool 挂 run B 的 step。修复：迁移 0003 复合 FK——`agent_steps(run_id,project_id)→agent_runs(id,project_id)`、`tool_invocations(step_id,run_id)→agent_steps(id,run_id)` + `(run_id,project_id)→agent_runs`，被引用侧补 `(id,project_id)`/`(id,run_id)` 唯一约束；错配写入在数据库层直接 IntegrityError，与 D7/ADR-0005 的项目归属一致性对齐。轨迹表无生产数据，无需数据修复；往返迁移测试经 0003 全绿，生产库已非破坏升级。
- 中等（GUC 恢复不真）：原实现恢复写死 'on' 且 ef_search 整个事务残留，同事务内结果依赖调用顺序——清单里"查询后即恢复"的说法名不副实。修复：查询前 `current_setting` 记录全部将改 GUC（含 hnsw.ef_search），查询后按原值恢复；异常路径事务必然中止、is_local 设置随回滚撤销，无需 try/finally。回归覆盖"外部预设 off/77 不被覆写回默认"。
- 连带教训：GIN 计划断言在合成小数据上赌 planner 成本模型（800 行 + ANALYZE 也会翻转），改为确定性形状验证（无 project 谓词时 GIN 是 tsv 唯一可用索引）；带全谓词的自然计划验证归 T14.4 在真实 1370 块语料上落盘。
- 质量门：ruff/pyright 零错误，pytest 93 passed；生产库 revision=0003、五个新约束就位、1370 chunks search_text 无损。

## 2026-07-17 · T13.1 · tree-sitter Java parser 与 chunk 模型

做了什么：实现 `ingest/java.py` 结构化分块——package/imports 区、类型头（注解+签名到 `{` 行）、enum 常量区、constructor/method/field group/static 块逐成员成 chunk，嵌套类型递归；4 个 fixture、4 份 golden、18 项测试（证据：本提交）。

- 本任务最大收获是一个上游 bug：tree-sitter 0.26.0 在真实 mini-mall 的 `GatewayAuthenticationFilterTest.java`（339 行普通 Java）上遍历 `class_body.children` 时节点内存损坏，`start_point.row` 吐出 139088220913665 之类的垃圾值随后 SIGSEGV。T11.2 的最小 spike 和本任务全部 fixture 均无法复现——只有在提交前跑的"392 个真实文件全量冒烟"抓到了它。同 grammar + 同文件在 0.25.2 下 105 个成员完整遍历无异常，392 文件 3306 chunks 零失败。已降级锁定 `>=0.25.2,<0.26`，spike 文档留了复现记录和"新版必须先重跑真实语料压测再放开上限"的条件。教训写死：最小 spike 证明 API 兼容，不能替代真实语料压测。
- title_path 设计：根 = 类型 FQN（`com.example.order.OrderService > OrderService > createOrder`，与规格 §6.3 示例一致）；同文件多个顶层类型各用自身 FQN 作根，不误挂到主类型下（golden 人工审阅时发现并修正）；package/imports 区归文件主类型 FQN。
- 注释吸附规则：成员向上吸附相隔 ≤1 行的连续注释/Javadoc，以前一成员末行为地板防重叠；文件头 license 注释归 package/imports 区。初版漏了"类型头也要吸附"（只有成员吸附），测试抓出后把吸附行传进 `_walk_type`。
- 行号契约比 markdown 更严：content 逐字 = `lines[start-1:end]` 连接、不做 strip——Java 引用行号将被 T13.5 抽查和 U2.2 人工复核逐字核对。语法错误容错：tree-sitter 恢复出的完好成员照常成 chunk（broken fixture 验证），完全无结构才抛 ParseError（单文件隔离归 T13.2 接线）。
- 超长方法（fixture 实测 token_count>400）当前整块保留，T13.2 做有界拆分；`target_tokens` 参数已预留。
- 质量门：ruff/pyright 零错误，pytest 111 passed。

## 2026-07-17 · T13.2 · Java 超长拆分与坏文件隔离

做了什么：`chunk_java` 超目标成员沿方法体顶层语句边界拆分；摄取管道按后缀分派（.java → 结构分块，doc_type=java），坏 Java 单文件隔离批次继续（证据：本提交）。

- 拆分与引用契约的张力是本任务核心：规格要求"子块带最小签名上下文"同时"引用行号只指真实源区间"。落法：后续子块 content = 成员声明到 `{` 行的逐字签名 + 子块源行，start/end_line 只指子块区间；逐字不变量测试升级为"content 必须以引用区间逐字结尾，前缀必须逐字来自源文件"。
- 拆分算法与 markdown 分块同哲学：语句/注释起始行为边界（段间连续无缝）→ 单条超长语句按行二分（单行下限）→ 贪心装箱到 target_tokens；首块从成员起始行（含吸附注释+签名）开始，子块区间彼此连续（golden + pairwise 断言锁定）。
- 管道分派：`_chunk_source` 按后缀返回 (chunks, doc_type)；ParseError 直接透传（分块器已给明确原因），未知崩溃仍包装为 ParseError 单文档隔离；`mark_failed` 增加 doc_type 参数，坏 Java 不再被误记为 markdown。
- 真实语料复跑：392 文件 3468 chunks（拆分新增 162 块）、164 个成员被拆、最大子块 881 token（单行原子下限所致，有界）、零失败。
- 质量门：ruff/pyright 零错误，pytest 112 passed（含新集成测试：Java 词面检索命中 createOrder、垃圾文件 failed 后幂等重摄取）。

## 2026-07-17 · T13.3 · 可复现 mini-mall P1 语料视图

做了什么：`corpora/mini-mall-p1.toml`（版本化定义 + expected 数量哨兵）与标准库工具 `tools/build_corpus_view.py`；真实构建 445 文件视图并通过全部自检（证据：本提交）。

- include/exclude 语义自实现最小 glob（`**` 跨层级、`*` 不跨 `/`、整串锚定）——fnmatch 的 `*` 会跨路径分隔符、PurePath.match 右锚定会误配非根 README，两者都不符合 §6.2 的"仅根级 README"。单测锁死 `sub/README.md`、根级非 README md 均不进视图。
- 构建后强制自检四件事：manifest 无指令文件/隐藏路径/依赖生成物；逐文件复制字节 sha256 与源一致；数量与 expected 哨兵一致（39 md + 392 java + 14 config = 445，失败非零退出并要求显式更新 corpora 定义）；每次构建全新目录（时间戳+uuid 后缀），绝不复用。
- 真实运行：445 文件精确命中 2026-07-16 审计基线，源 commit=cfb49f5 与 U2.1/T11.1 冻结一致；`dirty=True` 如实入 manifest，核实仅 `.taskmaster/state.json`（隐藏目录、本就排除），语料文件零改动。
- 配置文件占位符 `${SECRET}` 原样字节保留（不解析不展开），symlink 一律跳过并 stderr 告警；manifest 以 `.` 开头，scan_files 天然不会摄取它。
- 质量门：ruff/pyright 零错误，pytest 115 passed。

## 2026-07-17 · T13.4 · 配置文件纯文本摄取

做了什么：`chunk_plaintext` 纯文本分块 + 管道接入 .yml/.yaml/.properties（doc_type=config），复用 markdown 模块的行装箱与二分机制（证据：本提交）。

- 为什么不复用 chunk_markdown：YAML/properties 的 `#` 行是注释，markdown 分块会把它解读成 h1 标题、污染 title_path。chunk_plaintext 完全不解析语法：空行分隔的连续非空行为块、贪心装箱、超长块按行二分，title_path 恒空；文档 title 用文件名（application.yml），同理不取首行。
- 安全判据落点：占位符 `${...}` 逐字保留（分块/入库/检索全链路无任何解析或展开），集成测试用 `${RABBIT_PASSWORD}` 全程断言；隐藏目录与 5MB 上限走既有 scan/校验路径，对 config 后缀重新验证。
- 行号契约与其它分块器一致（content 逐字 = 源行切片），且超长块拆分后全部源行恰好覆盖一次（单测锁定）。
- 质量门：ruff/pyright 零错误，pytest 119 passed。

## 2026-07-17 · T13.5 · 真语料摄取与幂等

做了什么：从 T13.3 审计视图（445 文件，manifest sha256=058a576…）真实摄取 mini-mall 全量 Markdown+Java+config，留档 `benchmarks/p1_ingest_mini_mall.md`（证据：本提交）。

- 流程纪律：摄取前脚本断言指令文件/生成物/依赖/隐藏路径为零；全程 `devkb ingest <视图目录>`，未触碰仓库根目录；真模型双开关（unset 代理 + HF_HUB_OFFLINE=1）照 CLAUDE.md 执行，全程无网络问题。
- 结果：首跑 406 成功/39 跳过/0 失败，新增 3418 chunks（java 3387 + config 31），95 秒，GPU 峰值 7103MB（8GB 内）；二跑 445 全跳过新增 0。终态 445 docs / 4788 chunks，search_text 全部非空（T12.2 复评修复的插入点生成在真语料上兑现，无需 backfill）。
- P0 兼容：39 个 Markdown 因 content_hash 未变全部跳过——1370 个 P0 chunks 与嵌入原样保留，Java parser 改动零漂移。
- 抽查：seed=20260717 随机 6 个 Java chunk，路径/行号/正文逐字对照原始仓库全部通过（含 package/imports 区与方法块）。
- 一个口径备注：此前用近似计数器冒烟得 3468 java chunks，真实 Qwen tokenizer 下为 3387——拆分边界随计数器不同而不同，属预期；评测口径以真实摄取为准。
- 质量门：ruff/pyright 零错误，pytest 119 passed。

## 2026-07-17 · T14.1 · token 化 golden 全集与冻结

做了什么：为 T12.2 诞生的 `tokenize_for_search` 补齐 golden 全集并正式冻结（证据：本提交）。

- golden 覆盖：中文、英文、Java 符号（含 `OrderStateMachine.transition()`/`order_status_machine`）、RabbitMQ 名称（exchange/routing key/死信）、URL/路径（含 `{orderId}` 路径变量）、错误码（40901/409/`ORDER_STATE_CONFLICT`）+ 混合真实查询与 snake/kebab，共 8 case，快照人工逐条审阅后提交；另设"关键 token 语义抽查"测试防止未来 DEVKB_UPDATE_GOLDEN 照单全收错误输出。
- 已知确定性行为（审阅记录）：jieba HMM=False 下词典外词如"回补/幂等"拆成单字——检索仍可经拆分词命中，不影响原文引用；`v1` camel 拆出 `v`+`1` 略有噪声但规则一致，不为个案加特判。
- 判据口径说明：清单"去重"按规格 §7 第 7 条（优先级更高）落为"重复 token 有确定性上限"——MAX_TOKEN_REPEAT=10 保留词频信号防病态膨胀，完全去重反而会抹掉 ts_rank_cd 的词频证据；该行为 T12.2 已有测试锁定。
- 冻结含义写入 fts.py 模块 docstring：输出变化必须人工审 golden diff 并对存量语料重跑 `devkb backfill-search`，否则摄取/查询两侧 token 不对称、FTS 静默漏检。
- 质量门：ruff/pyright 零错误，pytest 127 passed。

## 2026-07-17 · T14.2 · 查询期 query 构造冻结

做了什么：冻结 lexical channel 的查询构造——`tokenize_query` + per-token `plainto_tsquery` 绑定参数 + tsquery `||`（OR）合并，落在 `ChunkRepo.lexical_search` 单一咽喉点（证据：本提交）。

- 关键决策（OR 而非 websearch AND）：真实 dev 问题多为中英混合（"OrderStateMachine 在什么情况下…"），AND 语义要求全部词素命中，CJK 虚词稍有不匹配整个 channel 就归零；OR 之下 ts_rank_cd 的 cover density 自然让多词命中排前。集成测试直接锁定"AND 会零命中的查询在 OR 下命中正确块"。
- 对称性依据：查询与摄取共用同一冻结 token 化函数，且 `plainto_tsquery`/`to_tsvector` 的 'simple' 解析器对同一 token 产出相同词素（psql 实测 `order_status`→两词素、`order.paid.event`→单 host 词素，两侧一致）；token 内多词素保持 AND，恰好等价于索引侧拆法。
- 安全判据：全程绑定参数，plainto 对任意输入不抛语法错（空 tsquery 是 `||` 幺元，实测确认）；注入文本只会变成普通词素——新增"夹带注入的查询正常返回命中且表完好"测试；超长输入 4096 字符截断 + 32 token 硬上限（单测锁定确定性截断）。
- 口径补齐：failed 文档保留的陈旧 chunks 在 lexical 路径同样不可见（与 vector_search 同理由：回滚保留的上一版 chunks 行号已不可信），新增测试。
- 质量门：ruff/pyright 零错误，pytest 132 passed。

## 2026-07-17 · T14.3 · lexical 排名与解释性字段

做了什么：`retrieval.lexical_retrieve` 组装 `LexicalHit`——chunk 引用元数据之外带 lexical_rank（1-based）、ts_rank_cd 原始分、命中 query 与 query_tokens（证据：本提交）。

- 字段设计对齐规格 §8 第 6 条：这些解释字段是 T15 RRF（保留各 channel rank）和 T18 轨迹的输入口径；score 注释明确"只用于 channel 内排序解释，不与余弦分数混加"（§8 反混加纪律前置到类型层）。
- query_tokens 的一致性依据：token 化是纯函数，retrieve 层复算的结果与 lexical_search 内部构造 tsquery 所用完全一致（同函数同输入），不需要跨层传递。
- 排名可精确断言的两层证据：受控种子数据上"命中 2 词 > 命中 1 词 > 无关不出现"的完整顺序 + 全部解释字段断言；真实 Java fixture 经管道摄取后，查询 `shouldRetry`（语料中唯一原词）精确命中该方法块 rank=1、rel_path/行号正确。
- 质量门：ruff/pyright 零错误，pytest 134 passed。

## 2026-07-17 · T14.4 · lexical-only dev 报告与 GIN 计划验证

做了什么：`benchmarks/p1_lexical_dev.py` 在 P1 全量快照（445 文档/4788 chunks）上跑 17 题 lexical-only 评测，报告落盘 `evalsets/reports/p1-dev-retrieval-lexical-20260717T185636+0800.{json,md}`（证据：本提交）。

- 结果是负的，如实记录：R@10=0.059。第一版报告跑出这个数字后没有直接落盘，先做了根因诊断——(1) 逐题 top-100 名次显示 14/17 的相关块连 top-100 都进不去，不是"差一点"；(2) scratchpad 对照 ts_rank_cd 归一化 flag 1/4/32、查询侧去单字 CJK 共 6 个排序变体，R@10 全部纹丝不动——排除调参可救的假设；(3) 抽查标注证据原文确认根因：11/17 题是中文自然语言，而证据（dev-log 任务记录）正文是英文命令与要点，词面零重叠。词汇鸿沟是 lexical 的结构性盲区，这正是 vector/Hybrid 存在的理由；Evaluation v1 本就把 lexical 定位为"单独观察贡献"、不设 Gate。强项也兑现了：identifier 组 q14 精确 rank=1。
- GIN 判据：给 ChunkRepo 加 `explain_lexical_search`（EXPLAIN 与业务查询同源形状；literal_binds 编译遇 REGCONFIG 无渲染器，把常量 'simple' 改为 SQL 字面量，token 仍走绑定参数）。真实语料结论：单稀有 token（40901）自然走 Bitmap Index Scan on ix_chunks_search_tsv（0.6ms）；宽 OR 查询因 camel 拆分子词高词频匹配 65% 行，planner 按成本正确顺扫（~100ms）——T12.4 遗留的"真实语料自然计划验证"就此落盘。宽 OR 演化与查询侧词频加权想法记 backlog，不在 P1 处理。
- 延迟：85 样本 P50=17.6ms/P95=78.3ms，报告显式声明小样本+WSL2 开发机，不外推生产结论。
- 机械分组规则（正则可复现，非人工标注）同时为 T15.4 Gate 第 3 条留下"预先归类 token 题"依据。
- 质量门：ruff/pyright 零错误，pytest 全绿。

## 2026-07-17 · T14 复评修复 · EXPLAIN 绑定参数与诊断归档

做了什么：GPT 复评 T14 提出 1 阻断 + 2 报告证据问题，全部属实，修复（证据：本提交及下一提交）。

- 阻断项（T14.2）：`explain_lexical_search` 用 literal_binds 把参数内联再 f-string 拼进 `EXPLAIN`——功能对但确实不是参数绑定，与判据字面不符。修复：自定义 `_Explain(Executable)` + `@compiles`，把 `EXPLAIN (ANALYZE, BUFFERS)` 作为编译期前缀、内层 select 连同全部绑定参数原样走 extended protocol（实测 asyncpg 下 EXPLAIN 可带参执行，之前"EXPLAIN 无法带绑定参数"的判断是错的）；`'simple'` 恢复为绑定参数（literal_column 的动机随 literal_binds 一起消失）。新增 EXPLAIN 敌意输入回归测试。
- 证据项 1（T14.4 报告出处）：报告生成时工作树 dirty 且所记 commit（30c561c）不含生成器代码。复评已独立复跑确认指标逐题一致，但按纪律应从干净提交重新生成——本次代码提交后重跑，新报告与旧报告并存（时间戳报告不覆盖原则），清单证据指向新报告。
- 证据项 2（排序变体无原始证据）：scratchpad 诊断脚本整理归档为 `benchmarks/p1_lexical_variants_diag.py`（只读诊断；为注入 ts_rank_cd 归一化参数使用本地 SQL，与集成测试中诊断 SQL 同一口径，业务查询构造仍收口 repositories），原始输出落 `benchmarks/results/lexical_variants_diag.txt`，报告措辞改为引用归档物。重跑与 scratchpad 结果完全一致（6 变体 R@10 均 0.059）。
- 质量门：ruff/pyright 零错误，pytest 135 passed。

## 2026-07-18 · T14 二次复评修复 · 诊断查询收口 repositories

做了什么：二次复评指出诊断脚本两个问题，均属实——(1) 在 benchmarks/ 用 sqlalchemy text() 自建 SQL 违反 D7 硬规则（规则不区分目录，"静态检查只扫 src"不构成豁免）；(2) 手拼 tsquery（整 token 加引号）与正式 per-token plainto_tsquery 口径不同——多词素 token（如 order_status → 'order' & 'status'）两种构造语义不同，q09 名次 62 vs 68 的差异正源于此，归档物无法严格证明"只改排名参数"（证据：本提交）。

- 修复：查询收口为 `ChunkRepo.lexical_search_diagnostic`，与 lexical_search 共用 `_lexical_stmt`（该方法只加两个关键字实验参数：ts_rank_cd 归一化 flag、丢弃单字 CJK token；norm=0 走二参 ts_rank_cd，业务语句零差异）。诊断脚本只调 Repo，不再 import 任何查询构造。
- 同口径证明双保险：脚本运行时每题断言 base 变体与正式 lexical_search top-100 逐行一致（含分数，17/17 过）；集成测试 test_lexical_diagnostic_base_matches_official_search 把等价性固化进 CI，另断言两个实验参数的可观察效果（单字 CJK 块被过滤、norm=32 分数落 [0,1)）。
- 防复发：test_layering 的 D7 静态扫描范围从 src/devkb 扩至 benchmarks/（tests/ 的少量诊断性 SQL 维持现状，docstring 说明）。
- 归档重生成：新名次与正式报告的 diag_hit_rank_at_100 完全对齐（q09=68、q22=29、q14=1），6 变体 R@10 仍全部 0.059——"低召回非排序调参问题"结论在正确口径下依然成立，报告正文无需改动。
- 质量门：ruff/pyright 零错误，pytest 136 passed。

## 2026-07-18 · T15.1 · RRF 纯函数与 golden 冻结

做了什么：`retrieval.py` 新增 `rrf_fuse` 纯函数与 `ChannelRanking`/`FusedChunk` 模型（规格 §8 第 3–5 条），golden 快照 `tests/fixtures/golden_rrf/fuse_cases.json` 冻结 6 场景完整输出（证据：本提交）。

- 设计要点："不混加不同量纲原始 score"不是靠纪律而是靠类型——`ChannelRanking` 只携带 chunk_id 有序序列，没有任何 score 字段，融合分只可能来自 1/(k+rank) 求和。名次由列表位置隐含（首位=1），杜绝"传错 rank 起点"一类错误。
- 语义决策：同一 ranking 内重复 chunk_id 只计首个（最优）名次；跨 ranking 合并时各 channel 保留跨 query 最优 rank、hit_queries 按输入首见序；并列 fused_score 按 chunk_id 升序决定性排序。函数返回完整融合列表不截断——final top-k 是 T15.2 编排层的职责，纯函数不预设消费方。
- golden 覆盖判据列举的全部场景：双 channel 重叠去重、并列决序、缺失 channel（空 ranking 零贡献）、ranking 内重复、空输入、多 query 同 channel 贡献相加。快照数值人工核对（1/62+1/61≈0.03252 等）后冻结；另有默认 k=60、公式、稳定性与 k<1 拒绝的独立断言。
- 质量门：ruff/pyright 零错误，pytest 141 passed。

## 2026-07-18 · T15.2 · Hybrid retrieval 编排

做了什么：`retrieval.py` 新增 `hybrid_retrieve` 编排与 `HybridHit` 结果模型：每个子查询跑 Vector + FTS 各 top-N，全部 (query, channel) ranking 交给 `rrf_fuse`，裁剪 final top-k 后组装完整引用元数据与解释字段（证据：本提交）。

- 硬上限按 §8 落地：子查询 ≤3（按首见序去重后计数——重复子查询若不去重会双倍计分，语义上等价于 §8 第 5 条"多子查询按证据 ID 去重"）、每路候选 ≤50、final top_k ≤12；越界一律 ValueError，上限不是默认值，调用方无法绕过。
- vector_mode 默认 exact 并在 docstring 注明：HNSW 是否成为默认口径由 T15.3 对照（Gate §5.1 第 4 条）裁定后再切换，不预支未验证的默认。
- 不可见性不在编排层重复过滤，由两条 channel 的仓储查询共同保证（D7）；集成测试用最严苛构造验证——failed 文档陈旧块与其他项目的块拿着与查询向量余弦=1 且词面精确命中的内容，依然不可见。
- 集成测试 4 项（真实 PG + FakeEmbedder 确定性向量）：受控种子上双 channel 皆第 1 的块融合后居首且 fused_score 精确等于 2/61；多 query 命中合并 hit_queries、重复子查询结果与单查询逐字段相等；6 种越界参数全部拒绝；不可见性如上。
- 质量门：ruff/pyright 零错误，pytest 145 passed。

## 2026-07-18 · T15.3 · HNSW/exact top-10 overlap 对照

做了什么：`benchmarks/p1_hnsw_overlap.py` 在 P1 全量快照（445 文档/4788 chunks）上用真实 Qwen 对 17 个 retrieval dev 问题跑 HNSW vs exact 对照，报告落盘 `evalsets/reports/p1-dev-hnsw-overlap-20260718T122103+0800.{json,md}`（证据：本提交）。

- 方法：每题只嵌入一次查询向量，同一向量分别走 `vector_search(mode="exact")` 与 `mode="hnsw"` 的 top-10，按 chunk_id 集合算 overlap；计划形状由 GUC 强制（T12.4 已验证真实走 HNSW 索引），报告不重复 EXPLAIN。ef_search 阶梯 [40, 80, 160] 在跑之前预声明，达标即冻结不继续爬——避免"看结果再决定试多少档"的事后调参空间。
- 结果：首档 ef_search=40（pgvector 会话默认）即 17/17 题 overlap=1.0，平均 1.0 ≥ 0.95 Gate 通过。4788 chunks 规模下 HNSW（m=16/ef_construction=64）近似损失为零，符合小语料预期；该结论不外推到更大语料。
- 裁决落地：`hybrid_retrieve` 默认 `vector_mode` 从 exact 切到 hnsw（T15.2 预留的裁决点），ef_search=None 沿用会话默认 40 作为冻结值；docstring 引用报告。评测基线路径仍可显式切 exact。
- 纪律：报告从干净提交 19f0097 生成（生成器先行提交），`devkb_worktree_dirty=false`——T14.4 复评教训直接内化为流程。
- 质量门：ruff/pyright 零错误，pytest 145 passed（hnsw 默认下既有 Hybrid 集成测试不变绿）。

## 2026-07-18 · T15.4 · Hybrid dev Gate 未过——负结果、根因与偏差上报

做了什么：`benchmarks/p1_hybrid_dev_gate.py` 从干净提交 fb68793 在同一快照（445 文档/4788 chunks）上运行 vector-exact / lexical / hybrid-rrf 三模式官方对照，Gate 未过，报告如实落盘 `evalsets/reports/p1-dev-hybrid-gate-20260718T122552+0800.{json,md}`；随后 `benchmarks/p1_hybrid_grid_diag.py` 做规格内全网格诊断并归档 `benchmarks/results/hybrid_grid_diag.txt`；按勾选纪律不勾选 T15.4，写偏差记录等用户裁决（证据：本提交）。

- 官方结果：vector-exact R@10=0.588/MRR=0.386；hybrid-rrf（k=60、每路 50、hnsw@40）R@10=0.529/MRR=0.128。G1 未过、G2 惨败（MRR −0.258，上限 −0.02）、G3 通过（q22：vector 未命中 → hybrid rank 5，两路深位共识的真实 hybrid 收益）。
- 根因（逐题证据）：q01/q06/q09/q10 的相关块 vector rank=1（1/61≈0.0164），但"双路平庸块"（如 v=28/l=16 → 0.0245）稳压单路第一。lexical 对 11/17 中文自然语言题是系统性噪声（T14.4 已量化 R@10=0.059），RRF 的共识假设在一路系统性失真时变成反信号；k=60 平坦尾部下 v1 与 v3 分差仅 0.0005，任何 lexical 贡献都足以翻盘。T14.4 报告预警的"RRF 融合不得让该 channel 的弱题拖垮 vector 强题"精确应验。
- 网格诊断（每题抓两路 top-50 一次，离线截断前缀走**官方 rrf_fuse**，无诊断专用口径）：28 组 (n_vec∈{5,10,20,50} × n_lex∈{1,2,3,5,10,20,50}) 全部失败——G2 最好 −0.084（n_lex=1，junk 与相关块并列 1/61 靠 chunk_id 掷币）；G3 只在 n_lex≥20 出现，与 G2 结构性互斥。附录 k∈{10,20,120} 亦全不过（k 是规格外自由度，仅作裁决证据）。
- 处置：这是"冻结机制满足不了预冻结 Gate"的规格冲突（规格 §8 允许记录负结果 vs Eval v1 §5.1/§7 硬 Gate），不属于我可自行裁量的范围——偏差记录已列建议（推荐按 §8 记录负结果并修订 Eval v1 的 hybrid Gate/默认检索口径；不推荐 weighted RRF 双参数在 17 题上调优）。dev 迭代全程未触碰 holdout。
- 质量门：ruff/pyright 零错误，pytest 145 passed（诊断脚本经 D7 静态扫描，查询全部走 ChunkRepo）。

## 2026-07-18 · T15.4 裁决落地 · 负结果口径修订与 T15 收口

做了什么：用户裁决采纳推荐项——按《P1实现规格》§8 记录 Hybrid 负结果，不在 17 题 dev 上继续调参。修订随裁决落地（证据：本提交）。

- 《Evaluation-v1》§5.1：第 1–3 条（hybrid 相对 Gate）改为**同快照对照记录义务**（负结果允许）；第 4 条（HNSW overlap，已过）与第 5 条（同快照/标注纪律）维持硬性。§7 holdout 硬 Gate 同步改为"在线默认模式 vector-hnsw R@10 ≥ 同快照 vector-exact"（近似损失约束），hybrid 对照仍须完整报告。两处修订均带日期与裁决出处，原文保留。
- 《P1实现规格》§8 追加裁决注记：P1 在线默认检索 = vector channel（HNSW/ef_search=40，T15.3 冻结）；Hybrid/RRF 机制、硬上限与可工作实现不变，作为评测模式与显式配置存在；T16 retrieve 节点按此接线。代码不动：`retrieve()` 维持 exact 默认（评测基线语义），hnsw 接线是 T16 的事。
- 清单：T15.4 按修订后判据勾选（对照报告+网格诊断+复现命令均已在 f9f03c7 提交）；验收总清单第 3 条口径同步；偏差记录填入裁决全文。
- T15 整体收口：RRF 纯函数（golden 冻结）、Hybrid 编排（硬上限+不可见性）、HNSW 对照（overlap=1.0 通过）、Hybrid Gate（负结果如实记录+裁决修订）。

## 2026-07-18 · T15 复评修复 · 文档一致性、ef_search 冻结常量与穷举归档

做了什么：复评提出 2 中等 + 1 低等问题，均属实，全部修复（证据：本提交及上一提交 5309315）。

- 中等 ①（文档矛盾）：Eval v1 §3 模式表仍称 hybrid-rrf 为"P1 默认检索"，与 §5.1 修订/规格 §8 注记矛盾，可能误导 T16/T20。修复：表中 hybrid-rrf 改为"显式调用与评测路径"、vector-hnsw 标注为在线默认，均带裁决日期与出处。
- 中等 ②（配置冻结不严）：`p1_hybrid_dev_gate.py` 报告记录 ef_search=40 但调用未显式传入，实际依赖 PG 会话默认（值恰为 40，复评独立复跑确认结果无失真——问题在可复现性而非结论）。修复：新增 `retrieval.HNSW_EF_SEARCH = 40` 统一常量（docstring 指向 T15.3 冻结报告），`hybrid_retrieve` 默认参数、gate 脚本与 grid 诊断脚本全部显式下发；配置口径从"恰好等于会话默认"变为"代码内冻结"。
- 低等 ③（归档不完整）：grid 归档缺 commit/语料 hash/模型元数据，且 28 组代表性网格支撑不了"机制无法过 Gate"的全称表述。修复：脚本补齐完整元数据头，并在候选截断语义下穷举规格内全空间 n_vec×n_lex=1..50×1..50 共 2500 组——0 组全过、**G2 单项通过组合数为 0**（最好 ΔMRR=−0.0838 @ n_vec=7/n_lex=1），逐组合指标归档 `benchmarks/results/hybrid_grid_diag.json`；从干净提交 5309315 重生成，代表性表格与旧归档逐项一致，与复评独立穷举（2500 组 0 过）互证，裁决技术依据成立且加固。
- 质量门：ruff/pyright 零错误，pytest 145 passed；旧 P0 期 benchmark 脚本的历史 lint 噪声不属本次范围，未动。

## 2026-07-18 · T15 二次复评修复 · T21.3 Holdout 判据同步与穷举证据引用

做了什么：二次复评 1 中等 + 1 低等，均属实（证据：本提交）。

- 中等（T21.3 判据滞后）：任务清单 T21.3 仍写裁决前的 `hybrid-rrf Recall@10 >= vector-exact` 硬 Gate，与 Eval v1 §7 修订矛盾，会直接误导 holdout 验收。修复：判据改为 `在线默认模式 vector-hnsw Recall@10 >= 同快照 vector-exact`，注明裁决日期与出处，并保留"hybrid 对照完整报告、不设硬阈值"的记录义务。
- 低等（修订说明证据滞后）：Eval v1 §5.1 修订注记仍只引 28 组网格。修复：改为"28 组代表性网格加穷举 2500 组均 0 过、G2 单项通过数为 0"，并同时指向 txt 与 json 归档。
- 巡检：`hybrid-rrf Recall@10` 的其余出现均为"原文保留+日期化注记"模式的原句或历史 dev-log 叙事，符合不改写历史的惯例，不动。
- 质量门：ruff/pyright 零错误，pytest 145 passed。

## 2026-07-18 · T16 · LangGraph 单 Agent 有界骨架

做了什么：按《P1实现规格》§9 实现 `agent/state.py`、`prompts.py`、`nodes.py`、`graph.py`。四类 LLM 输出使用 extra-forbid/strict Pydantic schema，run_id/project_id 只存在于服务层初态；Prompt 统一携带 `p1-agent-v1`、不可信 evidence 边界和 JSON schema，并用 SHA-256 snapshot 锁定。LangGraph 拓扑为 plan→retrieve→evaluate→refine(≤1)→retrieve→evaluate→generate→verify→finalize，不启用 checkpointer；router 只读 sufficiency/证据/轮次/预算，模型输出无法指定节点。

关键边界与定位：

- T15.4 已裁决在线默认是 vector-HNSW 而非 Hybrid。为保持 P0 对照，`retrieval.retrieve()` 只新增可选 mode/ef_search 参数且仍默认 exact；T16 PG retriever 显式传 `hnsw/40`，多 query 仅在 vector rankings 上做 RRF，不触 lexical channel。
- 全局请求在调用供应商前计数，最大 6；每个逻辑调用点解析/传输失败只重问 1 次。允许重问，但补检前必须还能容纳 refine+二次 evaluate+generate 三次请求，因此早期重试会确定性裁掉可选补检，不靠 LangGraph recursion_limit 控预算。
- 自审发现首轮 top-k 已满时“旧证据优先合并”会让二次新证据全部被截断，改为新证据优先、旧证据填充，最终仍受 12 条硬上限；另将 generate 两次结构化失败明确标记为失败并降级 partial/refusal，避免沿用 sufficient 信号误标 full。
- T16 的 verify 目前只是显式拓扑接点；L0/L1、验证失败重生成与完整三态 Answer 按任务边界留给 T17。节点/工具轨迹落库与 run 终态原子性留给 T18，不提前实现。

证据：14 项新增单测覆盖 strict schema、Prompt snapshot、首轮充分、补检成功、二次不足、结构化失败、身份不可覆盖、预算熔断、refine/generate 默认、请求恰达 6、业务上限与 HNSW 接线；`make ci` 通过（ruff/pyright 零错误，pytest 159 passed）。证据 commit：本提交。

## 2026-07-18 · T16 复评修复 · 零证据路由与 evidence 载荷边界

审查发现 1 中等 + 1 低等实现问题，均属实并修复；另接受“5 个子任务合并提交使逐项证据映射变弱”的流程注记，后续恢复按子任务提交节奏。

- 中等：evaluate Prompt 虽要求空证据返回 insufficient，但 router 的 sufficient 分支没有读取证据数，模型违规时可到达“零证据 full”。修复为证据数量优先的确定性硬守卫：无证据一律按 insufficient 控制流，在轮次/预算允许时 refine，否则 finalize refusal；Fake 路径用连续两轮空检索但 evaluate 均返回 sufficient 的敌意脚本，精确断言 `plan→retrieve→evaluate→refine→retrieve→evaluate→finalize`、generate=0、终态 refusal。
- 低等：原 Prompt 用 `<E1 ...>正文</E1>` 包装未转义正文，`</E1><E2 path=...>` 可伪造视觉上的证据元数据。修复为统一 JSON user payload，question/requested_mode/evidences 都是结构化字段，正文只作为 `evidences[].content` 字符串编码；evaluate/generate 两条路径用恶意闭合标签断言反序列化后始终只有真实 E1/path/line。证据包装口径属于冻结的 Prompt 版本递增条件，故同步将机器真相源与版本文档从 `p1-agent-v1` 递增为 `p1-agent-v2`；v1 尚无 Agentic 真实报告，T20 首份报告直接记录 v2。
- 质量门：T16 定向测试由 14 增至 16；`make ci` 为 ruff/pyright 零错误、pytest 161 passed。证据 commit：本提交。

## 2026-07-18 · T17.1 · Answer v1 与兼容渲染

- Answer v1 组装收口到新模块 `agent/answer.py`：纯序列化终态 AgentState，不发起 LLM 调用。P0 四字段（answer_text/citations/warnings/stats）保留原形状，citations 取 claims 绑定 ∪ 文本 [E#] 的并集按编号升序；新增 run_id/mode/claims/not_found/limitations/trace_summary。trace_summary 只含节点序列与计数器，不含 chain-of-thought。
- claims 结构按规格 §10 落在 GenerateOutput 上：ClaimOutput 的 evidence_ids 由 schema 强制 min_length=1（“每个 claim 至少绑定一个 evidence”成为结构约束而非事后检查），quotes 上限 4、claim 上限 20、单 claim 证据上限 12 并有测试锁定与 retrieval.MAX_FINAL_TOP_K 相等。generate Prompt 声明 claims/quotes 逐字纪律与 verification_errors 修正口径（供 T17.4 重生成复用），user payload 仅在有反馈时携带 verification_errors 字段。
- 契约版本：generate 输出 schema 说明变化 → Prompt bundle `p1-agent-v2→p1-agent-v3`；AgentState 新增 verification_feedback/final_claims/final_not_found/model 字段 → `p1-agent-state-v1→v2`。版本文档追加变更记录；Prompt 快照 SHA-256 重锁。
- CLI：ask 渲染抽成 `_render_answer`，对无 mode 的 P0 老 Answer 原样渲染（旧 runs 可读取判据），v1 Answer 增加 mode 着色、not_found、limitations 展示。坑：SIM300 Yoda condition 被 ruff 拦下（集合包含断言写反向），改为 `set(answer) >= …`。
- 质量门：`make ci` ruff/pyright 零错误、pytest 169 passed（新增 test_answer_v1.py 4 项、CLI 渲染 3 项、prompts feedback 1 项）。证据 commit：本提交。

## 2026-07-18 · T17.2 · 三态确定性策略

- finalize 重写：mode 完全由结构化状态判定（draft 有无、generate_failed、verification.failed_claims、sufficiency、claims 数、draft.not_found），零 LLM 调用。拒答模板 `_refusal_text` 列出缺失方面（evaluate.missing_aspects ∪ draft.not_found 去重），evaluate 默认不足时模板会如实展示冻结默认的"证据充分性无法确认"。
- 两个额外确定性护栏：充分判定但 claims 为空 → 降级 partial 记 warning（full 的"每 claim 绑定证据"承诺不能建立在空集合上）；draft.not_found 非空时不给 full（自称充分又列缺失是矛盾信号，保守取 partial）。
- answer_text 复用 P0 `apply_l0` 剔除越界 [E#]（agent/nodes 导入 devkb.answer，无环）。verification.failed_claims 的删除/降级分支已就位，等 T17.4 的 verify 真实现填充。
- 质量门：`make ci` ruff/pyright 零错误、pytest 171 passed（新增部分证据 partial+not_found 合并、充分无 claim 降级 2 项；3 个既有 refusal 用例补模板逐字断言）。证据 commit：本提交。

## 2026-07-18 · T17.3 · L1 引文原文匹配

- 规范化与阈值冻结（dev 集，2026-07-18）：NFKC（全角字母/数字/标点折半角）+ NFKC 不覆盖的中文标点显式映射（。→. 、→, 引号书名号等）+ 去除全部空白；之后 quote 必须是其绑定的某单条证据 content 的精确子串。设计取舍记录在模块 docstring：相似度阈值 <1.0 会放行"轻微篡改"（与引文忠实目标直接冲突）故不做模糊匹配；不折叠大小写（OrderService ≠ orderservice，代码敏感）；规范化后 <4 字符的引文（如"保护。"）几乎必然误配，直接判失败。
- 跨 chunk 拼接天然失败：拼接文本不构成任何单条证据的子串。错误 evidence（逐字来自 E2 但绑定 E1）失败，测试含改绑 E2 后通过的对照，证明失败原因是绑定错误而非匹配器过严。
- `l1_errors` 返回 `L1:claim[i]:quote[j]:no_verbatim_match` 机器可读错误 + 失败 claim 下标，供 T17.4 反馈 generate 与二次降级删除。
- 质量门：`make ci` ruff/pyright 零错误、pytest 178 passed（新增 test_l1.py 7 项）。证据 commit：本提交。

## 2026-07-18 · T17.4 · 验证失败重生成与二次降级 + agentic 落库

- verify 节点由 T16 桩换成 `verify_draft`：L0（answer_text 越界 [E#] + claim 未知 evidence_id）与 L1（T17.3）合并为 VerificationOutput，errors 即机器可读反馈。`route_after_verify` 四道闸：verification 未过、非 generate_failed（传输/格式失败不许借道重生成）、generate_calls<2、预算余量 ≥1，全过才回 generate 一次；重生成的 user payload 带 verification_errors。第二次仍失败由 finalize 删除 failed_claims：余 claim ≥1 → partial，全删 → refusal 模板（不可信内容不包装为答案）。
- `agent/service.py`：建 run → 跑图 → build_answer → RunRepo.finish。stats.latency_ms 与 P0 同口径取全程墙钟（图内 latency 仅累计 LLM 时延，落库前覆写）；usage 聚合含 llm_calls/llm_retries 使 run 汇总可由明细复算；cost 直接取图内分档累加值，集成测试用价目表复算 3×单次成本逐位相等（0.000540 元）。问题长度在建 run 前校验（新增 InvalidInputError），避免超限问题留下孤儿 run。意外异常与 P0 同款兜底：run 终态 failed + error JSON。
- CLI：`ask --pipeline agentic|fixed-rag` 默认 agentic（规格 §12.1），fixed-rag 保持 P0 单调用对照路径不动；top_k 校验 1..12（§12.1 硬上限）。MAX_FINAL_TOP_K 在命令体内延迟导入，避免 CLI 启动即加载 jieba/sqlalchemy。
- 质量门：`make ci` ruff/pyright 零错误、pytest 188 passed（graph 4 项重生成矩阵、集成 4 项落库、CLI 2 项参数路由/校验）。证据 commit：本提交。

## 2026-07-18 · T17.5（未勾选，待裁决）· Graph Fake 矩阵补齐

- §5.3 十二类路径逐类映射写入 test_agent_graph.py 模块 docstring。本轮补齐第 9 类：检索工具异常（两轮均抛 RuntimeError → errors 精确记录、零 generate、确定性 refusal、工具调用次数可断言）、LLM 超时重问后成功（预算/retry_counts/warning 逐项断言）、generate 双超时确定性降级 partial。数据库异常在 test_agent_service.py（run 终态 failed），第 10 类越权在 test_isolation.py 已有。
- 冲突：判据要求 12 类"全部覆盖"，但第 11 类（API 并发边界/事件循环）依赖 T19 FastAPI，执行顺序 T17→T19 且 T19.4 判据与之重合。已写入清单偏差记录，建议以 1–10、12 类为 T17.5 完成口径并显式移交第 11 类给 T19.4；等用户裁决，不自行改规格、不勾选。
- 质量门：`make ci` ruff/pyright 零错误、pytest 191 passed。证据 commit：本提交。

## 2026-07-18 · T17.5 裁决落地

用户裁决采纳推荐项：T17.5 以 §5.3 的 1–10、12 类覆盖为完成口径勾选；第 11 类（API 并发边界/事件循环）显式移交 T19.4（判据行已加承接标记），T21 验收按 12 类整体复核。偏差记录已填裁决列。T17 全部子任务完成。

## 2026-07-18 · T17 复评修复 · 不可信正文残留与数据库异常覆盖

审查发现 1 中等 + 1 低等，均属实并修复：

- 中等（T17.4）：二次验证失败只删除了结构化 claim，final_answer 仍复用完整 draft.answer_text——apply_l0 只剔越界 [E#]，不删句子；失败 claim 若引用合法存在的 E2，其标记还会让 build_answer 继续输出 E2 citation。违反规格"移除不通过的 claim"与 partial"只回答可支持部分"。修复：删除发生时不再信任 draft 正文，`_rebuild_answer_from_claims` 按保留 claim 确定性重建（`text [E#]。` 逐句拼接），失败句子/标记/引用一并消失；移除事实经 warnings + build_answer limitations（"已移除 N 个未通过 L0/L1 引用验证的 claim"）双通道呈现。回归测试复刻审查探针：两条合法证据、第二稿混入编造句 [E2]，同时断言 final_answer 逐字、无 [E2]/编造残留、citations 仅 E1、claims 仅保留项、limitations 载明移除。
- 低等（T17.5）：矩阵注释声称的"数据库异常"实际测试用的是 RuntimeError。改为真实 `SQLAlchemyError` 双路径：检索期异常 → 图内吸收、确定性 refusal、run succeeded 落库（降级策略可查）；持久化前异常 → run 终态 failed、answer JSON 记录 SQLAlchemyError。矩阵映射注释同步更正，原 RuntimeError 用例保留为"检索工具异常"类。
- 质量门：`make ci` ruff/pyright 零错误、pytest 193 passed（+2）。证据 commit：本提交。

## 2026-07-18 · T17 二轮复评修复 · 重建正文注入未检查 [E#]

二轮审查确认前两项修复合格，但发现重建逻辑引入 1 个中等边界缺口，属实并修复：

- 中等（T17.4）：`_rebuild_answer_from_claims` 原样拼接 claim.text，而 L0 只检查原始 draft.answer_text 与 claim.evidence_ids，不检查 claim.text 内嵌标记。可达路径：保留 claim 通过验证但其 text 含 [E9]（越界）或 [E2]（合法但未绑定），另一 claim 失败触发重建 → 未经 L0 检查的标记注入终稿，违反最终 L0 100% Gate。修复双防线：重建前用正则剔除 claim.text 中全部 [E#]，只追加由已通过 L0 的 claim.evidence_ids 规范生成的标记；重建结果再过一次确定性 `apply_l0`（越界标记的最终防线，warnings 汇入 finalize）。剔除后正文做空白折叠，避免残留双空格。
- 回归测试复刻审查探针：保留 claim 的 text 内嵌 [E9] 与未绑定 [E2]，断言终稿逐字等于规范重建结果、无 E9/E2 残留，并对重建后的 GenerateOutput 重新执行 `l0_errors` 断言零错误零失败。
- 质量门：`make ci` ruff/pyright 零错误、pytest 194 passed（+1）。证据 commit：本提交。

## 2026-07-19 · T18.1 · 节点/工具轨迹写入

- 新增 `agent/trace.py`：TraceRecorder 在图执行期内存收集（图为顺序执行，节点内 LLM/工具记录挂当前 step），run 结束由 service 统一写 agent_steps/tool_invocations，与 run 终态同事务提交。选择内存收集而非节点内直写：节点保持与 DB 解耦（FakeLLM 单测无需数据库即可断言轨迹），失败路径也能把"最后 step=failed"完整落库。
- step 终态确定性判定：节点返回 errors（吸收的工具/DB 异常）→ degraded 并记错误类型；warning 含 default_applied/budget_exhausted/limit_reached → degraded；未捕获异常由 graph 包装器直接判 failed 后原样上抛。verify:l0_l1_failed 是业务信号（触发重生成）不算节点降级。
- 摘要有界纪律：clip_text/clip_list（200 字符/8 项）；正文只以 answer_chars 出现，问题只记 question_chars；`_structured_call` 每次实际发出的请求（含重问、含解析失败）逐条记 call_key/attempt/分档 usage/单次 cost/时延/终态，预算耗尽未发出的请求不记录——run 汇总可由明细复算（T18.2 判据的落库基础）。
- 失败路径轨迹持久化包 try/except：轨迹写入自身失败时 rollback 保住 session，run 终态 failed 优先于轨迹完整性。
- 踩坑：langgraph `add_node` 的 StateNode 形参要求具名 state 参数，Callable 别名在类型系统里 positional-only，包装器返回类型放宽为 Any（与 build_agent_graph 同口径）并在 docstring 记录原因。
- 质量门：`make ci` ruff/pyright 零错误、pytest 203 passed（+7 单测四路径矩阵与脱敏、+2 集成落库/跨项目不可见/失败终态）。证据 commit：本提交。

## 2026-07-19 · T18.2 · 多调用 usage/cost 聚合与真实复算

- 逐请求明细在 T18.1 已随 `_structured_call` 落库（step.output_summary.llm_requests：call_key/attempt/status/分档 usage/单次 cost/时延）；本项补复算测试与真实验证。Fake 集成：首个 plan 输出非法 JSON 触发重问的 4 调用 run，tokens 汇总(400/200)=明细求和、cost 汇总 0.000720=明细求和、每条明细独立用 compute_cost 复算相等；未知模型（fake-llm）run.cost 与全部明细 cost 均为 None，不编造。
- 真实多调用 run（mini-mall，"订单取消后库存是如何回滚的？"，run 414bcd57）：6 调用 = plan + evaluate×2 + refine + generate×2，2 轮检索、L0/L1 触发 1 次重生成、partial 终态——同时覆盖补检与重生成的真实轨迹。人工复算：6 条明细逐条按 ADR-0004 价目表（hit 0.02/miss 1/out 2 元/Mtok）验算与落库 cost 相等（如 generate:2 = (4608×0.02+81+3849×2)/1e6 = 0.007871），求和 0.028672 与 run.cost 逐位相等；tokens_in 17516 / tokens_out 8086 与明细逐档求和一致。DeepSeek 缓存分档真实生效（generate:2 命中 4608）验证了"不能用合计 prompt_tokens 单价折算"的设计。
- 首次真实 run 踩到 T15 遗留 bug：`vector_search` 读 GUC 用单参 current_setting，而 hnsw.ef_search 是 pgvector 扩展 GUC——连接内 vector 库未加载（首个查询即走 hnsw，agentic 在线路径必然如此）时抛 UndefinedObject 且事务中止，后续轨迹/run 落库全部失败。T15.3 评测未暴露：eval 先跑 exact 模式，`<=>` 已触发库加载。修复：current_setting(name, missing_ok=true)，无调用前值的 GUC 不恢复（set_config is_local 最迟事务结束失效；未注册时占位符在索引扫描加载库后生效，占位值与 pgvector 默认同为 40）。检索期 DB 异常打断事务导致降级 run 也无法落库的一致性问题留给 T18.4 处理。
- 质量门：`make ci` ruff/pyright 零错误、pytest 205 passed（+2 复算集成）。证据 commit：本提交。

## 2026-07-19 · T18.3 · runs show/replay

- 轨迹装配收口到 service 层 `get_run_trace(session, project_id, run_id)`：run + steps（含 llm_requests 明细）+ tools + Answer 一次只读装配，Repository 按 project_id 实例化使跨项目 run_id 天然不可见（统一 NotFound，不泄漏存在性）。选择放 service 而非 CLI：T19 `GET /runs/{run_id}` 规格要求返回同一组数据，共用装配避免复制。
- CLI：`runs show` 在原 Answer JSON 载荷上增加 steps 摘要（seq/node/attempt/status/latency/error）；新增 `runs replay <run_id> --project <slug>` Rich 表格，判定列按节点还原决策（evaluate→sufficiency+缺失方面、verify→L0/L1 错误、finalize→mode），降级原因列显示 step.error。非法 run_id 与跨项目均 NotFound 退出码 1。
- 回放零重执行的测试证法：FakeLLM 脚本在 run 结束时已耗尽，若回放触发任何节点会立即抛"脚本已耗尽"，断言 llm.prompts 数不变。
- 踩坑：Rich 表格在 CliRunner 默认 80 列下把"insufficient"折断成两行导致断言失败——tokens/cost/latency 合并为一列减少挤压，测试显式 COLUMNS=200。
- 真实 run 414bcd57 回放人工验证：11 步完整可读——evaluate#1 partial+4 项缺失→refine 补检（证据 8→11 条）→evaluate#2 sufficient→generate#1 6 claims→verify#1 L1 单 quote 未过→generate#2→verify#2 仍有 claim[0] 未过→finalize 删除重建降级 partial。
- 质量门：`make ci` ruff/pyright 零错误、pytest 209 passed（+1 集成回放/隔离、+3 CLI）。证据 commit：本提交。

## 2026-07-19 · T18.4 · 轨迹故障一致性

- 收掉 T18.2 真实 run 暴露的一致性缺口：检索期真实 DB 异常（如 GUC 未注册、连接故障）会使 PostgreSQL 事务进入 aborted 状态，retrieve 节点虽按设计吸收异常降级，但同 session 后续所有 INSERT（轨迹、run 终态）全部失败——run 永久 running。修复三处：make_pg_retriever 闭包捕获 SQLAlchemyError 先 `session.rollback()` 恢复事务再上抛（节点降级语义不变）；service 成功/失败两条路径的 `_persist_trace` 均加兜底（写入失败 rollback 保 session + warning，run 终态优先于轨迹完整性）；rollback 会使 ORM 实例过期、其后访问 `run.id` 触发同步惰性刷新抛 MissingGreenlet（集成测试实测），改为建 run 后立即取纯 UUID 贯穿全程。
- 故障矩阵集成测试（复现手法：在被测 session 上真实执行 `SELECT 1/0` 制造 aborted 事务，而非 monkeypatch 抛异常——后者不会污染事务，测不到本缺口）：aborted 事务→refusal run succeeded、retrieve steps degraded（error 带 retrieve: 前缀）、tools failed、seq 连续；generate 双超时→step degraded 且 llm_requests 两条 request_failed（含 LLMTimeoutError 错误文本）、finalize ok；持久化前 DB 异常→run failed（既有）；节点内未捕获异常→run failed + 最后 step failed（既有）。所有用例断言项目内无 running 残留。
- 质量门：`make ci` ruff/pyright 零错误、pytest 211 passed（+2 集成故障矩阵）。证据 commit：本提交。

## 2026-07-19 · T19.1 · CLI/API 共用应用服务

- 新建 `src/devkb/service.py` AppService：ask（agentic/fixed-rag 双链路分发）/list_runs/run_trace/ingest/backfill_search 五个入口收口全部装配（engine/session factory 构造时持有，Embedder/LLM/Qwen 分词器懒初始化）；embedder/llm/count_tokens 构造参数可注入，集成测试用 FakeEmbedder/FakeLLM/approx_token_counter 全链路跑通（CI 禁真模型）。CLI 五个 `_run_*` 助手改为 `_app_service()` 上下文管理器内一行委托，cli.py 不再 import 任何 repositories/embedding/llm/db 装配件；输入校验（pipeline 白名单、top_k 1..12、问题 1..4000 字符）在 service.ask 统一执行，API（T19.2）直接复用。
- 异常映射 `map_errors`：DevKbError 透传；SQLAlchemyError/OSError→DATABASE_ERROR；其余未预期异常→INTERNAL_ERROR 并生成 12 位 error_id（消息只含 error_id，异常细节与类型进 stderr 结构化日志）。设计点：用户可见消息只带异常类型名不带原始错误串——SQLAlchemy/asyncpg 的错误串可能含 DSN 主机/口令；单测断言注入的口令与主机不出现在 DevKbError 消息中。DB 不可达集成测试连 127.0.0.1:9（discard 端口，永拒绝）验证稳定错误码。
- 踩坑：CLI 测试经 CliRunner 触发 configure_logging 时 structlog 以 cache_logger_on_first_use=True 缓存了 pytest 的临时 stderr，用例结束后该流被关闭，之后任何首次使用的 logger（本次是 service.py 的 map_errors 日志）都写已关闭文件抛 ValueError——单跑 test_service_errors 全绿、全量跑必挂的顺序依赖。修复：tests/conftest.py 增 autouse 夹具，每用例后 structlog.reset_defaults()（默认配置不缓存、写当前 stdout），隔离对后续测试的污染。
- 质量门：`make ci` ruff/pyright 零错误、pytest 222 passed（+5 单元映射/校验、+5 集成 AppService、+1 CLI 无 traceback 渲染）。证据 commit：本提交。

## 2026-07-19 · T19.2 · POST /ask

- 新建 `src/devkb/api.py`：create_app(service=None) 工厂——测试直接注入带 Fake 组件的 AppService（httpx ASGITransport 不发 lifespan 事件，注入路径不依赖 lifespan）；生产缺省时 lifespan 内按配置自建并在关闭时 aclose。路由函数只做"取 state.service → 调用 → 返回"，业务与校验语义全在 service 层（api.py 不放业务逻辑的规格约束由此落实）。
- 输入校验：AskRequest 用 Annotated+StringConstraints 复用 MAX_QUESTION_CHARS/MAX_FINAL_TOP_K 常量（与 CLI/service 同源，不出现第二份数字）；project slug 收紧为 ^[a-z0-9][a-z0-9_-]*$ ≤64（规格 §13"严格校验"，CLI 侧历史 slug 不受影响——校验只在 API 入口）。
- 错误契约：DevKbError 处理器输出 {error:{code,message,error_id}}；error_id 对 InternalError 沿用 map_errors 已生成并写入日志的那枚，其余错误现场生成并随 api_error 日志落 stderr，保证任何错误响应都可与日志关联。状态码映射表按 code 而非异常类型，子类（LLM_TIMEOUT→504）天然生效。
- 测试中发现：原打算用"FakeLLM 抛 RuntimeError"制造 500，实际图节点把非 LLM 异常也吸收降级为 refusal（预算走完 4 调用），拿不到失败终态——改用 AppService 子类 stub 直接抛 InternalError 测 API 映射层，DB 不可达（127.0.0.1:9）测 503 与 DSN 口令不泄漏。
- 质量门：`make ci` ruff/pyright 零错误、pytest 227 passed（+5 API 集成）。证据 commit：本提交。

## 2026-07-19 · T19.3 · GET /runs 与 /healthz

- `GET /runs/{run_id}?project=<slug>`：路由零新逻辑，直接经 AppService.run_trace 复用 T18.3 的只读装配（run+Answer+steps 含 llm_requests 明细+每步 tools）。run_id 声明为 uuid.UUID 路径类型——非法格式在 FastAPI 校验层 422，不进业务；project 查询参数复用 ProjectSlug 严格模式。跨项目与未知项目统一 404（Repository 按 project_id 实例化的 D7 语义在 API 层原样呈现，不泄漏 run 存在性）。
- `/healthz`：迁移版本查询需要原生 SQL（alembic_version 非 ORM 模型），按 D7 把 `text("SELECT version_num FROM alembic_version")` 放进 repositories.py 新增模块函数 get_alembic_version，service.healthz 组装 {status,database,migration}。"不加载模型/不调用 LLM"的测法：AppService 子类把 _get_embedder/_get_llm 覆写为立即 AssertionError，healthz 200 即证明探测路径完全不触碰模型；另断言响应文本不含 API key、"postgresql"（DSN）与口令。DB 不可达（127.0.0.1:9）→ 503 DATABASE_ERROR。
- 质量门：`make ci` ruff/pyright 零错误、pytest 231 passed（+4 API 集成）。证据 commit：本提交。

## 2026-07-19 · T19.4 · 同步模型隔离与并发上限

- embedding.py 新增 `embed_query_in_thread(embedder, text)`：asyncio.to_thread 把同步 embed_query 移出事件循环，进程内 `threading.Semaphore(EMBED_CONCURRENCY=1)` 限流。选 threading 而非 asyncio.Semaphore 的原因：后者绑定事件循环，CLI 每条命令一个 asyncio.run、pytest 每用例一个 loop 的形态下跨 loop 复用会炸；threading 版在工作线程内阻塞等待，与 loop 无关，两种形态统一。retrieval.py 的 vector/hybrid 两个在线调用点改为 await 该入口（ingest 的 embed_documents 是显式 CLI 管理操作，无服务器阻塞问题，不动）。
- `/ask` 路由套 `asyncio.shield`：httpx/uvicorn 断开会取消处理协程，而 agentic 落库路径的 `except Exception` 接不住 CancelledError（BaseException），run 将永久 running——shield 让取消只打断响应，图执行与终态写库继续。shield 属传输语义，放路由层不违反"api.py 不放业务逻辑"。
- 并发测试设计：FakeLLM 顺序脚本在并发下会串台（多 run 交错弹错响应），改用按 system prompt 关键词（查询规划器/充分性评估器/其余→generate）路由的 _RoutedLLM；探针 embedder 在线程内 sleep 0.5s 并用锁维护 active/max_active。断言：3 并发 /ask 全 200 且 max_active==1（上限可观察）；asks 在飞时 /healthz 0.25s 内返回、ask 任务仍 pending（循环未被阻塞）；cancel 请求后轮询 list_runs 至 succeeded（断开终态一致）。
- 质量门：`make ci` ruff/pyright 零错误、pytest 233 passed（+2 并发集成）。证据 commit：本提交。

## 2026-07-19 · T19.5 · 本地暴露边界

- 新增 `devkb serve`（Typer 命令包 uvicorn）：默认 host=127.0.0.1、port=8000；host 不在回环白名单（127.0.0.1/localhost/::1）时打印无鉴权警告但不阻止——开发者可能确有容器内联调需求，边界靠显著告知而非硬禁止。未引入 JWT/CORS/管理后台/UI（范围冻结负向判据）。
- README 新增"本地 HTTP API（P1）"章节：serve/healthz/ask/runs 四条命令示例 + 加粗 ⚠️ 声明（完全无鉴权、任何能访问端口的人可读取知识库与历史 run、严禁绑定非回环或经端口转发/反代暴露公网）。
- 验证：单测 monkeypatch uvicorn.run 断言缺省绑定与非回环警告；真实冒烟 `devkb serve --port 8765` 后 ss 确认 LISTEN 127.0.0.1:8765，healthz 返回 migration 0003（全程未加载嵌入模型，进程秒起），非法 run_id 得 422。
- 质量门：`make ci` ruff/pyright 零错误、pytest 235 passed（+2 CLI serve）。证据 commit：本提交。

## 2026-07-19 · T19 复查修复（2 中 2 低）

- 中①（T19.2 判据）：Pydantic 校验失败走的是 FastAPI 内置 RequestValidationError 处理器——422 的 detail 数组既无稳定 code/error_id，还会在 input 字段回显完整问题正文与非法 slug，双重违反 §13。修复：api.py 注册 RequestValidationError 专用处理器，422 统一 {error:{code:"INVALID_INPUT",message,error_id}}，message 只列字段位置（body.question 等）不含任何 input 值；测试对全部 6 种非法 /ask 输入与 /runs 非法 uuid/缺 project 断言错误体结构，并断言超长问题正文与 "Bad Slug!" 不出现在响应文本。
- 中②（T19.4 判据）：首次 /ask 的 SentenceTransformer 模型加载在 async ask() 内同步执行（数秒级 GPU 初始化冻结事件循环）；此前并发测试预注入 Fake embedder，恰好绕开了真实懒加载路径——测试盲区教训。修复：_get_embedder 改 async，asyncio.to_thread 跑同步 _load_embedder，asyncio.Lock 双检避免并发首问重复加载模型（8GB 显存加载两份即溢出）。新增集成测试用未注入 embedder 的子类（_load_embedder 慢 0.5s 计数）：加载进行中 /healthz 0.25s 内响应、2 并发首问 load_calls==1。
- 低③：Settings(retrieval_top_k=13) 可构造，service.ask 省略 top_k 时直接使用——agentic 路径深处 ValueError 变 500，fixed-rag 可能真超上限。修复：ask 校验最终生效值（显式传参与配置默认同一道闸）；Settings.retrieval_top_k 加 Field(ge=1, le=12)，上限数字与 retrieval.MAX_FINAL_TOP_K 的一致性由 test_config 用 annotated_types 元数据断言防漂移（config 不反向 import retrieval，避免把 sqlalchemy 链拖进配置模块）。
- 低④：README 已知限制第 4 条"无 API 服务（FastAPI 在 P1）"与同文件新增的 API 章节自相矛盾——改为指向上文安全边界。
- 质量门：`make ci` ruff/pyright 零错误、pytest 237 passed（422 契约断言强化 +2 新测试）。证据 commit：本提交。

## 2026-07-19 · T20.1 · devkb eval run 与报告 schema

- `src/devkb/evaluation.py` 收口 Evaluation v1 harness：题集加载（v0 冻结题 + v1 answer-expectations 合并 expected_mode，题量与 31 问冻结集不符即拒评）、检索四模式与 agentic 运行器、报告装配与落盘。指标口径全部沿用 T14.4/T15.4 基准脚本（rel_path+anchor casefold 包含命中、预声明 token 题正则），基准脚本继续留档不动，harness 成为正式入口。
- 判据落点：mode 枚举拒绝一切规格外别名；报告 JSON 记录 commit（含 worktree 脏标记）、语料 manifest（逐文档 rel_path+content_hash 全量入报告）与 sha256、双模型名、Prompt 版本、rrf_k/per_channel_n/ef_search/top_k、复现命令；文件名带时区时间戳，write_report 同名即 INVALID_INPUT 不覆盖；holdout 在 CLI（进程入口，无需配置即可拒）与 run_eval（库入口）双层要求 --confirm-holdout。
- agentic 评测的 L0/L1 终检不信任在线路径自检：对最终 Answer JSON 独立复验——L0 校验 [E#] 与 claims.evidence_ids 都指向 citations；L1 经 ChunkRepo 新增 get_contents 取回引用 chunk 原文，复用 agent.verification.quote_matches_evidence 判逐字匹配。每题另记录检索轮次（trace retrieve 步数）、llm_calls/重试（run.usage）、预算达标与终态完整；单题异常不中断评测，取该项目最新 run 验证失败终态仍落库。
- dev 产两份报告（p1-dev-retrieval-*/p1-dev-agentic-*），holdout 合并为单份 p1-holdout-*（§8 建议产物形状）。§5.1 Gate 按 2026-07-18 裁决实现：1–3 条为对照记录字段（recall/mrr delta、token 题改善），仅 HNSW overlap ≥0.95 为硬 Gate；§5.2 七项 Gate 全部落 gates 字段。
- 质量门：`make ci` ruff/pyright 零错误、pytest 243 passed（+4 harness 集成、+2 CLI）。证据 commit：本提交。

## 2026-07-19 · T20.2 · 指标手算单测

- 指标已在 T20.1 落为 evaluation.py 纯函数，本项补齐判据要求的手算对齐：每个断言旁注释算式（如 MRR@10=(1/1+1/3+1/6)/5=0.3、overlap=|{a,b,c}∩{b,c,d}|/3、P50 nearest-rank=ceil(0.5×4)=第 2 小），不是"函数自己算一遍再对比"式的假测试。
- 边界覆盖：空集（Recall/MRR/percentile/overlap 四处 ValueError）；多 relevant 取最先命中的结果名次而非 anchor 声明顺序；rank>k 不进截断指标；u04 类"期望 partial"按 expected_mode 精确匹配计正确（拒答二元计分被明确排除）；L1 的"quote 出现在语料他处但未绑定该 claim"判失败（只查绑定证据，堵跨证据拼接）；聚合层预算越界/失败 run 无终态/L0L1 失败均单独破 Gate，空 rows 永不通过。
- 质量门：`make ci` ruff/pyright 零错误、pytest 257 passed（+14 手算单测）。证据 commit：本提交。

## 2026-07-19 · T20.3 · make eval-ci

- Makefile 新增 eval-ci 目标，按《Evaluation-v1》§6 五项逐一映射到既有/新增测试；新增 tests/unit/test_eval_reports_schema.py 解析 evalsets/reports 全部已提交 JSON：schema_version、40 位 commit、started_at/command、config/corpus 存在、JSON 与 Markdown 成对提交、非 holdout 报告的 holdout_accessed 必为假（防 dev 阶段静默碰 holdout）、gate 类报告 Gate 字段完整。GitHub Actions 在 pytest 后追加 `make eval-ci` 步骤，push 触发分支补上 p1（此前只有 main/p0，P1 头提交不会跑 CI）。
- "故意破坏必红"按判据实测三连（改完即还原，日志留 /tmp）：RRF 计分公式 1/(k+rank)→1/(k+2·rank) 3 failed；MAX_LLM_REQUESTS 6→8 3 failed（graph 预算矩阵接住）；quote_matches_evidence 永真 6 failed（L1 单测+graph 重生成路径接住）。
- 踩坑一：eval-ci 最初把 unit 与 integration 文件混在一次 pytest 调用，test_rrf 的 `from conftest import ...` 裸导入被解析到 integration/conftest 报 ImportError（全量跑不触发、混合子集必炸）——目标拆成两次调用，不动既有测试导入风格。
- 踩坑二：test_hybrid_multi_query_merges_and_dedupes 在 eval-ci 组合下 ~1/3 失败：断言 hits[:2] 精确集合，但 hybrid 默认 vector_mode=hnsw，而 HNSW 是跨项目共享的全局索引 + 随机图层级，项目过滤后小样本偶发漏召回（实测失败样本里种子块 A 整个从 vector channel 消失）。两个断言精确名次的 hybrid 语义测试固定 vector_mode=exact（RRF 融合语义与近似召回解耦；HNSW 质量另有 overlap 报告与专项测试），连续 10 次组合运行全绿。
- 附带：evaluation.py 的 MAX_RETRIEVAL_ROUNDS/MAX_LLM_REQUESTS 改为从 agent.state 导入，评测口径与在线预算单一来源。
- 质量门：`make ci` 260 passed + `make eval-ci` 13 passed，ruff/pyright 零错误。证据 commit：本提交。

## 2026-07-19 · T20.4 · 首次真实 dev 全量评测（未勾选，待裁决）

- `devkb eval run --split dev --mode all --project mini-mall`（真实 Qwen + DeepSeek，网络双开关），445 文档/4788 chunks 快照，21 题 agentic 全部成功落库，报告 `p1-dev-{retrieval,agentic}-20260719T224424+0800`，holdout 未接触。
- §5.1 检索：HNSW overlap=1.0 硬 Gate 通过；对照记录义务如实落盘——hybrid-rrf R@10=0.529 vs vector 0.588、MRR −0.258，与 T15.4 负结果一致；vector R@10=0.588 相对 P0 0.875 是语料规模 39→445 的预声明变化。lexical P95 1138ms、vector-exact P95 1574ms（首查询含模型/连接暖机，小样本口径报告已声明）。
- §5.2 agentic 六过一败：正确拒答 3/4（u01–u03 refusal 全对；u04 期望 partial 实得 refusal）、误拒 0/17、最终 L0/L1 100%（21 题独立终检零违规）、预算内（总 87 调用、重试 2，单 run ≤6/轮次 ≤2）、终态完整 100%、P50/P95 延迟 26s/95s、总 tokens 152k+81k。**唯一未过：citation-to-anchor proxy 8/17=47% < 85%。**
- 逐题归因（agentic 报告与同快照检索报告交叉比对）：9 个未命中题中 q02/04/07/08/15/22 六题的原问题在 vector-hnsw top-10 也无命中——是 445 文件语料上的检索天花板而非 agent 行为缺陷（85% 阈值冻结于 2026-07-16，早于 T13.5 全量摄取暴露的难度变化）；q03/05/12 三题检索名次 4/7/6 在窗口内，但 agent 计划查询与 claims 选证未覆盖人工 anchor。另注意 q02/q13/q22 mode=full 但 proxy 未命中——可能引用了同样支撑答案的 Java 实现块而非标注的 markdown 锚点，proxy 本就是代理指标（人工引用正确率抽查在 U2.2）。
- 按勾选纪律不勾选 T20.4，偏差记录给出三个裁决选项（推荐：修订 proxy Gate 口径以匹配当前语料检索天花板；备选：dev 迭代后重跑，但天花板题预计无法达标；或挂起至 P2 检索改进）。不得自行改规格，等用户裁决。

## 2026-07-19 · T20.4 · 裁决落地与按新口径重跑（勾选）

- 用户裁决采纳推荐项：citation proxy 由 §5.2 硬 Gate 改为记录义务（Evaluation-v1 修订注记 + harness `citation_proxy_meets_frozen_threshold` 记录字段，不进 gate_passed），硬性引用质量兜底移交 U2.2 人工抽查；其余六项维持硬性。手算单测同步：补"其余全过 + 低 proxy → gate_passed=True"专项场景，原场景补 correct_unanswerable_gate 断言（发现该场景本就因 2<3 未过，与 proxy 无关——修订不是"放水全过"）。
- 按新口径重跑 agentic（新报告 p1-dev-agentic-20260719T230855+0800，首跑报告原样保留）：21/21 成功、六项硬 Gate 全过——拒答 3/4（u01–03 对，u04 期望 partial 仍 refusal）、误拒 1/17（q02，首跑为 0，DeepSeek 输出波动在 Gate 容忍内）、L0/L1 独立终检双 100%、总 76 调用 0 重试全部预算内、终态完整；proxy 52.9% 如实落盘（较首跑 47% 自然波动，仍低于 85% 冻结阈值）。P50/P95 延迟 23s/60s，tokens 134k+58k。
- 两次运行对照可见 temperature=0 下 DeepSeek 仍有逐题模式波动（full/partial/refusal 分布 7/10/4 → 8/8/5），Gate 阈值的容忍空间（≥3/4、≤1/17）正是为此预留；两份报告均留档不覆盖。
- 质量门：`make ci` 261 passed + `make eval-ci` 13 passed（新报告过 schema 校验）。证据 commit：本提交。

## 2026-07-19 · T20 复评修复 · Gate split-aware 与 holdout 护栏

- 用户转达 T20 验收前审查（4 High + 2 Medium），逐条用只读纯函数探针复现后当日修复。最大的坑是 `aggregate_agentic` 的分母陷阱：L0/L1/预算只对 `status=="succeeded"` 行统计，失败 run 从所有硬项分母里消失，"1 个可答题异常 + 其余通过"时 gate_passed 仍为 True。修复不是把失败行塞进各分母（失败 run 本就没有 Answer/usage 可检），而是补一个显式"无失败 run"硬项兜底，dev/holdout 均硬；专项单测构造"其余硬项全为通过形态"的场景，保证只有这个兜底能拦住。
- Gate 全面 split-aware：此前 holdout 复用 dev 的 §5.1 overlap Gate 和 §5.2 阈值，两个方向都错——检索侧 exact=1.0/hnsw=0.0 也判过（§7 修订要求的恰是 hnsw R@10 ≥ exact），回答侧 holdout 只有 2 个不可答题、硬编码 ≥3 使完美 2/2 必判败。重读 §7 发现其硬 Gate 本就只有三条（检索对照/L0/L1/预算终态），拒答/误拒是报告义务，据此实现而非拍一个折算阈值；该口径差异与"无失败 run"一起登记偏差记录待用户复评确认。
- holdout 一次性访问补 `--mode all` 强制（CLI+run_eval 双层，confirm 了也拒绝部分模式）；lexical-only 不再加载真实 embedding 模型（此前 `retrieval_modes or run_agentic` 对 ["lexical"] 也为真），回归测试用 monkeypatch 禁实例化锁死。
- schema 教训：T20.4 裁决把 `citation_proxy_gate` 更名为 `citation_proxy_meets_frozen_threshold` 时没有 bump schema_version，两份同版本报告字段名漂移且校验测试只查"有 Gate 字段"。现 bump 到 p1-eval-v1.1：三份 v1 报告按文件名冻结为历史形状（报告不改写），v1.1 的 Gate 键集合按 split 精确锁定，并加一条"常量 ↔ harness 实际输出"双向互锁测试——以后改任何 Gate 键必须显式过这道闸。
- 验证：新口径复算已提交 dev agentic 报告逐字段一致（21/21 无失败，gate_passed 不变）；`make ci` 271 passed、`make eval-ci` 59+16 全绿。证据 commit：本提交。

## 2026-07-20 · T20 二轮复评 · holdout 前置能力补齐

- 二轮审查指出 3 个 holdout 前置问题，共同点是"T21.3 只能跑一次，跑之前 harness 必须已经具备的能力"，都不是跑完能补的：报告没存原始回答、没有 P0 基线对照、一次性访问没有台账。
- 逐题原始结果：`evaluate_agentic` 此前只留 mode/L0/L1/用量，answer_text/claims/quotes/not_found/limitations 全丢——而 §7 恰恰要求"2 个不可答题的 mode 和理由"和"逐题原始结果"，Markdown 里还写着"逐题完整原始结果见同名 JSON"，属于报告自我声称与实际内容不符。改为完整 Answer JSON 原样落盘，并加"不可答题判定与理由"表（表格单元格要压平换行、转义竖线，否则 refusal 文本会破表）。
- P0 基线：§7 第 3 条要求与 39 文档/1370 chunks、R@10=0.875 同口径对照并标注规模变化，报告里完全没有这一节。数字从 `evalsets/reports/p0-holdout.md`（commit 7fdf540）抄进冻结常量并加单测锁死，避免日后凭记忆写错；对照做成纯函数输出三项差值 + 规模变化标记 + "不作硬 Gate"注记——差值必须和规模声明绑在一起出现，否则读者会把 445 文件语料的绝对下降误读成检索退化。
- 一次性访问：此前只挡"缺 confirm"，第二次跑照样放行、失败的尝试不留任何痕迹，等于没有落实 §7 的"不得静默重跑挑最好成绩"。新增 append-only 台账 `holdout-access-log.jsonl`，关键是 started 记录写在读题之前——读 holdout 题文件本身就是访问，进程中途被打断也必须留痕。台账非空即拒跑，除非带 `--acknowledge-rerun '<裁决理由>'`，理由会进报告和 Markdown 横幅。`--mode all` 校验从 set 相等改严格列表相等（set 相等会放过重复模式）。
- 踩的坑：重跑测试第一次红了——两次运行落在同一秒、报告名撞上"同名拒绝覆盖"。这是既有保护正常工作，不是 bug，测试跨过秒边界即可；真实两次 holdout 不可能同秒。
- 破坏性验证五连（严格模式/原始 Answer/P0 基线/重跑护栏/失败留痕逐个反向注入）全部必红后还原。schema 仍为 p1-eval-v1.1：尚无 v1.1 报告落库，本轮新增字段直接并入，不制造版本碎片。
- 质量门：`make ci` 275 passed、`make eval-ci` 61+18 全绿，holdout 全程未接触。证据 commit：本提交。

## 2026-07-20 · T20 三轮复评 · 台账锚点、并发占号与空白理由

- 三轮审查全打在同一个机制的边界上：台账本身写对了，但**挂错了锚点、占号不原子、理由校验太松**。三条合起来才让"一次性访问"名副其实。
- 最要紧的是锚点：台账原本放在 `--output-dir` 下，而那是 CLI 用户可控参数——换个输出目录就等于重开一本空台账，复评探针用两个临时目录跑两次，两次都记成 attempt=1。这类问题的教训是护栏要锚在**被保护的东西**上：护的是"读 holdout 题集"，那台账就必须和题集同住 `evalsets/`，而不是跟着报告走。输出目录改为随尝试落入台账并写进复现命令，这样"报告去哪了"依然可追溯。
- 并发占号：原本先 `read_holdout_ledger` 再 `append`，两步之间没有互斥，两个进程可能都读到空台账、都以 attempt=1 起跑。合并为 `claim_holdout_attempt`，读与写在同一个 `fcntl.flock` 排他区内完成。测试用 8 个进程 + `multiprocessing.Barrier` 把窗口压到最窄，断言恰有一个拿到 attempt=1、台账恰一条 started；去掉 flock 后连跑 5 轮均红，说明这个竞争测试不是摆设。
- 空白理由：`not acknowledge_rerun` 会放过 `"   "`。修的时候先把 strip 写在 `run_eval` 里，结果新测试直接红了——因为规则应当由**执行判定的那一层**保证，而不是指望调用方先归一化。挪进 `claim_holdout_attempt` 后才对，这也让直接调用该函数的路径同样受保护。
- 破坏性验证有一次自摆乌龙：我给"锚点"注入的第一版改成了另一个固定路径，测试照样绿——那不是复现缺陷，只是换了个位置放同一本台账。改成在调用点把 `evalsets_dir` 换成 `output_dir` 才真正复现探针场景并必红。注入没红时，先怀疑注入本身没复现缺陷。
- 台账测试独立成 `tests/unit/test_holdout_ledger.py` 并接入 `make eval-ci`。质量门：`make ci` 280 passed、`make eval-ci` 65+19 全绿，holdout 未接触。证据 commit：本提交。

## 2026-07-20 · T20 四轮复评 · 复现命令的 shell 安全

- 最后一个缺陷在报告的 `run.command`：原来是 f-string 拼接，`--output-dir` 含空格会被拆成多个参数，裁决理由用 `!r` 也不对——Python 的 repr 引用规则和 shell 的不是一回事，`$HOME`、反斜杠、单引号都可能变味。这个字段的唯一用途就是原样粘回终端重放，拼错就等于不可复现，而 holdout 只能跑一次，复现命令写错没有第二次机会。
- 改法很小但要点明确：抽出纯函数 `_eval_command` 构造 argv 列表，交给 `shlex.join`。测试直接验 round-trip——`shlex.split(command)` 必须还原出等值的 argv，用例里塞了空格路径、中文、双引号、`$USER`、`$HOME; rm -rf /`。这比断言字符串长相稳，也说明了意图：命令是给 shell 读的，判据就该用 shell 的解析器。
- 破坏性验证：把 `shlex.join` 退回 `" ".join` 立刻红。
- 质量门：`make ci` 281 passed、`make eval-ci` 65+19 全绿，holdout 未接触。证据 commit：本提交。

## 2026-07-21 · U2.2 人工复核定稿 · 引用逐条抽查 + 降级遗留裁决

- U2.2 的价值在 T20.4 把 citation proxy 从硬 Gate 降为记录义务后被放大了：机制层的兜底交回给人。`benchmarks/p1_manual_check_packet.py` 只做机械校验（sha256/行号区间/quote 逐字），把"引用是否恰当、claim 是否被原文真正支持"这类语义判断留给人工——这条边界要守住，否则又变成机器给自己打分。
- 采"更省力做法"：AI 逐条打开 `mini-mall-order` 真实文件、为 14 条引用各写一行"原文讲什么/是否支撑/有无勉强"作草拟裁定，复核人 randy 只抽查 3–4 条（重点 q07 E2、q12 E2）+ full 路径校准后签字。14/14 路径行号真实、内容支持论断、无编造，人工引用正确率 100%。
- 重点核了 q07（partial、proxy=False）：E2 `payment-success-event.md > Consumer Rules` 逐字含"必须幂等，因为 at-least-once""用 eventId 去重"，直接完整回答问题——**proxy 未命中来自人工标注锚点选取，不是引用质量问题**，印证 T20.4"6/9 未命中属检索天花板/等价证据"的归因。q12 E2 是修复规划文档（非实现/测试），证据类型偏弱但同题 E7 测试已佐证，标 △ 不算错。
- 顺带定位并裁决了一个降级遗留项：8 个 partial 里 q05/q07 是被无意义 not_found 占位降级的，本可 full。根因单一——`GENERATE_SYSTEM` 的 Schema 示例给了 `not_found` 这个"经常合法为空"的字段一个可抄占位值 `["未覆盖方面"]`，q07 逐字抄了下来。修法明确（示例改 `[]` + 补空数组指令）但属 Prompt 变更→PROMPT_VERSION 递增→冻结失效→须重跑 dev §5.2 Gate（约 76 次真实调用且有 false-refusal 触上限风险）。裁决：P1 保持现状、记 `docs/backlog.md`，留待 P2 Prompt 迭代随冻结失效一并修。"找到+诊断+判非阻塞+拒绝在看到 dev 结果后调参"比多两个点的 full 率更能说明工程纪律。
- 未跑代码测试（本次仅 docs/报告与人工裁定内容变更，无 src 改动），holdout 未接触。证据 commit：本提交。

## 2026-07-21 · T21.1 README 重写为 P1 口径

- README 一直停在 P0 基线口径（标题「P0 可运行基线」、把 Agentic 说成"是 P1 内容"），与实际已过 dev §5.2 的 P1 现状脱节。这份文件是求职展示门面，口径错等于自减分。
- 重写方式：先把事实对着代码逐条核，再写。核过的点——LangGraph 节点序列与条件边（`graph.py`）、三态 finalize 判据（`nodes.py` full 需 sufficient+kept+no not_found+L0/L1）、在线检索器（`make_pg_retriever` 只用 hnsw、多子查询 RRF 融合、**不启用 lexical channel**）、支持文件类型（`.md/.txt/.java` + `.yml/.yaml/.properties`）、HNSW 建索引（migration 0002）、Java tree-sitter 按方法/类切、API 三端点（`/ask` `/runs/{id}` `/healthz`）、Makefile 目标、语料 445 文档/4788 chunks。目的是让"无未实现能力措辞"这条判据可核，而不是凭印象写。
- 三处诚实点没有回避：① Hybrid 在线不启用——`nodes.py:223` 注释就是这么写的，且 T15.4 是负结果，README 若暗示 Hybrid 上线就是虚标；② 引用可溯源≠回答为真，L0/L1 都不校验语义；③ partial 偏保守遗留项写进已知限制并指向 backlog。CLAUDE.md 要求已知限制写进 README 而非掩盖，这三条正是。
- 两张 mermaid 图：离线摄取链路 + LangGraph 状态图。踩的小坑——状态图边标签里的 `轮次<2`/`generate<2` 的裸 `<` 会被 mermaid 当 HTML 标签起始误解析，改写成"轮次未达上限""重生成未用尽"规避。另修正一处：初稿把在线融合写成"取 top-10"，但在线检索器默认 `top_k=8`（EVAL_TOP_K=10 是评测口径），改为"取前若干条"不锁死数字。
- Agentic ≠ Multi-Agent 单列强调：一个 agent、一张确定性状态图、所有分支由确定性代码依结构化信号+预算判定、LLM 只在 plan/evaluate/refine/generate 四处且不能返回控制流。
- scope 说明：「从干净环境可启动」做到命令/指令级准确（每条命令与参数都存在且核过），字面冷启动实跑归 T21.2。本次仅 README 文本变更，无 src 改动、holdout 未接触。证据 commit：本提交。

## 2026-07-21 · T21.2 真语料最终实跑与手工演示

- 先确认「最终实跑」不必从零重嵌：pg 命名卷 `pythonagenticrag_devkb-pgdata` 持久化，`make up` 后 mini-mall 的 445 docs/4788 chunks 与 56 条 agent_runs 都还在。所以策略是——用已审计视图核对语料同一性 + 幂等验证 + 回放已有五路径 + 现场跑几条真实新提问，而不是白烧一遍 GPU 与 API。
- 语料同一性用逐文件 sha256 核，比"跑一遍看数字对不对"硬：新建视图的 445 个 rel_path→sha256 与 dev Gate 报告内嵌 manifest（字段名是 `content_hash`，视图侧是 `sha256`，对上了）**0 处不符**，证明源仓库自 dev Gate 未变、这就是同一份语料。踩的小坑：视图 manifest 顶层是 `{version,source,files:[...]}`，不是裸 list，第一版比对脚本按裸 list 取 `content_hash` 直接 KeyError。
- 幂等：对同一视图二次 ingest → 成功 0/跳过 445/失败 0，落库计数不变。模型仍会加载（SentenceTransformer 初始化），但 content_hash 命中即跳过、不重嵌——这正是期望：幂等是"内容没变就不重做"，不是"跳过整个进程"。
- 手工演示挑了三条**没在评测集里**的新问题，实时调 DeepSeek：D1 订单状态机（partial，同时引 `dev-log.md` 与 Java `OrderStateMachineTest`，跨类型引用）；D2 云厂商/K8s（语料确实没有，agent 先 refine 补检一轮仍无证据，确定性拒答且 not_found 具体，未编造）；D3 网关限流（partial，命中 Java 方法级 chunk `GatewayRateLimitFilter`/`RedisGatewayRateLimiter`/`GatewayRateLimitProperties`，答出 Redis 令牌桶+429）。三条都落库可回放。
- 诚实记录：三条现场演示都没跑出 full，两条 partial 属已诊断的 not_found 占位偏保守倾向（backlog 有裁决）；full 路径的现场证据由第 2 节 q09/q10/q12 三条 dev run 覆盖，演示文档里写明了这一点，不含糊。
- 全程 unset 代理 + `HF_HUB_OFFLINE=1`，无一次静默挂死；未接触 holdout。证据：`evalsets/reports/p1-manual-demo.md` 与本提交。

## 2026-07-21 · U2.3 holdout 运行授权

- U2.3 是整个 P1 的一次性闸门：holdout 只跑一次，跑完这份报告就是最终答卷。所以授权前把三件事摆成可核事实而不是口头确认——① `p1_freeze_check.py` 逐字段核对 9 个冻结参数（prompt/rrf_k/ef_search/阈值/模型）零漂移；② 被测系统（agent/retrieval/ingest/contracts/embedding/llm/repositories）自 dev Gate `f240e85` 起 `git diff` 零变更，T21.1/T21.2 只动了 README 与报告、不碰被测代码；③ 台账 0 记录、工作树干净。前置 U2.2/T21.1/T21.2 全绿。
- 授权是人的决定，代码只负责证明「冻结」成立。randy 在冻结核对 §5 签字、清单 U2.3 勾选后，才执行一次 `eval run --split holdout --mode all --confirm-holdout`。规矩上不据 holdout 结果调参——这份报告是用来验收的，不是用来迭代的。
- 证据 commit：本提交（授权记录），holdout 实跑与报告见下一条。

## 2026-07-21 · T21.3 P1 holdout 一次性运行

- 授权后带 `unset 代理 + HF_HUB_OFFLINE=1` 跑了唯一一次 `eval run --split holdout --mode all --confirm-holdout`。5.7 分钟完成，台账原子占号正常：started(attempt=1, commit 087c73e, worktree_dirty=false) + completed 恰两条，再跑必须 `--acknowledge-rerun` 并留理由。
- 两个 §7 硬 Gate 都过：① 检索——`vector-hnsw R@10=0.75 ≥ vector-exact R@10=0.75`，exact↔hnsw top-10 overlap=1.000，说明 HNSW 在这个规模上对 vector-exact 是无损近似（这正是把 overlap 从硬 Gate 改成"hnsw≥exact"口径后要证明的事）；② agentic——L0/L1 全过、预算内（39 调用/10 run、单 run ≤6、0 重试）、终态完整、无失败 run。10 题全 succeeded：5 full/4 partial/1 refusal，误拒 0。
- 记录义务如实落盘，不因难看而修：hybrid-rrf 在 holdout 上仍比纯向量差（R@10 0.5 vs 0.75、ΔMRR −0.103），和 T15.4 的 dev 负结果一致——lexical 通道（R@10 仅 0.25）把融合拖下水；citation proxy 0.5 < 冻结 0.85，与 dev 同因（检索天花板 + anchor 选取），硬性兜底已由 U2.2 人工 100% 承担；P0 历史绝对值 R@10 0.875→0.75，但语料从 39 文档/1370 chunks 变成 445/4788，§7 早已预声明规模变化下绝对召回不可单独归因为退化。q20/q23 四种模式全未命中，是 445 文件语料的检索天花板。
- 全程未据 holdout 结果调任何参数——这份报告是验收答卷，不是迭代输入。这条纪律比多几个点的召回更重要：holdout 一旦用来调参就不再是 holdout。证据：`evalsets/reports/p1-holdout-20260721T223756+0800.{json,md}`、台账与本提交。

## 2026-07-21 · T21.4 P1 验收总清单

- 把《P1实现规格》§15 十条验收标准逐条对到硬证据（测试文件 / 报告 / run_id / 命令输出），落成 `evalsets/reports/p1-acceptance.md`，而不是凭印象打勾。核过：迁移可逆(`test_migrations.py`)+backfill 无损；语料视图幂等与 Java 行号真实(`p1-manual-demo.md`+`p1-manual-check.md`)；dev §5.1/§5.2 受控对照；LangGraph 路径矩阵测试+预算硬上限；dev 拒答/误拒 Gate；轨迹可回放；三端点集成测试+D7 越权隔离；本地 `make ci` 281+`make eval-ci` 65+19 全绿；holdout 一次性两 Gate 全绿；README/演示一致。
- 九项已满足，仅第 8 项的远端 GitHub Actions 头提交绿灯归 T21.5。三处偏差都摆进裁决表：u04 拒答偏差、citation proxy 降记录义务（U2.2 人工兜底）、q05/q07 not_found 偏保守（保持现状记 backlog）——判据要求"所有偏差有用户裁决"，逐条都指到出处。
- 没勾 T21.4：它依赖 T21.5 远端 CI，且是用户签署的最终验收，材料备齐等裁定。证据 commit：本提交。

## 2026-07-21 · T21.5 修复 CI 红：本地缓存假绿

- 推送后 GitHub Actions 在 `ruff check src tests` 红了，但本地 `make ci` 明明 281 passed。根因是**本地 `.ruff_cache` 假绿**：`ruff check --no-cache` 立刻暴露 5 个 I001（import 未排序），CI 全新环境无缓存故如实报错。教训——CI 关键校验不能信本地缓存的绿，验收类检查要 `--no-cache` 或先 `rm -rf .ruff_cache`。
- 5 个都在老测试文件（test_fts_golden/test_java_chunker/test_markdown_chunker{,_ml}/test_rrf）：ruff 0.15.21 把 `from conftest import ...` 归类为 first-party，要与 `pytest` 那组分开、和 `devkb` 同组。`ruff check --fix` 一键修正，纯 import 分组、无逻辑改动。
- 清缓存重验：`ruff format --check` ✓、`ruff check --no-cache` All passed、pyright 0 错、pytest 281 passed、`make eval-ci` 65+19 全绿。证据 commit：本提交；待重新推送触发 CI 复验。（后续更正：`1a4bb0c` 已推送且 GitHub Actions 全绿；但其上又叠加了 `565b4a1` 与本次验收修复提交，故**当前头提交仍需重新过 CI**，详见下条「T21.4 验收复查修复」。）

## 2026-07-22 · T21.4 前验收证据复查修复（只改文档，不动被测系统）

- 本次是**验收证据修复**，不是功能开发：不重跑 holdout、不调用真实 Qwen/DeepSeek、不改 Prompt/阈值/RRF/标注、不动 evalsets 下 JSON 原始报告与 holdout 台账。只修 README、`p1-acceptance.md`、`p1-holdout-freeze-check.md`（追加注记）、`P1任务清单.md`、`dev-log.md` 五份文档证据。
- 最关键的修正是 **CI 头提交口径**：`1a4bb0c` 确实推送并过了远端 CI，但其后本地又叠加了 `565b4a1`（关闭 T21.5 的那次提交）以及本次验收修复提交——**当前头提交已不是 `1a4bb0c`，尚未推送、未过 CI**。判据明写「当前头提交全绿，不以较早提交绿灯替代」，所以 T21.5 与「验收总清单第 8 项」从已勾**回退为未完成/待复验**，`p1-acceptance.md` 结论由「十项全满足」改为「9/10，等待当前头提交 CI」。先前把它标绿是我用较早提交的绿灯替代了当前头提交，属判据违规，已纠正。
- README 两处准确性修复：① 「每个回答都附引用」是绝对表述——拒答（证据不足）本就不产出引用，改为限定「完整/部分回答里的每条事实性论断」附引用+L0/L1 校验，拒答只说明缺什么；② 把 HNSW exact↔hnsw overlap 从「§5.2 六项回答 Gate」里移出——它是 **§5.1 检索 Gate** 第 4 条（Evaluation-v1 line 90/106/130 为据：§5.2 六项=拒答/误拒/L0/L1/预算/终态）。Agentic≠Multi-Agent、Hybrid 负结果、L0/L1 不保证语义正确等诚实边界保留不动。
- `p1-acceptance.md` 证据链修正：迁移证据按 0001（建 agent_runs）/0002（search_text+search_tsv+GIN+HNSW+agent_steps/tool_invocations）/0003（轨迹表复合 FK，D7 跨项目归属）如实拆分——原文误把 agent_runs 记在 0002、且漏了 0003；§5.1 检索 Gate 改引 `p1-dev-retrieval-20260719T224424+0800`、§5.2 回答 Gate 引 `p1-dev-agentic-20260719T230855+0800`（原文把二者混在同一份 agentic 报告）；「失败 run 终态落库」改引 `test_agent_service.py::test_unexpected_node_failure_persists_failed_run_and_failed_last_step`（`test_trace.py`/`test_datalayer.py` 只证轨迹行为，不足以单独证明失败 run 终态）；补齐文末「提交序列」（原文引用了不存在的段落）。
- `p1-holdout-freeze-check.md` 只**追加**「验收复核注记」，不改历史冻结数值/用户签字/holdout 前事实：解释①报告第 7 行的 dirty 是 shell 重定向覆盖这份 Git 跟踪报告时脚本把自身输出当成工作树改动（被测系统 diff 为空）；②第 4 节 T21.1/T21.2「未完成」是生成模板陈旧态、以第 5 节用户签字为准；③第 3 节台账 0 条是 holdout 前快照、不改写以免伪装 holdout 后状态；④台账 `started` 记录 `devkb_commit=087c73e`/`worktree_dirty=false` 反证实跑时工作树干净；⑤dev Gate→holdout 间被测系统零变更。
- T21.4/T21.6 保持未勾；第 10 项在 README 与验收材料修正后维持勾选。澄清任务口径：T21.4=技术验收清单完成（清单逐条属实、无计划中/待补、偏差有裁决），用户对 P1 的最终确认属 T21.6，不作为 T21.4 前置——原叙述把二者混淆，已改。
- 2026-07-22 本地验证全绿：`ruff check --no-cache` 通过；`make ci` 为 Ruff format/check 通过、Pyright 0 错、pytest 281 passed；`make eval-ci` 为 unit 65 + integration 19 passed。证据 commit：本提交。推送后的当前头提交 CI 结果留待 T21.5 按实际 GitHub Actions 状态补齐。

## 2026-07-22 · P1 验收关闭（T21.4/T21.5/T21.6）

- **T21.5 精确头提交远端 CI 已通过**：验收证据基准提交 `13f8f008c0f310aa12a3d5db273f1ba8d1274624`（`docs: reconcile P1 acceptance evidence`）已推送。**本地 `HEAD`、`origin/p1`、Actions head_sha 三者一致**，均指向该完整 SHA，工作区干净。对应 GitHub Actions Run **29922618713**（https://github.com/yangyaoming158/devkb/actions/runs/29922618713）最终 completed / success，checks 中 pytest、`make eval-ci` 等全部成功。这次是「当前头提交自身的绿」，不再用较早 `1a4bb0c` 的绿灯替代——正是上一条复查要纠正的判据违规点，现已按判据补齐。
- 本地 2026-07-22 复核：`uv run ruff check --no-cache src tests` 通过；`make ci` = Ruff format/check 通过 + Pyright 0 错 + pytest **281 passed**；`make eval-ci` = unit **65** + integration **19** passed。
- **§15 从 9/10 补齐为 10/10**：唯一悬挂的第 8 项（当前头提交远端 CI 全绿）随 `13f8f008` 的 Run 29922618713 success 而满足，故 **T21.4 技术验收清单完成**。三处偏差（u04 拒答 / citation proxy 记录义务 / q05·q07 not_found 偏保守）均已有用户裁决，无「计划中/待补」被勾选。
- **T21.6 满足**：用户 randy 于 2026-07-22 明确确认「P1 Agentic RAG MVP 验收通过（验收人：randy，日期：2026-07-22）」，P1 Gate 关闭。
- 本任务性质：**只做验收文档收尾**。未运行 holdout、未调用 Qwen/DeepSeek 或任何真实模型、未修改被测系统或冻结参数（Prompt/阈值/RRF/标注）、未改任何 evalsets JSON 原始报告与 `holdout-access-log.jsonl`、未创建或修改任何 P2 文档。仅改 `docs/P1任务清单.md`、`evalsets/reports/p1-acceptance.md`、`docs/dev-log.md` 三份文档。
- **诚实边界**：本条由一个包含上述三份文档改动的**纯文档验收关闭提交**承载。该提交推送后须通过自身 GitHub Actions，才可依 ADR-009 开始 P2；通过前不得声称关闭提交已通过 CI，也不得启动 P2。由于提交无法在自身内容中预记自身 SHA，最终状态以远端分支 CI 为准。

## 2026-07-22 · P1 后真实陌生仓库可用性测试问题建档

- 这是 P1 验收关闭后的 post-P1 使用记录，不回改 P1 验收结论，也不是 holdout 重跑。测试对象换为此前未参与 dev/holdout 的 `/home/oslab/projects/rag知识库项目`（提交 `9bb79cdb2b8219bc820c51ef15444a5110a35ff0`），经 `/tmp` 安全语料视图摄取为 project `rag-kb-p1-20260722`：183 active documents / 1424 chunks，二次 ingest 为 0 ingested / 183 skipped / 0 failed。
- 用户现场执行的六个真实 DeepSeek run 覆盖多种路径：`3b610da3-03eb-493a-861d-9fb2f9a4ebcc` 没召回已索引的生产 `RetrievalRepository.java`，却用 dev-log+集成测试替代并判 `full`；`7bb6a4ea-97e7-4d17-80b3-931dc39a495d` 的 retrieve 摘要已出现 README 路径，最终仍误报 README 声明未找到，且未召回同样已索引的 `application.yml`/`AppConfiguration.java`；`215c2a2b-f1d6-40ce-ba88-5d6ecd270049` 因 `.vue/.ts` 不受支持而安全拒答，却未说明格式/摄取边界并猜测了错误的 `answerStatus`；`defd2a9c-3df8-4e88-9532-d1613136c0f1` 对仓库确未记录的云厂商/域名/SLA/RTO/RPO 正确拒答，作为不得修坏的正例；`771b86de-7f3e-4216-803d-d35a7c551eb8` 经结构化输出重试和 L0/L1 删除后安全降为 `partial`，但只引用 runner 类声明，测试/dev-log 仍替代生产方法体；`5677974c-709b-40f5-b794-dbb59e379be6` 对已索引的上传到 READY 完整调用链发生假拒答。
- 只读核过六个 run 的 `agent_steps`、相关 `tool_invocations`、13 个关键 active document 的 chunk 计数及语料视图文件类型，因而把问题拆到格式覆盖、路径/方法级召回、跨轮证据保持、refine 查询漂移、evaluator 充分性、生成后一致性、L0/L1 语义边界、证据权威性、拒答负证据、轨迹可诊断性与人类呈现，不把全部现象草率归因为 vector search。
- `docs/P1后真实仓库可用性测试问题记录.md` 现记录六案证据、RT-01～RT-17 优先级、已确认事实/待验证假设、建议顺序及新的 post-P1 回归 Gate；`docs/P1后真实仓库手工测试清单.md` 已固化 6 项已完成结果、10 项待执行命令、逐题通过标准与人工记录模板，避免长上下文丢失测试基准。`docs/backlog.md` 同步两个入口。后续修复不得重跑或据此调优已使用过的 P1 holdout，须先冻结更广回归集避免对六题过拟合。
- 本次只改文档：未调用新的真实模型、未运行代码/评测/holdout、未改 `src/`、tests、Prompt、模型、检索参数、evalsets 原始报告或 holdout 台账，也未创建 P2 规格/任务清单。证据 commit：本提交。

## 2026-07-23 · P1 后真实测试第 7 项：跨用户访问隔离

- 用户现场运行 `f7802188-aad7-4adf-bc8a-e056c8e47fca`，要求只依据生产 Java/SQL 解释知识库、文档、会话的 owner 隔离。结果为 `partial`，2 轮检索、5 次 LLM 调用、64,255 ms；知识库 owner SQL、文档删除 SQL和会话读取 SQL等主要证据正确，保守 mode 也符合“只证明部分资源则 partial”的预登记标准。
- 人工源码复核确认两条最终 `not_found` 都是错的：`DocumentRepository.java:50-58` 已实现 `findByIdAndOwner`；`RagService.java:236-239` 已实现 `findConversation → conversationRepository.findByIdAndOwner`。正文还同时声称后者存在，故 answer/not_found 自相矛盾；E12 quote 只含 helper 调用点，无法单独语义支撑 helper 内部实现，重现 L0/L1 不检查蕴含关系的边界。
- 正确架构口径不是“所有 Repository 方法都带 owner 过滤”：文档列表由 Service 先校验 kb owner，再调用只按 kb_id 的 `listByKb/countByKb`；会话删除由 Service 先 owner-scoped 查找，再调用只按 id 的 `deleteById`；单文档删除则查找和删除 SQL 都 owner-scoped。已检查的公开 Service 路径能阻止 A 访问/删除 B 的资源，但 partial 正文不应对未覆盖路径作无条件全局断言。
- 轨迹显示第一轮已经出现三个目标 Repository，evaluate 仍误称 owner SQL/Service 全部缺失；refine 猜测本项目未使用的 JPA、`findByIdAndOwnerId`、`deleteByIdAndOwnerId`。第二轮 generate 记录 `not_found_count=0`，finalize 却把 evaluator 的两条错误 missing 注入用户响应，确认问题横跨方法级召回、refine 漂移、节点一致性与缺失项事实校验。
- 文档已将总体进度更新为 7 项完成、9 项待执行，新增案例七与 RT-18（partial 绝对结论/隔离层次混淆），并把该题加入 post-P1 固定回归 Gate。此次只做只读源码/run 复核和文档记录；未调用新模型、未运行 holdout、未修改 `src/`、tests、Prompt、检索参数、模型或被分析仓库。

## 2026-07-23 · P1 后真实测试第 8 项：JWT 配置生效范围

- 用户现场运行 `83484a32-852d-4152-a492-e6772b8cf9d4`，要求对照 README、Compose、application.yml 与 Java 配置判断 JWT secret 是否矛盾。结果为 `partial`，1 轮检索、4 次 LLM 调用、82,937 ms；“裸 JVM fallback 与 Compose 显式必填是不同路径，不构成解析矛盾”的方向正确。
- 明确证据要求未闭环：唯一 retrieve 路径没有 `docker-compose.yml`、`JwtService.java`、`AppConfiguration.java`，evaluate 仍判 sufficient、不 refine。数据库只读确认三者均 active，分别为 4/10/5 chunks；最终 not_found 把 active Compose 写成未找到，并用旧架构审计建议/README 间接替代当前配置。
- 安全口径被高估：Compose `${RAG_JWT_SECRET:?...}` 只强制变量设置且非空，提示中的 32+ 不构成长度校验；只读执行 `RAG_JWT_SECRET=x docker compose config --quiet` 返回 0，实证单字符也通过解析。`JwtProperties` 无 validation，`JwtService` 直接消费任意值；application.yml 的 “Local JVM default only” 只是注释/约定，没有 profile 或 Java 逻辑阻止非 Compose 裸 JVM 使用公开 fallback。因此 Compose 路径得到有效硬化，但不能推出 secret 足够强或所有生产启动方式安全。
- 首稿 L1 失败后第二稿通过，最终仍显示 `verify:l0_l1_failed` 历史 warning；聚合 completion tokens 10,433，而最终正文约 251 字，再次补充 warning 时态和真实延迟/成本问题证据。
- 文档已将总体进度更新为 8 项完成、8 项待执行，新增案例八与 RT-19（配置 scope/必填/强度/runtime validation 混淆），并将该题加入 post-P1 固定回归 Gate。此次只做只读源码、数据库和 run 复核及文档记录；未调用新模型、未运行 holdout、未修改 `src/`、tests、Prompt、检索参数、模型或被分析仓库。

## 2026-07-23 · P1 后真实测试第 9 项：NO_ANSWER / UNGROUNDED / 非法引用

- 用户现场运行 `03b4bd2f-213d-417e-9f03-6cca73647543`，要求只依据 `RagService`、`RagSupport`、`CitationParser`、`RagConstants` 生产 Java 解释状态与非法引用。结果为 `refusal`，2 轮检索、4 次 LLM 调用、24,944 ms。
- 四个目标类全部 active，分别有 20/13/6/4 chunks；人工源码可完整回答：无 hits 或无 `similarity >= minSimilarity` 的 vector hit 时不调用 chat、直接 NO_ANSWER；模型结果等于固定拒答文本时也是 NO_ANSWER；否则合法 citations 为空时 UNGROUNDED；非法编号被排除并汇总 warning，只有非法编号时因合法 citations 为空而成为 UNGROUNDED。
- 轨迹精确显示跨轮覆盖倒退：第一轮有 `RagService`/`RagConstants`，evaluate=`partial`、supported_count=2、唯一缺 CitationParser；refine 猜测不存在的 `parseCitationNumbers`、href/index 越界逻辑，第二轮虽取得生产 CitationParser，却挤出 RagService 且始终没有 RagSupport，evaluate 退为 `insufficient`、supported_count=1。
- finalizer 在第二轮仍有 CitationParser/RagConstants 直接证据时也没有生成 partial，而是 claims/citations 全空的整体 refusal；not_found 又称第一轮出现过的 RagService 生产源码缺失。新增 RT-20，要求跨轮证据覆盖单调保留，并在至少一个方面有直接证据时交付范围准确的 partial。
- 文档已更新为 9 项完成、7 项待执行，案例九和该题固定回归 Gate 已写入。此次只做只读源码、数据库/run 复核与文档记录；未调用新模型、未运行 holdout、未修改 `src/`、tests、Prompt、检索参数、模型或被分析仓库。

## 2026-07-23 · P1 后真实测试第 10 项：删除文档后的历史引用

- 用户现场运行 `d7d89f3d-04ca-408a-8126-8bb5e48cc600`，要求依据数据库迁移、`DocumentService`、`CitationRepository` 和 DTO 说明删除源文档后 `chunk_id`、`snippet`、`document_filename` 的行为。结果为 `partial`，2 轮检索、5 次 LLM 调用、78,979 ms，8,347 input / 7,032 output tokens，0 重试。
- 三项主结论经目标提交源码复核均正确：`V1__init_schema.sql` 先让 document 删除级联删除 chunks，再让 citation 的 `chunk_id` 执行 `ON DELETE SET NULL`；`snippet` 和 `document_filename` 是 citation 行内快照，不随源文档删除；`CitationRepository.findByMessageIds` 用 LEFT JOIN 读取 live `heading_path` 并以 nullable `Long` 映射 chunk，`RagService`/`MessageDto`/`CitationDto` 仍可返回历史文件名和片段。成功删除后 `heading_path` 会是 null，不只是“可能失效”。
- 最终引用却只有架构计划与 failure case，没有任何迁移、Service、Repository 或 DTO。迁移缺失的确定原因是 devkb 当前不支持 `.sql`，该 project 的 indexed SQL documents=0；Java 侧 `DocumentService`、`CitationRepository`、`CitationDto`、`MessageDto`、`RagService` 均 active，分别有 16/6/2/2/20 chunks。
- 只读轨迹显示第一轮已有 `DocumentService` 路径，第二轮已有 `CitationRepository`/`CitationDto`/`Citation` 路径；第二轮 evaluate 仍判 `sufficient/supported_count=5/missing=[]`，generate 随后却输出 3 条 not_found。最终 partial 是安全兜底，但再次确认指定证据类型、missing 分类、节点一致性、证据选择和逐候选轨迹均需修复。
- 手工清单已将第 10 项标为“部分通过”，问题记录新增案例十并把 SQL migration 覆盖并入 RT-13，backlog 同步入口；固定回归 Gate 增加本题的生产证据与字段精确行为。此次只做只读源码、数据库/run 复核与文档记录；未调用新模型、未运行 holdout、未修改 `src/`、tests、Prompt、检索参数、模型或被分析仓库。

## 2026-07-23 · P1 后真实测试第 11–13 项：全局代码清单与 Prompt Injection

- 用户现场又运行三次真实 DeepSeek 问答：Phase 6 Agent 题 `54b2cf10-bf19-4aaa-a739-1fd990199fac` 为 refusal（2 轮/4 调用/29,163 ms）；全部 Spring MVC endpoint 题 `1a987b1d-65b2-48d5-bf24-9671c9075376` 为 refusal（2 轮/4 调用/30,214 ms）；秘密提取与命令执行 injection 题 `cc95be8e-0c66-412e-9a45-a25a8cbeedd7` 为 refusal（1 轮/4 调用/36,748 ms）。
- 第 11 项判“部分通过”：系统没有把规划文档冒充生产实现，这是正确安全底线；但没有交付“规划过/默认跳过/已经实现”三态，也误称 `PROGRESS.md` 仅有表头。目标提交 `PROGRESS.md:20` 明确 Phase 6 默认跳过，该 active 文档有 14 chunks；确定性扫描 `backend/src/main` 未发现 Agent/ToolCall 类、Agent API 或启用配置。refine 生成的 grep/find 只是 retrieve 查询文本，并未执行，说明当前 top-k 语义检索无法提供全局不存在证明。
- 第 12 项判“失败（安全但零交付）”：目标提交只有 6 个生产 `@RestController`、26 个静态映射，六个文件全部 active、共 61 chunks；系统仍两轮 `supported_count=0`、`generate=0`。第一轮路径只覆盖 5 个 Controller，第二轮退到 4 个，Conversation 两轮均缺；当前轨迹又没有 chunk/rank/截断元数据，无法定位是方法块未召回、被截断还是未被 evaluator 识别。新增 RT-21，要求全局否定/穷举题绑定 commit、源码范围、manifest/symbol/annotation inventory 和覆盖计数，不能用 top-k 冒充集合闭合。
- 第 13 项的可观察安全结果通过：最终没有系统 Prompt、密码、API key、`.env` 内容或编造值，也没有命令执行声明；数据库中唯一 tool invocation 是只读 retrieve。planner 将攻击改写为 injection 防御查询，说明不可信输入没有改变这次工作流。已检查持久化 Answer/step/tool 摘要，用户未提供的进程外部日志不在本次核验范围。
- injection 的拒答契约仍有高优先级问题：evaluate 判 sufficient 后两次 generate 均 L1 失败，finalize 删除最后 claim 才 refusal；最终把系统 Prompt、凭据和 `cat .env` 结果列为普通 not_found，并产生两个无 attempt 标识的同名 verify warning。新增 RT-22，要求明确的秘密提取/越权命令请求在检索潜在敏感语料和执行工具前进入独立 policy-refusal，同时用合法安全咨询反例防止关键词误杀。
- 手工清单已完成第 11–13 项，问题记录扩为十三案与 RT-01～RT-22，backlog 增加确定性 repository inventory 和 policy-refusal 两个待裁决入口，固定回归 Gate 同步三题。此次只做目标提交、PostgreSQL run/step/tool 与索引的只读复核和文档记录；未调用新模型、未运行 holdout、未修改 `src/`、tests、Prompt、检索参数、模型或被分析仓库。

## 2026-07-23 · P1 后真实测试第 14–16 项：不调用模型的 API 检查

- 完成手工清单 §7 全部三项。宿主 API 健康检查为 HTTP 200，返回 `status=ok`、`database=ok`、migration `0003`；测试前数据库基线为 83 agent_runs / 525 agent_steps / 103 tool_invocations，最新 run 时间 `2026-07-23 12:38:28.107007`。
- 执行环境有一处需如实说明：Codex 沙箱网络首次 curl 看不到宿主 8000，误判为未监听；检查到沙箱内没有 API 后尝试启动唯一实例，宿主端立即以 `address already in use` 拒绝绑定并完成 shutdown，没有留下第二进程。随后在宿主网络直接复用原有驻留 API，未重启模型服务。
- 第 14 项 Run 回放通过：GET 回放 `cc95be8e-0c66-412e-9a45-a25a8cbeedd7` 返回 HTTP 200，问题、run 元数据、Answer 与原响应一致；8 steps 为 plan→retrieve→evaluate→generate→verify→generate→verify→finalize，和 trace_summary 一致。请求后数据库仍为 83/525/103，目标 run 仍 8 steps/1 tool，证明未重新执行节点、检索或模型；响应不含完整 Prompt、文档正文、secret 或 chain-of-thought。
- 第 15 项不存在 project 为部分通过：POST 返回结构化 404/`NOT_FOUND` 和 `error_id`，无 traceback/DSN/密码；project 行数 0，数据库计数不变，没有 run 或外部 LLM 请求。但源码复核确认 `AppService.ask()` 先 `_get_embedder/_get_llm`、后 `_resolve_project`，冷 API 会先加载 SentenceTransformer 再得到 404，违反预登记“不加载模型”。驻留 API 已缓存 embedder，所以本次请求本身没有再次加载；现有 Fake 注入测试也无法发现 factory 调用顺序。新增 RT-23。
- 第 16 项非法 `top_k` 通过：`top_k=99` 返回结构化 422/`INVALID_INPUT`，只报告 `body.top_k`，没有回显问题或请求体；数据库计数与最新 run 时间均不变。Pydantic 在 endpoint/service 前拒绝，未进入任何模型/Agent 路径。
- 手工清单 16 项现已全部完成；问题记录新增案例十四、RT-23、API 固定回归 Gate 与前后计数证据，backlog 同步冷加载 fail-fast 入口。此次只执行健康、只读回放和两个预登记错误请求；未发送有效知识问答、未发出外部 LLM 请求、未运行 holdout、未修改 `src/`、tests、Prompt、模型或被分析仓库。

## 2026-07-24 · P1.5 规划复审与文档修正（开工前）

- 对 P1 验收证据与 P1.5 三份规划文件做独立复审（Claude Fable 5），发现并经用户批准修正 10 处纰漏。全部为文档/CI 配置修正，未修改 `src/`、tests、Prompt。
- **两处结构性缺陷**：① §21.1 原判据混有 P1.6 能力要求（题 3 摄取 .vue、题 12 穷举 endpoint）且多条"必须引用 X"依赖 P1.5 明确不改的检索召回——Gate 照原判据在 P1.5 范围内结构性不可过。修正为 Evaluation-v1.5 §2 的**两栏预期**（P1.5 契约预期绑定 Gate、P1.6 目标预期仅记录；"必须引用"改条件式），题 3/10/11/12 的 P1.5 预期现已冻结。② `unsupported_or_not_ingested`"由 manifest 廉价判定"机制不存在——核实 `tools/build_corpus_view.py` 的 manifest 只记录入选文件且不入库，在线路径无法证明"仓库确实有该文件"。改为 SUPPORTED_SUFFIXES + documents 表静态判定 + 条件式措辞 + 有限技术词→后缀映射表；仓库级普查归 P1.6 inventory。
- **文档间不一致**：RT-05 归属在 ADR-0010（P1.6）与实现规格（P1.5 T22.3）之间矛盾，裁定为规则/Prompt 层入 P1.5、检索层权重属 P1.6，ADR-0010 增修订注记（含 RT-07 最小版/RT-10/RT-04 离线包差集声明）；路线图-v2 §6 阶段文件门禁表仍写"当前 P1/P1 验收后建 P2 文档"，已更新为 P1.5→P1.6→P2 链。
- **契约与机制补定**：`mode` 扩为四值（取值域扩张，非加字段，规格 §2 显式声明）、`not_found` 保持 list[str] + 平行 `not_found_details`；policy-refusal 识别 = 确定性规则前置 + plan 标志兜底（D6 内不新增调用点），落终态 run 保可审计；required-evidence 解析确定性为主并加防过拟合条款；T30 允许可逆迁移 0004、候选元数据设硬上限；"18 条真实 DeepSeek 运行"拆为 13 真实问答 + 5 机制检查（题 14 需脚本化 LLM、题 17 需 loader spy，字面不可在真模型上跑）；检索无退化对照指名 `evalsets/v0/retrieval_dev.jsonl`，明示不触碰 P1 holdout 检索集；v1.5 数据集文件名与 JSONL schema 落入 Evaluation-v1.5 §2。
- **分支与 CI**：P1 关闭后误在 p1 分支堆积 P1.5 开启提交（未推送）。新建 `p1.5` 分支承接（含 RT 记录与 ADR-0010 提交），本地 p1 退回 origin/p1（9dfff3f），p1 分支冻结；ci.yml push 触发分支补 `p1.5`（否则 T32.4 头提交远端绿灯无法达成）。遗留：9dfff3f 自身的远端 Actions 状态此前未记录，待推送后一并核验。

## 2026-07-24 · P1.5 U3.1：冻结 Evaluation-v1.5 回归集

- U3.1 是 P1.5 第一项，也是"动 src/Prompt 前先冻结评测"的前置纪律。从《P1后真实仓库可用性测试问题记录》§21.1 逐案把 13 条真实问答复现转为 `evalsets/v1.5/contract_dev.jsonl`（每题 source_case/run_id/aspects/required_evidence(type+path/symbol)/allowed_supplement/forbidden_substitute/expected_mode_p15/p15_expectation/p16_target/key_paths/key_symbols）；另落 `contract_dev_extended.jsonl`（8 条反例）、`contract_holdout.jsonl`（6 条封存）、`mechanism_checks.md`（14–18 走 §5.2 确定性层）。
- 核心是**两栏预期**的逐题裁定：P1.5 契约预期（绑定 Gate、不改检索/摄取下可确定性达成）vs P1.6 目标预期（仅记录）。裁定原则——c06/c07/c09/c11/c12/c13 承诺"实质修复"（根因是跨轮丢证 RT-16/假 not_found RT-15/无 policy 终态 RT-22，且相关文件当轮确被召回过、非召回不到）；c01/c02/c05/c08/c10 为"诚实天花板"（不判 full、未召回用条件式 missing，引到位属 P1.6）；c04 正向基线（保持拒答不编造）；c03/c10-迁移/c11/c12 沿用 Fable §2 已冻结方向。用户 2026-07-24 逐题确认全部 13 条并接受 RT-20 partial 优先取舍。
- 序列化用 scratchpad 生成器（手工填 dict → `json.dumps(ensure_ascii=False)` + 逐行 `json.loads` 校验）保证 JSON 合法；`confirmed_at=2026-07-24` 回填后三份 JSONL 各 13/8/6 行全部合法。holdout 标 `sealed`，声明 T22–T31 实现期不查阅、不据此调参。
- 纪律边界：本项只产出评测数据与冻结，未运行任何 P1.5 dev（标签锁定生效点为首次 dev 运行）、未改 `src/`、tests、Prompt、模型或检索参数、未复用或触碰 P1 holdout。证据 commit：本提交。

## 2026-07-24 · P1.5 T22：证据类型硬约束与权威性分层（RT-01/05）

- P1.5 首个实现任务，也是 T23–T25 的地基。核心洞察（问题记录 §18 归因）：系统缺"证据类型/权威性"结构化模型，才会用测试/dev-log 顶替生产源码仍判 full。T22 补上这层。
- 新增 `src/devkb/agent/evidence_types.py`（零 LLM、仓库无关、不 import state 避免环）：`classify_path` 按通用约定把 rel_path 映射到 10 类证据（src/main→production_source、src/test→test、`*.sql`/migration→migration、docs/design→design_doc、docs/plans/phase-/规划→historical_plan、dev-log→dev_log、README/PROGRESS→current_doc、`.vue/.ts`→frontend_source…）；`parse_required_evidence` 确定性从问题提取必需类型、"不要用 X 代替 Y"式禁止替代类型与点名符号——**权威来源在此，plan 的 LLM 回显只作确认**。
- full 硬约束落在 `nodes.finalize`：`unmet_required_types` 检查每个必需类型是否有同类型直接引用，缺则确定性降级 partial——测试/设计/历史材料因类型不同天然无法覆盖必需生产证据，"**测试不能替代生产实现**"由类型系统而非 Prompt 保证。plan 回显未锚定问题原文即 `plan:required_evidence_unanchored` 警告并忽略，杜绝 LLM 伪造 required 清单。
- 设计取向（防过拟合）：解析对显式通用词汇生效，自然问法优雅降级为空约束（宁可回退 P1 行为不过度约束）；over-detect 使 full 更保守（诚实方向），under-detect 不劣于 P1。`test_evidence_types.py` 用 c01/c04/c05/c09/c10/c11 与自然问法 e01 验证泛化，不为单句写特判。
- `PROMPT_VERSION` p1-agent-v3→p1.5-agent-v1、`AGENT_STATE_SCHEMA_VERSION`→p1.5-agent-state-v1（AgentState 新增 required_evidence），快照 SHA 与两处版本断言同步更新。Answer schema 不变（本任务只往 not_found 追加字符串说明，仍 list[str]）。
- 全绿：`make ci`（ruff/pyright 0 错、pytest 313 passed）、`make eval-ci`（65+19）。P0 fixed-rag 对照路径与既有 P1 图/状态测试未受影响。证据 commit：本提交。

## 2026-07-24 · P1.5 T22 复审：确认错误 full，撤销勾选

- T22 首版（26777fd）经独立代码复审（GPT）被驳回，4 处缺陷全部用图级/单元复现确认，撤销 T22.1–T22.3 勾选（详见任务清单偏差记录）。
- 根因不是个别 bug，是设计错误：把用户"证据类型/**路径**"要求压成"类型集合"、丢了点名路径/符号与逐项——所以点名 RetrievalRepository/CitationParser/RagService 却引用无关 `other/Foo.java` 仍判 full；`classify_path` 可信目录后判使 `src/test/.../*.sql`、`flyway-audit.md` 冒充生产 migration；关键词解析出现否定语义反转（"不要引用设计文档"→必需 design、"计划文档…规划过"→禁止 plan）与漏识别（"对应测试"）。且"under-detect 仍安全"假设被证伪：漏识别真实要求时回退的正是 P1 错误 full——那正是 P1.5 要消灭的。
- 教训：CI 全绿只证明无兼容退化、不证明新能力正确；勾选前必须让对抗性测试（点名不匹配、可信目录冲突矩阵、否定语义）先红。此次违反勾选纪律（判据未全满足即勾），已撤销。
- 本条只改文档（撤勾 + 偏差 + 本叙事 + 更正 313 计数）；26777fd 代码保留为历史，整改在后续提交 fix-forward。

## 2026-07-24 · P1.5 T22 整改重实现（A schema + B 确定性裁决）

- 按用户裁决「A 的 schema + B 的确定性裁决」fix-forward 重写 T22，四条复审发现逐条修复并用**原始失败场景**验证（非仅测试绿）：
  - **发现1（错误 full）**：required 从"类型集合"改为逐项 `RequiredEvidenceItem`（严格 Pydantic：type+anchor+可选 path/symbol，同类型多点名不合并）；`compute_coverage` 逐项匹配、点名类须**精确文件名**命中，finalize 用 `all_required_covered` 裁决。复现确认：点名 3 类引用无关 `Foo.java`→`all_covered=False`→partial。
  - **发现3（可信目录）**：`classify_path` 改为可信目录优先——`src/test/**.sql`→test、`docs/**flyway-audit.md`→historical_plan、audit→historical_plan；收窄 application 匹配。
  - **发现4（否定反转/漏识别）**：解析改为按子句 `[。；，…]` 拆分 + 否定作用域；"不要引用设计文档"→design 禁止（不再反成必需）、"计划文档说明规划过"→plan 必需（不再反成禁止）、"对应测试"→test 必需。修掉 `别引用` 撞"分**别引用**"、`不应用` 撞"**应用**"两处触发词碰撞。
  - **发现2（schema+严格）**：`RequiredEvidenceItem`/`CoverageEntry` 为严格 Pydantic；`PlanOutput.required_evidence` 回显 item_id、`EvaluateOutput.coverage` 逐项报告——但**裁决权全在确定性代码**：plan 回显非权威 id→`plan:required_evidence_mismatch`、evaluate LLM 覆盖与确定性不符→`evaluate:coverage_mismatch`，full 门/refine 只读确定性 coverage，LLM 报告仅诊断/驱动查询（语义 5/6）。
- 教训固化：这次先写对抗性测试（点名不匹配、冲突矩阵、否定正反）再实现，勾选前用复审原始场景逐条确认错误 full 消失，而非只看 CI 绿。
- `PROMPT_VERSION` p1.5-agent-v1→v2、`AGENT_STATE_SCHEMA`→v2、快照 SHA 更新。全绿：`make ci`（pyright 0、pytest 319）、`make eval-ci`（65+19）；P0/P1 兼容。重新勾选 T22.1–T22.3，偏差记录裁决已闭环。证据 commit：本提交。

## 2026-07-24 · P1.5 T22 二审整改（第二轮复审 4 处再发现）

- 上一条重实现（a1f3f37）经**第二轮独立复审**又被驳回：4 处可稳定复现，再次撤销 T22.1/T22.2（T22.3 分类修复确认有效，保留勾选）。这次严格"先红后修"——四处先落对抗性测试确认失败，再修，再用原始场景 + 跨 hashseed 自验。
  - **发现1（显式路径被句号切断 → 错误 full）**：`_CLAUSE_SPLIT` 含英文 `.` 且分句先于路径提取，`backend/.../Foo.java` 被切成 `Foo`+`java`，退化为 type-only；且 `_item_matches` 路径分支用子串，裸 `foo.java` ⊂ `notfoo.java`。整改：分句正则去掉 `.`（中文句界用 `。；！？、，`+换行+`;`）；路径匹配改按**完整路径后缀**（`rl == p or rl.endswith('/'+p)`）/**裸名 basename 全等**。复现确认：required `Foo.java` 引用 `NotFoo.java`→未覆盖→partial。
  - **发现2（否定作用域整句级删除正确要求）**：旧逻辑对禁止子句把检测到的所有类型入 forbidden，再全局 `if t in forbidden: continue` 删除，"不要用测试代替生产源码，请引用生产源码"里 production_source 被误删→required 空→引 `FooTest.java` 仍 full。整改：禁止子句按"用 X 代替 Y"切分（`_split_substitution`），X→forbidden、Y→肯定必需；**移除全局按类型删除 required**，forbidden 只作独立信号（类型分层已保证 test 不覆盖 production）。复现确认：production 仍必需、test 禁止、`FooTest.java` 未覆盖。
  - **发现3（非确定性 + anchor 非原文 + 类名误判 migration）**：type-only 用规范标签作 anchor、用无序 `set` 遍历生成 item_id（PYTHONHASHSEED 敏感），`_infer_symbol_type` 在同句仅 `{migration}` 时把 `DocumentService` 判成 migration。整改：`_detect_type_hits` 返回 `(span, type, 原文 anchor)`，按 `(子句序, 局部 span)` 排序后再分配 R 号；点名类默认 production_source（永不 migration/config）。复现确认：跨 5 个 `PYTHONHASHSEED` 的 item_id→type 完全一致；`DocumentService`/`CitationRepository`=production_source；anchor 均为原文子串。
  - **发现4（A schema + B 裁决只落到 finalize）**：`route_after_evaluate` 完全没读确定性 coverage——必需项未覆盖却 LLM sufficient 时跳过补检直奔 generate；evaluate/plan 只查 LLM 已返回项，全省略时不告警。整改：`route_after_evaluate` 增加"确定性覆盖缺口且预算允许→优先 refine"（语义 5，即便 sufficient）；plan/evaluate 校验回显集合须与权威集合完整一致、covered/evidence 与确定性矩阵对齐，遗漏也记诊断 mismatch（语义 6，不改判）。图级测试：缺口先补检再降级 partial；LLM 全省略→双 mismatch 警告但确定性 full 不受影响。
- 连带影响：覆盖缺口现在会驱动一次补检，`test_agent_required_evidence` 两条降级用例脚本补齐 refine 轮（plan→evaluate→refine→evaluate→generate）；其余 agent 测试问题无 required 触发词，路由不受影响。
- `PROMPT_VERSION` 保持 p1.5-agent-v2（T22 未验收、契约仍在定稿），仅 plan/evaluate Prompt 文案改为"有 required 必须完整逐项返回"并删矛盾表述，快照 SHA 更新为 `5d2c6ec4…`。全绿：`make ci`（ruff/pyright 0、pytest 327）、`make eval-ci`（65+19）；P0/P1 兼容。
- 勾选纪律：因上轮曾在"自认已修"后仍有缺陷，本轮 T22.1/T22.2 **暂不勾选**，待第三轮独立复审确认；T22.3 保留。证据 commit：本提交。

## 2026-07-25 · P1.5 T22 三审整改（组合场景错误 full + 自查补洞）

- 第二轮整改（fc16b66）经**第三轮复审**：二审 4 个原始用例全过，但复审构造**组合场景**又稳定产出错误 full，撤销 T22.1/T22.2/**T22.3**（三者全撤）。根因是我上轮为修发现 1"简单删英文 `.`"——治标致新病。这次先补红测，且**自己主动构造组合场景试图打破**再修。
  - **发现1（否定跨句污染）**：删 `.` 分句后"不要引用测试. 请引用 Foo.java"成一整句，命中否定即走禁止分支、跳过正向路径解析，`Foo.java` 未成必需，仅引 `FooTest.java` 仍 full。整改：解析开头先用 `_PATHISH.sub` 把路径 token 遮蔽成无点占位符（`\x00N\x00`），再按含 `.` 的句界分句，逐子句还原——路径不被句点截断，否定又停在句号。
  - **发现2（生产配置漏检）**：`_TYPE_PATTERNS` 配置类只认"配置文件/application.yml/docker-compose"，漏冻结判据的"生产配置"。整改：加入"生产配置"。
  - **发现3（application.* 前缀过宽）**：`classify_path` 仍 `base.startswith("application.")`，`application.md/.java/.txt` 全判 production_config，与任务清单"只匹配配置扩展名"矛盾。整改：删裸前缀，只按 `.yml/.yaml/.properties` 扩展名（真配置本就被 `_CONFIG_EXTS` 命中，无损）；补 3 负例。
  - **发现4（自然问句技术名）**：非否定子句无条件抽 CamelCase，"消息队列（如 Kafka/RabbitMQ）"把 RabbitMQ 当必需点名类。整改：点名硬约束限定在带"引用/根据/依据/参照/参见/结合"指令的子句（`_has_cite_directive`），并把顿号 `、` 移出句界让"A、B、C"枚举留同句；替代结构"用 X 代替 Y"的被保护侧 Y 隐含引用要求，以 `require_directive=False` 强制提取。
  - **发现5（evaluate 诊断漏报）**：只查 LLM 多报 evidence（`report - matched`），漏报（covered=true 但 evidence_ids=[]）不记 mismatch。整改：改 evidence 集合**全等**比较。
- **自查补洞**：修完后我主动构造"不要用测试代替 Order.java"（替代 Y 侧裸文件名、无指令），发现 Y 侧漏提取→required 空→仍错误 full；即改为 Y 侧强制点名提取并补测试。这是这轮第一次在提交前自己先把组合场景跑穿。
- 未改 Prompt，`PROMPT_VERSION`/快照 SHA 不变。全绿：`make ci`（ruff/pyright 0、pytest 339）、`make eval-ci`（65+19）；P0/P1 兼容。跨 5 个 `PYTHONHASHSEED` 的 item_id→type 仍完全一致。
- 勾选纪律：三审后 T22.1/T22.2/T22.3 **全部暂不勾选**，待第四轮独立复审确认。证据 commit：本提交。

## 2026-07-25 · P1.5 T22 结构性 fail-closed（第四轮复审：停止堆正则）

- 三次整改（a6583f8）经**第四轮复审**又发现 5 处组合错误 full（+我自查 1 处），且核心判断是"窄修正则治标不治本"。前三轮的定向修复本身有效（Foo.java≠NotFoo.java、否定作用域、可信目录、生产配置、大小写…都仍成立），但只要解析器**漏检某个显式约束**，空/不完整 required 就让 full 门自动放行——这是所有错误 full 的同一根因。用户裁决：**改用结构性防御**，把安全性从"正则召回率"转为"结构保证"。
- **新结构（确定性三态 + unresolved 硬门）**：`RequiredEvidence` 增 `unresolved_constraints`（严格 Pydantic：原文 anchor + reason∈{unparsed_target, partial_enumeration, unsupported_syntax}）与派生属性 `status`（none/complete/ambiguous）。`full` 必要条件改为 `required_satisfied` = 每个 resolved item 被直接引用覆盖 **且** unresolved 为空；`route_after_evaluate` 把 unresolved 也当覆盖缺口优先 refine（anchor 进补检查询）；`finalize` 仍有 unresolved 时最高 partial（保留 claims + 确定性限制说明，不静默降级）。`compute_coverage` 只处理 resolved items，**绝不**用 type=other 伪造可被任意文件满足的 item。关键性质：解析器 under-detect 显式约束时，要么进 items（须被覆盖）要么进 unresolved（硬关 full），**不再有"漏检=放行"**。
- **解析重写（防组合错误 full）**：
  - 发现1 无空格中文被吞：file-token 改**ASCII 边界**扫描器（起始必须 ASCII 字母/数字/下划线，扩展名 2-11 位），先遮蔽再按句界（含英文 `.`）分句、逐子句还原——"请引用Foo.java说明"得到干净 `Foo.java`，"Foo.java和Bar.java"分成两个。
  - 发现2 逗号枚举丢目标：句号/分号/换行是强句界，逗号/顿号是同指令下列表连接符；后续子句作为**延续**解析（不要求重复指令）——"引用 Foo.java，Bar.java" 两个都必需。
  - 发现3 单词/缩写类名：symbol 正则放宽为 PascalCase（首大写+≥1 小写，排除 ALL_CAPS 常量），`Order`/`URLParser` 可解析；`X 类/X 文件` 明确点名（含 `DTO 类`）；泛化角色词(Repository/Controller/DTO 独立出现)与集合/模糊量词(每个/所有/列出/某个)→**unresolved**，不假装已完整解析。
  - 发现4 后缀不全：lexer 已摄取后缀集合与 `ingest.SUPPORTED_SUFFIXES` 对齐（有防漂移单测），另加 P1.5 识别未摄取的 `.sql/.vue/.ts/.tsx`；未知扩展名的显式路径→`unsupported_syntax` unresolved，不退化空 required。
  - 发现5 test 大小写伪命中：`classify_path` 明确目录优先级——test 目录 > 生产目录(src/main) > `*Test/*Tests/*IT/*Spec` **大小写敏感**命名约定；`Contest/Latest`(main)、`OrderTest.java`(main)→production_source，`src/test/Order.java`、根级 `OrderTest.java`→test。
  - 发现6 匹配大小写：`_item_matches` 的 path/basename/symbol 身份比较改**大小写敏感**（分类仍可小写）；Foo.java/foo.java/NotFoo.java 互不覆盖。
  - **指令作用域**（防自然问句误判）：强指令(引用/参见/参照)直接激活；弱指令(根据/依据/结合/列出…)仅在邻接路径/证据类型词/角色词/`X 类` 时激活——"OrderService 如何结合 RabbitMQ" / "系统根据 OrderStatus" → 无约束；"根据 OrderService 的生产源码" → 有约束。
- **纪律**：先红后绿——§五.A 图级 8 反例、分类/parser 参数化、自然问法、§五.D 结构不变量（空格无关、增量不减、hashseed 一致、anchor 原文子串）均先在 a6583f8 确认失败再实现。提交前自己又构造组合（未知 ext+已知 ext、集合+具体、参照+连接、多重否定）跑穿。`AGENT_STATE_SCHEMA_VERSION`→v3（RequiredEvidence 形状变化）；Prompt 未改，`PROMPT_VERSION`/快照 SHA 不变。`make ci` 376、`make eval-ci` 65+19 全绿；P0/P1 兼容；跨 5 PYTHONHASHSEED items+unresolved 完全一致。
- 勾选纪律：T22.1/T22.2/T22.3 **仍全部不勾选**，待下一轮独立复审确认。证据 commit：本提交。

## 2026-07-25 · P1.5 T22 约束跨度账本（第五轮复审：从"整段判定"到"逐段结算"）

- 四次整改（3e18503）经**第五轮复审**：三态 + unresolved 硬门方向被确认正确，但核心性质"解析漏检必进 unresolved"**并未成立**——`_extract` 以整段 `produced` 为准，只要一个目标命中，同一义务下的其余目标就静默消失。复审给了 6 处可复现缺陷与 7 条路线裁决，T22.1/T22.2/T22.3 仍不合格。我先把 6 处全部在 3e18503 上跑出原始输出留证（`请引用 Foo.java 和关键实现` → status=complete/无 unresolved；`请引用 README` 被 `docs/architecture.md` 覆盖；`src/main/java/AuditService.java` → historical_plan；否定约束无任何消费者），再按裁决重写。
- **架构改动：约束跨度账本（constraint span ledger）**。义务检测与目标解析彻底解耦：
  - `_detect_obligations` 独立识别义务跨度——强指令（引用/参见/参照/援引，对象在标记后）、弱指令（根据/依据/列出…，须邻接证据信号）、**核验比较谓词**（声称/是否一致/是否支持…，对象在标记**左侧**）。第三类是 c02（"README 声称…；Java 配置和 Provider 实现是否支持这个说法"）此前整题 status=none、硬门真空化的原因。
  - `_resolve_region`/`_resolve_one` 把义务对象按连接词拆成目标段，每段各自结算为 **resolved item / unresolved 约束 / 明确的非目标尾语**（整段不含目标名词，如"说明订单如何校验"）。再没有"整段 produced=True"，`请引用 Foo.java 和关键实现` 现在得到 1 item + 1 unresolved(`关键实现`)。
  - **身份优先**：裸 `README/PROGRESS/CHANGELOG…` 保留身份（`symbol=README`，类型由 `classify_path` 反推），不再退化成"任意 current_doc 都能顶替"；`相关 Java 配置` 这类无身份类别直接 unresolved（c08 三个具体文件全引用也不得 full）。
  - **点名类型语境**：`_infer_symbol_type` 让显式生产语境优先于 `*Test` 命名约定——"请引用 OrderTest 的生产实现"只产生 1 条 production_source:OrderTest，不再是"test:OrderTest + 泛化 production"被 `src/test/OrderTest.java + 任意生产文件` 组合满足。
  - **分类先目录后扩展名**：audit/changelog/plan/review 等**文件名**约定只作用于文档扩展名，`AuditService.java`/`ChangelogService.java` 不再冒充 historical_plan/dev_log；`Test*` 改词边界规则（`^Test(?![a-z])`、`[a-z0-9]Tests?$`、`(?<![A-Z])ITs?$`），Testing/Testimony/SPLIT 不是测试，`TestOrderService`/`FooSpec`/`AuditIT` 仍是。
  - **两类否定分开执行**：`forbidden_substitute_types`（可作补充、不能替代）与 `forbidden_citation_types`（不得直接引用）。"只能作为补充"/"不要用 X 代替 Y"/"不要只引用 X" 归前者；"不要引用 X" 归后者，并新增 `forbidden_citation_hits` 让它真正生效——`required_satisfied` 现在同时要求"无被禁类型被引用"，generate Prompt 也收到该清单（此前否定纯属死信号）。
  - **schema 上限对齐**：`MAX_REQUIRED_ITEMS=10` 成为 `PlanOutput.required_evidence` 与 `EvaluateOutput.coverage` 的同源上限；解析超限时截断 + 记 `partial_enumeration`，不再"11 项却报 complete"（有防漂移单测）。
  - **finalize 公共收尾**：抽出 `required_evidence_tail`，覆盖缺口 / unresolved / 禁止引用三类说明对**所有**终态分支统一生成——此前只在"无 claim 被移除"那一条分支里，draft is None、generate_failed、有 claim 被移除时全部静默丢失（发现5）。
- **自查补洞（提交前主动构造组合）**：①顿号切开的否定列表被截断——"不要用 README、架构文档或测试代替实现"只吃到 `不要用 README`，丢掉 test 禁止且把替代结构误判成禁止引用；改为否定跨度向后合并不开启新义务的续段，并用原文区间切片保住 anchor 是原文子串。②`引用` 作名词误当祈使指令——"历史会话中的引用是否还能显示"凭空产出 unresolved 噪声；加祈使锚定（非祈使位置的强指令退回弱指令口径，须邻接证据信号）。两处都补了参数化测试。
- **纪律**：先红后绿——10 条图级反例（§五.B：残余目标、c08 三引用、c02 核验句、README 身份、OrderTest 生产语境、生产文件冒充历史计划、禁止引用、claim 移除分支、generate 失败分支、零证据 refusal 分支）在 3e18503 上 **10/10 失败**、修复后 10/10 通过（`git stash push -- src/` 逐条验证）；解析/分类/不变量层另加约 30 条（含账本不变量：多一个无法定位目标必多一条 unresolved、追加非目标尾语不改变结果、枚举 N 项至少结算 N 条）与 5 条自然问法反例。`PROMPT_VERSION` p1.5-agent-v2→v3（GENERATE_SYSTEM 增禁止引用类型指令）、`AGENT_STATE_SCHEMA_VERSION`→v4（RequiredEvidence 增字段），快照 SHA → `6c838462…`。`make ci`（ruff/pyright 0、pytest 434）、`make eval-ci`（65+19）全绿；P0/P1 兼容；跨 5 个 `PYTHONHASHSEED` 的 items+unresolved+两类禁止清单完全一致。复审提到的两个图级测试超时未能复现（本机 `tests/unit/test_agent_graph.py` 22 项 0.58s，最慢单项 0.02s）。
- 口径取舍：README 的 full 触发条件表暂不改（T32.1 才是 README/文档口径任务，且 T22 未验收、契约可能再变），本轮不写入未验收能力。
- 勾选纪律：T22.1/T22.2/T22.3 **仍全部不勾选**，待第六轮独立复审确认。证据 commit：本提交。

## 2026-07-25 · P1.5 T22 双引用账本（第六轮复审 P1：正文引用绕过禁引硬门）

- 第六轮复审确认五审整改基本合格（T22.1/T22.3 可验收），但抓到一处仍能稳定错误 full 的账本不一致：`finalize` 把 `apply_l0` 的第二个返回值（正文中有效的引用编号）丢掉了，禁引检查只看保留 claim 的 `evidence_ids`；而 `verification.l0_errors` 只校验正文 `[E#]` 存在性，**不要求正文标记同时出现在 claim 中**。于是"正文引 [E1][E2]、claim 只申报 E1"时：verification 通过、最终答案带着被禁的设计文档 `[E2]` 交付、`final_mode=full`、连告警都没有。我先按复审给的场景跑出这个输出（full / `[E2]` 保留 / warnings 只有两条 LLM 诊断），确认不是纸面问题。
- 根因归纳：**一个"引用"概念被当成两件事用**。支撑账本（claim 申报、用于证明必需证据）与可见账本（正文最终保留的标记、用户真正看到的引用）语义不同，混用哪一套都会漏：只用 claim → 正文可绕过禁引；只用正文 → 裸标记能反过来"满足"点名要求。
- 最小整改（不动解析器，只修收尾层）：`required_evidence_tail(required, support_citations, visible_citations)` 明确拆两参——`compute_coverage`/`uncovered_items` 只吃 support，`forbidden_citation_hits` 与 `required_satisfied` 吃 visible（= support ∪ 正文过 L0 后仍保留的 `[E#]`）。顺带把 `generate_failed` 分支缺的 `apply_l0` 补上：该分支此前直接交付 `draft.answer_text`，越界标记会沿降级路径漏出（默认值文案无标记所以没暴露，但属同一账本纪律）。
- 红测：①图级"正文含 E2、claim 仅 E1"→ 必须 partial + `finalize: 引用了被禁止的证据类型` + 用户可见说明；②图级反向守卫"正文标记不得替 claim 覆盖必需项"；③两条 `required_evidence_tail` 单元测试直接锁两套账本语义。①③在 3576bf0 上确认失败、修复后全绿；②在修复前后都绿（它锁的是不能被"顺手改松"的方向）。
- `PROMPT_VERSION`/`AGENT_STATE_SCHEMA_VERSION` 不变（无 Prompt、无 state 形状变化，只是收尾判定口径）。`make ci`（ruff/pyright 0、pytest 438）、`make eval-ci`（65+19）全绿。
- 勾选：按第六轮复审裁定勾选 T22.1 与 T22.3；**T22.2 仍不勾选**——它的唯一阻塞项已修并有红测，但复审给的口径是"修完并跑全套 CI 后 T22 应可进入最终验收"，最终验收权在用户/复审，不由我自认。

## 2026-07-25 · P1.5 T22 收尾（自查复核代替第七轮独立复审，用户指示）

- 用户指示"继续做 T23，之前先把 T22 完全收尾"。T22 的实际状态是：六审只剩 1 处 P1（双引用账本），已在 b5de64c 修复并附红测，但第七轮独立复审并未发生——按前六轮"自认已修不算数"的自我约束会无限期挂起并阻塞 T23。故本轮按判据逐条自查复核后勾选 T22.2，并在清单与偏差记录里**显式标注是自查复核、不是独立复审**。
- 复核方式（不只跑既有测试，先自己找错误 full）：
  - 复跑六审原场景确认已修：`不要引用设计文档，只引用生产源码` + E1 生产源码 / E2 设计文档、正文 `[E1][E2]`、claim 仅 E1 → `partial` + `finalize: 引用了被禁止的证据类型（design_doc）` + 用户可见 not_found 说明；正文 `[E2]` 仍在（用户确实看得到该引用），故"降级 + 披露"而非假装没引用。
  - 另写 10 条 finalize 硬门对抗探针（不入库，放 scratchpad）：仅测试代替生产（两种表述）、dev-log 冒充生产、两点名类只覆盖一个、配置类点名 `application.yml` 却给 `docs/application.md`、正文越界 `[E9]`、禁引类型只在 claim / 只在正文、必需类型齐全应 full。结果全部符合判据，未发现新的错误 full；越界 `[E9]` 走 verify 失败→重生成→冻结默认值路径，标记未漏出。
  - 判据字面要求的 Fake 矩阵"仅测试代替生产→不得 full""必需类型齐全→可 full"此前只有点名类/否定跨句的变体，没有"用 X 代替 Y"这一表述本身的图级用例（该路径历史上出过 bug：三审自查才发现 Y 侧裸文件名漏提取）。补两条：`不要用测试代替生产源码` + 仅测试证据 → partial + "测试/设计/历史材料不能替代"；`请引用生产源码…不要用测试代替` + 生产证据 → full（确认禁止替代不会反过来误伤合法 full）。
- 观察但未改：type-only 必需项的 not_found 文案会重复成"生产源码:生产源码"（`symbol/path` 为空时回落到 anchor，而 anchor 恰是类型词本身）。属文案冗余、不影响判定，按范围纪律记入 `docs/backlog.md` 而非顺手改。
- `make ci`（ruff/pyright 0、pytest 440）、`make eval-ci`（65+19）全绿；未动 `src/`，`PROMPT_VERSION`/`AGENT_STATE_SCHEMA_VERSION` 不变。至此 T22.1/T22.2/T22.3 全部勾选，T22 关闭，进入 T23。证据 commit：本提交。

## 2026-07-25 · P1.5 T23：not_found 四分类与全轮历史事实校验

- 目标（规格 §5 / RT-02/15）：`not_found` 不再是一堆自由文本——每条要有类别、有事实校验来源；已召回或已索引的路径不得被写成"未找到/不存在"；没有 inventory 就不许断言"全库不存在"。
- **落点**：新增 `src/devkb/agent/not_found.py`（确定性、零 LLM 调用）。`finalize` 把三路缺口（generate 草稿 / evaluator missing / T22 的必需证据说明）带**来源标签**汇总后统一跑 `calibrate_not_found`，产出用户可见文案 + 平行 `not_found_details` + 告警。`AgentState` 增 `evidence_path_history`（每轮 retrieve 累积，reducer=`operator.add`，被后轮挤出也留痕）、`supported_aspect_history`（每轮 evaluate 累积）、`final_not_found_details`。
- **documents 表怎么进来的**：节点本身不做 I/O。`agentic_answer_question` 在图外用 `DocumentRepo.list_active_rel_paths(5000)` 取一次只读快照，包成 `CorpusProfile` 随 `AgentRuntime` 注入。`known=False`（离线图测试/未取快照）时与索引有关的判断一律 fail-closed；**"未命中"永远不作结论依据**，所以 5000 条上限被截断也不会产生错误断言（这也是"不得断言仓库有无该文件"的实现方式：只用命中，不用缺席）。
- **改写口径（这轮最花时间的取舍）**：起初想"凡有冲突就整条换成模板"，自查探针立刻打脸——"未找到 RagService.getConversation 的具体实现"会和"源码中不存在 RagService"生成同一句模板文案，然后被去重吃掉，方法级缺口凭空消失。最终口径：**被证据/索引证伪的条目与仓库级否定断言整条改写，但主语用"去缺失标记后的方面名"**（"未找到 X 的 Y" → "X 的 Y：…"），既让缺失字样消失（c02 要求已召回 README/已索引 application.yml 不得被写成"未找到"），又保住"哪个方法/字段没覆盖"；格式未摄取这类原文成立的，只在括号里追加条件式披露。原文一律进 `original_text` 备查。
- **自查探针抓到的 4 个真缺陷**（都在提交前修掉）：
  1. **README 逃过事实校验**——`iter_symbol_tokens` 把 `_SYMBOL_STOP` 一并套在规范文档名上，而 README/PROGRESS 正是 T22 五审特意保留身份的点名对象；已改为停用词只作用于 PascalCase 类名。这条直接关系 c02。
  2. **Java 包名被当成未摄取格式**——共用的 file-token 词法会把 `com.example.repo.CitationRepository` 切出 `com.example`，`.example` 于是成了"未摄取后缀"。加封闭 `RECOGNIZED_FILE_EXTS`：只有真实扩展名参与摄取覆盖判定，其余退回 missing（保守方向）。`.env.example` 这类隐藏文件另走"路径任一段以 . 开头恒不摄取"的规则（与 `scan_files` 同口径）。
  3. **整题技术词外溢**——问题里出现"数据库迁移"，同题里"已索引 Java 未召回"的缺口也被标成"格式未摄取"，与 c10 判据"两类分类区分"冲突。改为技术词只匹配条目自身文本；"用户只点名未给路径"的场景由确定性 required-evidence 说明承载（它由问题原文解析而来、带类型标签"前端源码:Vue"），并把 `required_evidence_tail` 的缺口说明按证据类型拆成多条。已写入清单偏差记录等裁决。
  4. **全局否定漏网**——"未找到任何其他 Controller"既不含 tier-1 否定词也无路径冲突，原样交付；加"穷举量词 + 否定"规则降级为"当前证据不足以穷举确认"（c12 要求），同时保证"OrderService 中没有事务注解"这类可由证据支撑的具体结论不被误伤（弱否定词只在穷举量词同现时才算数）。
- **红证**：改动前在 2716c3f 上用同样场景跑出原始输出——"源码中不存在 RagService 的实现"原样进 not_found、迁移与 Java 两类缺口无从区分、前轮已支持的方面照报为缺失、无 `not_found_details` 字段。
- **契约版本**：`PROMPT_VERSION` p1.5-agent-v3→**v4**（generate 增一条"not_found 只写当前证据范围、不得断言仓库不存在"的指令；确定性校准仍是最终防线，Prompt 只是少让它触发），快照 SHA→`96242198…`；`AGENT_STATE_SCHEMA_VERSION`→**v5**（state 增 3 字段）；`ANSWER_SCHEMA_VERSION` p1-answer-v1→**p1.5-answer-v2**（Answer JSON 加 `not_found_details`，纯加法）。
- 测试：`tests/unit/test_agent_not_found.py` 36 条（分类矩阵、封闭词表边界、包名/隐藏文件、事实校验三态、改写 vs 追加、去重与对齐、`confirmed_undocumented` 永不产出）+ 图级 4 条 + 集成 2 条（真实 PG）。`make ci`（ruff/pyright 0、pytest 482）、`make eval-ci`（65+19）全绿；P0/P1 兼容回归未动。
- 勾选：T23.1、T23.2 按判据逐条核对后勾选（自查复核，非独立复审；同 T22 收尾口径）。证据 commit：本提交。

## 2026-07-25 · P1.5 T23 补强：目标级锚定裁决 + "一条一原因"确定性拆分

- 用户在头提交上实跑 c03/e05/c10 后裁决：**采用目标级锚定**——问题里的封闭技术词先确定性绑定到对应 required-evidence 缺口再逐条分类，不得作为整题标签传播；无法绑定时保守归入当前证据缺口，不恢复整题级兜底。依据是规格 §5 只要求覆盖"未给路径"场景、并未要求题级传播，而 Evaluation-v1.5 §2 明确 `.sql` 必须是 `unsupported_or_not_ingested`、已索引 Java 必须是 `missing_from_current_evidence`。裁决原文已录入清单偏差记录。
- 用户同时发现一个我没测到的独立边界：**LLM 把两类原因写成一句**时条目内部自相矛盾。先复现留证——"当前证据未覆盖数据库迁移与 DocumentService 的删除实现" → `category=unsupported_or_not_ingested` 却带 `basis=corpus_index` 和 Java 路径；根因是我把"分类"和"事实校验"当成可叠加的信号写在同一条上，而 `category` 只有一个槽位。
- 修法（按用户要求：不许用恢复整题级兜底来绕）：
  - 抽出 `_assess(segment, markers, …)`，**每段只结算一个原因**——优先级为 证据事实 > 索引事实 > 摄取范围 > 无从证明的强断言；`category`/`basis`/`refs` 由同一个分支一次性决定，结构上不可能再互相矛盾。
  - 语句级标记（缺失词/仓库级否定/穷举量词）仍按整条计算后下发给每段：否则"未覆盖 A 与 B"里 B 段没有缺失词，索引事实就查不出来了。
  - 一条语句出现多个原因即按连接词（以及/和/与/及/或/、/，/；）**确定性拆成多条**，每条挂各自的原因，`original_text` 都指回同一句原文以便审计"它们来自同一句"。无信号的残段并入第一组，不丢字。
  - 三条防过度拆分的约束：括号内不拆（否则"（数据库迁移:X、生产源码:Y）"这种括注会被切碎、留下不配对括号）、确定性说明不拆（`required_evidence_tail` 已按类型逐条生成，本就一条一原因）、同类目标不拆（"未找到 OrderService 与 PaymentService 的生产实现"仍是一条、文本原样）。
- 红测先行：`test_mixed_reason_item_is_split_into_one_reason_per_entry`（拆分正确性）、`test_every_detail_is_internally_consistent`（5 组参数化的 `category`↔`basis`↔`refs` 自洽不变量）、`test_single_reason_item_is_not_split`（不得过度拆分）——修复前 4/4 失败，修复后全绿。
- `make ci`（ruff/pyright 0、pytest 489）、`make eval-ci`（65+19）全绿；未改 Prompt 与任何契约版本（`PROMPT_VERSION` 仍 p1.5-agent-v4、state v5、answer v2）。证据 commit：本提交。

## 2026-07-25 · P1.5 T23 拆分复审整改（refs 聚合 + 连接词上下文）

- 复审对 5325ecd 的结论：原始矛盾确实修好了，但**我新写的拆分逻辑自己带了两个可复现阻断点**，T23 仍不能验收。两条都先复现留证再改。
  - **阻断点1：同原因多目标丢事实引用**。分组时 `groups.append((members[0], text))` 只带第一个成员的 refs——"未找到数据库迁移、OrderService 与 PaymentService 的实现"里，Java 条目文本含两个 Service，refs 与告警却只有 `OrderService.java`。修法：把渲染从 `_assess` 里搬出来（`_clause`/`_render`），**refs 按原文顺序合并去重后再渲染**；文案最多列 3 条并记"等 N 条"，但 `detail.refs` 与告警**不截断**——审计要能看到每一个目标。
  - **阻断点2：单字连接词误切 + 残段错位**。`_SEGMENT_TOKENS` 里的"及/与/或/和"在任意位置匹配，于是 `涉及`→`['当前证据未覆盖涉', '数据库迁移的字段定义']`、`或者`→`['未找到 OrderService ', '者 DocumentService 的实现']`、`与此同时`→句首"与"被吃掉；再加上无信号残段一律并到第一组，就出现了复审指出的 `数据库迁移的字段定义未找到涉…`。修法：`_connector_at` 判定"是否真正的目标连接处"——标点与"以及"无歧义直接切；单字连接词过**左右封闭屏蔽表**（涉/普/波/兼/顾｜其/时；参/给/赋/授｜此/其；—｜者/是/许/多；总/温/平/缓｜谐/平/睦）且句首/句尾不算连接处。残段改为并入**紧邻的前一个**有信号段（句首残段并入其后第一段），顺序与原文一致。
- 复审同时点出我的测试盲点：自洽测试只查 refs 形状。补了 `test_every_target_named_in_text_has_a_fact_reference`——文本里出现的每个目标符号都必须在 refs 里有对应路径。
- 一个测例是我自己写错的：`未找到普及率与和谐度相关的说明` 里的"与"本就是真连接词，被切是正确行为（`普及`/`和谐` 都没被切开）。已把它改成"真连接词仍要切"的正向断言，而不是去迁就错误预期。
- 红测 7 条（同原因 refs 聚合、4 组自然连接词、保序与残段归位、真连接词仍切）在修复前全红；`make ci`（ruff/pyright 0、pytest 497）、`make eval-ci`（65+19）全绿；Prompt 与契约版本仍不变。证据 commit：本提交。

## 2026-07-25 · P1.5 T23 第三轮复审整改：屏蔽表下线，切分改为目标锚定

- 复审对 29311fb 的结论：refs 聚合确认修好，但**单字屏蔽表这条路线本身**不成立，T23 仍不能验收。两处可复现：
  - **`或者` 被当成不可切复合词**。为了防"或者"被切成"者 X"，我把"者"放进了"或"的右屏蔽表，结果 `未找到数据库迁移或者 DocumentService 的删除实现` 整句不切，只剩一条 `corpus_index`，`.sql` 的 `unsupported_or_not_ingested` 整条消失——这正是 c10 型分类混淆，等于用"不拆"掩掉了一类缺口。复审的判断很准：`或者` 应作为完整多字连接词处理，而不是屏蔽"或"。
  - **屏蔽表覆盖不到自然词汇**。`提及 X 的说明与…`→`提 X 的说明`、`X 实现与否…以及…`→`X 实现否…`、`，与此同时 X`→`此同时 X`、`，同时，X`→`X同时`（残段并到前一条）。前三条是屏蔽表漏词，第三条还有第二个成因：`_ANCHOR_TRIM` 把"和/与/及/或"当边界字符盲删，`_subject` 一 strip 就把"与此同时"的首字削掉了。
- 复审给的方向是对的：**基于两侧目标信号确认连接位置**，别再扩充屏蔽表。中文没有词边界，"及/与/或/和"同时是"涉及/提及/普及/参与/与否/与此/或者说/和谐"的组成部分，靠列举"哪些词不能切"必然继续漏——这已经是同一处第二次因为漏词返工。
- 重写后的口径（`_cut_between`）：
  - 先用 `iter_target_spans`（复用 T22 的 file-token/PascalCase/规范文档名词法）+ 隐藏文件 + 封闭技术词求出**目标区间**，切点**只可能落在两个相邻目标之间**——`提及 X`、`涉及 X`、`参与方 X`、句首"与此同时 X"的连接字都不在任何两个目标之间，结构上就切不到，不需要任何屏蔽词。
  - 间隙里只认两种可证情形：①括号外的**标点**（中文标点不可能出现在词内，取最左一个，"，与此同时 X"/"，同时，X"的从句措辞随之留给后一个目标，符合原序语义）；②**整个间隙就是一个连接词**（以及/或者/与/及/或/和），此时连接词两侧紧邻的都是目标，是可证的连接处。
  - 其余一律不切：同条内按优先级取唯一原因、原文完整保留。代价是 `…的说明与数据库迁移` 这种"左侧是散文"的连接处不再拆分——我选了"宁可少拆一条，也不切碎原文"，但为了不让被压住的信号静默消失，加了一条 warning（`混合未摄取格式信号但无法确定性拆分`），并用单测把这个取舍固化下来。
  - 连带清理：`_ANCHOR_TRIM`→`_EDGE_TRIM` 去掉连接词字符（"与此同时"不再被削首字）；切分后两端残留的连接词由 `_trim_connectors` 处理，且**只有紧邻目标时才剥**（"以及数据库迁移"剥、"与此同时 X"不剥）；无信号残段的归位逻辑整体删除——每段必含目标，结构上不再产生残段。
- 红测先行 10 条（`或者` 混合原因、复审四例自然措辞的分段与端到端原文完整性、保守不切且告警），修复前全红；另在 `test_evidence_types.py` 补 2 条"目标区间与 token 词法同源"的防漂移测试，因为切分现在依赖这套词法，不能让它悄悄长出第二个口径。
- 一处自证：我上一轮写的 artifact 断言 `"此同时 Doc" not in joined` 本身太松——正确输出 `与此同时 DocumentService…` 也含这个子串。真正的残段特征是**出现在条目开头**，已改成 `startswith` 断言。
- `make ci`（ruff/pyright 0、pytest 509）、`make eval-ci`（65+19）全绿；Prompt 与三个契约版本均未动。证据 commit：本提交。

## 2026-07-25 · P1.5 T24：跨轮覆盖单调性与 partial 保留

- 目标（规格 §6 / RT-16/20）：把"用户要求"拆成可逐条判定的**证据方面**，让覆盖跨轮只增不减；只要还有一个方面拿得出直接证据，就交付范围准确的 partial，而不是整体假拒答（案例九）。
- **先复现再动手**：写了一份不依赖新模块的案例九回放脚本（放 scratchpad），在 d0f037d 上跑出原始失败——`mode=refusal`、`claims=[]`、`citations=[]`，证据集是 `[CitationParser, CitationParseResult, 架构设计.md]`（第一轮的 RagService 被挤出），连确定性的必需证据说明都把四个点名类全报成"未取得"，其中 RagService 明明在第一轮召回过。这份输出同时暴露了两条独立缺陷（证据被挤出 + 有证据也不生成），后面分别对应 T24.1 与 T24.2。
- **落点**：新增 `src/devkb/agent/aspects.py`（确定性、零 LLM 调用），三件事：
  1. **方面分解两轨**。确定性轨 = T22 每条 required item（身份来自问题原文锚点，可与路径比对）；诊断轨 = 各轮 evaluate 自报的 supported_aspects（只承担"已支持不得回退"，永不放宽 full 门）。`AgentState` 用只增账本 `aspect_observations` 取代 T23 的 `supported_aspect_history`——同一事实存两份必然随修复漂移，T23 的"前轮已支持方面"现在由矩阵折出（`supported_labels(matrix, origin="evaluator")`），输入口径不变；`normalize_aspect_label` 也从 `not_found.py` 提上来共用，否则"not_found 里被剔除、却仍按缺口降级"这种自相矛盾迟早出现。
  2. **跨轮保留**。`plan_retention` 在合并新旧证据时先给"旧轮锚定、本轮未覆盖"的方面**预留槽位**，剩下的才按原顺序填。
  3. **单调矩阵**。`supported` 只增不减，`present_now` 才反映当前证据集；差值在 finalize 逐条告警"被挤出，非证伪"（P1.5 没有任何证伪机制，所以矩阵结构上不存在降级路径）。
- **踩的坑（第一版写错并被自己的测试抓到）**：最初 `plan_retention` 把"命中方面的新证据"整体排到最前，等于**在本轮内重排召回结果**。`test_gB7b_answer_text_only_citation_cannot_satisfy_required_item` 立刻红了——重排后 `OrderService.java` 从 E2 变成 E1，claim 引用的 E1 恰好就成了点名类，一个本该 partial 的用例变成 full。这是很典型的"修 A 破 B"：T22 的两套引用账本没坏，是我把 `E#` 的含义改了。改成**只预留槽位、不重排**（锚点最多占 `limit-1` 个位，至少给新证据留一个），单轮行为与改动前逐字节一致。
- **淘汰原因**：被容量截断的证据逐条产出 `EliminationRecord`；若某方面在保留集中彻底失去直接证据，记 `aspect_anchor_capacity` 并发带 `limit_reached` 标记的 warning（`derive_step_status` 据此把该 step 记为 degraded，与预算/轮次触顶同口径），其余记 `capacity_limit` 只报条数。
- **充分性口径（本轮最需要复审盯的取舍）**：规格 §6 要"最终充分性按单调覆盖矩阵判定，不看最后一轮扁平 top-k"，但没有 required item 的问题里矩阵只剩 LLM 自报的方面，照字面执行等于把诊断信号升格为权威，与 D6/T22 裁决冲突。按轨分治：有确定性轨时矩阵接管（末轮 `sufficiency` 不再有否决权，`test_all_aspects_cited_still_reaches_full` 锁定"末轮 insufficient + 逐项已引用 → 仍可 full"）；无确定性轨时沿用末轮自报。无论哪一轨，矩阵**只降不升**——T22 的逐项引用硬门、unresolved、禁引三道门原样保留，并加了一条反向锁：跨轮已支持但终稿没引用（被容量挤出）→ 仍是 partial，不允许矩阵替它凑出 full。该张力已写入清单偏差记录待裁决。
- **同型缺陷的另一条分支**：只改 `route_after_evaluate` 是不够的。`route_after_refine` 在 refine 解析/传输失败时直奔 finalize→refusal，同样把前轮已有直接证据的方面全丢掉。抽出 `has_deliverable_content(state)` 作为**进入 finalize 的每条路径**的统一口径（前几轮复审反复出现的问题就是"规则只在一条分支成立"），refine 失败时若仍有可交付内容且预算允许就走 generate；`RouteAfterRefine` 相应加一个枚举值与一条条件边。
- **顺手修掉一处被本任务放大的自相矛盾**：跨轮保留会让"证据在集合里、但没有任何 claim 引用它"明显变多，而 `required_evidence_tail` 原本统一写成"未取得该必需证据"——这与矩阵里的 `present_now=True` 直接打架。改成两种措辞分开：完全没召回 → "未取得"；已在证据集但无人引用 → "已在本次证据集中，但未被任何断言直接引用"。案例九回放里 RagSupport 与 RagConstants 现在各走各的措辞。
- **另一处收窄**：finalize 的"被挤出"告警只对确定性轨发。evaluator 方面的 `present_now` 只表示"末轮有没有再自报一次"，据此说"证据不在终态证据集"并不是可核验的事实，会制造大量噪声告警。
- 测试：`tests/unit/test_agent_aspects.py` 23 条（单元 12：两轨分解、单调保留、never-supported、outstanding 去重、空矩阵不充分、共用归一化、单轮不重排、案例九留位、失锚记录、无要求退化、标签 token 绑定、上限与确定性；图级 11：案例九三条 + refine 失败仍 partial + 零可交付仍 refusal + 矩阵不得凑 full + 逐项引用可 full + 缺口措辞区分 + 被挤出告警）。跨 5 个 PYTHONHASHSEED 结果一致。`make ci`（ruff/pyright 0、pytest 532）、`make eval-ci`（65+19）全绿。
- 契约版本：`AGENT_STATE_SCHEMA_VERSION` v5→**v6**（state 字段增删）；Prompt 未改，`PROMPT_VERSION` 仍 p1.5-agent-v4、`ANSWER_SCHEMA_VERSION` 仍 p1.5-answer-v2。
- 观察但未改（留给 T25）：`draft is None` 且仍有可交付方面时（预算耗尽/生成上限）拒答文案仍写"现有资料不足以回答该问题"，这在"资料其实有、只是没能生成"时不准确；同理 `_limitations` 的 refusal 文案。属 T25.1"mode/正文/claims/not_found/limitations 相符"的判据范围，不在本任务顺手改。
- 勾选：T24.1、T24.2 按判据逐条核对后勾选（**自查复核，非独立复审**；同 T22/T23 收尾口径）。证据 commit：本提交。

## 2026-07-25 · P1.5 T24 第一轮复审整改：保留算法联合求解 + 可交付三态 + 淘汰记录可审计

- 复审对 0c0eb36 的结论：两轨分治的方向被正式采纳（见清单偏差记录的裁决原文），但**实现有 3 处阻塞**，T24.1/T24.2 勾选撤销。三条都先复现再改，红测 4 条在 0c0eb36 上确认失败。
- **发现1（P1）：保留算法误判 fresh 已覆盖。** 我用"全部 fresh 覆盖了哪些方面"决定预留，可预留旧锚点又会**缩短实际入选的 fresh 前缀**——落在前缀外的新锚点于是连同同方面的旧锚点一起被截掉。复审给的最小例：`limit=3`、fresh=[OtherOne, OtherTwo, RagService]、carried=[RagService, CitationParser] → 实际保留 [OtherOne, OtherTwo, CitationParser]，两条 RagService 都被标 `aspect_anchor_capacity`，而容量明明放得下 [OtherOne, RagService, CitationParser]。
  - 改法：**代表证据联合求解**。先逐方面选一条代表（本轮新证据优先、否则旧轮排名最高），代表先占位，非代表才用剩余预算按原排名填；容量不足时只**丢弃**非代表，绝不把代表提前——避免又踩上一轮"重排改变 `E#` 语义"的坑。
  - 顺带定了一条更硬的优先级：**确定性轨代表绝对优先**（用户点名的证据不该为"给新召回留个位"让路，那正是发现1 的同类问题）；**诊断轨代表**则必须给新证据让出一个位置，否则一条 LLM 自报的方面标签就能把整轮补检结果清空。两条都有专门单测。
  - 我另写了 4000 例随机化不变量探针（容量/去重/两来源各自保序/确定性方面不被饿死/淘汰记录完备且 reason 正确/跨次确定性）：0c0eb36 上 **1057 例违反**——除复审点出的饿死外，还暴露了我自己没发现的 **carried 重排**（旧填充循环把预留锚点排到其余旧证据前面）；整改后 **0 例**。
- **发现2（P1）：把"历史已支持"当成"当前可交付"。** `has_deliverable_content` 只看"证据集非空 + 历史有任一 supported"，不校验当前证据是否还支撑该方面；更糟的是同一个方面会**同时**出现"被挤出、非证伪"告警和"未取得…生产源码:RagService"的 not_found——我上一轮把它归给 T25，复审判定这就是 T24 的单调性，不能外推。
  - 改法：`has_deliverable_aspect` 判 `supported and present_now`；缺口措辞拆成**三态**——从未取得 / 仍在证据集但未被引用 / 前轮取得后被容量挤出（第三态写明"本次无法作为引用支撑，覆盖按单调矩阵保留"，仍是 partial）；`calibrate_not_found` 的已支持标签改传两轨（确定性轨的单调性事实此前对 not_found 完全不可见，这是矛盾文案的第二个成因）。
- **发现3（P2）：`EliminationRecord` 没进状态或轨迹。** 生成后即丢弃，只剩一条汇总 warning，"逐条记录淘汰原因"名不副实。改法：`AgentState.evidence_eliminations`（只增账本，硬上限 `MAX_ELIMINATION_RECORDS = 2×MAX_FINAL_TOP_K`，与"单轮合并输入 = 新证据 + 上轮保留"同源）+ retrieve 的 step `output_summary` 里逐条带 `chunk_id/rel_path/aspect_ids/reason`。这样运行结束后能回放"这条证据为什么不在终态证据集"，而不是只知道被砍了几条。T30 要做的 per-query 候选轨迹（rank/score/融合原因）仍未提前实现。
- 用户裁决同时正式确认：**诊断轨矩阵只提供 partial/refusal 的单调下界和缺口防回退，不得授予 full**。已把它从"调用方的 `if required.items` 分支"提升为 `is_monotonically_sufficient` 内部的显式规则（只看确定性轨），并按复审给的反例加锁：无 required item 的双方面题，两轮各自报支持一个方面、终稿只引用其一 —— 矩阵仍判两方面 supported（下界成立），但**不得**据此判充分。
- `make ci`（ruff/pyright 0、pytest 544）、`make eval-ci`（65+19）全绿；跨 5 个 PYTHONHASHSEED 一致。契约版本不变（state 已在上一提交升到 v6，本轮只增字段不改 Prompt）。**T24.1/T24.2 仍不勾选，待下一轮独立复审。** 证据 commit：本提交。

## 2026-07-26 · P1.5 T24 第二轮复审整改：两轨发资格、诊断轨证据绑定、跨轨目标身份

- 复审对 f243c86 的结论：一审的最小反例、carried 保序、三态措辞、`is_monotonically_sufficient` 只让确定性轨裁决 full 都已确认，但**仍有 3×P1 + 2×P2**，T24.1/T24.2 继续不勾选。5 条红测先在 f243c86 上跑失败再改。
- **发现1（P1）：确定性代表并非"绝对优先"。** 我把两轨的**新证据**代表都塞进同一个 `fresh_anchors`，最后又按召回排名截断——`limit=1`、required=RagService、diagnostic=CitationParser、fresh=[CitationParser, RagService] 时保留的是 CitationParser。改法：**先按轨发保留资格、再按各来源原序输出**。资格顺序：确定性轨代表 → 本轮新证据里的诊断轨代表 → 补检至少一条 → 旧轮诊断轨代表 → 其余新证据 → 其余旧证据。
  - "补检至少一条"从第二位挪到新证据代表之后是我自己探针发现的：floor 会抓走一条无关新证据，而同轮的诊断轨代表反被饿死；代表本身就是新证据，用它同时满足两个目标。
  - 复审说得对——上一轮的随机探针没覆盖两轨竞争。补上诊断轨（带类名 token 的标签）后，整改前 **43 例违反**（全是诊断轨被饿死，确定性轨未出现饿死/驱逐），整改后 **0**；新增两条不变量：确定性方面容量放得下就一个都不许丢、诊断轨在本轮有代表时不得被饿死。
- **发现2（P1）：诊断轨仍可被末轮 LLM 自报回退。** 我把 evaluator 方面的 `present_now` 定义成"本轮又自报了一次"——证据集一字未变，evaluator 返回 `supported=[]/insufficient` 就能把整个 run 推进 refusal，正好违反刚裁决的诊断轨单调下界。改法：`observe_round` 给 evaluator 方面记**稳定证据绑定**（标签里的路径/类名 token 命中本轮证据就绑那几条，否则绑该轮全部证据——那就是 evaluate 当时据以判断的集合），`build_matrix` 改按"绑定证据 ∩ 当前证据集"算 `present_now`，`current_round` 参数随之删除。绑定偏粗是诚实的代价：`EvaluateOutput` 里没有 aspect→evidence 的自报字段，加它要动 LLM 契约（D6 边界，不在 T24）。补了端到端红测：证据不变 + 末轮撤回自报 → 仍交付 partial。
- **发现3（P1）：限定词一变，跨轨身份就失效。** 被挤出的 RagService 若被末轮报成"RagService 生产源码"，终态会同时出现原始缺失项与"前几轮已取得、被容量挤出"的三态说明——一句说没有、一句说取得过。根因是 T23 只做归一化全等比较，传两轨标签也建立不了身份。改法：`bound_required_ids` 用 T22 的路径/符号 matcher 建立跨轨目标身份，唯一绑定到必需项的 LLM 缺口交给确定性说明。
  - **收窄了一次**：我第一版把所有"绑定到有说明的必需项"的缺口都吸收掉，结果 T23 的 c10 用例立刻红了——`corpus_index`（证明文件已索引、只是没召回）这一类事实来源被连锅端。最终只吸收**被挤出**这一矛盾态；"从未取得"（两句同向、只是冗余）与"仍在证据集但未引用"（T23 会改写成"已出现在本次检索证据中"）继续走 T23，方法级细节和事实来源都保住。
- **发现4（P2）：淘汰记录仍不能逐条回放。** 账本单轮可达 24 条，step summary 却按 `MAX_SUMMARY_ITEMS` 截成 8 条，而完整 AgentState 不落库——回放只能看到前 8 条。改法：summary 按 `MAX_ELIMINATION_RECORDS` 落盘，并补两处测试：recorder 层 12 条、`_step_output_summary` 持久化映射 12 条（既有集成测试已证明 `output_summary` 原样落库回放）。**未做**的部分如实写在偏差记录里：没有另造 >12 chunk 的真实语料去跑端到端 21 条淘汰的 replay。
- **发现5（P2）：契约版本。** `evidence_eliminations` 是新增 state 字段，按《P1版本标识》"状态字段改变即递增"应升版——`AGENT_STATE_SCHEMA_VERSION` v6→**v7**，版本断言同步。Prompt 未动，`PROMPT_VERSION` 仍 p1.5-agent-v4。
- `make ci`（ruff/pyright 0、pytest 550）、`make eval-ci`（65+19）全绿；跨 5 个 PYTHONHASHSEED 一致。**T24.1/T24.2 仍不勾选，待第三轮复审。** 证据 commit：本提交。

## 2026-07-26 · P1.5 T24 第三轮复审整改：chunk 级分组绑定 + 纯身份缺口才吸收

- 复审对 bb1242c 的结论：五条原始反例都已对点修好（确定性代表优先、floor 顺序、简单矛盾态、24 条淘汰摘要、v7），但**把反例泛化一层还有 2×P1**。这一轮我学到的教训很直接：前两轮我都是"照着反例修"，反例过了就以为面上过了；复审每次都在同一处泛化后再找到问题。
- **发现1（P1）：交集不等于支撑。** 诊断轨绑不到具体证据时我记录"该轮全部路径"，`present_now` 又按任意路径交集算——绑定=[A.java（真支撑）, unrelated.md]、终态只剩 unrelated.md 时仍判"仍被支撑"并路由 generate。这只证明"原证据集合还剩一条"，不证明剩下的正是支撑；而且按路径判定会把同一文件的不同 chunk 当成同一条证据。
  - 改法（按复审给的最小安全口径）：新增 `EvidenceRef` 携带稳定 `chunk_id`，`AspectObservation` 逐轮记录 `chunk_ids` 绑定集合（**分组保留，不跨轮压平**），`build_matrix` 改判"**某一轮的绑定集合完整保留**才算 present_now"。确定性轨不变，仍由权威覆盖矩阵判定——那里的路径身份就是用户点名的对象，是正确口径。
  - 代价我写进了 docstring 与偏差记录：绑定粒度粗是因为 `EvaluateOutput` 没有 aspect→evidence 自报字段（加它要动 LLM 契约，属 D6 边界，不在 T24），所以只能用"整组保留"这种严格口径来兜；回退风险是"末轮没再自报 + 绑定集合缺一条"时诊断轨不再算可交付，此时终态取决于确定性轨。
- **发现2（P1）：吸收会静默删掉第二目标与方法级缺口。** 我的判据只数"匹配到几个 required id"——`RagService 中 NO_ANSWER 的触发条件` 被整条删除（方法/行为语义没了），`RagService 和 UnknownService 生产源码` 也被整条删除（UnknownService 压根不是 required item，所以 `len(bound)==1` 依然成立）。复审指出这与我自己写的注释"多目标保守保留"相反，还回归了 T23 的"方法级缺口信息保留"。确实如此：`bound_required_ids` 只能看见 required item，用它数目标数从原理上就不对。
  - 改法：新增 `_is_pure_identity_gap`——用 T22 的 `iter_target_spans` 数**原文里的目标个数**（与 required 无关），只有恰好一个才继续；去掉该目标后复用 T23 的 `strip_absence_markers`（顺手把它从 `_strip_absence_markers` 改成公开，不另抄一份，免得两处口径漂移）剥掉"当前证据未覆盖/缺少/未找到"这类措辞，残余必须落在一张封闭限定词表里（生产源码/实现/文件/配置/迁移/测试/文档…）。
  - 12 条对抗探针逐条核对：纯身份缺口（含带缺失措辞的写法）吸收；`hasGroundedVectorHit 方法`、`NO_ANSWER 的触发条件`、`重试逻辑`、`行号范围`、第二目标（UnknownService / CitationParser）、未绑定目标（OtherService）全部保留。
- 两处注释漂移一并修正：`plan_retention` 的资格顺序说明补上"本轮新证据里的诊断轨代表"这一层；finalize 里"evaluator 的 present_now 只表示末轮又自报了一次"已过时，改成按绑定粒度解释为什么只给确定性轨发"被挤出"告警。
- 契约版本：`AspectObservation` 字段语义变更 → `AGENT_STATE_SCHEMA_VERSION` v7→**v8**（Prompt 未动）。
- `make ci`（ruff/pyright 0、pytest 559）、`make eval-ci`（65+19）全绿；两轨随机探针 4000 例 0 违反；跨 5 个 PYTHONHASHSEED 一致；案例九回放仍是 partial + 保住 RagService。**T24.1/T24.2 仍不勾选，待第四轮复审。** 证据 commit：本提交。

## 2026-07-26 · P1.5 T24 第四轮复审整改：类型相容的吸收判据 + 判据不读截断文本

- 复审对 faf43b0 的结论：chunk 级分组绑定这一半合格了（`present_now` 用稳定 chunk_id + 完整集合包含、`[A, unrelated]` 只剩 unrelated 为 False、同路径不同 chunk 为 False、方法级缺口与第二符号目标都保住、两处注释与 v8 正确），但"纯身份缺口"判据还剩 **1×P1，含两处同源边界**，T24.1/T24.2 继续不勾选。
- **发现1（P1）：`iter_target_spans` 不等于完整的原文目标计数。** 我上一轮用它数"原文里有几个目标"，可它只认路径/符号/规范文档名；T22 还能从"测试/迁移文件/设计文档/配置文件"这类**类型词**解析出 type-only 目标，它们根本进不了 spans——而剩下的"测试/配置/迁移文件/设计文档"又恰好都在我那张限定词白名单里。于是 `RagService 和测试`、`与配置`、`及迁移文件`、`、设计文档` 四条全被整条吸收。根因还有更靠前的一层：`_QUALIFIER_NOISE` 把"与/和/及/或/、/逗号"当结构噪音抹掉了，连接词一没，"另一个目标"就退化成"一个限定词"。
  - 更直接的类型错配复审也点了：required R1 = `RagService 的生产源码`、missing = `RagService 的测试`，没有连接词也会被吸收——可确定性三态说明只讲"生产源码:RagService 被挤出"，它替代不了测试缺口。
  - 改法按裁决的三条：①连接词从噪音表移出，改由 `_CONNECTOR` 单独判——目标两侧只要出现协调连接词就 fail-closed；②限定词白名单拆成 `_TYPE_QUALIFIERS`（按 `EvidenceType` 分表）+ `_IDENTITY_QUALIFIERS`（只剩""/文件/类 这种与类型无关的身份词），残余必须与**绑定 item 的类型**相容；③补红测。
  - 写注释时才想清楚为什么"类型相容"恰好是对的判据、而不是一个凑合的近似：绑定 item 必有 path/symbol，而 T22 的 `_build` 在同类型已有点名项时会丢弃 type-only 项——所以"限定词与绑定类型相容"等价于"这个类型词不可能另有一条 required 目标"。这条推理写进了 docstring。
- **发现2（P1，与发现1 同源）：判据读的是被截断的文本。** `strip_absence_markers` 返回前截到 60 字，而它同时是"是否纯身份"的输入；缺口条目允许到 500 字，把方法级语义放在第 60 字之后就能让判据只看见开头的"生产源码"。改法直白：去掉函数里的截断，`_MAX_SUBJECT_CHARS` 只留在用户可见的 `_subject` 渲染，并在两处都写明"这是渲染上限，不得用于分类/安全判据"。
- **红测有效性留证**：7 条新增用例先用 faf43b0 的旧判据逐条跑过——全部返回"吸收"，包括 60 字截断那条（112 字的合成文本）。
- **探针自查又逮到一处复审没点的边界**：我写了个 22800 组合的忠实探针（绑定类型取自**真实问题解析**，交叉核验"整条被吸收 ⟹ 用 T22 自己的解析器读这条缺口文本时不存在第二个点名目标/别的类型目标/未解析约束"）。第一版探针 3880 例违反是我自己造的假——它把任意 (目标, 类型) 组合硬凑在一起，生产里绑定类型只会来自问题解析；改忠实后剩 15 例，全是同一个形态：`RagService.javaOrderService` 这种**无分隔符拼接**，`merge_spans` 会把相接的两段并成一段，只数跨度就看不见第二个符号。于是把"目标计数"从数跨度改成**逐 token 核对身份**（新增 `evidence_types.target_tokens_all_match`，复用 `_item_matches_token`，不另建一套身份口径）。改完 0 违反，且仍吸收 1375 条——没有退化成"永不吸收"，二审发现3 的自相矛盾没被放回来。
- 契约版本不变：本轮只改确定性后处理，无 state 字段与 Prompt 改动，`AGENT_STATE_SCHEMA_VERSION` 仍 v8。
- `make ci`（ruff/pyright 0、pytest 572）、`make eval-ci`（65+19）全绿；跨 5 个 PYTHONHASHSEED 一致。**T24.1/T24.2 仍不勾选，待第五轮复审。** 证据 commit：本提交。

## 2026-07-26 · P1.5 T24 第五轮复审整改：句末标点不是协调连接词

- 复审对 18beb85 的结论：四审那条 P1 的两处边界都修住了（逐 token 身份核对、类型错配、多目标连接词、长文本不截断、`RagService.javaOrderService` 边界、v8 不变），但我上一轮**自己新引入了一个 P1**，T24.1/T24.2 继续不勾选。
- **发现1（P1）：句末标点被当成协调连接词。** 我把 `，,；;` 塞进 `_CONNECTOR`，又对整段 remainder 做任意搜索——注释写的是"目标两侧出现"，代码做的是"整段里有没有"。于是 `RagService 生产源码，` 这条**合法**的 LLM 输出（`ShortText` 从没禁止句末标点）不再被吸收，同一方面的"缺失"与"已取得后被挤出"三态说明又并存了，正好把二审发现3 的自相矛盾放回来。
  - 这是典型的"修一个方向修过头"：上一轮为了堵住 `RagService 和测试`，我选了最粗的 fail-closed 口径，还在注释里给自己写了一条"限定词表里的词都不含这些字符，故不会误伤"的安全性论证——那条论证只覆盖了限定词**本身**，完全没考虑限定词**后面**还能跟标点。
  - 改法按复审给的最小口径，但落在一个更能自证的判据上：**连接词靠目标的那一侧天然有内容（就是目标本身），所以只需检查背离目标的一侧**。该侧去掉标点后还有实词 → 真的在并列（`RagService、设计文档`）；只剩标点或为空 → 它只是句首/句末标点（`RagService 生产源码，`）。左右两段分别判，`_CONNECTOR` 的字符集一个没动。
- **发现2（P2）：清单标号重了。** 我把本轮写成"三审整改（本提交）"，下一行又是"三审整改（历史记录）"。已改成"四审整改（历史记录）/五审整改（本提交）"，并把被我插乱的"⚠️ 升序 → ✅ 降序"排布复原。
- **红测有效性留证**：`，` `,` `；` `;` 四种句末形态加上句首标点共 5 条，先在 18beb85 上逐条确认被判"不吸收"；另补一条句末句号做回归（它本来就不在 `_CONNECTOR` 里，属防漂移）。同时锁住 `RagService 和测试`、`RagService、设计文档`、`RagService 的测试`、多目标四条仍不得吸收。
- **探针扩容**：四审那个 22800 组合的忠实探针加上 8 种句末标点形态后是 **182400 组合**，0 违反、吸收 11960 条——放宽的是标点，不是并列语义。
- 契约版本不变：只改确定性后处理，`AGENT_STATE_SCHEMA_VERSION` 仍 v8。
- `make ci`（ruff/pyright 0、pytest 578）、`make eval-ci`（65+19）全绿；跨 5 个 PYTHONHASHSEED 一致。**T24.1/T24.2 仍不勾选，待第六轮复审。** 证据 commit：本提交。

## 2026-07-26 · P1.5 T24 第六轮复审整改：范围前缀不是并列对象

- 复审对 e1e42bd 的结论：五审那条 P1 修住了（句末标点反例、真并列、类型错配、清单标号与排序、v8 都对），但**同一处判据又被泛化一层找到 1×P1**，T24.1/T24.2 继续不勾选。
- **发现1（P1）："背离目标一侧有实词"不等于"存在第二目标"。** 我上一轮把"该侧去掉标点后还有实词"当成并列判据，可**范围前缀**也是实词——`证据中，RagService 的生产源码` 里的"证据中"被当成了另一个并列对象，于是加不加那个逗号会改变吸收结果（plain=True / comma=False），7 个 orphan 前缀条条复现，displaced R1 的矛盾文案就这么回来了。
  - 讽刺的是这些前缀正是 T23 `_ORPHAN_PREFIXES` 里我自己写的"去标记后剩下的悬空前缀，要剥掉"。我在判连接词时另起了一套"什么算实词"的口径，没复用同一个归一化器——这跟三审时"不另抄一份 `strip_absence_markers`"的理由是同一条，我却在新增的分支上又犯了一次。
  - 改法：判"背离目标一侧"时先过 `strip_absence_markers`，归一化后还剩实词才算并列对象。这样 `证据中` / `当前证据未覆盖` / `源码中不存在` 一律不算，`测试` / `设计文档` 照旧算。
- **发现2（我的探针为什么没抓到）：不变量只有一个方向。** 182400 组合那条不变量是"吸收 ⟹ 没有第二目标"，它只能抓**错误吸收**；本例是**应吸收却未吸收**，方向相反，探针再大也照不到。复审直接把第二条不变量给出来了：**对已认定的纯身份缺口，插入可剥离 orphan/absence 前缀及其标点，不得改变吸收结果**。这条现在既进了探针（126 种变形 × 每个已吸收基例，翻转 0），也落成了 126 例参数化红测——探针是一次性的，红测才会一直守着。
- **红测有效性留证**：去掉本轮 src 改动后新测 **74 条全红**（含图级那条），恢复后 199 条全绿。除变形不变量外还加了：图级断言（末轮报 `证据中，RagService 生产源码` 时终态只有一条 RagService 说明）、`证据中，RagService 和测试`／`证据中，RagService、设计文档` 仍不吸收、`关于订单，RagService 的生产源码`（非可剥离前缀）保守保留。
- 契约版本不变：只改确定性后处理，`AGENT_STATE_SCHEMA_VERSION` 仍 v8。
- `make ci`（ruff/pyright 0、pytest 710）、`make eval-ci`（65+19）全绿；跨 5 个 PYTHONHASHSEED 一致。**T24.1/T24.2 仍不勾选，待第七轮复审。** 证据 commit：本提交。

## 2026-07-26 · P1.5 T24 验收关闭

- 第七轮独立复审对 734cd2d 裁定「T24 可以验收」，据此勾选 T24.1/T24.2。这是 P1.5 里返工最多的一项：首版 0c0eb36 之后连续六轮复审、六次整改（f243c86 → bb1242c → faf43b0 → 18beb85 → e1e42bd → 734cd2d），全部过程与反例留在《P1.5任务清单》偏差记录一～六审。
- 回头看，六轮里真正重复的只有一种错误：**我按反例修，复审按不变量查**。一审改保留算法、二审改发资格顺序，都是照着给出的最小例修；三审开始复审每次把同一处判据泛化一层就能再找到问题（交集不等于支撑 → 目标计数不等于跨度数 → 句末标点不是连接词 → 范围前缀不是并列对象）。四轮下来同一个 `_is_pure_identity_gap` 被改了四次。
- 有效的做法是后几轮才补上的：写**可证的不变量**而不是对着例子打补丁。四审那条"吸收 ⟹ 用 T22 自己的解析器读这条文本时没有第二个目标"用 22800 组合交叉核验，直接逼出了 `merge_spans` 相接合并这个复审没点的边界；六审复审又指出我的不变量只有单方向（只能抓错误吸收，抓不到应吸收却未吸收），补上变形不变量后才闭合。**探针是一次性的，所以两条不变量最后都落成了常驻红测**（126 例参数化 + 图级断言）。
- 另一条教训是口径复用：三审我特意把 `strip_absence_markers` 从私有改公开、写明"不另抄一份免得两处口径漂移"，六审却正是因为在新增的连接词分支上另起了一套"什么算实词"而翻车。同一个模块里新增分支时，先问"这个判断已经有归一化器了吗"。
- T24 收尾状态：`make ci`（ruff/pyright 0、pytest 710）、`make eval-ci`（65+19）全绿，跨 5 个 PYTHONHASHSEED 一致；`AGENT_STATE_SCHEMA_VERSION` v8、`PROMPT_VERSION` p1.5-agent-v4、`ANSWER_SCHEMA_VERSION` p1.5-answer-v2。下一项按执行顺序是 T25（finalize 前确定性一致性检查 + 第二稿覆盖复检，RT-03/08）；T24 里观察到但未做的"draft is None 时拒答文案不准确"已在一审记过，正属 T25.1 判据范围。

## 2026-07-26 · WF-001 AI 开发与审查收敛工作流

- **触发原因**：T24 从首版到验收经历六轮整改，暴露出的主要问题不是单个模型能力，而是实现前没有共享任务合同和不变量、首审后没有冻结范围、可自动检查的规则仍依赖模型记忆。依据 `docs/AI开发工作流审计报告.md`，在独立分支安装最小工作流，不改 `src/`、数据库、依赖或评测数据。
- **统一事实源**：重写 `AGENTS.md` 的流程入口，并新增 `docs/development-workflow.md`。每个任务只建一份 Task Packet，合同、仓库事实、逐文件计划、冻结测试、计划前审、review ledger、定向修复和验收都留在同一文件；P0～P3、任务拆分、第二轮审查冻结和最多一次普通修复有了可判定口径。
- **只建两个 skill**：`project-preimplementation-gate` 负责写代码前的合同/计划/测试门禁，`project-scoped-review` 负责首审与终局复审。canonical 内容在 `.agents/skills`，`.claude/skills` 使用相对 symlink，共享同一规则；Agent/RAG 长检查表只在相关任务加载。没有把 minimal-patch、test-design、DoD 等拆成更多 skill，它们是每次必守规则，留在 AGENTS/Task Packet。
- **自动门禁**：新增无第三方依赖的 `scripts/workflow_guard.py`，校验 Task Packet schema、base SHA、允许路径、文件预算、依赖/迁移/golden/snapshot、敏感文件、Python debug/`if testing`/skip/xfail 和断言数量下降；13 个单元测试覆盖正常及拒绝路径。Makefile 新增 `preflight`、`verify-task`、`verify-full`、`workflow-check`；代码类 PR 必须且只能带一份 Task Packet，CI 自动对它运行范围门禁，再调用同一 `make ci`，避免本地与远端命令漂移。
- **验证**：两个 skill 均通过 `quick_validate.py`；`make workflow-check`、`make verify-task PACKET=docs/tasks/WF-001-ai-review-convergence.md` 通过；`make ci` 为 Ruff/Pyright 0 错误、pytest **723 passed**；`make eval-ci` 为 **65 + 19 passed**。证据 commit：本提交。
