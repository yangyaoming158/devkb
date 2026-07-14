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
