# W3 support 标注核对表

- run_sha256：`5e542b25fb248b7924f0eca56788e5924715999f1fbeb0ace7f2d79273624442`
- 总数：56；已标注：56；unlabeled：0

## 判定口径（严格引文级）

- **supported**：quote 本身直接支持 claim 的主要事实，数字/日期/条件/范围一致。
- **partial**：quote 支持主要事实，但 claim 多了关键条件/范围/第二个断言，或措辞强弱不一致。
- **unsupported**：quote 与 claim 的主要事实不符、矛盾，或只是主题相关。
- 不看条款上下文，不看问题，不补外部知识；只判断 quote 文本。

## 待核对条目

## 01. d001 / c1

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？
- claim：申请本科生国家奖学金须具备第八条基本条件中的一至五项，包括具有中华人民共和国国籍、热爱社会主义祖国拥护中国共产党的领导、遵守宪法和法律遵守学校规章制度、诚实守信道德品质优良、在校期间学习成绩优异且社会实践创新能力综合素质等方面特别突出。
- quote（located）：

  > 其中，申请国家奖学金应同时符合条件（一）至（五）

- issues：—
- fact_uncovered：—
- 当前标签：partial
- 判定：[ ] supported  [x] partial  [ ] unsupported
- note：quote 仅含“符合条件（一）至（五）”，claim 对条件具体内容（国籍、拥护领导等）的展开不在引文中

## 02. d001 / c2

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？
- claim：申请本科生国家奖学金的学生须为二年级及以上年级。
- quote（located）：

  > （一）年级要求：二年级及以上年级。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：年级要求“二年级及以上”一致

## 03. d001 / c3

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？
- claim：申请本科生国家奖学金要求学习成绩排名与综合测评成绩排名均位于前10%（含10%）。
- quote（located）：

  > （二）成绩要求：学习成绩排名与综合测评成绩排名均位于前10%（含10%）。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：成绩排名前10%（含）一致

## 04. d001 / c4

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？
- claim：学习成绩排名和综合考评成绩排名达到前30%（含30%）但未进入前10%的学生，须在其他方面表现非常突出，并在申请时提交经学校审核盖章确认的详细证明材料。
- quote（located）：

  > 学习成绩排名和综合考评成绩排名没有进入前10%，但达到前30%（含30%）的，须在其他方面表现非常突出，申请时须提交详细的证明材料，证明材料须经学校审核盖章确认。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：未进前10%但达前30%的附加要求及证明材料一致

## 05. d001 / c5

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？
- claim：申请研究生国家奖学金须同时具备第六条基本条件中的一至五项，并满足第七条规定的具体条件，如二年级以上（含二年级）学术型研究生原则上应发表高水平学术论文或有其他突出科研成果。
- quote（located）：

  > （三）二年级以上（含二年级）学术型研究生原则上应发表高水平学术论文或者有其他突出科研成果；二年级以上（含二年级）专业型研究生有应用性研究成果；
  > 其中，申请国家奖学金的，应同时符合条件（一）至（五）

- issues：—
- fact_uncovered：—
- 当前标签：partial
- 判定：[ ] supported  [x] partial  [ ] unsupported
- note：支持条件（一）至（五）及学术型研究生论文要求；claim 中“第六条/第七条”条文编号与“第七条规定的具体条件”概括不在引文中

## 06. d002 / c1

- 状态：kept
- 问题：国家奖学金的奖励标准是多少？
- claim：国家奖学金的奖励标准为本科生8000元/生/年。
- quote（located）：

  > 本科生8000元/生/年
  > 国家奖学金的奖励标准为每人每年8000元

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：本科生8000元/生/年一致

## 07. d002 / c2

- 状态：kept
- 问题：国家奖学金的奖励标准是多少？
- claim：国家奖学金的奖励标准为硕士研究生20000元/生/年。
- quote（located）：

  > 硕士研究生20000元/生/年
  > 国家奖学金的奖励标准为博士研究生每生每年30000元，硕士研究生每生每年20000元。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：硕士研究生20000元/生/年一致

## 08. d002 / c3

- 状态：kept
- 问题：国家奖学金的奖励标准是多少？
- claim：国家奖学金的奖励标准为博士研究生30000元/生/年。
- quote（located）：

  > 博士研究生30000元/生/年
  > 国家奖学金的奖励标准为博士研究生每生每年30000元，硕士研究生每生每年20000元。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：博士研究生30000元/生/年一致

## 09. d003 / c1

- 状态：kept
- 问题：本科生每学期选课学分的下限和上限分别是多少？
- claim：本科生每学期所选课程（含重修）总学分最低不得少于10学分。
- quote（located）：

  > 本科学生每学期所选课程（含重修）总学分一般最低不得少于10学分

- issues：—
- fact_uncovered：—
- 当前标签：partial
- 判定：[ ] supported  [x] partial  [ ] unsupported
- note：引文含“一般”，claim 改为绝对表述，措辞强弱不一致

## 10. d003 / c2

- 状态：kept
- 问题：本科生每学期选课学分的下限和上限分别是多少？
- claim：本科生每学期所选课程总学分上限为28学分。
- quote（located）：

  > 上限为28学分；修读辅修专业的学生，每学期修读总学分上限为40学分

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
- claim：学生进入教室应着装整洁、得体、大方。
- quote（located）：

  > 学生应着装整洁、得体、大方

- issues：—
- fact_uncovered：—
- 当前标签：partial
- 判定：[ ] supported  [x] partial  [ ] unsupported
- note：claim 增加“进入教室”范围限定，引文仅作一般着装要求

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
- claim：学生触犯国家法律、构成刑事犯罪的，给予开除学籍处分。
- quote（located）：

  > 触犯国家法律，构成刑事犯罪的

- issues：—
- fact_uncovered：—
- 当前标签：partial
- 判定：[ ] supported  [x] partial  [ ] unsupported
- note：引文仅含条件，未含“给予开除学籍处分”后果

## 15. d005 / c2

- 状态：kept
- 问题：哪些情形会被给予开除学籍处分？
- claim：代替他人或者让他人代替自己参加考试、组织作弊、使用通讯设备或其他器材作弊等严重作弊或扰乱考试秩序行为的，给予开除学籍处分。
- quote（located）：

  > 代替他人或者让他人代替自己参加考试、组织作弊、使用通讯设备或其他器材作弊、向他人出售考试试题或答案牟取利益，以及其他严重作弊或扰乱考试秩序行为的

- issues：—
- fact_uncovered：—
- 当前标签：partial
- 判定：[ ] supported  [x] partial  [ ] unsupported
- note：引文仅含作弊情形，未含“给予开除学籍处分”后果

## 16. d005 / c3

- 状态：kept
- 问题：哪些情形会被给予开除学籍处分？
- claim：学位论文、公开发表的研究成果存在抄袭、篡改、伪造等学术不端行为情节严重，或者代写论文、买卖论文的，给予开除学籍处分。
- quote（located）：

  > 学位论文、公开发表的研究成果存在抄袭、篡改、伪造等学术不端行为，情节严重的，或者代写论文、买卖论文的

- issues：—
- fact_uncovered：—
- 当前标签：partial
- 判定：[ ] supported  [x] partial  [ ] unsupported
- note：引文仅含学术不端情形，未含“给予开除学籍处分”后果

## 17. d005 / c4

- 状态：kept
- 问题：哪些情形会被给予开除学籍处分？
- claim：学生受到治安管理处罚且情节严重、性质恶劣的，给予开除学籍处分。
- quote（located）：

  > 受到治安管理处罚，情节严重、性质恶劣的

- issues：—
- fact_uncovered：—
- 当前标签：partial
- 判定：[ ] supported  [x] partial  [ ] unsupported
- note：引文仅含治安处罚情形，未含“给予开除学籍处分”后果

## 18. d005 / c5

- 状态：kept
- 问题：哪些情形会被给予开除学籍处分？
- claim：屡次违反学校规定受到纪律处分，经教育不改的，给予开除学籍处分。
- quote（located）：

  > 屡次违反学校规定受到纪律处分，经教育不改的

- issues：—
- fact_uncovered：—
- 当前标签：partial
- 判定：[ ] supported  [x] partial  [ ] unsupported
- note：引文仅含屡次违纪情形，未含“给予开除学籍处分”后果

## 19. d006 / c1

- 状态：kept
- 问题：本科学生一学期累计旷课多少学时会被给予纪律处分？
- claim：本科学生一学期内累计旷课9学时及以下的，由学院通报批评，不给予纪律处分。
- quote（located）：

  > 在一学期内，本科学生累计旷课9学时及以下的，由学院通报批评

- issues：—
- fact_uncovered：—
- 当前标签：partial
- 判定：[ ] supported  [x] partial  [ ] unsupported
- note：引文支持“由学院通报批评”，claim 增加“不给予纪律处分”不在引文中

## 20. d006 / c2

- 状态：dropped
- 问题：本科学生一学期累计旷课多少学时会被给予纪律处分？
- claim：本科学生一学期内累计旷课10—19学时的，给予警告处分。
- quote（located）：

  > 累计旷课10—19学时的，给予警告处分

- issues：NUMBER_NOT_SUPPORTED
- fact_uncovered：1学期
- 当前标签：partial
- 判定：[ ] supported  [x] partial  [ ] unsupported
- note：引文缺“一学期内/本科学生”范围；fact_uncovered:1学期

## 21. d006 / c3

- 状态：dropped
- 问题：本科学生一学期累计旷课多少学时会被给予纪律处分？
- claim：本科学生一学期内累计旷课20—29学时的，给予严重警告处分。
- quote（located）：

  > 累计旷课20—29学时的，给予严重警告处分

- issues：NUMBER_NOT_SUPPORTED
- fact_uncovered：1学期
- 当前标签：partial
- 判定：[ ] supported  [x] partial  [ ] unsupported
- note：引文缺“一学期内/本科学生”范围；fact_uncovered:1学期

## 22. d006 / c4

- 状态：dropped
- 问题：本科学生一学期累计旷课多少学时会被给予纪律处分？
- claim：本科学生一学期内累计旷课30—39学时的，给予记过处分。
- quote（located）：

  > 累计旷课30-39学时的，给予记过处分

- issues：NUMBER_NOT_SUPPORTED
- fact_uncovered：1学期
- 当前标签：partial
- 判定：[ ] supported  [x] partial  [ ] unsupported
- note：引文缺“一学期内/本科学生”范围；fact_uncovered:1学期

## 23. d006 / c5

- 状态：dropped
- 问题：本科学生一学期累计旷课多少学时会被给予纪律处分？
- claim：本科学生一学期内累计旷课40—49学时的，给予留校察看以上处分。
- quote（located）：

  > 累计旷课40-49学时以上的，给予留校察看以上处分

- issues：NUMBER_NOT_SUPPORTED
- fact_uncovered：1学期
- 当前标签：partial
- 判定：[ ] supported  [x] partial  [ ] unsupported
- note：引文缺“一学期内/本科学生”范围；引文“40-49学时以上”与 claim“40—49学时”措辞亦有出入

## 24. d007 / c1

- 状态：kept
- 问题：微专业一般由多少门课程组成，总学分和培养周期是多少？
- claim：微专业一般由5～6门课程组成
- quote（located）：

  > “少”
是指课程少，一般由5～6 门课程组成，总学分10-12 学分，培养周期为3-4 个学期

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：一般由5～6门课程组成一致

## 25. d007 / c2

- 状态：kept
- 问题：微专业一般由多少门课程组成，总学分和培养周期是多少？
- claim：微专业总学分为10-12学分
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
- claim：微专业培养周期为3-4个学期
- quote（located）：

  > 总学分10-12 学分，培养周期为3-4 个学期

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：培养周期3-4个学期一致

## 27. d008 / c1

- 状态：kept
- 问题：申请国家励志奖学金需要满足哪些条件？
- claim：申请国家励志奖学金的学生须同时符合第八条中的条件（一）至（四）和（六）（七）。
- quote（located）：

  > 申请国家励志奖学金应同时符合条件（一）至（四）和（六）（七）

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：条件（一）至（四）和（六）（七）一致；“第八条”为定位信息

## 28. d008 / c2

- 状态：kept
- 问题：申请国家励志奖学金需要满足哪些条件？
- claim：申请国家励志奖学金的年级要求为二年级及以上年级。
- quote（located）：

  > （一）年级要求：二年级及以上年级。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：二年级及以上年级一致

## 29. d008 / c3

- 状态：kept
- 问题：申请国家励志奖学金需要满足哪些条件？
- claim：申请国家励志奖学金的学生须在评审学年度考试、考查科目无不及格。
- quote（located）：

  > （二）评审学年度考试、考查科目无不及格。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：评审学年度考试考查科目无不及格一致

## 30. d008 / c4

- 状态：kept
- 问题：申请国家励志奖学金需要满足哪些条件？
- claim：申请国家励志奖学金须评审学年度学习成绩和综合测评成绩不低于前50%。
- quote（located）：

  > （六）评审学年度学习成绩和综合测评成绩不低于前50%

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：学习成绩和综合测评不低于前50%一致

## 31. d008 / c5

- 状态：kept
- 问题：申请国家励志奖学金需要满足哪些条件？
- claim：申请国家励志奖学金须为通过家庭经济困难认定的学生，且生活简朴。
- quote（located）：

  > （七）通过家庭经济困难认定的学生，生活简朴

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：通过家庭经济困难认定且生活简朴一致

## 32. d009 / c1

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？奖励标准是多少？
- claim：本科生申请国家奖学金须具备《华南农业大学本科生国家奖助学金实施办法》第八条基本条件中的（一）至（五）项，即具有中华人民共和国国籍、热爱社会主义祖国拥护中国共产党的领导、遵守宪法和法律遵守学校规章制度、诚实守信道德品质优良、在校期间学习成绩优异且社会实践创新能力综合素质等方面特别突出。
- quote（located）：

  > 其中，申请国家奖学金应同时符合条件（一）至（五）

- issues：—
- fact_uncovered：—
- 当前标签：partial
- 判定：[ ] supported  [x] partial  [ ] unsupported
- note：quote 仅含“符合条件（一）至（五）”，claim 对条件具体内容的展开不在引文中

## 33. d009 / c2

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？奖励标准是多少？
- claim：本科生申请国家奖学金的年级要求为二年级及以上年级。
- quote（located）：

  > （一）年级要求：二年级及以上年级。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：二年级及以上年级一致

## 34. d009 / c3

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？奖励标准是多少？
- claim：本科生申请国家奖学金要求学习成绩排名与综合测评成绩排名均位于前10%（含10%）；未进入前10%但达到前30%（含30%）的，须在其他方面表现非常突出并提交经学校审核盖章确认的详细证明材料。
- quote（located）：

  > （二）成绩要求：学习成绩排名与综合测评成绩排名均位于前10%（含10%）。学习成绩排名和综合考评成绩排名没有进入前10%，但达到前30%（含30%）的，须在其他方面表现非常突出，申请时须提交详细的证明材料，证明材料须经学校审核盖章确认。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：前10%与前30%完整规则及证明材料要求一致

## 35. d009 / c4

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？奖励标准是多少？
- claim：本科生国家奖学金的奖励标准为每人每年8000元。
- quote（located）：

  > 国家奖学金的奖励标准为每人每年8000元

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：每人每年8000元一致

## 36. d009 / c5

- 状态：kept
- 问题：申请国家奖学金需要满足哪些条件？奖励标准是多少？
- claim：研究生国家奖学金的奖励标准为博士研究生每生每年30000元，硕士研究生每生每年20000元。
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
- claim：学生纪律处分的种类包括警告、严重警告、记过、留校察看、开除学籍五种。
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

  > 除开除学籍处分以外，给予学生处分期限一般为6至12个月

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：处分期限一般为6至12个月一致

## 39. d010 / c3

- 状态：kept
- 问题：学生违纪处分有哪些种类？处分期限一般多久？
- claim：警告处分期限为6个月，严重警告处分为8个月，记过处分期限为10个月，留校察看处分期限为12个月。
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

## 40. d010 / c4

- 状态：kept
- 问题：学生违纪处分有哪些种类？处分期限一般多久？
- claim：处分到期后按照《华南农业大学学生违纪处分实施办法》相关规定程序予以解除，解除处分后学生获得表彰、奖励及其他权益不再受原处分影响。
- quote（located）：

  > 到期按照《华南农业大学学生违纪处分实施办法》相关规定程序予以解除。解除处分后，学生获得表彰、奖励及其他权益，不再受原处分的影响。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：到期解除程序及解除后权益不受影响一致

## 41. d011 / c1

- 状态：kept
- 问题：本科生申请转专业需要满足哪些基本条件？哪些情形不允许转专业？
- claim：申请转专业的学生必须为我校在籍普通全日制本科生。
- quote（located）：

  > 申请转专业的学生必须为我校在籍普通全日制本科生，且符合以下基本条件：

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：在籍普通全日制本科生一致

## 42. d011 / c2

- 状态：kept
- 问题：本科生申请转专业需要满足哪些基本条件？哪些情形不允许转专业？
- claim：申请转专业的学生须入学已满一学期。
- quote（located）：

  > （一）入学已满一学期；

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：入学已满一学期一致

## 43. d011 / c3

- 状态：kept
- 问题：本科生申请转专业需要满足哪些基本条件？哪些情形不允许转专业？
- claim：申请转专业的学生在校期间不得受过警告及以上处分。
- quote（located）：

  > （三）在校期间未受过警告及以上处分；

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：在校期间未受过警告及以上处分一致

## 44. d011 / c4

- 状态：kept
- 问题：本科生申请转专业需要满足哪些基本条件？哪些情形不允许转专业？
- claim：申请转专业的学生在校期间须未转过专业，因大类招生分流不计入转专业次数。
- quote（located）：

  > （四）在校期间未转过专业（因大类招生分流不计入转专业次数）。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：未转过专业及大类招生分流不计入次数一致

## 45. d011 / c5

- 状态：kept
- 问题：本科生申请转专业需要满足哪些基本条件？哪些情形不允许转专业？
- claim：招生时已有明确规定不能转专业的学生，含定向生、国防生、艺术类、体育类等专业学生，不允许转专业。
- quote（located）：

  > （一）招生时已有明确规定不能转专业的学生，含定向生、国防生、艺术类、体育类等专业学生；

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：招生时规定不能转专业的情形一致

## 46. d012 / c1

- 状态：kept
- 问题：推免生需要满足哪些基本条件和学业要求？
- claim：推免生须为纳入国家普通本科招生计划录取、未曾参与过推免环节的应届毕业生，且不含第二学士学位、独立学院学生。
- quote（located）：

  > 纳入国家普通本科招生计划录取、未曾计入历年应毕业本科生范围、未曾参与过推免环节的应届毕业生（不含第二学士学位、独立学院学生）

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：claim 为引文子集，无引文外多余断言

## 47. d012 / c2

- 状态：kept
- 问题：推免生需要满足哪些基本条件和学业要求？
- claim：推免生须品行优良，无考试作弊或剽窃他人学术成果等行为，无违法违纪受处分记录，思想品德考核不合格者不予推荐和录取。
- quote（located）：

  > 思想品德考核不合格者不予推荐和录取。无考试作弊或剽窃他人学术成果等行为，无违法违纪受处分记录。

- issues：—
- fact_uncovered：—
- 当前标签：partial
- 判定：[ ] supported  [x] partial  [ ] unsupported
- note：claim 增加“品行优良”概括，引文仅有思想品德考核及无作弊/无处分等具体表述

## 48. d012 / c3

- 状态：kept
- 问题：推免生需要满足哪些基本条件和学业要求？
- claim：推免生申请时前三学年（五年制专业为前四学年）的学业平均学分绩点（GPA）不低于3.20，且GPA排名位于同届同专业前50%（含50%）之内。
- quote（located）：

  > 前三学年（五年制专业为前四学年）的学业平均学分绩点（GPA）不低于3.20，且学业平均学分绩点（GPA）排名位于同届同专业前50%（含50%）之内

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：GPA不低于3.20且排名前50%一致

## 49. d012 / c4

- 状态：kept
- 问题：推免生需要满足哪些基本条件和学业要求？
- claim：推免生申请时须按教学计划修完应修课程，无处于缓考状态课程，补考或重修后无不及格成绩。
- quote（located）：

  > 按照教学计划修完应修课程；无处于缓考状态课程；补考或重修后无不及格成绩

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：修完应修课程、无缓考、补考重修后无不及格一致

## 50. d012 / c5

- 状态：kept
- 问题：推免生需要满足哪些基本条件和学业要求？
- claim：推免生外语水平须达到全国大学英语四级考试成绩不低于425分，或雅思不低于5.5分，或托福不低于80分；英语类专业学生须通过英语专业四级考试，日语类专业学生须通过日语专业四级考试。
- quote（located）：

  > 全国大学英语四级考试成绩不低于425 分，或雅思考试成绩不低于5.5 分，或托福考试成绩不低于80 分；英语类专业学生须通过英语专业四级考试，日语类专业学生须通过日语专业四级考试

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：四级425/雅思5.5/托福80及英语、日语专业四级要求一致

## 51. d013 / c1

- 状态：kept
- 问题：数学与应用数学专业 2024 版培养方案的培养目标是什么？
- claim：数学与应用数学专业2024版培养方案的培养目标是培养拔尖创新型人才。
- quote（located）：

  > 能运用数学理论和计算机技术解决实际问题的拔尖创新型人才

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：拔尖创新型人才一致

## 52. d013 / c2

- 状态：kept
- 问题：数学与应用数学专业 2024 版培养方案的培养目标是什么？
- claim：该培养目标要求学生具备良好的数学基础和思维能力，掌握数学学科基本理论与技能、掌握科学思维方法。
- quote（located）：

  > 具备良好的数学基础和思维能力，掌握数学学科基本理论与技能、掌握科学思维方法

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：数学基础与思维能力、基本理论与技能、科学思维方法一致

## 53. d013 / c3

- 状态：kept
- 问题：数学与应用数学专业 2024 版培养方案的培养目标是什么？
- claim：该专业毕业生可在政府、科技、教育、经济、管理、金融等部门从事研究、教学、应用开发和管理等工作。
- quote（located）：

  > 毕业生可在政府、科技、教育、经济、管理、金融等部门从事研究、教学、应用开发和管理等工作

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：就业部门与工作类型一致

## 54. d013 / c4

- 状态：kept
- 问题：数学与应用数学专业 2024 版培养方案的培养目标是什么？
- claim：该专业毕业生可在国内外知名大学、科研机构继续攻读本专业或相关高新技术学科、交叉学科专业的研究生学位。
- quote（located）：

  > 或在国内外知名大学、科研机构继续攻读本专业或相关高新技术学科、交叉学科专业的研究生学位

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：国内外知名大学/科研机构继续攻读研究生学位一致

## 55. d014 / c1

- 状态：kept
- 问题：数学与应用数学专业 2024 版培养方案的专业核心课程有哪些？
- claim：数学与应用数学专业 2024 版培养方案的专业核心课程为概率论、数理统计、实变函数、数值分析、数学建模、运筹学、近世代数、泛函分析。
- quote（located）：

  > 概率论，数理统计，实变函数，数值分析，数学建模，运筹学，近世代数，泛函分析。

- issues：—
- fact_uncovered：—
- 当前标签：supported
- 判定：[x] supported  [ ] partial  [ ] unsupported
- note：八门专业核心课程列表一致

## 56. d014 / c2

- 状态：kept
- 问题：数学与应用数学专业 2024 版培养方案的专业核心课程有哪些？
- claim：2022 版培养方案的专业核心课程中还包括离散数学、计算机科学绪论、Matlab 程序设计与应用。
- quote（located）：

  > 概率论，数理统计，实变函数，数值分析，数学建模，运筹学，离散数学，计算机科学绪论，近世代数，泛函分析，Matlab 程序设计与应用。

- issues：—
- fact_uncovered：—
- 当前标签：partial
- 判定：[ ] supported  [x] partial  [ ] unsupported
- note：引文含离散数学等三门课，但未标明“2022版”，也未建立“还包括”的对照关系
