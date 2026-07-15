# ADR-0003：演示语料与解析优先级

- 状态：已接受（2026-07-12 冻结；本文件为 T10.0 转写，内容以《范围冻结与架构决策》D2/〇-2 为准）
- 依据：《范围冻结与架构决策》D2、已确认决策〇-2、审查处置 C1

## 决策

**演示语料 = `/home/oslab/projects/mini-mall-order`（只读使用）**。2026-07-12 核查：Spring Boot 3.5 多服务项目（order/inventory/payment/product/user/notification + api-gateway），392 个 Java 文件约 3.8 万行，docs/ 下 54 个 Markdown（架构评审/各阶段验收/部署/可观测性/开发日志），14 个 SQL、37 个 yml。文档已是 Markdown，无需格式转换。

**解析优先级：Markdown(P0) → Java + 配置文件(P1) → OpenAPI/PDF/Python(P2)**。原规划"Python 解析优先"与 Java 语料直接矛盾（C1），已对调。

## P0 摄取范围（冻结）

- `docs/**/*.md` + 根级 `README*.md`；
- **不摄取** CLAUDE.md / AGENTS.md / TASKMASTER.md：这些文件含指令样式文本，留作 P2 prompt injection 测试样本。

## 后果

- P0 管道只需 Markdown 解析 + 标题树分块，无需 tree-sitter/PyMuPDF（依赖门禁一致）。
- 语料真实且带完整开发史（dev-log、验收报告），使 Evaluation v0 的"真实问题 ≥60%"来源纪律可执行。
- mini-mall 语料摄取后的实际 chunk 数属待验证假设（源文档第五节⑤），由 T10.2 实跑记录。
