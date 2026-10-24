# W7 test_v1 changelog

## 2026-10-24 · 标注错误修正

- 旧 canonical hash：`ed451233c9fe79397443d5a2ffbf68b9debd665040b4e5d50b0f2a4282d2e1ac`
- 修正：`test-036` 的 gold 指向 `agri-grad-plan-crop-0901@v2025#一、培养目标`，但 2025 版
  `effective_to=2026-08-31`，在 `as_of=2026-10-24` 已过期；改为 2026 版 gold
  `agri-grad-plan-crop-0901@v2026#一、培养目标`，问题/profile 年份同步改为 2026。
- 原因：测试集标注错误，不是模型结果调题。
- 旧冻结集/manifest 归档为 `test_v1.v0.jsonl` / `test_v1.v0.manifest.json`。
- 新 hash：`2a8d0e1223621520e5807aaa3b2a244110f9835bb7bae6d5bc35e4ed7f7e8f18`。
