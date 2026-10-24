# W6-A2 support 标注核对表

- run_sha256：`b7ee6884ec3191d2b908afdd6cb97fe7191a5a74302b3281a96f249c07e6f569`
- 总数：50；已标注：50；unlabeled：0

## 判定口径（严格引文级）

- **supported**：quote 本身直接支持 claim 的主要事实，数字/日期/条件/范围一致。
- **partial**：quote 支持主要事实，但 claim 多了关键条件/范围/第二个断言，或措辞强弱不一致。
- **unsupported**：quote 与 claim 的主要事实不符、矛盾，或只是主题相关。
- 不看条款上下文，不看问题，不补外部知识；只判断 quote 文本。

## 待核对条目

## 01. d001 / c1

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？
- claim：本科生申请国家奖学金应同时符合第八条规定的基本申请条件（一）至（五）。
- quote（located）：

  > 其中，申请国家奖学金应同时符合条件（一）至（五），申请国家励志奖学金应同时符合条件（一）至（四）和（六）（七），申请国家助学金应同时符合条件（一）至（四）和（七）（八）。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：“申请国家奖学金应同时符合条件（一）至（五）”一致；“本科生/第八条基本申请条件”为识别与定位信息

## 02. d001 / c2

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？
- claim：申请国家奖学金的本科生须为二年级及以上年级。
- quote（located）：

  > 申请国家奖学金的学生，除具备第八条规定的基本条件外，须同时具备下列条件：
（一）年级要求：二年级及以上年级。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：年级要求二年级及以上一致；“本科生”为适用对象识别

## 03. d001 / c3

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？
- claim：申请国家奖学金的本科生学习成绩排名与综合测评成绩排名均须位于前10%（含10%）。
- quote（located）：

  > 申请国家奖学金的学生，除具备第八条规定的基本条件外，须同时具备下列条件：
（一）年级要求：二年级及以上年级。
（二）成绩要求：学习成绩排名与综合测评成绩排名均位于前10%（含10%）。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：成绩排名前10%（含）一致；“本科生”为适用对象识别

## 04. d001 / c4

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？
- claim：学习成绩排名和综合考评成绩排名没有进入前10%但达到前30%（含30%）的，须在其他方面表现非常突出，并在申请时提交经学校审核盖章确认的详细证明材料。
- quote（located）：

  > 学习成绩排名和综合考评成绩排名没有进入前10%，但达到前30%（含30%）的，须在其他方面表现非常突出，申请时须提交详细的证明材料，证明材料须经学校审核盖章确认。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：前30%未进前10%的突出表现与证明材料要求实质一致（claim 为同义改写）

## 05. d001 / c5

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？
- claim：研究生申请国家奖学金的，应同时符合基本申请条件（一）至（五）。
- quote（located）：

  > 其中，申请国家奖学金的，应同时符合条件（一）至（五）；申请学业奖学金的，应同时符合条件（一）至（四）和（六）；申请国家助学金的，应同时符合条件（一）至（四）和（七）。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：“申请国家奖学金的，应同时符合条件（一）至（五）”一致；“研究生”为适用对象识别

## 06. d001 / c6

- 状态：dropped
- 问题：申请国家奖学金需要满足哪些条件？
- claim：申请国家奖学金的研究生须为纳入全国研究生招生计划的全日制研究生。
- quote（located）：

  > 研究生国家奖学金申请条件：申请国家奖学金的学生，除具备第六条规定的基本条件外，须同时具备下列条件：（一）纳入全国研究生招生计划的全日制研究生；

- issues：TOO_MANY_CLAIMS
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：（一）纳入全国研究生招生计划的全日制研究生一致；issues 的 TOO_MANY_CLAIMS 属生成侧问题，按引文级口径判 supported

## 07. d002 / c1

- 状态：kept
- 问题：国家奖学金的奖励标准是多少？
- claim：研究生国家奖学金的奖励标准为博士研究生每生每年30000元，硕士研究生每生每年20000元。
- quote（located）：

  > （一）国家奖学金的奖励标准为博士研究生每生每年30000元，硕士研究生每生每年20000元。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：博士30000元、硕士20000元/生/年一致；“研究生”为奖励层次识别

## 08. d002 / c2

- 状态：kept
- 问题：国家奖学金的奖励标准是多少？
- claim：国家奖学金的奖励标准为每人每年8000元。
- quote（located）：

  > 国家奖学金的奖励标准为每人每年8000元

- issues：—
- fact_uncovered：—
- 当前标签：supported
- prefill 建议：supported（来自 baseline，待确认）
- prefill note：每人每年8000元一致
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：每人每年8000元一致（与 prefill 一致）

## 09. d002 / c3

- 状态：kept
- 问题：国家奖学金的奖励标准是多少？
- claim：国家奖学金的奖励标准：本科生8000元/生/年。
- quote（located）：

  > 奖励标准：本科生8000元/生/年，硕士研究生20000元/生/年，博士研究生30000元/生/年。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：本科生8000元/生/年一致；claim 为引文子集

## 10. d003 / c1

- 状态：kept
- 问题：本科生每学期选课学分的下限和上限分别是多少？
- claim：本科学生每学期所选课程（含重修）总学分一般最低不得少于10学分。
- quote（located）：

  > 本科学生每学期所选课程（含重修）总学分一般最低不得少于10学分

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：下限10学分（含“一般”）一致

## 11. d003 / c2

- 状态：kept
- 问题：本科生每学期选课学分的下限和上限分别是多少？
- claim：本科学生每学期所选课程（含重修）总学分上限为28学分。
- quote（located）：

  > 本科学生每学期所选课程（含重修）总学分一般最低不得少于10学分，上限为28学分

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：上限28学分一致

## 12. d003 / c3

- 状态：kept
- 问题：本科生每学期选课学分的下限和上限分别是多少？
- claim：修读辅修专业的学生，每学期修读总学分上限为40学分。
- quote（located）：

  > 修读辅修专业的学生，每学期修读总学分上限为40学分

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：辅修专业每学期上限40学分一致

## 13. d004 / c1

- 状态：kept
- 问题：学生进入教室对着装有什么要求？
- claim：学生应着装整洁、得体、大方。
- quote（located）：

  > 学生应着装整洁、得体、大方，不得穿拖鞋、背心、睡衣、超短裙、吊带衫、低胸服装等进入教室。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：“应着装整洁、得体、大方”与引文首句一致

## 14. d004 / c2

- 状态：kept
- 问题：学生进入教室对着装有什么要求？
- claim：学生不得穿拖鞋、背心、睡衣、超短裙、吊带衫、低胸服装等进入教室。
- quote（located）：

  > 不得穿拖鞋、背心、睡衣、超短裙、吊带衫、低胸服装等进入教室。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：禁穿清单及“进入教室”一致

## 15. d005 / c1

- 状态：kept
- 问题：哪些情形会被给予开除学籍处分？
- claim：学生触犯国家法律，构成刑事犯罪的，给予开除学籍处分。
- quote（located）：

  > 学生有下列情形之一的，给予开除学籍处分：
（一）违反宪法，反对四项基本原则、破坏安定团结、扰乱社会秩序的；
（二）触犯国家法律，构成刑事犯罪的；

- issues：—
- fact_uncovered：—
- 当前标签：supported
- prefill 建议：supported（来自 baseline，待确认）
- prefill note：引文含处分总起句及（二）触犯国家法律情形，一致
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：引文含处分总起句及（二）触犯国家法律情形（与 prefill 一致）

## 16. d005 / c2

- 状态：kept
- 问题：哪些情形会被给予开除学籍处分？
- claim：学生代替他人或者让他人代替自己参加考试、组织作弊、使用通讯设备或其他器材作弊、向他人出售考试试题或答案牟取利益，以及其他严重作弊或扰乱考试秩序行为的，给予开除学籍处分。
- quote（located）：

  > 学生有下列情形之一的，给予开除学籍处分：
（一）违反宪法，反对四项基本原则、破坏安定团结、扰乱社会秩序的；
（二）触犯国家法律，构成刑事犯罪的；
（三）受到治安管理处罚，情节严重、性质恶劣的；
（四）代替他人或者让他人代替自己参加考试、组织作弊、使用通讯设备或其他器材作弊、向他人出售考试试题或答案牟取利益，以及其他严重作弊或扰乱考试秩序行为的；

- issues：—
- fact_uncovered：—
- 当前标签：supported
- prefill 建议：supported（来自 baseline，待确认）
- prefill note：引文含处分总起句及（四）作弊情形，一致
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：引文含处分总起句及（四）作弊情形（与 prefill 一致）

## 17. d005 / c3

- 状态：kept
- 问题：哪些情形会被给予开除学籍处分？
- claim：学生学位论文、公开发表的研究成果存在抄袭、篡改、伪造等学术不端行为，情节严重的，或者代写论文、买卖论文的，给予开除学籍处分。
- quote（located）：

  > 学生有下列情形之一的，给予开除学籍处分：
（一）违反宪法，反对四项基本原则、破坏安定团结、扰乱社会秩序的；
（二）触犯国家法律，构成刑事犯罪的；
（三）受到治安管理处罚，情节严重、性质恶劣的；
（四）代替他人或者让他人代替自己参加考试、组织作弊、使用通讯设备或其他器材作弊、向他人出售考试试题或答案牟取利益，以及其他严重作弊或扰乱考试秩序行为的；
（五）学位论文、公开发表的研究成果存在抄袭、篡改、伪造等学术不端行为，情节严重的，或者代写论文、买卖论文的；

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：引文含处分总起句及（五）学术不端情形，一致

## 18. d005 / c4

- 状态：kept
- 问题：哪些情形会被给予开除学籍处分？
- claim：殴打他人，造成伤害的，给予记过以上处分，直至开除学籍。
- quote（located）：

  > 寻衅滋事、打架斗殴、打击报复他人的，除应依法承担相关法律责任、赔偿经济损失并承担受害者的医疗及其它必要费用外，视其情节，分别给予以下处分：
（一）骚扰、恐吓、威胁他人的，给予警告以上处分；
（二）侮辱、诽谤、诬告他人的，给予警告以上处分；
（三）策划、怂恿、挑唆他人打架斗殴的，给予严重警告以上处分；
（四）殴打他人，未造成伤害的，给予严重警告以上处分；造成伤害的，给予记过以上处分，直至开除学籍；

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：（四）“造成伤害的，给予记过以上处分，直至开除学籍”一致；claim 为引文子集

## 19. d006 / c1

- 状态：kept
- 问题：本科学生一学期累计旷课多少学时会被给予纪律处分？
- claim：凡未经请假，或请假未获批准擅自离校，或请假逾期未归者，均以旷课论处。
- quote（located）：

  > 凡未经请假，或请假未获批准擅自离校，或请假逾期未归者，均以旷课论处

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：旷课论处三种情形一致

## 20. d006 / c2

- 状态：kept
- 问题：本科学生一学期累计旷课多少学时会被给予纪律处分？
- claim：在一学期内，本科学生累计旷课10—19学时的，给予警告处分。
- quote（located）：

  > （二）在一学期内，本科学生累计旷课9学时及以下的，由学院通报批评；累计旷课10—19学时的，给予警告处分

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：10—19学时→警告；“在一学期内，本科学生”由首句统摄

## 21. d006 / c3

- 状态：kept
- 问题：本科学生一学期累计旷课多少学时会被给予纪律处分？
- claim：在一学期内，本科学生累计旷课20—29学时的，给予严重警告处分。
- quote（located）：

  > （二）在一学期内，本科学生累计旷课9学时及以下的，由学院通报批评；累计旷课10—19学时的，给予警告处分；累计旷课20—29学时的，给予严重警告处分

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：20—29学时→严重警告；范围由首句统摄

## 22. d006 / c4

- 状态：kept
- 问题：本科学生一学期累计旷课多少学时会被给予纪律处分？
- claim：在一学期内，本科学生累计旷课30-39学时的，给予记过处分。
- quote（located）：

  > （二）在一学期内，本科学生累计旷课9学时及以下的，由学院通报批评；累计旷课10—19学时的，给予警告处分；累计旷课20—29学时的，给予严重警告处分；累计旷课30-39学时的，给予记过处分

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：30-39学时→记过；范围由首句统摄

## 23. d006 / c5

- 状态：kept
- 问题：本科学生一学期累计旷课多少学时会被给予纪律处分？
- claim：在一学期内，本科学生累计旷课40-49学时以上的，给予留校察看以上处分。
- quote（located）：

  > （二）在一学期内，本科学生累计旷课9学时及以下的，由学院通报批评；累计旷课10—19学时的，给予警告处分；累计旷课20—29学时的，给予严重警告处分；累计旷课30-39学时的，给予记过处分；累计旷课40-49学时以上的，给予留校察看以上处分

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：40-49学时以上→留校察看以上；范围与措辞均一致

## 24. d007 / c1

- 状态：kept
- 问题：微专业一般由多少门课程组成，总学分和培养周期是多少？
- claim：微专业课程少，一般由5～6 门课程组成。
- quote（located）：

  > 是指课程少，一般由5～6 门课程组成，总学分10-12 学分，培养周期为3-4 个学期

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：课程少、一般5～6门一致

## 25. d007 / c2

- 状态：kept
- 问题：微专业一般由多少门课程组成，总学分和培养周期是多少？
- claim：微专业总学分10-12 学分。
- quote（located）：

  > 一般由5～6 门课程组成，总学分10-12 学分，培养周期为3-4 个学期

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：总学分10-12学分一致

## 26. d007 / c3

- 状态：kept
- 问题：微专业一般由多少门课程组成，总学分和培养周期是多少？
- claim：微专业培养周期为3-4 个学期。
- quote（located）：

  > 一般由5～6 门课程组成，总学分10-12 学分，培养周期为3-4 个学期

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：培养周期3-4个学期一致

## 27. d008 / c1

- 状态：kept
- 问题：申请国家励志奖学金需要满足哪些条件？
- claim：申请国家励志奖学金的学生，除具备第八条规定的基本条件外，须同时具备年级要求：二年级及以上年级。
- quote（located）：

  > 国家励志奖学金申请条件申请国家励志奖学金的学生，除具备第八条规定的基本条件外，须同时具备下列条件：
（一）年级要求：二年级及以上年级。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：第八条基本条件外加年级要求二年级及以上，一致

## 28. d008 / c2

- 状态：kept
- 问题：申请国家励志奖学金需要满足哪些条件？
- claim：申请国家励志奖学金的学生，须评审学年度考试、考查科目无不及格。
- quote（located）：

  > 申请国家励志奖学金的学生，除具备第八条规定的基本条件外，须同时具备下列条件：
（一）年级要求：二年级及以上年级。
（二）评审学年度考试、考查科目无不及格。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：评审学年度考试、考查科目无不及格一致

## 29. d008 / c3

- 状态：kept
- 问题：申请国家励志奖学金需要满足哪些条件？
- claim：申请国家励志奖学金应同时符合条件（一）至（四）和（六）（七）。
- quote（located）：

  > 其中，申请国家奖学金应同时符合条件（一）至（五），申请国家励志奖学金应同时符合条件（一）至（四）和（六）（七），申请国家助学金应同时符合条件（一）至（四）和（七）（八）。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- prefill 建议：supported（来自 baseline，待确认）
- prefill note：励志奖学金条件（一）至（四）和（六）（七）一致
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：条件（一）至（四）和（六）（七）一致（与 prefill 一致）

## 30. d008 / c4

- 状态：kept
- 问题：申请国家励志奖学金需要满足哪些条件？
- claim：国家励志奖学金备选名单须来源于当年8月30日系统家庭经济困难数据库。
- quote（located）：

  > 国家励志奖学金备选名单须来源于当年8月30日系统家庭经济困难数据库

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：备选名单来源于当年8月30日家庭经济困难数据库，一致

## 31. d009 / c1

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？奖励标准是多少？
- claim：申请国家奖学金的学生，除具备第八条规定的基本条件外，须同时具备年级要求，即二年级及以上年级。
- quote（located）：

  > 国家奖学金申请条件：申请国家奖学金的学生，除具备第八条规定的基本条件外，须同时具备下列条件：
（一）年级要求：二年级及以上年级。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：第八条基本条件外加年级要求二年级及以上，一致

## 32. d009 / c2

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？奖励标准是多少？
- claim：国家奖学金的成绩要求是学习成绩排名与综合测评成绩排名均位于前10%（含10%）。
- quote（located）：

  > （二）成绩要求：学习成绩排名与综合测评成绩排名均位于前10%（含10%）。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：成绩要求前10%（含）一致

## 33. d009 / c3

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？奖励标准是多少？
- claim：国家奖学金的奖励标准为每人每年8000元。
- quote（located）：

  > 国家奖学金的奖励标准为每人每年8000元

- issues：—
- fact_uncovered：—
- 当前标签：supported
- prefill 建议：supported（来自 baseline，待确认）
- prefill note：每人每年8000元一致
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：每人每年8000元一致（与 prefill 一致）

## 34. d009 / c4

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？奖励标准是多少？
- claim：研究生申请国家奖学金的，应同时符合条件（一）至（五）。
- quote（located）：

  > 其中，申请国家奖学金的，应同时符合条件（一）至（五）

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：“申请国家奖学金的，应同时符合条件（一）至（五）”一致；“研究生”为适用对象识别

## 35. d009 / c5

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？奖励标准是多少？
- claim：二年级以上（含二年级）学术型研究生原则上应发表高水平学术论文或者有其他突出科研成果。
- quote（located）：

  > （三）二年级以上（含二年级）学术型研究生原则上应发表高水平学术论文或者有其他突出科研成果

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：二年级以上学术型研究生论文/科研成果要求一致

## 36. d010 / c1

- 状态：kept
- 问题：学生违纪处分有哪些种类？处分期限一般多久？
- claim：纪律处分的种类包括：（一）警告；（二）严重警告；（三）记过；（四）留校察看；（五）开除学籍。
- quote（located）：

  > 纪律处分的种类：
（一）警告；
（二）严重警告；
（三）记过；
（四）留校察看；
（五）开除学籍。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：五种纪律处分种类一致

## 37. d010 / c2

- 状态：kept
- 问题：学生违纪处分有哪些种类？处分期限一般多久？
- claim：除开除学籍处分以外，给予学生处分期限一般为6至12个月。
- quote（located）：

  > 除开除学籍处分以外，给予学生处分期限一般为6至12个月

- issues：—
- fact_uncovered：—
- 当前标签：supported
- prefill 建议：supported（来自 baseline，待确认）
- prefill note：处分期限一般为6至12个月一致（与 prefill 一致）
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：处分期限一般为6至12个月一致（与 prefill 一致）

## 38. d010 / c3

- 状态：kept
- 问题：学生违纪处分有哪些种类？处分期限一般多久？
- claim：警告处分期限为6个月。
- quote（located）：

  > （一）警告处分期限为6个月

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：警告处分期限6个月一致

## 39. d010 / c4

- 状态：kept
- 问题：学生违纪处分有哪些种类？处分期限一般多久？
- claim：留校察看处分期限为12个月。
- quote（located）：

  > （四）留校察看处分期限为12个月

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：留校察看处分期限12个月一致

## 40. d010 / c5

- 状态：kept
- 问题：学生违纪处分有哪些种类？处分期限一般多久？
- claim：除开除学籍处分以外，给予学生处分一般设置6到12个月期限，到期按照《华南农业大学学生违纪处分实施办法》相关规定程序予以解除。
- quote（located）：

  > 除开除学籍处分以外，给予学生处分一般设置6到12个月期限，到期按照《华南农业大学学生违纪处分实施办法》相关规定程序予以解除。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- prefill 建议：supported（来自 baseline，待确认）
- prefill note：6到12个月期限及到期按实施办法解除一致
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：6到12个月期限及到期按实施办法解除一致（与 prefill 一致）

## 41. d011 / c1

- 状态：kept
- 问题：本科生申请转专业需要满足哪些基本条件？哪些情形不允许转专业？
- claim：申请转专业的学生必须为我校在籍普通全日制本科生，且入学已满一学期。
- quote（located）：

  > 申请转专业的学生必须为我校在籍普通全日制本科生，且符合以下基本条件：
（一）入学已满一学期；

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：在籍普通全日制本科生及入学已满一学期一致

## 42. d011 / c2

- 状态：kept
- 问题：本科生申请转专业需要满足哪些基本条件？哪些情形不允许转专业？
- claim：申请转专业的学生须政治思想表现优良，品行端正，身体状况符合转入专业的要求。
- quote（located）：

  > 申请转专业的学生必须为我校在籍普通全日制本科生，且符合以下基本条件：
（一）入学已满一学期；
（二）政治思想表现优良，品行端正，身体状况符合转入专业的要求；

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：政治思想表现优良、品行端正、身体状况要求一致

## 43. d011 / c3

- 状态：kept
- 问题：本科生申请转专业需要满足哪些基本条件？哪些情形不允许转专业？
- claim：申请转专业的学生在校期间未转过专业（因大类招生分流不计入转专业次数）。
- quote（located）：

  > 申请转专业的学生必须为我校在籍普通全日制本科生，且符合以下基本条件：
（一）入学已满一学期；
（二）政治思想表现优良，品行端正，身体状况符合转入专业的要求；
（三）在校期间未受过警告及以上处分；
（四）在校期间未转过专业（因大类招生分流不计入转专业次数）。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：未转过专业及大类招生分流例外一致

## 44. d011 / c4

- 状态：kept
- 问题：本科生申请转专业需要满足哪些基本条件？哪些情形不允许转专业？
- claim：招生时已有明确规定不能转专业的学生，含定向生、国防生、艺术类、体育类等专业学生，不允许转专业。
- quote（located）：

  > 下列情形之一不允许转专业：
（一）招生时已有明确规定不能转专业的学生，含定向生、国防生、艺术类、体育类等专业学生；

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：不允许转专业情形（一）一致

## 45. d011 / c5

- 状态：kept
- 问题：本科生申请转专业需要满足哪些基本条件？哪些情形不允许转专业？
- claim：实行学年制的毕业班学生，以及实行学分制且其学分已修满教学计划规定总学分的三分之二及以上的学生，不允许转专业。
- quote（located）：

  > 下列情形之一不允许转专业：
（一）招生时已有明确规定不能转专业的学生，含定向生、国防生、艺术类、体育类等专业学生；
（二）以特殊招生形式录取的学生，国家有相关规定或录取前与学校有明确约定的（艺术类专业录取的学生，入学后不得转入非艺术类专业）；
（三）招生时有特殊要求的提前批次录取学生；
（四）特殊录取类型学生申请转到统招专业的，如第二学士学位等；
（五）实行学年制的毕业班学生，以及实行学分制且其学分已修满教学计划规定总学分的三分之二及以上的学生；

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：不允许转专业情形（五）一致

## 46. d013 / c1

- 状态：kept
- 问题：数学与应用数学专业 2024 版培养方案的培养目标是什么？
- claim：本专业旨在培养具有高度社会责任感和良好的道德、科学与文化素养，具备良好的数学基础和思维能力，掌握数学学科基本理论与技能、掌握科学思维方法，并通过初步的科学研究训练，能运用数学理论和计算机技术解决实际问题的拔尖创新型人才。
- quote（located）：

  > 本专业旨在培养具有高度社会责任感和良好的道德、科学与文化素养，具备良好的数学基础和思维能力，掌握数学学科基本理论与技能、掌握科学思维方法，并通过初步的科学研究训练，能运用数学理论和计算机技术解决实际问题的拔尖创新型人才。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- prefill 建议：supported（来自 baseline，待确认）
- prefill note：培养目标全文一致
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：培养目标全文一致（与 prefill 一致）

## 47. d013 / c2

- 状态：kept
- 问题：数学与应用数学专业 2024 版培养方案的培养目标是什么？
- claim：毕业生可在政府、科技、教育、经济、管理、金融等部门从事研究、教学、应用开发和管理等工作。
- quote（located）：

  > 毕业生可在政府、科技、教育、经济、管理、金融等部门从事研究、教学、应用开发和管理等工作

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：就业部门与工作类型一致

## 48. d013 / c3

- 状态：kept
- 问题：数学与应用数学专业 2024 版培养方案的培养目标是什么？
- claim：毕业生可在国内外知名大学、科研机构继续攻读本专业或相关高新技术学科、交叉学科专业的研究生学位。
- quote（located）：

  > 或在国内外知名大学、科研机构继续攻读本专业或相关高新技术学科、交叉学科专业的研究生学位

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：升学去向一致

## 49. d014 / c1

- 状态：kept
- 问题：数学与应用数学专业 2024 版培养方案的专业核心课程有哪些？
- claim：专业核心课程为概率论，数理统计，实变函数，数值分析，数学建模，运筹学，近世代数，泛函分析。
- quote（located）：

  > 概率论，数理统计，实变函数，数值分析，数学建模，运筹学，近世代数，泛函分析。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：八门专业核心课程列表一致

## 50. d014 / c2

- 状态：kept
- 问题：数学与应用数学专业 2024 版培养方案的专业核心课程有哪些？
- claim：专业核心课程中的数学建模课程代码为610638，学分为2。
- quote（located）：

  > 专业核心课程 数学建模 数学与信息学
610638 2 32 32 0 0 4

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：课程表行含“数学建模”及 610638、2，与 claim 的课程代码与学分一致
