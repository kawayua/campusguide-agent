# W10 指标口径与证据台账

> 用途：简历、30 秒介绍、面试回答只引用这里能追溯的数字；同一数字必须带运行/模型/测试集口径。

| 声明 | 数值 | 来源 | 口径/限制 |
|---|---:|---|---|
| 语料规模 | 225 文档 / 244 版本 / 4,026 块 | `data/manifest.json` | W1–W7 历史报告按当时 213/232/3,952，不回填 |
| 向量索引 | 4,026 点 | `data/vector_manifest.json` | `articles_v1@hybrid-6bafb68410b9` |
| W2 开发集 | 20/20 outcome；引用 100%；quote 100%；p50 3.34s / p95 14.7s | `docs/weekly/w2.md` | p95 未达 10s，已接受 |
| W3 baseline | rendered support 75.0%（56/56 人工） | `docs/weekly/w3.md` | 未过 85%；W3 记为有条件通过 |
| W6-A prompt | 54/54 人工，98.1% | `data/eval/w6a-dev-report.md` | 只改 prompt；不是 W6-B 图结果 |
| W4 检索 | Recall@8 1.000；hybrid+rerank MRR 0.958；population 5/5 | `docs/weekly/w4.md` | 检索/工具指标，不是答案质量 |
| W5 流程 | 3 个流程；closed/open；495 tests（W5 时点） | `docs/weekly/w5.md` | 流程事实来自 YAML |
| W6-B 评测 ultraspeed | 39/39 引用；0 quote mismatch；拒答 100%；误拒 0；partial 1；support 39 supported / 3 partial / 0 unsupported；rendered 92.7%；p50 20.2s / p95 43.6s | `data/eval/w6b-ultraspeed-final-dev-report.md` | 评测档；42/42 人工标注；p95 达标、p50 未达 15s |
| W6-B 生产 vPro | 46/46 引用；0 quote mismatch；support 44 supported / 1 partial / 0 unsupported；rendered 97.8%；p50 41.7s / p95 174.4s | `data/eval/w6b-mcp-final-dev-report.md` | 生产/部署基线；45/45 人工标注；不能写成评测档 |
| W7 remediation4 | support 1.000；answer 0.875；version 1.000；拒答 1.000；误拒 0；p50 7.37s / p95 21.38s | `data/eval/w7-remediation4-report.md` | **总门禁未通过**（p95 >10s）；`w7` 已冻结 |
| W8 演示层 | 646 tests；页面 smoke 6/6；p50 1.46s / p95 138.8s（生产 vPro）；Docker 2.79GB | `docs/weekly/w8.md`、`data/eval/w8-ui-smoke.md` | 页面 smoke 不替代 W6/W7；UI 不展示模型 |
| W9 开源 PR | 无 PR、无 merged | `docs/weekly/w9.md` | deferred；GitHub 443/无认证 |

## 禁止表述

- “W7 通过” / “W7 50 题全过” → 错；W7 总门禁未过。
- “W9 已 merged” / “已给 Dify 提交 PR” → 错；W9 deferred。
- “support 97.8% 是 W6-B 评测主结果” → 错；97.8% 是生产 vPro，评测 ultraspeed 是 92.7%。
- “support 92.7% 是生产结果” → 错；92.7% 是评测 ultraspeed。
- “所有问题 p95 <10s” → 错；W7 p95 21.38s，生产 vPro 174.4s，评测 ultraspeed 43.6s。
- “Prompt 98.1% = W3 通过” → 不严谨；W3 baseline 75.0%，W6-A 是同 20 题 remediation，两个数字都保留。
- “全自动、无需人工” → 错；有 handoff、clarify 上限、人工入口。

## 一句话口径

**W6-B 评测档 ultraspeed：39/39 引用、0 quote mismatch、拒答 100%、人工 support 92.7%、p50 20.2s / p95 43.6s；生产 vPro：support 97.8%、p95 174.4s；W7 质量门禁过但 p95 21.38s，总门禁未过；W9 deferred。**