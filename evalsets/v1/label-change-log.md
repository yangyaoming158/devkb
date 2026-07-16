# Evaluation v1 标签变更记录

> 审计日期：2026-07-16
> mini-mall 源 commit：`cfb49f5090befe62f024f1781144fe77e5b905c7`
> 用户确认：2026-07-16，全部同意

## 结论

本次 source-only 盲审未修改 Evaluation v0 的任何 JSONL：

- 25 个原可答题和 35 个 `rel_path + anchor` 全部有效；
- 6 个边界题继续保留 `answerable=false`，不移动 split，不改变 17+4、8+2 分组；
- 新增 `answer-expectations.jsonl` 冻结三态期望：u01/u02/u03/u05=`refusal`，u04/u06=`partial`。

`u06` 因 P1 新增 Java 语料而能回答签名算法（HMAC256），但密钥轮换仍无证据；这属于 expected mode 的边界细化，不是对 v0 answerable/relevant 标签的修改。

| ID/范围 | v0 变更 | Evaluation v1 处理 | 依据 |
|---|---|---|---|
| q01–q25（25 个可答题） | 无 | expected full | 35/35 路径和 anchor source-only 核验通过 |
| u01/u02/u03/u05 | 无 | expected refusal | 允许语料无核心答案 |
| u04/u06 | 无 | expected partial | 有可支持的局部事实，但问题核心仍缺证据 |
