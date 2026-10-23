# W3 v1 归档

本目录保存已经回退的 W3 v1 设计与修复清单，仅用于复盘和重新设计参考。新的最终重建设计见
`docs/superpowers/specs/2026-10-23-w3-citation-verification-abstention-design-v2.md`。

## 为什么归档

W3 v1 的确定性核对实现已经跑通 20 题结构门禁，但：

- rendered 支持率 75.0%，低于 85% 门禁；
- 57 条 support 标注是 AI 预标注，未形成正式人工评测；
- Verifier 的 blocking issue 与 PASS/ANSWERED 存在契约不一致；
- 中文数字/日期覆盖有漏检；
- 最终报告与实现 commit 不完全对应。

因此按用户决定回退到 **W3 无代码阶段**，保留 W2 实现，W3 重新设计。

## 内容

- `2026-10-23-w3-citation-verification-abstention-design.md`：W3 v1 设计；
- `2026-10-23-w3-fix-plan.md`：W3 v1 修复清单与已知问题。

归档文档中的路径、指标和结论对应 W3 v1 时点，不代表当前 main。

## 代码与数据备份

- `backup/w3-final`：W3 v1 最终代码与报告（`9037dd4`）；
- `backup/w3-v1`：W2 文档提交（`81079b1`）；
- `stash@{0}`：回退前未提交的 promptfix 实验与 `docs/build-vs-reuse-assessment.md`。

这些备份不在当前 main 上；如需复用，先在新分支中恢复。
