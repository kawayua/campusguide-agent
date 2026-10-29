from pathlib import Path
import json, collections, statistics, re
from datetime import datetime
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT

ROOT=Path(__file__).resolve().parents[4]
OUT=ROOT/'docs/reports'
QA=Path(__file__).resolve().parent
STEM='CampusGuide_100题逐题错误分析与修改建议_20261028'
STATUS={'complete':'主要要点完整','partial':'部分覆盖','error':'内容错误','failed':'有据未答','valid_clarification':'合理澄清','valid_abstention':'合理拒答'}
# 每行依次为题号、优先级、问题归类、逐题诊断、应答要点、修改与验收要求、共性修复编号。
DATA=r'''
001|P0|条件方向错误|正文把“开考30分钟后禁止迟到入场”写成“开考后30分钟内禁止入场”，把时间条件反转；引用虽真，结论仍错。|区分迟到入场与离开考场两条规则；原文“后”与“内”不能互换；不得将携带手机等同于已经使用通讯设备作弊。|把主体、动作、阈值、比较方向、例外组成条件断言；验收须拒绝“30分钟内禁止入场”，同时保留替考开除学籍的正确结论。|R1
002|保持|合理澄清|未发现本轮错误。原题没有说明本科或研究生，先确认培养层次是合理处理。|澄清本科还是研究生后，再分别说明补考、重修条件与成绩记录；不能将首轮未展开当作漏答。|保留澄清；增加分别回答“本科”“研究生”的后续测试，验证不反复追问、不串用制度。|R9
003|P1|课程清单漏答|漏掉种子科学与工程专业核心课程，实践课程也未完整覆盖，回答缺少所问的两类课程。|核心课应包含作物育种学、种子生产学、种子加工与贮藏学、种子检验学、种子经营管理学；实践项按2024版原表补全。|按专业、年级分别锁定核心课章节及实践表；验收对两组名称分别做集合比对，不以引用到培养方案即判全覆盖。|R3 R4
004|保持|核心正确|未发现影响问题结论的错误；已说明3+1+X模式和本研课程衔接。|保留已核实的8学分要求与最多10学分认定口径，分别说明适用阶段。|作为回归正例；检查数字、阶段及上限条件不被后续修改破坏，不强制扩写无关制度。|R9
005|P1|方案对象混用|比较2022与2024新农学创新班时，混入本科分流补充方案；课程比较缺失，单条宽泛结论被多单元重复计为覆盖。|以两年对应创新班正式方案比较培养目标与课程，不拿本科分流方案替代完整创新班方案。|将对象、班型、版本、方面绑定为独立证据单元；验收两年目标与课程四格都有相应依据，缺任何一格不得报完整。|R3 R4
006|保持|人物方向正确|未发现对应关系错误；王孝林与蔡占东的研究主题已区分。|王孝林对应植物微生物组，蔡占东对应大豆遗传改良。|保留人物和方向的一一绑定；增加人物顺序互换测试，不能交叉归属。|R9
007|P1|检索身份误过滤|补考重修已有回答，但专业学位研究生专项制培养部分未答；graduate与细分身份的匹配及上下游过滤不一致。|分别回答课程考核不合格的处理，以及专项制培养的组织、实施和考核要求。|统一研究生父子身份映射，将问询者身份与文档适用对象分开；专项制条款必须能通过检索和验证，不能靠重复已答部分补数。|R2 R4
008|保持|核心正确|本科教学督导与研究生教学差错事故的认定处理均有覆盖，未发现明确事实错。|分清本科督导机制和研究生教学事故制度各自对象与程序。|只建议压缩长段、按两套制度分段；不得为追求简短删掉认定与处理环节。|R6
009|保持|合理证据边界|没有完整实验室清单，拒绝给出农学院实验室总数是正确的；附带奖学金示例与问题无关。|说明知识库缺少可据以计数的完整实验室名录；科研平台数量不能直接当实验室总数。|删去无关示例，明确缺失口径及需核实材料；不得随意用科研平台、实践基地或网页链接数推算总数。|R7
010|保持|活动事实正确|论坛11与论坛12的报告人和主题区分正确。|分别绑定傅向东、朱有勇及各自报告主题。|保留两条活动记录独立溯源，交换活动序号提问时仍能正确匹配。|R9
011|P2|名称呈现问题|人员与负责事项基本正确，但输出存在“数信与信息学院”等学院名称写法不规范的问题。|按原文正式学院名称，分别列农学院辅导员与数学与信息学院学生工作部门。|实体名称由规范名称生成，不改动已经核实的人名和负责事项；验收不得产生简称拼接式错名。|R6
012|保持|核心正确|已答申请程序以及每周8小时、每月40小时的限制；未列岗位分类不构成本题漏答。|保留申请与时间上限，区分周和月口径。|按题面验收，不额外把“所有岗位类别”列为必答；边界用8和40分别验证。|R9
013|P2|正式职务需精确|人员及分工基本正确，但不能将“副院长主持行政全面工作”简写成正式任命的“院长”。|党委书记为陈文艺；行政负责人夏强的正式职务按语料保留“副院长，主持行政全面工作”。|人名、正式职务、代主持事项分字段输出；若用户问院长，解释实际行政负责人及正式头衔，不擅自升格。|R1 R6
014|保持|评分基准需纠正|实际回答审批对象“本科生院教学运行科”与原文相符；旧参考答案写“教学院长”不应反向纠错。|调课、停课、补课的申请审批对象以当前语料所载程序为准。|修改评测基准中的错误审批对象；保留当前正确答复，不依据旧gold改坏答案。|R9
015|保持|核心正确|招生选拔方式与学位证书要求已覆盖。|保留高考招生选拔、双学士学位在同一本证书注明等原文要求。|回归检查“双学士学位”与“两本证书”不混同，不能套用辅修学位规定。|R9
016|P1|关系比较未落实|将两类制度合并成校外导师“可能第一也可能第二”，未回答分别如何对应。|校外导师细则：校外第一、校内第二；校外第二导师细则：校外第二、校内第一。|比较单元保留角色及制度名；验收必须输出两对明确映射，不能用“可能”替代有条件的确定规则。|R1 R4
017|保持|核心正确|2024级计算机人工智能选修组的理论与实验课程及学分口径已覆盖。|保留2024版五门理论、五门对应实验及8学分修读要求，不混入2022版。|以精确课程集合和组内学分规则做回归；不得根据当前热门课程自行增删。|R3 R9
018|P2|精简无关信息|本科教学、研究生教育及实验室安全的负责人对应正确，额外招生工作角色容易分散重点。|分别保留王少奎、谢庆军、梁志辉及所问职责。|优先回答三个对应关系；若保留其他职务，明确属于补充而非混作所问负责人。|R6
019|保持|阈值正确|一般、黄色、红色学业预警的触发条件已有覆盖。|保留各自欠修学分、成绩或学业状态阈值及适用时间口径。|将等于、超过、累计和当学期分开测试，不能因数字一致就放过比较符号错误。|R1 R9
020|P0|证据类型错用|用毕业要求支撑矩阵中的能力描述替代毕业设计课程要求；核心课程部分正确。|培养计划中617017毕业论文或设计工学课程为6学分、14周、第8学期；能力目标可作补充。|区分课程进程表与毕业要求支撑矩阵，优先提取正式课程行；验收必须保留学分、周数、学期的对应列。|R1 R3 R5
021|P1|问题范围漏覆盖|只答课堂文明，漏掉教学时间不得安排文娱活动、公共设施使用要求；语料没有的活动审批流程不能补造。|补充教学楼活动与设施管理相关规定；对审批入口、审批人等未收录部分明确说明。|拆出活动安排、教室使用、公共设施三类单元；验收覆盖已收录规则并披露未收录审批流程，不以编造换完整。|R4 R7
022|保持|核心正确|学术不端类型及举报、调查、处理和申诉程序均有实质回答。|保留行为类型与程序，避免把一般违纪或通报批评一概叫作学术不端。|回归检查程序顺序及处理主体；无需扩写所有相关制度全文。|R1 R9
023|保持|核心正确|贷款申请与在校、毕业后利息承担已覆盖。|在校期间财政贴息，毕业后按适用规定承担利息；保留起算时点与例外条件。|增加毕业、休学及继续升学等边界测试，不把“毕业后”无条件等同于全部由个人即时承担。|R1 R9
024|保持|核心正确|个人、集体作品的作者和数量要求及毕业设计排除已覆盖。|保留个人作品合作人数、贡献比例和第一作者要求，集体作者总数限制及毕业设计不得申报的范围。|按不同作品类型分别验收，避免个人不超过2名合作者与集体总人数不超过10名混用。|R1 R9
025|保持|核心正确|因病或特殊困难转学的条件及证明材料已有覆盖。|保留病情证明、特殊困难材料及规定审批要求，不能把转专业流程替代转学。|保持转学与转专业意图区分；对材料名、审批主体做回归。|R9
026|保持|核心正确|2027推免资格名单公示和异议反馈渠道已覆盖。|分别保留校级与学院公示、反馈途径及适用期限。|验收不得把学院联系方式泛化成学校唯一申诉渠道，也不把其他年度日期挪入。|R3 R9
027|P1|阶段条件漏答|进站、在站、出站混写；进站申请条件漏答，而重复列出若干在站和出站事项。|补齐博士毕业一般不超过3年、年龄原则上在35周岁以下等原文条件，并按校级与学院规定分别说明其他要求。|以进站资格、在站管理、出站考核三阶段拆解并逐项绑定证据；验收不能以重复考核项充当进站覆盖。|R3 R4
028|P1|招生材料被误过滤|招生复试细则被AUDIENCE_MISMATCH排除，转而检出奖学金材料，导致有据未答。|回答复试环节、复试外语30%专业40%综合30%、综合成绩初试折算50%加复试50%及录取规则，严格按2026细则。|招生申请者与在读研究生分开建模；验收应命中当年招生细则且验证各比例，不再用奖学金凑答。|R2 R3
029|保持|核心正确|国家奖学金、学业奖学金和国家助学金的对象与条件已区分。|保留三类资助及院级补充规定的适用范围。|只优化长度；校级规则与农学院细则须有明确标记，不能将学院条件泛化到全校。|R6 R9
030|P1|同名班型混淆|只答普通农学，对丁颖创新班声称没有证据，实际相关培养方案存在。|按2024丁颖创新班与普通农学两套方案比较培养目标与课程安排。|实体键包含专业、班型、年级；验收两边都有目标及课程依据，“没检索到”不得直接表述为知识库没有。|R3 R7
031|P1|跨版本比较不完整|回答偏向2024版，未完成2022与2024软件工程实践教学的对照。|从两版实践表分别列集中实践课程、课程学分或安排变化，不能只复述一年的方案。|建立版本配对及实践表保护检索；验收每个差异都有两年依据，表中未载数值不推测。|R3 R5
032|P2|表格残留影响理解|实践学分结论基本正确，但正文出现“……专业实践｜第五学期”等表格残片。|数学学硕与农艺种业专硕的实践要求分开表达，保留1学分与6学分及相应学期要求的适用对象。|表格转正文时剔除竖线和断行残片；不得把一个专业的学期要求扩展到另一专业。|R5 R6
033|保持|核心正确|困难补助类型与学校学院帮扶措施已有覆盖。|保留突发困难与临时困难相关补助及帮扶；本题没有要求所有申请入口。|验收按所问类型与措施，不把申请网址等可选信息设为强制扣分点。|R9
034|保持|核心正确|已正确回答数学与信息学院8个本科专业及名称。|以本科专业为计数对象，不混入研究生学科、微专业或产业学院。|检查清单去重后数量为8且名称集合相符；保持当前答案。|R1 R9
035|P0|补充规则与处分性质错用|将科研成果第一署名单位要求写成无例外通则，漏掉2026年9月14日补充说明中的联培情形；把一般通报扣分当作学术不端处理。|保留科研计分；按适用条件说明联培第一导师为合作单位导师等署名例外；学术不端另按第七节处理，不用通报扣分替代。|建立正文与补充说明的适用关联，生成规则时同时读取例外；验收联培与非联培两种情形、一般通报与学术不端两类性质均区分。|R1 R3 R4
036|P2|意图过度拆分|基地清单和国家级基地已答，但“实践教学基地”被拆成实践课程意图，出现无关课程、重复内容和假缺项。|直接列校外实践教学基地并标出国家级，不需军训或其他实践课程。|采用最长实体短语及类型识别，基地与课程分开；验收清单正确且不附带无关课程缺项警告。|R3 R6
037|保持|核心正确|困难认定等级、申请表及证明材料已覆盖。|保留认定等级与各类证明的原文适用要求。|回归检查等级名称和证明材料，不自动把困难认定等同于助学金最终获批。|R9
038|保持|活动集合正确|已覆盖知识库中2026年6月对应学院两条学术交流消息。|分别保留6月18日、6月28日活动及其主题与人物。|按学院与月份过滤完整本地新闻集合；说明“知识库收录”，不能外推现实世界仅有两场。|R3 R9
039|P2|答案前后矛盾|一处声称微专业证书未提及，后面又给出证书规则；另有辅修证书内容重复，核心事实虽有但整体不能称完美。|统一说明微专业与辅修的报名条件、培养定位和证书，并删除已被补全的缺证据声明。|补充后按问答单元替换旧缺项，而非简单追加；验收同一事项不能同时出现“未收录”和明确规则。|R4 R6
040|保持|核心正确|转专业一般条件及禁止申请情形均已覆盖。|区分可申请的一般条件与不得申请的具体情形，不与转学程序混用。|回归检查否定条件、年级及特殊类别限制，保留完整适用范围。|R1 R9
041|P1|研究生实体识别失败|轨迹中问题单元subject为空，入选证据几乎只覆盖农业工程，工业工程与管理方案未被有效保护；却概括为证据完全缺失。|围绕两个对象分别比较目标与课程；2025至2026是方案版本范围，不能任意扩成多个人学年份或把研究生专业当本科。|补全研究生学科专业实体、层次与别名；按对象和章节分配证据名额。层次仍不明确时局部澄清；验收两对象均有依据，不能沿用旧参考中错误的比较对象。|R3 R4 R7
042|保持|核心正确|本科国家奖学金、国家励志奖学金、国家助学金申请条件已分别覆盖。|保留奖学金与助学金的评选目标及困难认定要求差异。|压平嵌套编号即可；不能因三类名字相似而合并条件。|R6 R9
043|P1|教师资格文档误过滤|研究生导师选聘制度被按在读研究生身份误过滤，导致博士导师与硕士导师资格未答。|按两份2025选聘细则逐项比较政治师德、学术水平、成果及指导条件；具体数值以对应条款为准。|查询对象是教师资格，不等于提问人必须是教师；验收两套制度均能入选且条件分别归属，不能从招生或奖学金制度拼接。|R2 R3
044|保持|活动事实正确|领航者成长系列第四期主题与发布时间相符。|保留“淬炼通用核心能力 明晰职业发展航向”及10月20日发布信息。|区分发布时间与活动举行时间；无新来源时不推断活动已举行。|R3 R9
045|P1|时间与办理安排未展开|已给综合测评总体时间范围及最终公示形式，但对实施安排的相关章节覆盖不足，正文未清楚解释已知与缺失的时间细节。|补充通知中系统办理与公示安排；只有总时间范围时，明确未给每个环节的独立截止日。|将时间、办理安排、公示方式分别核对；验收不编造分环节日期，能够明确哪些日期是原文已载。|R4 R7
046|P2|长段与重复|选课、退课、补选与未选课不得参加考试的规则已答，单一长段及考试内容重复降低可读性。|分别说明选、退、补及考试资格，保留各自时间和审批限制。|用四个短段输出，语义去重；事实与引用对应关系不能因压缩丢失。|R6
047|P2|课程解析产生假缺项|核心课程与理学学位已答，但解析将“数学建模。拓展必选：数据结构与算法”粘成课程，造成假缺项和错误部分回答状态。|只从核心课程章节提取课程名，拓展必选另属一组。|课程清单解析识别中文句号、分组标题和章节边界；验收不产生跨句伪课程，完整回答不再报缺项。|R5 R6
048|保持|研究方向正确|数学及软件工程学硕研究方向分别有覆盖。|保留2025至2026版对应研究方向，不能混成专业核心课程。|按专业方向集合回归，不把版本范围拆成两个不同招生年。|R3 R9
049|P1|课程结构数字漏答|只保留159.5总学分，必修89、选修33、实践37.5被丢弃；已有结构表信息未被数字校验正确利用。|回答159.5等于89加33加37.5，并说明三类结构口径。|为结构汇总表保留表头、行列、版本、单位上下文，不仅识别六位课程代码行；验收四个数都可回溯且加总一致，不能放开任意无依据数字。|R4 R5
050|保持|科研人物主题正确|张艳霞、梁云团队及研究主题已匹配。|按2026年2月新闻中的人物与根系分割等具体主题分别表述。|保留人物和新闻独立绑定，不能把同名教师页中的其他方向代替本篇成果。|R3 R9
051|P2|组织表达可优化|六类通识选修及计划认定要求已有覆盖，长段阅读成本较高；不能杜撰所有学生通用的最低学分。|区分课程类别、所在专业培养方案要求和认定规则。|按类别与认定要求分段；回归防止引入一个适用于所有专业的虚构统一最低学分。|R6 R9
052|保持|活动与资格正确|丁颖挑战杯选拔时间、参赛资格、作品数量与排除条件已覆盖。|保留10至11月、最多3件作品等通知所载规则及适用对象。|验收与第24题互相一致，不能把个人作者人数当作品数量。|R1 R9
053|保持|委员会对应正确|三个委员会与负责人一一对应正确。|学术分委员会刘耀光，学位分委员会储成才，教学指导委员会储成才。|检查同一人兼任不同委员会不会被去重成漏掉一个委员会。|R6 R9
054|保持|通知材料正确|国家奖学金材料和截止时间已覆盖。|保留材料清单及10月10日09时截止；在2026年10月28日语境下该时点已过。|历史通知仍按原日期回答，若提出办理建议须说明已截止，不能称“即将截止”。|R3 R9
055|P1|办理渠道与费用混用|本科与研究生渠道虽有部分覆盖，但人工办理收费被泛化为自助打印收费，适用渠道界线不清。|人工办理前5份免费、超出每份5元、校友相关人工办理15元等按原文区分；自助打印费用未明确则说明未知。|费用事实绑定人员类别、办理渠道、数量和单位；验收不得把人工收费套入自助打印。|R1 R4
056|保持|名录计数正确|2012至2024基地目录的总数与分级计数正确。|12个基地，其中国家级1、省级1、院级10；限定该目录期间。|计数须有去重后的名录集合，不能暗示截至当前所有新增基地仍只有12个。|R1 R9
057|保持|发布日期正确|袁隆平追梦奖学金拟推荐名单公示的发布时间已答。|按新闻发布时间10月9日，不把公示学年当发布时间。|分别保存学年与发布时间两个字段，年月日输出一致。|R3 R9
058|保持|公式口径正确|德智体美劳权重及德智体计算方法已覆盖。|德20、智65、体5、美5、劳5；保留德10加5加5减扣分、智55加10、体3加2的结构。|公式逐项回归；未问美劳细则不额外作为必答扣分。|R1 R9
059|P1|学院案例泛化全校|全校研究生答辩条件只用农学院作物学局部条款回答；学校学位授予细则被身份过滤，局部例子不能替代通则。|先给学校硕士3至7人单数、博士5或7人且至少2名校外专家、公开提前3日、博士一般不少于60分钟和硕士不少于30分钟等原文要求，再标注学院补充。|恢复校级有效条款，校院分层排序；验收学校通则齐全，学院与学科局部条件显式标注范围。|R2 R3 R4
060|P1|推免资格问成奖助|应届本科生申请2027推免的通知被按研究生身份过滤，答案偏成奖学金，未答申请资格。|按校级办法与农学院细则说明资格及衔接，包括GPA不低于3.20且排名前50%、外语等按原文规定的条件与例外。|将推免申请对象标为本科生，研究生是目标去向；验收不再返回推免奖学金，外语替代条件不得删成只有CET4一种。|R2 R3 R4
061|P1|实践部分漏答|研究方向已部分答出，不能整体算“有据未答”；实践漏答，证据槽被本科计算机进程表占用。|从研究生专章分别说明学硕方向与实践活动、专硕研究领域与6学分专业实践及第5学期等要求。|限定研究生层次并保护“实践活动”章节；验收学硕实践与专硕实践分别齐全，不能用学硕必修环节总4学分冒充实践4学分。|R3 R4 R5
062|保持|新闻集合完整|10月16日、19日通知与所问电动自行车、综合测评内容均覆盖，还检出助学金通知。|保留对应日期的全部已收录通知，不能因原参考只列两条而扣掉真实第三条。|按日期和学院做完整集合验收，允许有据且相关的额外命中。|R3 R9
063|P1|安全教育层级不全|答复偏概括，未充分区分校院新生教育、实验过程教育等要求；事故报告方向基本正确。|补齐原文教育培训层级与定期应急演练要求，按安全制度说明报告渠道。|以教育层级与报告程序分单元；不把日常演练都说成每次实验前必须完成，不虚构1小时或24小时报告时限。|R4 R7
064|P1|班型版本混用并验证失败|普通农学与创新班证据混排；选中证据已包含两年普通农学结构，但生成反复把创新班的138学分套给普通农学，修改后引文不匹配，全部被拒。根因不能只写“没检索到”。|普通农学2022为166学分，必修62.5、选修55.5、实践48；2024为155学分，必修72.5、选修43、实践39.5。|锁定普通班与版本的结构行，分开验证各年事实并保留已通过子项；验收总分减11，三类分别加10、减12.5、减8.5；现有拦截138机制应保留。|R3 R4 R5
065|P2|事实已齐但比较不直观|两年总学分和核心课清单均已正确给出；原判部分覆盖把未显式作差也算漏答，偏严。另有“5至8门”等说明粘连成伪课程的覆盖问题。|163降至159.5，减少3.5；核心名单新增计算机科学概论、面向对象程序设计，离散结构不再列为核心，不能推断已取消开课。|建议改为主要要点完整；优化差异摘要并修课程解析。验收禁止“离散结构被取消课程”这种超出原文推断。|R5 R6 R9
066|保持|组织清单正确|学生组织与产业学院清单已有覆盖。|保留13个学生组织与3个产业学院的分类，不把产业学院纳入学生组织计数。|按类型分别去重计数，名称来自对应名录。|R1 R9
067|保持|负责人正确|按知识库记录回答储成才，未发现本样本中的事实错误。|明确这是知识库所载农学院院长信息。|对“现任”“最新”查询增加资料更新时间边界，不能宣称已进行现实任职核查。|R3 R9
068|保持|人员系别正确|三位教师与所在系的对应正确。|田绪红计算机系、张大斌信息系、杨德贵数学系。|人名顺序互换与单独追问做回归，不能根据相邻文本串系。|R1 R9
069|保持|请假边界正确|无法按时报到的请假手续和一般两周上限已覆盖。|保留不可抗力等例外条件，不把一般上限改成所有情况绝对两周。|验收需同时命中一般规则与例外，不能只比对数字2。|R1 R9
070|P1|办理流程漏答|免修、缓考条件已有部分覆盖，但免修申请的审批链条未充分回答。|免修按第十二条说明个人申请、任课教师、学院、本科生院各环节；缓考依对应考前申请及批准程序说明。|把“条件”和“流程”拆开，各自要有完整证据；验收不得仅列疾病等条件就判全部回答，未经证据不得增设审批人。|R4 R7
071|P1|补充方案学分漏答|本博贯通模式与分流替换关系已答，补充方案的155学分、2815学时等要求漏掉。|区分完整3加1加4贯通方案与本科阶段分流补充方案，补齐补充方案所载学习要求。|以方案层次与版本绑定数值，结构表和说明分取；验收155与2815各有原表依据，不能将其当全部创新班贯通总量。|R3 R4 R5
072|保持|活动与招生年度正确|10月20日两条活动信息及面向2027级硕士招生的说明正确。|区分新闻发布年份2026与招生入学年份2027。|回归年度角色绑定，不将2027误读为新闻发布日期。|R3 R9
073|保持|科研成果对应正确|9月24日吴健、刘向东团队及玫瑰香、多倍体育性等主题对应正确。|分别保留两条研究报道的人物、研究对象与成果。|交换提问顺序仍正确绑定，不将两条报道拼成同一项目。|R1 R9
074|P2|编号可读性|保留入学资格与休学的学籍差异以及参军保留期限已有覆盖；嵌套编号较乱。|保留入学资格期间不具有学籍，休学保留学籍；参军退役后2年等规定按对应条款表述。|整理编号即可；不要凭一般经验否认第五条原文已经列出的材料要求。|R6 R9
075|P1|材料清单漏答|资格和办理时间已答，但漏掉第五部分的两类申报表及理由填写等材料要求。|按通知补齐国家助学金与国家励志奖学金相应表单，保留理由代码及备注不超过100字等原文要求。|材料单独建单元，与资格、截止日期分别验收；不能因日期和条件正确就判完成全部办理要求。|R4 R5
076|P2|实践课程分类待规范|核心课与实践内容已有覆盖，但将泛称“实验教学”同具体集中实践课程并列，容易混淆课程类别。|核心课程清单单列；集中实践环节按原表列具体课程，分散实验不自动等同集中实践。|标明课程、类别及环节的层级；验收所有集中实践项都能落到原表具体条目，不以泛称凑课程。|R1 R5
077|P2|评分范围偏严|两专业培养目标和核心课均已列出，原判要求额外说明学位类别并把缺少一句对比总结判漏答，超出必要要求。|可概括数据科学偏数据技术与分析实现、大数据管理偏管理决策与应用，之后列各自核心课；学位类别仅可选补充。|建议改为主要要点完整，另列比较表达优化；验收以目标及课程为必答，不将未问学位作为硬性扣分。|R4 R6 R9
078|P2|评分加入题外要求|已回答共同申请条件和18对36学分、学位论文不少于6学分等差别；原判仅因没说证书授予而扣分，与题面不符。|题面只要求条件与最低学分，现有事实足以完成；证书规则可作为后续问答。|建议改为主要要点完整；修改评测清单，禁止把第39题的证书要求机械迁移到本题。|R9
079|P2|重点被长段淹没|地理信息科学毕业要求与能力已有覆盖，但长串编号及额外学分信息让重点不突出。|先概括空间数据获取处理、分析建模与系统应用等原文能力，再分组呈现毕业要求。|重排表达，不把题外但正确的学分当事实错误；验收不得漏掉已正确回答的毕业要求。|R6 R9
080|P1|模块约束漏答|列了选修方向和课程，但信息系统开发方向子模块的2、6、4学分及跨门类限选2学分未答。|分别给专业公共选修、方向与子模块约束；主修与辅修10.5学分分开；9、12、22的关系需按同一表层级说明，不能直接当互斥三项相加。|表格抽取保留父子模块、任选条件和合计层级；验收开发方向2加6加4及跨门类2均有依据，避免把10.5读成105。|R4 R5
081|保持|活动人物主题正确|论坛12报告人、主题及发布时间已覆盖。|朱有勇与其报告主题按活动新闻绑定，发布时间为7月23日。|不要将论坛11报告人或文章采集时间替代本条新闻信息。|R3 R9
082|保持|通知集合正确|9月心理健康活动观测加分证明相关通知已覆盖。|分别列已收录的两条通知及其适用活动，不扩为全院所有现实活动。|允许提供有据的日期，但没有问日期时不因缺少非必要日期扣分。|R3 R9
083|P1|仅报办法名无法指导办理|正文只说按申诉办法申诉，没有展开决定书应载申诉途径与期限，也未说明申诉处理办法全文未收录。|说明从处分决定书确认途径和期限，按所引办法办理，依法按程序申诉；具体受理部门及完整流程因全文缺失须进一步核实。|真实暴露材料缺口并补齐已收录条款；不得把离校5个工作日或退学申诉时限移作纪律处分申诉时限。|R4 R7
084|P2|补充后旧否定未删除|四个系及主任已答出，但前段又称其余主任未列，出现同一答案自相矛盾。|保留完整系与主任映射，删除补充前遗留的缺项声明。|最终输出按实体属性合并新旧结论，验收同一主任信息不能同时出现已知与未知。|R4 R6
085|保持|学院沿革正确|2014年合并合署关系及三个系已有正确回答。|区分数学与信息学院、软件学院的合署关系和各系组成。|时间、合署办学与独立机构类型分字段，不能把合署改写为软件学院已取消。|R1 R9
086|保持|核心正确|本科毕业论文与研究生学位论文抽检、优秀评选基本条件已有覆盖，未发现确定性事实错误。|按本科与研究生分开；若追问研究生分类门槛，再依据原文展开学博、学硕、专硕对应标准。|本题无需强制列所有A、B、C等级细则；扩写必须有对应层次依据，不能用一套门槛统摄所有类型。|R3 R9
087|保持|收录边界正确|按知识库收录回答9月17日论坛消息合理。|限定“知识库收录的9月论坛”，不能断言现实9月只有这一场。|月份过滤、发布时间和收录范围一起回归，未收录不能推测。|R3 R7
088|P1|例外条款挤掉一般规则|入选证据偏向学士学位细则第五条补授情形，毕业一般条件与正常授位条件未获保护，结果有据未答。|先按学籍规定第四十一条回答毕业条件，再按学位细则相应条款回答正常授位条件，最后说明特殊补授。|拆分本科毕业与学士授位两单元，保护一般条款；不要仅全局降低重排阈值。验收两类一般条件均有证据，例外不覆盖通则。|R3 R4
089|保持|公示学年正确|两个不同学年的国家助学金与国家励志奖学金公示已区分。|保留2026至2027学年助学金和2025至2026学年励志奖学金，不把同年发布当同一学年。|以资助类别、学年、发布日期三字段匹配；未要求个人名单时不强制展开姓名。|R3 R9
090|保持|最终答案正确但有重试|最终回答10月两条所问新闻正确；早先尝试曾出现覆盖核对429，不能据最终成功宣称全程无服务问题。|保留10月14日党员教育及10月10日2026级军训相关报道，按新闻发布日期表述。|内容评价维持通过；运行记录另记尝试次数、429节点与恢复结果，不能把核对服务不可用显示成语料缺失。|R8 R9
091|P2|重复与姓名清洗|梁云为学院教师、属于计算机系已答，系名重复；没有说教授不构成本题错误。|直接回答“是，属于计算机系”，职称仅在有据且需要时补充。|去重并保留姓名边界；修复OCR不能拆坏人名，评测不强制未问职称。|R6 R9
092|P2|对象扩展与伪课程|普通农学的核心课程、4年与农学学士正确，但附加多个创新班博士阶段信息；句号与“博士阶段”粘连产生伪课程和假缺项。|只按2024普通农学回答五门核心课、4年、农学学士；创新班需用户明确指定后再展开。|实体优先精确班型，清单解析切断阶段标题；验收不出现“种子学。博士阶段”等伪课程或无关博士规则。|R3 R5 R6
093|保持|研究方向匹配正确|储成才、楚雯飞、谢庆军及其主要研究方向分别匹配。|研究方向按各自教师资料归属，不将合作成果误说成全部人的共同方向。|互换人名顺序做回归，列表与引用逐项绑定。|R1 R9
094|P2|制度衔接需核实|学校“入学后第4周前”和学院“4月选配、6月完成”均照实给出，但表面时间差异未说明；这属于来源衔接待核实，不是已证实抄错。|保留两份原文及其适用对象，提示时间安排存在需确认的衔接问题；不能自行断言新院规覆盖校规。|建政策冲突提示并交业务核实适用场景或修订关系；验收不得静默删除任一原文或无依据择一生效。|R1 R7
095|保持|最近消息口径正确|按知识库最近收录新闻给出8月10日及英文报告题目，未将抓取日期当发布时间。|“最近”限定本地收录集合及其时间截点，不能等同现实最新。|按发布时间排序，采集时间仅作元数据；无联网验证时保留知识库范围限定。|R3 R9
096|P1|博士招生被错当在读规则|博士申请考核材料被研究生身份过滤，错检奖学金等内容；无有效引用最终被拒，问题在上游选材。|按农学院2026申请考核制博士细则回答资格、材料提交及笔面试等考核环节；日期和渠道必须出自该细则。|区分考生与在读博士，保护招生专属文档；验收三类所问信息均有有效引用，不放松无引文拦截来掩盖问题。|R2 R3 R4
097|P2|语言呈现小问题|培养目标、4年及161学分分项正确，正文夹用“and”等不必要英文。|161等于101.5加26.5加33；主修与辅修66.5、34等不同口径分开。|统一中文表达，保留数值和总分结构；不得为了精简把辅修数字混入主修总分。|R5 R6
098|P0|实体分类错误|把普通实验室一并称为实验教学示范中心；有云计算与大数据实验室的判断正确。引用与语义检查都通过，说明仅校验同段出现不足。|示范中心只列基于云平台的计算机、信息数学与应用统计、数学建模三个；云计算与大数据实验室另列为实验室。|每个枚举项须验证其实体类型与谓词；验收中心数量为3，普通实验室不能因同页出现就继承示范中心类别。|R1 R5
099|保持|专业与班型正确|三个本科专业、两个创新班和两个国家级一流专业建设点已有区分。|专业、班型、一流建设点分别列示，创新班不另计为本科专业。|类型分开计数与归属，不能将班型数累加到专业总数。|R1 R9
100|P2|澄清模板多余|“两个学院”没有指明对象，先追问合理；附带“哪一份办法”与问题不相关。|只需询问要比较哪两个学院，再按确认对象给研究生授权点或招生专业。|精简澄清模板；下一轮提供学院名后直接办理，不将首轮缺少专业清单算失败。|R6 R9
'''
MODULES={
'R1':{'name':'约束和关系的语义校验','priority':'P0','files':['src/campusguide/verification/facts.py','src/campusguide/verification/semantic.py'],'action':'校验条件方向、否定、主体动作、角色、实体类型、适用例外与制度关系。对列表逐项验类型，对时间阈值核对比较符号；保留当前拒绝无依据数字的机制。','test':'001、020、035、098为发布前必过题；另测阈值边界、联培例外、混合中心和实验室列表。'},
'R2':{'name':'提问者身份与文档适用对象分离','priority':'P1','files':['src/campusguide/agents/planner.py','src/campusguide/retrieval/filters.py','src/campusguide/verification/scope.py'],'action':'不从问题提到研究生就断定提问人是在读研究生。建立本科申请者、研究生在读、教师导师等查询对象及父子身份映射，向量检索、重排、语义验证共用同一解释。','test':'007、028、043、059、060、096应恢复适用材料，原本排除不适用群体的正例仍须保持。'},
'R3':{'name':'对象层次班型版本和章节检索','priority':'P1','files':['configs/organization.yaml','src/campusguide/agents/question_units.py','src/campusguide/retrieval/corpus_views.py','src/campusguide/agents/retriever.py','src/campusguide/agents/graph.py'],'action':'建立专业及学科实体的层次、班型、别名、年级或版本，区分2025至2026版本区间；按对象及所问方面保护证据名额，补查时保留原question_units和过滤修正。','test':'005、030、031、041、061、064、071、088、092；不得用本科表回答研究生实践，也不得用分流补充方案替代创新班完整方案。'},
'R4':{'name':'按所问要点核实覆盖并补齐流程','priority':'P1','files':['src/campusguide/agents/question_units.py','src/campusguide/agents/coverage.py','src/campusguide/agents/graph.py'],'action':'把对象与目标、条件、流程、材料、时间、数值分别绑定到具体结论。引用ID存在不是实际覆盖；比较题要求各对象各方面都有证据，补充核对后重算覆盖。','test':'005、016、027、045、049、070、075、080、083；用一条泛称断言假装覆盖所有单元的负例必须失败。'},
'R5':{'name':'课程清单与表格结构抽取','priority':'P1','files':['src/campusguide/verification/coverage_items.py','src/campusguide/verification/facts.py','src/campusguide/retrieval/corpus_views.py'],'action':'识别中文句号与分组标题，剔除“最为核心的5至8门”等说明。保留结构表的表头、行列、学期、学时、任选范围及版本，区分支撑矩阵与正式进程表。','test':'020、047、049、064、065、071、075、080、092；10.5不变成105，未知数字仍拒绝，选修合计层级不得被扁平相加。'},
'R6':{'name':'最终答复合并去重和矛盾检查','priority':'P2','files':['src/campusguide/qa/finalize.py','src/campusguide/agents/graph.py','src/campusguide/agents/advisor.py'],'action':'按问题单元合并最终有效结论，补充证据后撤销过期缺项；同一实体同一属性只保留一致结果。限制无关扩写，去除表格残片，保持学院及职务规范名称。','test':'036、039、047、065、084、091、092、100；不能同时出现某事项“未收录”和已给明确规则。'},
'R7':{'name':'区分语料缺失检索遗漏与政策冲突','priority':'P1','files':['src/campusguide/agents/advisor.py','src/campusguide/qa/finalize.py','src/campusguide/agents/coverage.py'],'action':'只有确认相关资料确未收录才说明知识库缺失；检索未命中和核对服务失败应分别标记。申诉全文缺失等情况给已知部分并披露边界，学校学院冲突提示待核实。','test':'009保留合理拒答；021、041、083、088不再用假定无证据替代实际排查；094不擅自择一制度优先。'},
'R8':{'name':'供应商错误分类与可恢复运行','priority':'P1','files':['src/campusguide/agents/providers.py','src/campusguide/agents/coverage.py','src/campusguide/agents/graph.py'],'action':'分别记录429、超时、鉴权、额度及解析异常，按供应商明确反馈分类。有限重试、并发与会话释放、熔断；覆盖核对不可用不能改写成业务缺资料。','test':'无故障注入时完成基线；故障注入验证真实错误标签、有限重试、完整尝试链。最终答案成功与首试成功分别统计。'},
'R9':{'name':'评测基准和统计可信度','priority':'P1','files':['data/eval/answer_quality/gold_v1.jsonl','scripts/build_merged_100_report.py','scripts/build_pending_57_report.py'],'action':'按题面设置必要与可选检查项，保存原判与复核判不覆盖历史；按选定session去重计算引用与耗时，中位数用统计定义，分离最终成功和重试成本。','test':'061、065、077、078判定调整须可追溯；100条最终回答312条引用，中位23.135秒；替换旧session不能重复计数。'}
}

def load_jsonl(p): return [json.loads(l) for l in p.read_text(encoding='utf-8').splitlines() if l.strip()]
base=ROOT/'data/eval/answer_quality'
cases=load_jsonl(base/'merged_100_20261028/merged_cases.jsonl')
raw=[]
for path in sorted((base/'batch_retest_20261028_162015').glob('batch_*/results.jsonl')): raw.extend(load_jsonl(path))
raw.extend(load_jsonl(base/'batch_pending_57_20261028/results.jsonl'))
by_session={r['session_id']:r for r in raw}
articles=load_jsonl(ROOT/'data/processed/articles.jsonl')
article_map={(a['article_id'],a.get('part_no',1)):a for a in articles}
gold={r['id']:r for r in load_jsonl(base/'gold_v1.jsonl')}
rows={}
for line in DATA.strip().splitlines():
 x=line.split('|'); assert len(x)==7,(x[0],len(x)); n=int(x[0]); assert n not in rows
 rows[n]={'priority':x[1],'category':x[2],'diagnosis':x[3],'answer_requirements':x[4],'change_and_acceptance':x[5],'modules':x[6].split()}
# DATA实际以7列记录，不添加隐含默认字段。

assert set(rows)==set(range(1,101))
assert len(cases)==100 and len({c['session_id'] for c in cases})==100
adjustments={61:'partial',65:'complete',77:'complete',78:'complete'}
confirmed_trace={1,3,5,7,16,20,21,27,28,30,31,35,36,39,41,43,45,47,49,55,59,60,61,64,65,70,71,75,77,78,80,83,84,88,90,92,94,96,98}
records=[]; audit_issues=[]; citation_count=0
for c in cases:
 n=c['case_number']; d=rows[n]; r=by_session[c['session_id']]; reply=r['reply']; ct=reply.get('citations',[]); citation_count+=len(ct)
 for i,ref in enumerate(ct):
  art=article_map.get((ref['article_id'],ref.get('part_no',1)))
  if art is None: audit_issues.append({'case_number':n,'citation':i+1,'issue':'article_missing'}); continue
  a,b=ref.get('quote_start'),ref.get('quote_end')
  if not isinstance(a,int) or not isinstance(b,int) or art['text'][a:b]!=ref.get('quote',''):
   audit_issues.append({'case_number':n,'citation':i+1,'issue':'offset_quote_mismatch'})
 proof=c['review'].get('source_proof') or []
 if isinstance(proof,dict): proof=[proof]
 refs=[]
 for item in proof+ct+gold.get(c['id'],{}).get('source_anchors',[]):
  a=article_map.get((item.get('article_id'),item.get('part_no',1)))
  if a:
   ref={'article_id':a['article_id'],'part_no':a.get('part_no',1),'doc_id':a.get('doc_id'),'version_id':a.get('version_id'),'doc_title':a['doc_title'],'article_no':a['article_no']}
   if ref not in refs: refs.append(ref)
 if n==41:
  refs=[z for z in refs if '软件工程' not in z['doc_title']]
  for a in articles:
   if ('工业工程与管理' in a['doc_title'] or ('农业工程' in a['doc_title'] and a.get('version_id')=='v2026')) and ('培养目标' in a['article_no'] or '课程' in a['article_no']):
    ref={k:a.get(k) for k in ['article_id','part_no','doc_id','version_id','doc_title','article_no']}
    if ref not in refs: refs.append(ref)
 trace=reply.get('trace',[]); trace_text=json.dumps(trace,ensure_ascii=False)
 trace_nodes=[t.get('node') for t in trace if isinstance(t,dict)]
 original=c['review']['status']; recommended=adjustments.get(n,original)
 d.update({'case_number':n,'id':c['id'],'question':c['question'],'original_status':original,'recommended_status':recommended,'status_changed':original!=recommended,'original_reason':c['review'].get('reason',''),'outcome':c['outcome'],'session_id':c['session_id'],'started_at':c['started_at'],'elapsed_s':c['elapsed_s'],'actual_answer':c['answer'],'citation_count':len(ct),'source_references':refs[:6],'citations':ct,'trace_summary':{'nodes':trace_nodes,'audience_mismatch_mentions':trace_text.count('AUDIENCE_MISMATCH'),'rerank_drop_mentions':trace_text.count('RERANK_DROP')},'evidence_strength':'答案与原文核对；结合对应session轨迹定位直接原因' if n in confirmed_trace else '答案与审核记录核对；工程建议不等于已完成因果实验','code_refs':list(dict.fromkeys(f for m in d['modules'] for f in MODULES[m]['files'])),'scope':'只诊断和提出修改建议，本报告不表示已改代码或已重新验收'})
 records.append(d)
counts_original=dict(collections.Counter(r['original_status'] for r in records)); counts_recommended=dict(collections.Counter(r['recommended_status'] for r in records))
times=[c['elapsed_s'] for c in cases]
summary={'as_of':'2026-10-28','source_scope':'所提供DOCX、合并100题选定记录、对应session原始轨迹、4026条本地语料及当前代码；未调用外部模型或重新跑题','selected_answers':100,'original_counts':counts_original,'recommended_counts':counts_recommended,'grading_adjustments':[{'case_number':n,'original':records[n-1]['original_status'],'recommended':records[n-1]['recommended_status'],'reason':records[n-1]['diagnosis']} for n in adjustments],'final_citations':citation_count,'citation_offset_audit_issues':audit_issues,'sum_elapsed_s':sum(times),'median_elapsed_s':statistics.median(times),'pending_batch_attempts':76,'pending_batch_selected':57,'pending_batch_extra_attempts':19,'note':'引用定位正确不等于引用支持结论；这些质量判断是固定样本的人工复核，不是独立泛化准确率。'}
QA.mkdir(parents=True,exist_ok=True); OUT.mkdir(parents=True,exist_ok=True)
(QA/'diagnosis_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
jsonl=''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in records)
(OUT/(STEM+'.jsonl')).write_text(jsonl,encoding='utf-8')

INTRO=[
'这100条选定答案暴露了可重复定位的质量问题，但不能概括为“只有27题答对”，也不能将其余题一律归为模型供应商故障。原审核认为64题主要要点完整；逐题复核后，建议将第65、77、78题从部分覆盖调整为主要要点完整，第61题从有据未答调整为部分覆盖。按这一口径，67题主要要点完整、19题部分覆盖、4题内容错误、7题有据未答，另有2题合理澄清和1题合理拒答。',
'其中30题仍有实质性内容问题，即19题部分覆盖、4题错误和7题有据未答。另一些已经回答核心问题的题，仍存在重复、假缺项、前后矛盾、范围过宽或表格残片，所以“主要要点完整”不等于“完全无瑕疵”。本报告逐题列明实际问题；没有确定错误的题明确保留，不为凑数量虚构缺陷。',
'本次诊断依据原始测试记录、对应session轨迹、本地语料及代码，未重新运行100题，也未修改应用代码。判断限于知识库收录内容和该批样本的运行状态；活动新闻的“最近”与人员的任职信息不表示已经联网核实到2026年10月28日。'
]
METRICS=[['指标','原报告或原判','本次复核'],['主要要点完整','64题','67题 建议口径'],['部分覆盖','21题','19题'],['内容错误','4题','4题'],['有据未答','8题','7题'],['合理澄清与合理拒答','2题与1题','维持2题与1题'],['最终选定答案服务失败','0题','0题 不代表首试均成功'],['100条最终答案引用数','325条','312条'],['耗时合计','2615.4秒','2615.427秒 约43.6分钟'],['中位耗时','23.4秒','23.135秒 约23.1秒']]
CAUSE=[
'最严重的是验证器放过了有引用但错误的结论。第1题反转“30分钟后”和“30分钟内”，第98题把普通实验室归为示范中心；第20题引用能力矩阵代替正式毕业设计课程要求，第35题漏了联培成果署名的补充例外并混淆一般通报与学术不端。这四题说明真实引文与数字出现并不能保证结论正确。',
'有据未答主要发生在检索过滤与对象识别。第28、43、59、60、96题的对应轨迹可见身份过滤排除适用资料；提问中出现研究生，不表示问询者就是在读研究生。第41题的专业对象识别为空，工业工程与管理方案未获有效证据名额。第61题被本科表占位，第88题只保住特殊补授条款，丢了正常毕业授位规则。这些有明确流程成因，不能靠换更强模型直接保证解决。',
'第64题是特别重要的反例：正确的两年普通农学学分结构已经进入候选证据，但生成反复把创新班的138学分套给普通农学，并出现引文不匹配，因而被拒。这里既有班型版本混用，也有生成和修订失败；拒绝错误数字本身是保护机制，不能通过放松验证让错误答案过关。',
'覆盖与最终整理同样有问题。同一泛称结论被算作多个所问要点已覆盖，材料和流程则被遗漏；课程解析不识别中文句号和分组标题，制造伪课程与假缺项；补充答案后旧的“没有证据”提示未撤销，形成第39、84题那样的自相矛盾。修复应作用于结构和状态更新，而不只是继续增加提示词。',
'供应商与模型能力应分开判断。所选100条最终答案没有服务失败，但补测57题用了76次尝试，多出19次；第90题曾有覆盖核对429。429、超时、额度、鉴权须依据实际错误码分类，现有记录不足以认定都是欠费。与此同时，上述实质性答案错误发生在模型已返回结果的情况下，不能统一归咎API不可用。当前生成和语义核对复用同一模型，也不能当作彼此独立的质量保证。'
]
ERRATA=[
'原报告325条引用来自两轮汇总相加，包含被补测替代的旧题次引用。按合并100题的选定session重新去重后为312条。本次逐条对照本地article_id、part_no及quote_start和quote_end，312条定位与引文切片均匹配，发现0处定位错误；这仅证明引用原文确实存在，不证明结论正确或回答完整。',
'100条选定答案耗时之和为2615.427秒，中位数为23.135秒。原脚本按排序后上中位项取值，得到23.4秒，未对偶数样本取中间两值平均。该耗时不包含所有被舍弃的尝试及间隔等待，不能当作从首次开跑到全部完成的实际墙钟耗时。',
'第61题已有研究方向答案，应是部分覆盖；第65、77题均给出了题目要求的两边事实，但比较表达可更直接；第78题题面未要求证书，不能据此扣分。这四处仅在新报告中提出复核建议，不覆盖原始判定；所有统计同时保留原口径。',
'评测项必须区分必答与可选。第14题旧参考审批对象错误应改参考；第41题旧参考比较对象错误应改参考；第12题岗位类别、第33题申请入口、第77题学位类别、第78题证书、第91题职称不宜加为题外硬性要求。第83题缺申诉办法全文时，不得拿退学申诉或离校时限充当处分申诉时限。'
]
PHASES=[
'先冻结本报告所用100条选定session、语料版本和原判定，修正评测口径与统计；将第1、20、35、98题及边界变体设为优先防错用例。阶段结果应是可追溯的正确基线，而非改分数让系统看起来更好。',
'随后修R2和R3，恢复应命中的招生、导师与校级条款，解决培养层次、普通班与创新班、完整方案与分流方案、两年版本配对。小批复核时先检查证据是否正确，再判断生成质量；检索仍错误时不急于换模型。',
'再修R4与R5，补齐材料流程等独立要点，完成课程清单与结构表解析；最后通过R6和R7统一最终答案与证据边界。第39、84题不再前后冲突，完整答复不再带伪课程缺项；合理澄清与合理拒答保持正确。',
'R8与业务修复并行治理运行可靠性。修完后按每批10题重跑全部100题，同时加入同义表达、对象顺序互换、数值阈值、层次班型及版本边界的新题。对原题要核对期望事实，对变体要核对规则；不把“接口返回ANSWERED”当人工验收通过。',
'若正确证据和结构化约束下仍出现语义错误，再用同一证据、同一提示、同一参数和明确样本对不同模型做对照，分别测生成准确性、校验漏放、首次成功率、重试次数与耗时。模型更换是否有效由对照结果决定，不能由本批日志直接断言供应商或某模型全面不可用。'
]

md=[f'# CampusGuide 100题测试逐题诊断与修改建议','', '2026年10月28日  基于已完成测试的离线复核','']
for t in INTRO: md += [t,'']
md += ['## 结果与口径','', '| 指标 | 原报告或原判 | 本次复核 |','|---|---|---|']
md += ['| '+' | '.join(r)+' |' for r in METRICS[1:]];md+=['']
md+=['## 问题集中在哪里','']
for t in CAUSE:md += [t,'']
md+=['## 报告与评分修正','']
for t in ERRATA:md += [t,'']
md+=['## 共性修改与执行顺序','']
for k,m in MODULES.items():
 md += [f"### {k} {m['name']}",'',f"优先级：{m['priority']}。{m['action']}",'',f"修改位置：{'；'.join(m['files'])}。",'',f"验收：{m['test']}",'']
for i,t in enumerate(PHASES,1):md += [f'{i}. {t}','']
md += ['## 100题逐题诊断','', 'P0为明确严重错误应先防错，P1为实质漏答或系统性检索覆盖问题，P2为呈现、状态或评分口径改进；保持表示没有确认的内容错误。以下正确要点是验收锚点，并非未经复测的修复后实际输出。','']
for d in records:
 n=d['case_number'];md += [f"### 第{n:03d}题 {d['question']}",'',f"**判定**：原判{STATUS[d['original_status']]}；建议复核{STATUS[d['recommended_status']]}。优先级{d['priority']}，{d['category']}。",'',f"**错误点或不足**：{d['diagnosis']}",'',f"**应答要点**：{d['answer_requirements']}",'',f"**修改与验收**：{d['change_and_acceptance']}",'',f"**工程关联**：{'、'.join(d['modules'])}。证据强度：{d['evidence_strength']}。",'']
 refs=d['source_references'][:3]
 if refs: md += ['**语料定位**：'+'；'.join(f"{r['article_id']} part {r['part_no']}" for r in refs)+'。','']
 md += [f"**记录定位**：session `{d['session_id']}`；系统状态{d['outcome']}；最终引用{d['citation_count']}条。",'']
md+=['## 资料与限制','', '原始Word报告：docs/reports/CampusGuide_已测100题测试记录_20261028.docx。逐题与session以data/eval/answer_quality/merged_100_20261028/merged_cases.jsonl为准，原始轨迹来自batch_retest_20261028_162015各批次及batch_pending_57_20261028/results.jsonl。语料为data/processed/articles.jsonl。本次复核没有调用外部模型，也没有联网更新任职或活动消息。','', '配套JSONL保留每题原判、复核判、原始答案、引用、session、逐题建议和代码文件，便于其他agent按模块实施与复测。代码定位是建议修改入口；未做修复后的因果实验，不将建议等同已验证修复。','']
(OUT/(STEM+'.md')).write_text('\n'.join(md),encoding='utf-8')
if '--text-only' in __import__('sys').argv:
 print(json.dumps({'markdown':str(OUT/(STEM+'.md')),'records':len(records),'citation_audit_issues':audit_issues},ensure_ascii=False)); raise SystemExit(0)

# DOCX 用连续章节承载长解释，不用满页表格包装叙述。
doc=Document();sec=doc.sections[0];sec.page_width=Inches(8.5);sec.page_height=Inches(11)
sec.top_margin=Inches(.7);sec.bottom_margin=Inches(.65);sec.left_margin=Inches(.78);sec.right_margin=Inches(.78)
sec.header_distance=Inches(.28);sec.footer_distance=Inches(.28)
for style in doc.styles:
 if style.type==1:
  style.font.color.rgb=RGBColor(0,0,0)
  if style.element.rPr is not None:
   for color in style.element.rPr.findall(qn('w:color')):
    for key in ['themeColor','themeTint','themeShade']:color.attrib.pop(qn('w:'+key),None)
 for border in list(style.element.iter(qn('w:pBdr'))):border.getparent().remove(border)
normal=doc.styles['Normal'];normal.font.name='宋体';normal.font.size=Pt(11);normal.element.get_or_add_rPr().get_or_add_rFonts().set(qn('w:eastAsia'),'宋体')
normal.paragraph_format.line_spacing=1.15;normal.paragraph_format.space_after=Pt(5)
for sn,size in [('Title',23),('Heading 1',15),('Heading 2',12),('Heading 3',11)]:
 st=doc.styles[sn];st.font.name='微软雅黑';st.font.size=Pt(size);st.font.bold=True;st.font.color.rgb=RGBColor(0,0,0);st.element.get_or_add_rPr().get_or_add_rFonts().set(qn('w:eastAsia'),'微软雅黑');st.paragraph_format.keep_with_next=True;st.paragraph_format.space_before=Pt(12 if sn!='Title' else 0);st.paragraph_format.space_after=Pt(6)
doc.styles['Subtitle'].font.color.rgb=RGBColor(0,0,0);doc.styles['Subtitle'].font.italic=False;doc.styles['Subtitle'].font.size=Pt(10)
header=sec.header.paragraphs[0];header.text='CampusGuide  100题逐题诊断';header.runs[0].font.size=Pt(9);header.paragraph_format.space_after=Pt(0)
f=sec.footer.paragraphs[0];f.alignment=WD_ALIGN_PARAGRAPH.CENTER
f.add_run('2026年10月28日  ·  第 ').font.size=Pt(9)
fld=OxmlElement('w:fldSimple');fld.set(qn('w:instr'),'PAGE');f._p.append(fld);f.add_run(' 页').font.size=Pt(9)
doc.core_properties.title='CampusGuide 100题测试逐题诊断与修改建议';doc.core_properties.subject='逐题错误点 根因 修改建议 验收条件';doc.core_properties.author='';doc.core_properties.last_modified_by=''
def para(text,label=None):
 p=doc.add_paragraph()
 if label:p.add_run(label).bold=True
 p.add_run(text);return p
def heading(text,level=1):return doc.add_paragraph(text,style=f'Heading {level}')
def table(rows,widths):
 t=doc.add_table(rows=1,cols=len(rows[0]));t.alignment=WD_TABLE_ALIGNMENT.CENTER;t.autofit=False
 for c,w in zip(t.columns,widths):c.width=Inches(w)
 for i,row in enumerate(rows):
  cells=t.rows[0].cells if i==0 else t.add_row().cells
  for j,(cell,txt) in enumerate(zip(cells,row)):
   cell.width=Inches(widths[j]);cell.vertical_alignment=WD_CELL_VERTICAL_ALIGNMENT.CENTER
   tcpr=cell._tc.get_or_add_tcPr();margin=OxmlElement('w:tcMar')
   for edge in ['top','bottom','left','right']:
    el=OxmlElement('w:'+edge);el.set(qn('w:w'),'75');el.set(qn('w:type'),'dxa');margin.append(el)
   tcpr.append(margin)
   borders=OxmlElement('w:tcBorders')
   for edge in ['top','left','bottom','right','insideH','insideV']:
    el=OxmlElement('w:'+edge);el.set(qn('w:val'),'single');el.set(qn('w:sz'),'4');el.set(qn('w:color'),'D9D9D9');borders.append(el)
   tcpr.append(borders)
   p=cell.paragraphs[0];p.paragraph_format.space_after=Pt(2);p.paragraph_format.space_before=Pt(2);p.paragraph_format.line_spacing=1.05
   if j==0:p.alignment=WD_ALIGN_PARAGRAPH.LEFT
   else:p.alignment=WD_ALIGN_PARAGRAPH.CENTER
   r=p.add_run(txt);r.font.size=Pt(10)
   if i==0:
    r.bold=True;sh=OxmlElement('w:shd');sh.set(qn('w:fill'),'E8EFF5');tcpr.append(sh)
  if i==0:
   repeat=OxmlElement('w:tblHeader');t.rows[0]._tr.get_or_add_trPr().append(repeat)
  cant=OxmlElement('w:cantSplit');t.rows[i]._tr.get_or_add_trPr().append(cant)
 doc.add_paragraph().paragraph_format.space_after=Pt(1)
 return t

doc.add_paragraph('CampusGuide 100题测试\n逐题诊断与修改建议',style='Title')
doc.add_paragraph('2026年10月28日  基于已完成测试的离线复核',style='Subtitle')
for t in INTRO:para(t)
heading('结果与口径');table(METRICS,[2.45,2.1,2.39])
heading('问题集中在哪里')
for t in CAUSE:para(t)
heading('报告与评分修正')
for t in ERRATA:para(t)
heading('共性修改与执行顺序')
for k,m in MODULES.items():
 heading(f"{k} {m['name']}",2);para(m['action'],f"{m['priority']}  ");para('；'.join(m['files']), '修改位置  ');para(m['test'],'验收重点  ')
heading('建议执行顺序',2)
for i,t in enumerate(PHASES,1):para(t,f'{i}  ')
heading('100题逐题诊断')
para('P0为明确严重错误应先防错，P1为实质漏答或系统性检索覆盖问题，P2为呈现、状态或评分口径改进；保持表示没有确认的内容错误。应答要点是回归验收锚点，并非未经复测的修复后实际输出。')
for d in records:
 heading(f"第{d['case_number']:03d}题 {d['question']}",2)
 p=para(f"原判{STATUS[d['original_status']]}；建议复核{STATUS[d['recommended_status']]}。{d['priority']}  {d['category']}")
 p.paragraph_format.keep_with_next=True
 para(d['diagnosis'],'错误点或不足  ')
 para(d['answer_requirements'],'应答要点  ')
 para(d['change_and_acceptance'],'修改与验收  ')
 para('、'.join(d['modules'])+'。'+d['evidence_strength']+'。','工程关联  ')
 refs=d['source_references'][:2]
 if refs:
  p=para('；'.join(f"{r['doc_title']} {r['article_no']} 第{r['part_no']}片段" for r in refs),'语料定位  ')
  for r in p.runs:r.font.size=Pt(9)
 p=para(f"session {d['session_id']}；系统{d['outcome']}；最终引用{d['citation_count']}条。",'记录定位  ')
 for r in p.runs:r.font.size=Pt(9)
heading('资料与限制')
para('原始报告：docs/reports/CampusGuide_已测100题测试记录_20261028.docx。选定答案以merged_100_20261028/merged_cases.jsonl为准，轨迹按session对齐，避免把同题早先失败尝试误作最终答案。本地语料为data/processed/articles.jsonl。')
para('配套JSONL保留原判与复核判、原始答案、引用、session及逐题建议，便于其他agent实施。代码位置是待修改入口，尚未进行修复后因果实验或新一轮验收。没有联网更新任职与活动消息，也没有重新发送语料给模型服务。')
# 移除任何可继承的标题段边框，禁止主题色回流。
for border in list(doc.element.iter(qn('w:pBdr'))):border.getparent().remove(border)
doc.save(OUT/(STEM+'.docx'))
assert not audit_issues,audit_issues
assert len(records)==100
print(json.dumps({'files':[str(OUT/(STEM+s)) for s in ['.docx','.md','.jsonl']], 'summary':summary,'priority_counts':dict(collections.Counter(r['priority'] for r in records))},ensure_ascii=False,indent=2))
