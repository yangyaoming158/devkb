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
