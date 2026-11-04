# V7 Git准备说明（2026-11-04）

本轮停止调参，只整理文档和提交范围，保留现有V7业务实现及全部本地实验。没有继续调用真实问答API、修改业务逻辑或批量格式化。当前不是上线发布；未执行commit、push、生产别名切换或生产配置调整。

## 提交范围

整理时分支为`main`，基准提交`8a7e226`。按用户补充要求，提交清单由182个文件扩为185个文件，其中169个Python文件，纳入现有3个UI文件及更直观的对比效果说明。该范围是此前累积的RAG/OCR实现、配置、测试及本轮文档，不能描述成仅有最后一轮7文件修复。

- 解析/表格行列与父子结构、来源及受众元数据、检索、引用核验、覆盖与答案处理的累积实现；Xiaomi OCR与Gemini疑难区域实验适配器及测试。
- README、历史评测/合规文档的当前状态说明、[阶段报告](reports/v7-final-report-20261104.md)及[指标JSON](reports/v7-final-metrics-20261104.json)。
- 日期物化脚本与测试需要的`data/eval/answer_quality/ingestion_refactor_20261030/date_fields_to_materialize.json`：42个版本的日期依据元数据；它是`.gitignore`中的唯一新增实验目录例外，不含完整制度正文。
- 新增忽略规则排除本轮完整语料、原始答案、数据库、临时测试和模型实验产物。忽略规则不删除本地文件，也不会移除此前已经跟踪的历史文件。

现已纳入已有的3个UI文件改动：`src/campusguide/ui/app.py`、`theme.py`、`view_models.py`，文件字节保持原样。README增加交互和样式改前/改后表，并补齐B/V7的MRR与指标差值；报告及JSON增加同50题和OCR同8题的差值，沿用既有测评、不新增API调用。另有此前未跟踪的`docs/reports/RAG质量诊断与修复_20261030.md`，留作单独审阅。未运行`git add .`，不自动纳入该历史报告或本地原始实验。

## 本轮离线检查

| 检查 | 实际结果 |
|---|---|
| 完整工作区pytest | 2639 passed，9 warnings，113.00秒；8个类型变异测试的Pydantic序列化警告、1个沙箱pytest缓存写入警告 |
| Python编译 | `compileall -q src scripts`通过 |
| 本次范围Python文件lint | 原166个文件0项问题；新增纳入的3个UI文件单独lint亦为0项问题 |
| 全库lint | 19项问题，涉及10个与HEAD完全未变的已跟踪文件；是已有问题，本轮未修 |
| 格式门 | 164个文件需要格式化、222个已符合；未批量格式化 |
| Git空白检查 | 整理前`git diff --check`与暂存后`git diff --cached --check`均通过 |
| 源码/配置冻结 | 146个文件与已接受快照SHA-256一致 |
| 既有实验完整性 | 同50题24项、OCR同8题55项产物哈希均未变 |

9个警告与修复阶段报告的8个警告不同，多出的一个来自当前沙箱无法写既有pytest缓存。收尾测试在现有完整工作区执行，已包含本次纳入的UI代码；没有宣称干净checkout、Docker构建或线上并发测试通过。CI会执行全库lint和格式门，因此当前不能宣称CI绿色或已经可合并发布。按停止修bug要求，仅记录，不扩大修改范围。

lint问题位于已有的评测脚本、providers/entities以及R2/R4/R5/R8测试，具体为未使用导入/变量、导入顺序、旧字符串格式、UTC写法和导出排序等。格式门是另一项独立失败，不能用pytest通过替代。

## 本地审阅材料

`data/git-preparation/v7-20261104/`仅本地保存：`checks.json`及检查原始日志、pytest XML、`lint_baseline.json`、`scope_ruff.json`、`preservation.json`、原清单`stage_paths.json`/`stage_paths.nul`、扩展后的精确`stage_paths_with_ui.json`/`stage_paths_with_ui.nul`及`ui_ruff.json`，并准备提交信息`COMMIT_MESSAGE.txt`。这些文件自身不加入提交。

暂存检查已经通过；已按扩展清单暂存185个文件；当前已跟踪文件的工作区改动均已纳入暂存。可以继续审阅：

```bash
git diff --cached --stat
git diff --cached --check
git diff --cached --name-only
git status --short
```

建议提交标题：`chore(rag): preserve V7 implementation and document evaluation limits`。该提交应作为研发阶段留档；若准备正式上线，仍需单独完成全库OCR、来源/表格核验、独立dev阈值校准与holdout验收，以及现有CI失败项的专门处理。本轮不继续这些工作。
