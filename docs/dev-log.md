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
