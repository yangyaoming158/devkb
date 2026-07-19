# devkb — 软件项目知识助手（P0 可运行基线）

把一个软件项目的 Markdown 文档摄取为向量知识库，用一条固定 RAG 管道回答问题，**每个回答附带可溯源的真实引用（文件路径 + 行号 + 标题路径）**。当前为 P0 基线：Markdown → pgvector → 单次 LLM 调用 → L0 引用校验。Agentic 能力（自主规划/补检/拒答）是 P1 内容，见 `docs/范围冻结与架构决策.md`。

- 技术栈：Python 3.12 + uv ｜ PostgreSQL 16 + pgvector（单库四职责）｜ SQLAlchemy(async) + Alembic ｜ Qwen3-Embedding-0.6B（本地 GPU，ADR-0002）｜ DeepSeek（唯一在线 LLM 调用点，ADR-0004）｜ Typer + Rich
- 实测规模：mini-mall-order 语料 39 文档 → 1370 chunks（约 33 万 tokens），摄取 54s（RTX 4060 8GB）；单问端到端约 5s，成本约 0.0015 元/问

## 起步（4 步）

```bash
cp .env.example .env        # 填入 DEVKB_LLM_API_KEY（DeepSeek）
make up                     # 启动 postgres（pgvector 镜像）
uv run alembic upgrade head # 建表（vector(1024)，ADR-0002 冻结维度）
uv run devkb ingest <你的文档目录> --project demo   # 摄取（需要 GPU，见下）
uv run devkb ask "订单状态机允许哪些状态流转？" --project demo
uv run devkb runs list --project demo               # 历史 run（token/延迟/成本）
```

- `devkb ask --json` 输出完整 Answer JSON（含引用、warnings、usage）；`devkb runs show <run_id>` 取回历史 run。
- **网络提示（WSL/代理环境实测）**：本地代理会干扰对 DeepSeek 的直连，而 unset 代理后 sentence-transformers 联网校验 huggingface.co 会因 DNS 污染挂死。模型首次下载后，运行时加 `HF_HUB_OFFLINE=1` 并 unset 代理最稳。
- 嵌入模型要求宿主 GPU（ADR-0002：本模型 CPU 批量编码不可用；fp16 + batch≤32 为 8GB 显存实测约束）。

## 本地 HTTP API（P1）

```bash
uv run devkb serve                    # 默认只监听 127.0.0.1:8000
curl localhost:8000/healthz           # 进程/数据库/迁移版本探测（不加载模型）
curl -X POST localhost:8000/ask -H 'content-type: application/json' \
  -d '{"project":"demo","question":"订单状态机允许哪些状态流转？"}'
curl "localhost:8000/runs/<run_id>?project=demo"   # run + steps + 工具调用 + Answer
```

> **⚠️ 安全边界：P1 API 完全无鉴权，仅限本机使用，严禁绑定非回环地址或经端口转发/反代暴露公网。** 任何能访问该端口的人都可读取知识库内容与全部历史 run。按范围冻结决策，P1 不加入 JWT/CORS/管理后台。

## 开发

```bash
make ci    # ruff + pyright + pytest（全部测试用 FakeEmbedder/FakeLLM，不触网）
```

## 已知限制（P0 如实声明，均为 P1/P2 规划内容）

1. **无拒答策略**：证据不足时是否说"没找到"完全依赖 prompt 约束，没有确定性拒答分支；不可答问题可能得到看似有据的回答（P1 引入确定性拒答/部分回答策略）。
2. **纯向量检索（精确扫描）**：无关键词/混合检索与 RRF，对精确标识符类查询（报错码、类名）召回偏弱；精确扫描换取召回=100% 的无近似基线，量大后需要 HNSW（P1）。
3. **诚实性仅到 prompt 级 + L0**：只校验引用编号 `[E#]` 确实指向本次证据（越界剔除并记 warning），**不校验引文内容与原文是否一致、更不校验语义支持度**（L1 模糊匹配 P1、L2 语义支持度离线评测 P2）。引用保证可溯源，不保证回答为真。
4. **单用户单机**：无鉴权、无 API 服务（FastAPI 在 P1）、无增量索引（文档变更靠 content_hash 全文档重嵌）。
5. **摄取范围**：仅 .md/.txt（≤5MB/文件）；代码文件解析是 P1（tree-sitter Java）。
