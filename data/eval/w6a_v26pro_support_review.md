# W6-A support 标注核对表

- run_sha256：`0cb8656e3b5d1457f45ff96910a1953330e1a4d626912343f089682bbc6b7537`
- 总数：53；已标注：53；unlabeled：0

## 判定口径（严格引文级）

- **supported**：quote 本身直接支持 claim 的主要事实，数字/日期/条件/范围一致。
- **partial**：quote 支持主要事实，但 claim 多了关键条件/范围/第二个断言，或措辞强弱不一致。
- **unsupported**：quote 与 claim 的主要事实不符、矛盾，或只是主题相关。
- 不看条款上下文，不看问题，不补外部知识；只判断 quote 文本。

## 待核对条目

## 01. d001 / c1

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？
- claim：本科生申请国家奖学金，应同时符合基本申请条件（一）至（五）。
- quote（located）：

  > 基本申请条件：
（一）具有中华人民共和国国籍；
（二）热爱社会主义祖国，拥护中国共产党的领导；
（三）遵守宪法和法律，遵守学校规章制度；
（四）诚实守信，道德品质优良；
（五）在校期间学习成绩优异，社会实践、创新能力、综合素质等方面特别突出；

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：“基本申请条件”及（一）至（五）条目正文一致；“本科生/申请国家奖学金”为适用对象识别

## 02. d001 / c2

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？
- claim：申请国家奖学金的学生，除具备第八条规定的基本条件外，须同时具备的条件之一是年级要求为二年级及以上年级。
- quote（located）：

  > 国家奖学金申请条件：申请国家奖学金的学生，除具备第八条规定的基本条件外，须同时具备下列条件：
（一）年级要求：二年级及以上年级。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：第八条基本条件外加（一）年级要求二年级及以上，一致

## 03. d001 / c3

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？
- claim：申请国家奖学金须具备的成绩要求是学习成绩排名与综合测评成绩排名均位于前10%（含10%）。
- quote（located）：

  > （二）成绩要求：学习成绩排名与综合测评成绩排名均位于前10%（含10%）。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：成绩要求前10%（含）一致

## 04. d001 / c4

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？
- claim：学习成绩排名和综合考评成绩排名没有进入前10%但达到前30%（含30%）的，须在其他方面表现非常突出，申请时须提交详细的证明材料，证明材料须经学校审核盖章确认。
- quote（located）：

  > 学习成绩排名和综合考评成绩排名没有进入前10%，但达到前30%（含30%）的，须在其他方面表现非常突出，申请时须提交详细的证明材料，证明材料须经学校审核盖章确认。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：前30%未进前10%的突出表现与证明材料要求逐句一致

## 05. d001 / c5

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？
- claim：研究生申请国家奖学金，应同时符合研究生国家奖助学金申请基本条件（一）至（五）。
- quote（located）：

  > 研究生国家奖助学金申请基本条件：
（一）具有中华人民共和国国籍；
（二）热爱社会主义祖国，拥护中国共产党的领导；
（三）遵守宪法和法律，遵守研究生培养单位规章制度；
（四）诚实守信，道德品质优良；
（五）学习成绩优异，科研能力显著，发展潜力突出；

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：“研究生国家奖助学金申请基本条件”及（一）至（五）条目正文一致

## 06. d002 / c1

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

## 07. d002 / c2

- 状态：kept
- 问题：国家奖学金的奖励标准是多少？
- claim：国家奖学金用于奖励全日制在校生中特别优秀的学生，奖励标准为本科生8000元/生/年，硕士研究生20000元/生/年，博士研究生30000元/生/年。
- quote（located）：

  > 国家奖学金。用于奖励全日制在校生中特别优秀的学生，奖励标准：本科生8000元/生/年，硕士研究生20000元/生/年，博士研究生30000元/生/年。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- prefill 建议：supported（来自 baseline，待确认）
- prefill note：奖励对象与本硕博8000/20000/30000标准一致
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：奖励对象与本硕博8000/20000/30000标准一致（与 prefill 一致）

## 08. d002 / c3

- 状态：kept
- 问题：国家奖学金的奖励标准是多少？
- claim：国家奖学金的奖励标准为博士研究生每生每年30000元，硕士研究生每生每年20000元。
- quote（located）：

  > （一）国家奖学金的奖励标准为博士研究生每生每年30000元，硕士研究生每生每年20000元。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：博士30000元、硕士20000元/生/年一致

## 09. d003 / c1

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

## 10. d003 / c2

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

## 11. d003 / c3

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

## 12. d004 / c1

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

## 13. d004 / c2

- 状态：kept
- 问题：学生进入教室对着装有什么要求？
- claim：学生不得穿拖鞋、背心、睡衣、超短裙、吊带衫、低胸服装等进入教室。
- quote（located）：

  > 不得穿拖鞋、背心、睡衣、超短裙、吊带衫、低胸服装等进入教室

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：禁穿清单及“进入教室”一致

## 14. d005 / c1

- 状态：kept
- 问题：哪些情形会被给予开除学籍处分？
- claim：学生违反宪法，反对四项基本原则、破坏安定团结、扰乱社会秩序的，给予开除学籍处分。
- quote（located）：

  > 学生有下列情形之一的，给予开除学籍处分：
（一）违反宪法，反对四项基本原则、破坏安定团结、扰乱社会秩序的；

- issues：—
- fact_uncovered：—
- 当前标签：supported
- prefill 建议：supported（来自 baseline，待确认）
- prefill note：引文含“给予开除学籍处分”总起句及（一）情形，条件与后果均一致
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：引文含“给予开除学籍处分”总起句及（一）情形（与 prefill 一致）

## 15. d005 / c2

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

## 16. d005 / c3

- 状态：kept
- 问题：哪些情形会被给予开除学籍处分？
- claim：学生受到治安管理处罚，情节严重、性质恶劣的，给予开除学籍处分。
- quote（located）：

  > 学生有下列情形之一的，给予开除学籍处分：
（一）违反宪法，反对四项基本原则、破坏安定团结、扰乱社会秩序的；
（二）触犯国家法律，构成刑事犯罪的；
（三）受到治安管理处罚，情节严重、性质恶劣的；

- issues：—
- fact_uncovered：—
- 当前标签：supported
- prefill 建议：supported（来自 baseline，待确认）
- prefill note：引文含处分总起句及（三）治安处罚情形，一致
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：引文含处分总起句及（三）治安处罚情形（与 prefill 一致）

## 17. d005 / c4

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

## 18. d005 / c5

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

## 19. d006 / c1

- 状态：kept
- 问题：本科学生一学期累计旷课多少学时会被给予纪律处分？
- claim：在一学期内，本科学生累计旷课9学时及以下的，由学院通报批评。
- quote（located）：

  > （二）在一学期内，本科学生累计旷课9学时及以下的，由学院通报批评

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：9学时及以下→学院通报批评，范围与后果一致

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
- claim：微专业项目需符合“少、精、尖、跨”特点，“少”是指课程少，一般由5～6门课程组成。
- quote（located）：

  > 微专业项目需符合“少、精、尖、跨”特点。“少”
是指课程少，一般由5～6 门课程组成，总学分10-12 学分，培养周期为3-4 个学期

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：“少、精、尖、跨”及“少”指5～6门课程一致；claim 为引文子集

## 25. d007 / c2

- 状态：kept
- 问题：微专业一般由多少门课程组成，总学分和培养周期是多少？
- claim：微专业一般由5～6门课程组成，总学分10-12学分。
- quote（located）：

  > 微专业项目需符合“少、精、尖、跨”特点。“少”
是指课程少，一般由5～6 门课程组成，总学分10-12 学分，培养周期为3-4 个学期

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：5～6门课程及总学分10-12一致

## 26. d007 / c3

- 状态：kept
- 问题：微专业一般由多少门课程组成，总学分和培养周期是多少？
- claim：微专业的培养周期为3-4个学期。
- quote（located）：

  > 微专业项目需符合“少、精、尖、跨”特点。“少”
是指课程少，一般由5～6 门课程组成，总学分10-12 学分，培养周期为3-4 个学期

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：培养周期3-4个学期一致

## 27. d008 / c1

- 状态：kept
- 问题：申请国家励志奖学金需要满足哪些条件？
- claim：申请国家励志奖学金的学生应同时符合基本申请条件（一）至（四）和（六）（七）。
- quote（located）：

  > 申请国家励志奖学金应同时符合条件（一）至（四）和（六）（七）

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：条件（一）至（四）和（六）（七）一致

## 28. d008 / c2

- 状态：kept
- 问题：申请国家励志奖学金需要满足哪些条件？
- claim：申请国家励志奖学金的学生，除具备第八条规定的基本条件外，年级要求为二年级及以上年级。
- quote（located）：

  > 申请国家励志奖学金的学生，除具备第八条规定的基本条件外，须同时具备下列条件：
（一）年级要求：二年级及以上年级。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：第八条基本条件外加年级要求二年级及以上，一致

## 29. d008 / c3

- 状态：kept
- 问题：申请国家励志奖学金需要满足哪些条件？
- claim：申请国家励志奖学金的学生，评审学年度考试、考查科目须无不及格。
- quote（located）：

  > 须同时具备下列条件：
（一）年级要求：二年级及以上年级。
（二）评审学年度考试、考查科目无不及格。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：条件（二）无不及格一致；适用对象为识别信息

## 30. d008 / c4

- 状态：kept
- 问题：申请国家励志奖学金需要满足哪些条件？
- claim：国家励志奖学金用于奖励资助全日制在校生中品学兼优的家庭经济困难学生。
- quote（located）：

  > 国家励志奖学金。用于奖励资助全日制在校生中品学兼优的家庭经济困难学生

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：奖励资助对象一致

## 31. d008 / c5

- 状态：kept
- 问题：申请国家励志奖学金需要满足哪些条件？
- claim：同一学年内，学生不能同时获得国家奖学金和国家励志奖学金。
- quote（located）：

  > 同一学年内，学生可以同时申请并获得国家助学金和国家奖学金或国家励志奖学金，但不能同时获得国家奖学金和国家励志奖学金。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：不能同时获得国家奖学金和国家励志奖学金一致；claim 为引文子集

## 32. d009 / c1

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？奖励标准是多少？
- claim：申请国家奖学金应同时符合基本申请条件（一）至（五）。
- quote（located）：

  > 基本申请条件：
（一）具有中华人民共和国国籍；
（二）热爱社会主义祖国，拥护中国共产党的领导；
（三）遵守宪法和法律，遵守学校规章制度；
（四）诚实守信，道德品质优良；
（五）在校期间学习成绩优异，社会实践、创新能力、综合素质等方面特别突出；
（六）评审学年度学习成绩和综合测评成绩不低于前50%；
（七）通过家庭经济困难认定的学生，生活简朴；
（八）勤奋学习，积极上进。
其中，申请国家奖学金应同时符合条件（一）至（五），申请国家励志奖学金应同时符合条件（一）至（四）和（六）（七），申请国家助学金应同时符合条件（一）至（四）和（七）（八）。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：引文含“其中，申请国家奖学金应同时符合条件（一）至（五）”明确对应

## 33. d009 / c2

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？奖励标准是多少？
- claim：申请国家奖学金的年级要求为二年级及以上年级。
- quote（located）：

  > 国家奖学金申请条件：申请国家奖学金的学生，除具备第八条规定的基本条件外，须同时具备下列条件：
（一）年级要求：二年级及以上年级。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：年级要求二年级及以上一致

## 34. d009 / c3

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？奖励标准是多少？
- claim：申请国家奖学金的成绩要求是学习成绩排名与综合测评成绩排名均位于前10%（含10%）。
- quote（located）：

  > （二）成绩要求：学习成绩排名与综合测评成绩排名均位于前10%（含10%）。学习成绩排名和综合考评成绩排名没有进入前10%，但达到前30%（含30%）的，须在其他方面表现非常突出，申请时须提交详细的证明材料，证明材料须经学校审核盖章确认。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：成绩要求前10%（含）一致；claim 为引文子集

## 35. d009 / c4

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？奖励标准是多少？
- claim：国家奖学金的奖励标准为每人每年8000元。
- quote（located）：

  > 国家奖学金的奖励标准为每人每年8000元;国家励志奖学金的奖励标准为每人每年5000元;

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：每人每年8000元一致

## 36. d009 / c5

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？奖励标准是多少？
- claim：国家奖学金的奖励标准为博士研究生每生每年30000元，硕士研究生每生每年20000元。
- quote（located）：

  > （一）国家奖学金的奖励标准为博士研究生每生每年30000元，硕士研究生每生每年20000元。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：博士30000元、硕士20000元/生/年一致

## 37. d010 / c1

- 状态：kept
- 问题：学生违纪处分有哪些种类？处分期限一般多久？
- claim：纪律处分的种类包括：警告、严重警告、记过、留校察看、开除学籍。
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
- note：警告、严重警告、记过、留校察看、开除学籍五种一致

## 38. d010 / c2

- 状态：kept
- 问题：学生违纪处分有哪些种类？处分期限一般多久？
- claim：除开除学籍处分以外，给予学生处分期限一般为6至12个月。
- quote（located）：

  > 除开除学籍处分以外，给予学生处分期限一般为6至12个月，各类处分的期限：

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：处分期限一般为6至12个月一致

## 39. d010 / c3

- 状态：kept
- 问题：学生违纪处分有哪些种类？处分期限一般多久？
- claim：各类处分的期限为：警告处分期限为6个月，严重警告处分为8个月，记过处分期限为10个月，留校察看处分期限为12个月。
- quote（located）：

  > （一）警告处分期限为6个月；
（二）严重警告处分为8个月；
（三）记过处分期限为10个月；
（四）留校察看处分期限为12个月。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：警告6/严重警告8/记过10/留校察看12个月一致

## 40. d011 / c1

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

## 41. d011 / c2

- 状态：kept
- 问题：本科生申请转专业需要满足哪些基本条件？哪些情形不允许转专业？
- claim：申请转专业的学生须政治思想表现优良、品行端正，身体状况符合转入专业的要求。
- quote（located）：

  > （二）政治思想表现优良，品行端正，身体状况符合转入专业的要求；

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：政治思想表现优良、品行端正、身体状况要求一致

## 42. d011 / c3

- 状态：kept
- 问题：本科生申请转专业需要满足哪些基本条件？哪些情形不允许转专业？
- claim：申请转专业的学生在校期间须未受过警告及以上处分。
- quote（located）：

  > （三）在校期间未受过警告及以上处分；

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：未受过警告及以上处分一致

## 43. d011 / c4

- 状态：kept
- 问题：本科生申请转专业需要满足哪些基本条件？哪些情形不允许转专业？
- claim：申请转专业的学生在校期间须未转过专业，因大类招生分流不计入转专业次数。
- quote（located）：

  > （四）在校期间未转过专业（因大类招生分流不计入转专业次数）。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：未转过专业及大类招生分流例外一致

## 44. d011 / c5

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

## 45. d012 / c1

- 状态：kept
- 问题：推免生需要满足哪些基本条件和学业要求？
- claim：推免生基本条件之一为：纳入国家普通本科招生计划录取、未曾计入历年应毕业本科生范围、未曾参与过推免环节的应届毕业生（不含第二学士学位、独立学院学生）。
- quote（located）：

  > 推免生应具备以下基本条件：
（一）纳入国家普通本科招生计划录取、未曾计入历年应毕业本科生范围、未曾参与过推免环节的应届毕业生（不含第二学士学位、独立学院学生）。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：推免生基本条件（一）一致

## 46. d012 / c2

- 状态：kept
- 问题：推免生需要满足哪些基本条件和学业要求？
- claim：思想品德考核作为推免生遴选的重要内容和录取的重要依据，思想品德考核不合格者不予推荐和录取。
- quote（located）：

  > （三）以德为先，将学生思想品德考核作为推免生遴选的重要内容和录取的重要依据。思想品德考核不合格者不予推荐和录取。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：思想品德考核作为遴选重要内容、不合格不予推荐录取一致；claim 为引文子集

## 47. d012 / c3

- 状态：kept
- 问题：推免生需要满足哪些基本条件和学业要求？
- claim：推免生申请时须同时符合的学业要求之一为：前三学年（五年制专业为前四学年）的学业平均学分绩点（GPA）不低于3.20，且学业平均学分绩点（GPA）排名位于同届同专业前50%（含50%）之内。
- quote（located）：

  > 推免生申请时须同时符合以下学业要求：
（一）前三学年（五年制专业为前四学年）的学业平均学分绩点（GPA）不低于3.20，且学业平均学分绩点（GPA）排名位于同届同专业前50%（含50%）之内。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：GPA不低于3.20且排名前50%一致

## 48. d012 / c4

- 状态：dropped
- 问题：推免生需要满足哪些基本条件和学业要求？
- claim：推免生申请时须同时符合的学业要求之一为：按照教学计划修完应修课程；无处于缓考状态课程；补考或重修后无不及格成绩。
- quote（located）：

  > 推免生申请时须同时符合以下学业要求：（二）按照教学计划修完应修课程；无处于缓考状态课程；补考或重修后无不及格成绩。

- issues：QUOTE_MISMATCH, NO_VALID_CITATION
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：学业要求（二）修完应修课程/无缓考/无不及格文本与 claim 一致；issues 标注 QUOTE_MISMATCH/NO_VALID_CITATION 属引文定位问题，按引文级口径判 supported

## 49. d012 / c5

- 状态：dropped
- 问题：推免生需要满足哪些基本条件和学业要求？
- claim：推免生申请时须同时符合的学业要求之一为：全国大学英语四级考试成绩不低于425 分，或雅思考试成绩不低于5.5 分，或托福考试成绩不低于80 分；英语类专业学生须通过英语专业四级考试，日语类专业学生须通过日语专业四级考试。
- quote（located）：

  > 推免生申请时须同时符合以下学业要求：（三）全国大学英语四级考试成绩不低于425 分，或雅思考试成绩不低于5.5 分，或托福考试成绩不低于80 分；英语类专业学生须通过英语专业四级考试，日语类专业学生须通过日语专业四级考试。

- issues：QUOTE_MISMATCH, NO_VALID_CITATION
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：学业要求（三）四级425/雅思5.5/托福80及专业四级文本与 claim 一致；issues 标注 QUOTE_MISMATCH/NO_VALID_CITATION 属引文定位问题，按引文级口径判 supported

## 50. d013 / c1

- 状态：kept
- 问题：数学与应用数学专业 2024 版培养方案的培养目标是什么？
- claim：数学与应用数学专业（2024版）培养目标是：本专业旨在培养具有高度社会责任感和良好的道德、科学与文化素养，具备良好的数学基础和思维能力，掌握数学学科基本理论与技能、掌握科学思维方法，并通过初步的科学研究训练，能运用数学理论和计算机技术解决实际问题的拔尖创新型人才。
- quote（located）：

  > 本专业旨在培养具有高度社会责任感和良好的道德、科学与文化素养，具备良好的数学基础和思维能力，掌握数学学科基本理论与技能、掌握科学思维方法，并通过初步的科学研究训练，能运用数学理论和计算机技术解决实际问题的拔尖创新型人才。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：培养目标全文一致；“2024版”为版本识别

## 51. d013 / c2

- 状态：kept
- 问题：数学与应用数学专业 2024 版培养方案的培养目标是什么？
- claim：该培养目标下，毕业生可在政府、科技、教育、经济、管理、金融等部门从事研究、教学、应用开发和管理等工作，或在国内外知名大学、科研机构继续攻读本专业或相关高新技术学科、交叉学科专业的研究生学位，成为相关领域的拔尖创新优秀人才。
- quote（located）：

  > 毕业生可在政府、科技、教育、经济、管理、金融等部门从事研究、教学、应用开发和管理等工作，或在国内外知名大学、科研机构继续攻读本专业或相关高新技术学科、交叉学科专业的研究生学位，成为相关领域的拔尖创新优秀人才。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：就业与升学去向全文一致

## 52. d014 / c1

- 状态：kept
- 问题：数学与应用数学专业 2024 版培养方案的专业核心课程有哪些？
- claim：2024 版数学与应用数学专业培养方案的专业核心课程为：概率论，数理统计，实变函数，数值分析，数学建模，运筹学，近世代数，泛函分析。
- quote（located）：

  > 概率论，数理统计，实变函数，数值分析，数学建模，运筹学，近世代数，泛函分析。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：八门专业核心课程列表一致；“2024版”为版本识别

## 53. d014 / c2

- 状态：kept
- 问题：数学与应用数学专业 2024 版培养方案的专业核心课程有哪些？
- claim：2022 版数学与应用数学专业培养方案的专业核心课程为：概率论，数理统计，实变函数，数值分析，数学建模，运筹学，离散数学，计算机科学绪论，近世代数，泛函分析，Matlab 程序设计与应用。
- quote（located）：

  > 概率论，数理统计，实变函数，数值分析，数学建模，运筹学，离散数学，计算机科学绪论，近世代数，泛函分析，Matlab 程序设计与应用。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：十一门专业核心课程列表与引文逐项一致；“2022版”为版本识别（非对照性表述）
