# P1 新依赖最小 spike 与许可核查（T11.2）

> 核查日期：2026-07-16 ｜ Python：3.12 ｜ 锁文件：`uv.lock`
>
> 许可信息来自上游 LICENSE 或已安装 wheel/sdist 元数据；本记录不构成法律意见，也不保证转授权、商业使用或所有依赖链在任意场景下均无风险。

## 决策结论

选择冻结当前 Python 3.12 实际解析成功的最小组合：

| 用途 | 直接依赖与锁定版本 | 许可元数据 | 维护状态证据 | 当前风险/边界 |
|---|---|---|---|---|
| LangGraph 状态图 | `langgraph==1.2.9`; `langchain-core==1.4.9` | 两者均标记 MIT | PyPI 包上传时间分别为 2026-07-10 / 2026-07-08；上游 release 持续更新 | 只使用 `StateGraph`；不引入 `langchain` 元包、ChatModel、VectorStore 或 provider adapter |
| FastAPI async + ASGI server | `fastapi==0.139.1`; `uvicorn==0.51.0`; 测试 `httpx==0.28.1` | FastAPI 标记 MIT；Uvicorn/HTTPX 标记 BSD-3-Clause | FastAPI/Uvicorn 包上传时间为 2026-07-16 / 2026-07-08；HTTPX 最新稳定包为 2024-12-06 | 不使用 `fastapi[standard]`，避免引入 CLI/模板等无关 extras；HTTPX 只是 dev 直接依赖 |
| Java 纯解析 | `tree-sitter==0.26.0`; `tree-sitter-java==0.23.5` | 两者均标记 MIT | Python binding 包于 2026-06-30 上传；Java grammar 最新 PyPI release 为 2024-12-21，官方仓库仍开放维护 | grammar 发版节奏较慢；用真实 ABI + CST 结构测试锁定兼容性，解析器永不执行 Java |
| CJK 预分词 | `jieba==0.42.1` | 标记 MIT | 最新 PyPI release 为 2020-01-20，上游活跃度低 | Python 3.12 首次编译会报 invalid escape `SyntaxWarning`，但功能 spike 通过；T14 必须封装为纯函数、关闭 HMM 并用 golden 锁输出 |

直接依赖版本在 `pyproject.toml` 记录下界，本次实际解析版本由 `uv.lock` 精确锁定。未引入 Redis、reranker、PyMuPDF、LlamaIndex、LangFuse/MCP SDK、Neo4j driver 或 Dify/n8n 运行依赖。

LangGraph 1.2.9 的必需转递依赖包含 `langgraph-checkpoint`、`langgraph-prebuilt`、`langgraph-sdk`；`langchain-core` 的依赖链包含 `langsmith`。P1 不导入或配置 LangSmith，不引入 PostgreSQL checkpoint adapter，不启用 LangGraph checkpointer。这些转递包不代表对应能力已纳入范围。

## 最小成功样例

一次性 spike 已删除，有效契约收敛在 `tests/unit/test_p1_dependency_spikes.py`：

1. LangGraph：编译 `START -> increment -> END` 状态图，精确断言终态和节点访问序列。
2. FastAPI：使用 HTTPX `ASGITransport` + `AsyncClient` 在进程内 async 请求 `/ping`，不启动端口或网络。
3. tree-sitter Java：用官方 grammar 解析内存中的 Java bytes，断言 package/class/field/constructor/method CST 节点且无 parse error。
4. jieba：新建独立 `Tokenizer`，`HMM=False` 连续两次分词得到完全相同的冻结 token 列表。

## 核查来源（2026-07-16 访问）

- LangGraph：<https://github.com/langchain-ai/langgraph/releases> 与 <https://github.com/langchain-ai/langgraph/blob/main/LICENSE>
- langchain-core：<https://github.com/langchain-ai/langchain/releases> 与已安装包 metadata
- FastAPI：<https://fastapi.tiangolo.com/release-notes/> 与 <https://github.com/fastapi/fastapi/blob/master/LICENSE>
- Uvicorn：<https://uvicorn.dev/release-notes> 与 <https://github.com/Kludex/uvicorn/blob/main/LICENSE.md>
- HTTPX：<https://github.com/encode/httpx/releases> 与 <https://github.com/encode/httpx/blob/master/LICENSE.md>
- py-tree-sitter：<https://github.com/tree-sitter/py-tree-sitter> 与 <https://github.com/tree-sitter/py-tree-sitter/blob/master/LICENSE>
- Java grammar：<https://github.com/tree-sitter/tree-sitter-java> 与 <https://github.com/tree-sitter/tree-sitter-java/blob/master/LICENSE>
- jieba：<https://github.com/fxsjy/jieba> 与 <https://github.com/fxsjy/jieba/blob/master/LICENSE>
