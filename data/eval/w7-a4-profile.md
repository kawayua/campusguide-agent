# W7 A4 profile 过滤消融

- 数据集：`test_v1.jsonl`（208cf3c5f032…）
- 冻结测试集 hash：`2a8d0e122362…`
- profile 题：5 道
- baseline run：`23b384f15556…`
- A4 run：`70f52008476c…`

## 人群错配

| 变体 | 有 retrieved 错配的题 | retrieved 错配/总数 | 有 cited 错配的题 | cited 错配/总数 |
|---|---:|---:|---:|---:|
| baseline（有 profile） | 0/5 | 0/35 | 0/5 | 0/15 |
| A4（无 profile 元数据） | 1/5 | 7/34 | 1/5 | 4/20 |

## 拒答与误拒

| 变体 | 拒答召回 | 误拒率 | 原因码正确率 |
|---|---:|---:|---:|
| baseline（有 profile） | 1.000 | 0.000 | 1.000 |
| A4（无 profile 元数据） | 1.000 | 0.000 | 1.000 |

## 逐题错配

| 题目 | 变体 | retrieved 错配 | cited 错配 |
|---|---|---:|---:|
| test-040 | a4 | 7 | 4 |

## 说明

- A4 关闭 profile 的学院/专业/入学年份等元数据过滤与查询扩展，保留 audience 硬过滤和版本意图 guard。
- 错配判定复用 `article_matches_profile`：只在文档有明确约束、且用户已知值与约束冲突时计为错配。
- A4 只比较错配与拒答，不使用 answer correctness/support 标签；关闭 profile 后的答案变化不由本消融评价。
- W7 冻结集只有 5 道 profile 题，A4 比例只作方向性证据。
