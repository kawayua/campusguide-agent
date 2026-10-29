"""Build evidence-based report for the 100 live UI callback cases (no app changes)."""

from __future__ import annotations

import ast
import hashlib
import html
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/eval/corpus_random_100_live"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def lines(path):
    return [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines() if s.strip()]


def dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def code(path, line):
    return f"[{path}:{line}]({ROOT.as_posix()}/{path}:{line})"


def pct(v, n=100):
    return f"{100 * v / n:.1f}%"


def short(s, n=360):
    s = re.sub(r"\s+", " ", s).strip()
    return s[:n] + ("…" if len(s) > n else "")


def bigrams(s):
    s = re.sub(r"[^\w\u4e00-\u9fff]", "", s.lower())
    return {s[i : i + 2] for i in range(len(s) - 1)}


def main():
    cases = lines(ROOT / "data/eval/corpus_random_100_questions.jsonl")
    results = lines(OUT / "results.jsonl")
    assert len(cases) == len(results) == len({r["id"] for r in results}) == 100
    assert [c["id"] for c in cases] == [r["id"] for r in results]
    articles = lines(ROOT / "data/processed/articles.jsonl")
    bydoc = defaultdict(list)
    for a in articles:
        bydoc[(a["doc_id"], a["version_id"])].append(a)
    ids = {a["article_id"] for a in articles}
    risks = load(OUT / "static_section_risks.json")
    riskmap = {r["id"]: r for r in risks}
    traces = lines(ROOT / "data/traces/2026-10-27.jsonl")
    by_session = {t["session_id"]: t for t in traces if t.get("session_id")}
    matched = [by_session[r["session_id"]] for r in results if r.get("session_id") in by_session]
    assert len(matched) == 100
    (OUT / "matched_server_traces.jsonl").write_text(
        "\n".join(json.dumps(t, ensure_ascii=False) for t in matched) + "\n", encoding="utf-8"
    )
    debug = html.unescape(str(results[0]["raw_outputs"][8]))
    dl = dict(re.findall(r"<dt>(.*?)</dt><dd>(.*?)</dd>", debug, re.S))
    model_metadata = {
        key: dl.get(key)
        for key in ["model_profile", "models", "prompts", "index_version", "graph_version"]
    }
    diagnostics = {
        "qdrant_collection": httpx.get(
            "http://127.0.0.1:6333/collections/articles_v1", timeout=15
        ).json(),
        "models_from_live_ui": model_metadata,
        "text_manifest": load(ROOT / "data/manifest.json"),
        "vector_manifest": load(ROOT / "data/vector_manifest.json"),
        "code_hashes_collected_after_run": {
            p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
            for p in [
                "src/campusguide/agents/graph.py",
                "src/campusguide/agents/planner.py",
                "src/campusguide/agents/retriever.py",
                "src/campusguide/qa/guard.py",
                "src/campusguide/retrieval/section_select.py",
                "src/campusguide/mcp/policy_search_server.py",
                "configs/flows/scholarship-application.yaml",
            ]
        },
        "process_observation": "UI is local Windows Python campusguide.ui.app with policy_search_server and policy_meta_server children; docker compose app logs empty. No restart performed.",
    }
    dump(OUT / "environment_snapshot.json", diagnostics)
    custom = {
        18: (
            "误澄清",
            "领导分工应直接回答：王少奎分管本科教学；谢庆军分管研究生；梁志辉分管实验教学中心及实验室安全。无需询问提问人的学历。",
            "Planner 对同时出现本科、研究生及分别的公开事实问题触发 AUDIENCE_UNKNOWN。",
        ),
        35: (
            "误拒答",
            "应按现行 v2026 奖学金办法和2026年9月14日补充说明解释成果计分、学术规范及联培成果署名认定；不得仅凭2026年与办法的字面组合拒答。",
            "历史正则命中“2026年研究生奖学金评选办法”，未检查是否为当年现行文件。",
        ),
        54: (
            "核心正确",
            "申请材料：《国家奖学金申请审批表》、学习成绩与综合测评证明、个人事迹材料及学院要求的其他材料；截止2026-10-10 09:00。",
            "最终可见答案覆盖材料及截止时间。但混合查询的检索仍失败，仅由预置流程输出；不能据此认定检索或语义核对通过。",
        ),
        55: (
            "误澄清",
            "应分别说明本科生办理渠道、行政楼305及研究生院培养科行政楼309；本科人工办理收费按在校生5份内免费、超出每份5元，毕业生每份15元。研究生费用另询培养科，自助打印未明确的收费不可擅自套用。",
            "跨人群比较被强制要求选择单一用户身份，没有分别作答。",
        ),
        57: (
            "答非所问",
            "应回答袁隆平追梦奖学金拟推荐人选公示消息发布日期为2026-10-09；该日期是消息发布日期，不推断实际公示起止日期。",
            "“奖学金”通用别名匹配国家奖助学金流程；“何时发布”未被识别为事实查询，于是返回无关国家奖助学金办理清单。",
        ),
        89: (
            "部分正确",
            "应分别介绍国家助学金受助人选拟推荐名单公示（2026-10-21至10-22）和国家励志奖学金拟获奖人选公示（2026-10-23至10-24）；不输出个人名单。",
            "把两类奖助项目问题路由为单个申请流程，award_type只保留inspirational，遗漏国家助学金公示且缺少完整公示期。",
        ),
    }
    review = []
    detail = [
        "# 100 道问题逐题结果与证据对照",
        "",
        "每题均为通过正在运行的 Gradio `/on_send` 回调取得的实际最终输出。每题使用独立对话，不代答澄清、不自动重试。",
        "",
        "“语料核对片段”为在题库预先指定的文档与版本范围内，按问题与片段字符重合选取的参考定位材料；未经逐条标注为完整标准答案，不能当成 gold recall 或正确率。94道运行阻断题不判定内容真假。",
        "",
    ]
    for c, r in zip(cases, results, strict=True):
        n = int(c["id"].split("_")[-1])
        manual = custom.get(n)
        if manual:
            status, expected, analysis = manual
        else:
            assert r["outcome"] == "INDEX_UNAVAILABLE"
            status = "运行阻断"
            expected = "依据题目所指文档和适用版本回答全部子问题；证据缺少或统计口径不明确时，应说明证据边界。"
            analysis = "检索节点 evidence=0，warnings含INDEX_UNAVAILABLE，未进入Advisor/Verifier。不能把本次运行故障归为知识库不存在答案或模型事实错误。"
        if c.get("evidence_note"):
            expected += " " + c["evidence_note"]
        candidates = []
        qgrams = bigrams(c["question"])
        for ref in c["source_documents"]:
            aset = bydoc[(ref["doc_id"], ref["version_id"])]
            ranked = sorted(
                aset,
                key=lambda a: (
                    len(qgrams & bigrams(a["text"])) / math.sqrt(max(1, len(bigrams(a["text"]))))
                ),
                reverse=True,
            )
            for a in ranked[:2]:
                candidates.append(
                    {
                        "article_id": a["article_id"],
                        "part_no": a.get("part_no", 1),
                        "doc_id": a["doc_id"],
                        "version_id": a["version_id"],
                        "block_type": a["block_type"],
                        "excerpt": short(a["text"], 450),
                    }
                )
        rr = {
            "id": r["id"],
            "question": r["question"],
            "category": r["category"],
            "system_outcome": r["outcome"],
            "review_status": status,
            "expected_behavior": expected,
            "analysis": analysis,
            "section_filter_risk": riskmap.get(r["id"]),
            "reference_evidence_candidates": candidates,
            "elapsed_s": r["elapsed_s"],
            "answer": r["answer"],
            "source_documents": c["source_documents"],
        }
        review.append(rr)
        detail += [
            f"## {n:03d} · {status}",
            "",
            c["question"],
            "",
            f"分类：{c['category']}；系统状态：`{r['outcome']}`；耗时：{r['elapsed_s']:.3f} 秒。",
            "",
            f"**期望与证据边界：** {expected}",
            "",
            f"**判断：** {analysis}",
            "",
        ]
        if r["id"] in riskmap:
            detail += [
                "**额外静态风险：** 当前工作区默认检索仅允许 article，而本题部分预期文档只有 section。此风险独立于本轮索引故障，不计作已实测的第二次检索失败。",
                "",
            ]
        detail += ["**实际最终答案：**", "", r["answer"], "", "**参考语料片段：**", ""]
        for a in candidates:
            detail += [
                f"- `{a['article_id']}`，part_no={a['part_no']}，类型={a['block_type']}：{a['excerpt']}"
            ]
        detail += [
            "",
            f"完整结构化输出见 results.jsonl 中 `{r['id']}`；服务端轨迹见 matched_server_traces.jsonl 中 session_id=`{r['session_id']}`。",
            "",
        ]
    (OUT / "case_details.md").write_text("\n".join(detail), encoding="utf-8")
    (OUT / "review.jsonl").write_text(
        "\n".join(json.dumps(v, ensure_ascii=False) for v in review) + "\n", encoding="utf-8"
    )
    counts = Counter(r["outcome"] for r in results)
    quality = Counter(r["review_status"] for r in review)
    nodes = Counter(t["node"] for r in results for t in r["reply"].get("trace", []))
    lat = sorted(r["elapsed_s"] for r in results)
    start = datetime.fromisoformat(results[0]["started_at"])
    end = datetime.fromisoformat(load(OUT / "progress.json")["updated_at"])
    checklist_refs = [
        v
        for r in results
        for step in r["reply"].get("checklist", [])
        for v in step.get("citations", [])
    ]
    citation_audit = {
        "top_level_claim_citations": sum(len(r["reply"].get("citations", [])) for r in results),
        "checklist_reference_mentions": len(checklist_refs),
        "invalid_checklist_reference_ids": sorted({v for v in checklist_refs if v not in ids}),
        "note": "流程中可能包含不适用或未显示的步骤；引用ID存在不等于支持具体回答，也不表示运行时已水合证据。",
    }
    summary = {
        "completed": 100,
        "outcomes": dict(counts),
        "review_statuses": dict(quality),
        "latency_s": {
            "p50": statistics.median(lat),
            "p95_nearest_rank": lat[math.ceil(0.95 * len(lat)) - 1],
            "max": max(lat),
            "sum": sum(lat),
            "wall_clock": (end - start).total_seconds(),
        },
        "node_visits": dict(nodes),
        "section_filter_risk_cases": len(risks),
        "matched_server_traces": len(matched),
        "citation_audit": citation_audit,
        "started_at": start.isoformat(),
        "completed_at": end.isoformat(),
        "accuracy_note": "运行阻断94题；不报告正常服务条件下答案准确率。6道非INDEX最终输出逐题核对，其中1题核心正确，1题部分正确，1题答非所问，2题误澄清，1题误拒答。",
    }
    dump(OUT / "summary.json", summary)
    report = [
        "# 知识库100题真实服务测试报告",
        "",
        f"测试日期：2026-10-27（Asia/Shanghai）；运行时间：{start.strftime('%H:%M:%S')}—{end.strftime('%H:%M:%S')}。",
        "",
        "100题已逐条完成。当前服务首先存在检索链路故障：94题最终返回 INDEX_UNAVAILABLE；其余6题中，1题核心内容正确、1题部分正确、1题答非所问、2题错误澄清、1题把现行办法误判为历史版本。**目前不宜把本轮作为模型正常问答准确率评测，也不能把系统3次ANSWERED直接算成3题答对。**",
        "",
        "## 测试方法与范围",
        "",
        "通过 `gradio_client` 请求已经运行的 `http://127.0.0.1:7860/` 的 `/on_send`，与网页输入提交调用相同的UI回调、会话处理及真实后端。严格按题库顺序串行，每题传入空BrowserState建立独立对话；只记录首轮最终回答，不补充用户身份、不改写问题、不自动重试。未用fake答案、未替换检索后端；本轮不包含浏览器DOM点击、视觉布局或多轮完成率测试。",
        "",
        "题库为本轮之前生成的100题，含126份不同文档的来源映射、15题近期活动与消息。它是探索性题集，没有事先人工冻结的完整标准答案；报告对可见的6道非基础设施输出作语料核对，94道基础设施故障单列。近期目录语料只保存标题、发布日期与链接，不能据此推断活动举办时间或正文细节。",
        "",
        f"页面调试信息：model_profile=`{model_metadata['model_profile']}`；模型配置为 `{model_metadata['models']}`；索引版本 `{model_metadata['index_version']}`。这记录的是配置，不代表Advisor/Verifier实际被调用。",
        "",
        "本次只新增测试脚本与结果文件，未修改业务源码、配置、语料或向量索引，未重启正在运行的网页服务。工作区此前已有 aliases.yaml、graph.py、section_select.py 及相关单元测试改动，本次保留。代码定位与静态风险以报告生成时工作区为准，当前进程在启动时加载的代码可能早于这些改动。",
        "",
        "## 结果统计",
        "",
        "| 指标 | 结果 |",
        "|---|---:|",
        "| 完成请求 | 100/100 |",
        f"| 最终INDEX_UNAVAILABLE | {counts['INDEX_UNAVAILABLE']} |",
        f"| 最终CLARIFY | {counts['CLARIFY']} |",
        f"| 最终HISTORICAL_NOT_SUPPORTED | {counts['HISTORICAL_NOT_SUPPORTED']} |",
        f"| 系统标记ANSWERED | {counts['ANSWERED']} |",
        f"| 人工核对核心正确 | {quality['核心正确']} |",
        f"| 人工核对部分正确 | {quality['部分正确']} |",
        f"| 人工核对答非所问 | {quality['答非所问']} |",
        f"| 请求耗时p50 / p95 / 最大值 | {summary['latency_s']['p50']:.2f}s / {summary['latency_s']['p95_nearest_rank']:.2f}s / {summary['latency_s']['max']:.2f}s |",
        f"| 请求耗时累计 / 测试墙钟时长 | {sum(lat):.2f}s / {(end - start).total_seconds():.2f}s |",
        f"| 进入Retriever / Advisor / Verifier的题数 | {nodes['retriever']} / {nodes['advisor']} / {nodes['verifier']} |",
        "",
        "p95采用nearest-rank。耗时包含Gradio排队、回调与客户端开销；大量请求在生成前终止，因此这些延迟不能用作正常回答速度指标。95次Retriever均未返回有效证据，其中1次出现在最终仍标为ANSWERED的混合流程题054。",
        "",
        "| 题目范围 | 总题数 | 索引失败 | 其他结果 |",
        "|---|---:|---:|---|",
    ]
    for category in dict.fromkeys(c["category"] for c in cases):
        subset = [r for r in results if r["category"] == category]
        ct = Counter(r["outcome"] for r in subset)
        other = "、".join(f"{k} {v}" for k, v in ct.items() if k != "INDEX_UNAVAILABLE") or "无"
        report.append(f"| {category} | {len(subset)} | {ct['INDEX_UNAVAILABLE']} | {other} |")
    report += [
        "",
        "## 发现与原因定位",
        "",
        "### P0：正在运行的检索链路持续失败，尚不能确认具体底层异常",
        "",
        "94道最终失败题均呈现 Planner → Retriever → Finalize，evidence=0，warnings含 qN:INDEX_UNAVAILABLE，未进入Advisor或Verifier。额外的054也有同类检索失败，但流程清单仍令最终状态成为ANSWERED。",
        "",
        "独立只读诊断取得的反证：Qdrant返回HTTP 200，articles_v1为green、4,026点；所有4,026个payload可被当前Article模型正常还原，其中article 1,366、section 2,660。独立创建同配置检索服务可以完成构建、本地模型预热与一次真实检索，返回ok=true和相同index_version。这排除了“本机Qdrant完全没启动”“集合没构建”“当前数据全部不可解析”作为统一解释；不证明网页常驻MCP进程也连接到了同一目标、具有相同环境或处于正常状态。该独立查询只用于可用性诊断，不计入100题正确率，也未验证其返回结果能正确回答院长问题。",
        "",
        f"错误传播定位：{code('src/campusguide/mcp/policy_search_server.py', 145)} 将RetrievalError等映射为INDEX_UNAVAILABLE；{code('src/campusguide/agents/retriever.py', 232)} 对全部查询失败只保留query_id与错误码，未将原始异常消息写入本轮trace。页面据此统一提示“Qdrant未启动或索引未构建”，具体原因不足。",
        "",
        "已确认的是网页当前检索路径失败；尚未确认的是常驻MCP原始异常及具体故障点。应用实际运行在Windows本地Python进程，docker compose的app日志为空。应首先读取该应用启动终端的policy-search stderr，核对其Qdrant连接目标、工作目录、环境、模型状态与异常堆栈，再决定是否重启或修复；本轮没有进行这些修改。证据见diagnostic_backend.json、diagnostic_payloads.json和matched_server_traces.jsonl。",
        "",
        "### P1：事实型新闻问题被“奖学金”通用别名误路由",
        "",
        "**057题**问“袁隆平追梦奖学金公示何时发布”，语料明确登记2026-10-09，实际却输出本科生国家奖助学金申请清单，没有回答日期。",
        "",
        f"{code('configs/flows/scholarship-application.yaml', 14)} 存在通用别名“奖学金”；{code('src/campusguide/flows/registry.py', 84)} 按子串匹配流程；{code('src/campusguide/agents/planner.py', 40)} 的事实词规则不包含“何时发布”；{code('src/campusguide/agents/planner.py', 437)} 在命中流程且没有识别到事实意图时默认走流程。diagnostic_rules.json复现procedure_flag=false、fact_flag=false而命中国家奖助学金流程。",
        "",
        "### P1：同一问题含两类奖助项目时只保留一个槽位",
        "",
        "**089题**要求分别介绍国家助学金和国家励志奖学金公示，实际只保留励志奖学金相关清单，漏掉助学金。语料对应公示期分别为2026-10-21—10-22与2026-10-23—10-24。",
        "",
        f"此问同样被视为流程；award_type是单值enum，{code('src/campusguide/flows/slots.py', 117)} 收集命中的别名后按长度排序只取一个，独立复现抽取值为inspirational。多项目事实咨询缺乏逐项拆分，既造成遗漏，也引入大量无关办理信息。",
        "",
        "### P1：公开事实与跨人群比较被无条件追问个人身份",
        "",
        "**018题**问三位领导分工，**055题**要求分别说明本科与研究生证明办理，两题均只得到“你是本科生还是研究生”。018的正确分工可直接从agri-leadership读到：王少奎负责本科教学、谢庆军负责研究生、梁志辉负责实验室安全。055本应分别解释两个对象的规则。",
        "",
        f"{code('src/campusguide/agents/planner.py', 640)} 只要同时出现“本科/研究生”及“分别/区别/不同/差异”，在无个人profile时就返回AUDIENCE_UNKNOWN；未区分公开事实查询、多对象比较和个人办理咨询。两题在进入检索前即被截断，diagnostic_rules.json可独立复现。",
        "",
        "### P1：带当年年份的现行办法被历史版本正则误拒",
        "",
        "**035题**查询2026年研究生奖学金评选办法和补充说明，却得到HISTORICAL_NOT_SUPPORTED。两份预期文档在语料中均存在现行v2026。",
        "",
        f"{code('src/campusguide/qa/guard.py', 17)} 将“年份+年+最多8字符+办法/政策/规定/版”等全部视为历史意图，实际命中完整片段“2026年研究生奖学金评选办法”，没有结合当前年份、版本状态或比较意图判断。此问题在Planner guard阶段发生，与Qdrant不可用无关。",
        "",
        "### P1：默认仅查article会排除教师、学院与活动语料（静态确认）",
        "",
        f"对100题按当前工作区 `_default_block_types` 与预先映射的语料类型交叉检查，**{len(risks)}题**存在：至少一份预期文档只有section，但默认检索仅允许article。{code('src/campusguide/agents/graph.py', 88)} 仅对allows_sections命中问题加入section；{code('src/campusguide/retrieval/section_select.py', 21)} 主要识别培养方案、课程、学分等词，没有覆盖师资、院长、学院简介和大多数活动新闻意图。",
        "",
        "典型包括“农学院院长是谁”“梁云属于哪个系”“近期论坛何时发布”。这是一项代码和语料元数据交叉验证出的潜在召回阻断，不计作本轮已经复现的独立线上检索错误；当前基础设施故障优先发生，无法用本轮结果量化其实际影响。完整题目及被排除文档见static_section_risks.json。",
        "",
        "### P2：ANSWERED状态不能代表题意已完整满足，混合流程还可能掩盖检索失败",
        "",
        "**054题**材料和截止时间与语料一致，记核心正确；但其检索evidence=0，Advisor/Verifier未运行，答案来自确定性流程。057、089也未运行这两个节点，却都标ANSWERED。",
        "",
        f"{code('src/campusguide/agents/graph.py', 632)} 对已有flow/checklist的结果以缺槽及closed状态决定ANSWERED或PARTIAL_ANSWER，未以原问题事实覆盖完整性决定。建议后续把“流程可渲染”“检索成功”“问题已完整答复”分别判断；本报告没有修改实现。",
        "",
        "## 逐题记录与复核边界",
        "",
        "完整的100题问题、实际最终答案、人工判断、预期行为、参考语料片段和session_id见[case_details.md](case_details.md)；review.jsonl为同内容的结构化版本。results.jsonl保留全部原始Gradio输出（含BrowserState、答案、引用、清单及调试HTML），matched_server_traces.jsonl保留按本轮session_id筛选的服务端轨迹，避免混入其他用户或历史测试。",
        "",
        f"本轮最终答案的顶层claim citations共{citation_audit['top_level_claim_citations']}条；流程checklist中引用ID提及{citation_audit['checklist_reference_mentions']}次，其中不存在于语料的ID为{len(citation_audit['invalid_checklist_reference_ids'])}个。这只能说明预置流程引用ID存在，不表示引用已被线上水合、更不表示答案语义受到证据支持；不报告100%引用支持率。",
        "",
        "“农学院有多少个实验室”这题的语料主要列举重点平台，未提供所有实验室的完整总数；恢复检索后应核对统计口径并在不足时明确说明，不能强行列为必须给出数字的可答题。跨年份培养方案题也应先核对业务是否支持并列现行入学年级，不能仅因年份不同就默认历史文件已废止。",
        "",
        "## 后续处理顺序（仅建议，尚未修改）",
        "",
        "先取得网页常驻policy-search原始异常，恢复运行链路并用本轮同一题库复测；随后检查section范围、事实/流程意图和多项目拆解，再调整身份澄清与历史正则。保留本轮原始结果作为故障基线，新一轮写入不同目录；至少对100题逐项标注标准事实或可接受拒答条件后，再统计完整正确率、部分正确率、召回与引用支持。",
        "",
        "本轮未覆盖正常生成、语义核对质量和多轮办理完成率，因为没有任何题进入Advisor或Verifier。不能据此评价模型本身好坏，也不能根据失败拒答数量推断知识库覆盖率。",
        "",
        "## 复现与产物",
        "",
        "运行环境使用项目现有.venv。可在仓库根目录执行：",
        "",
        "```powershell",
        r".venv\Scripts\python.exe scripts/eval_corpus_random_100_live.py",
        r".venv\Scripts\python.exe scripts/report_corpus_random_100_live.py",
        "```",
        "",
        "测试脚本遇到已完成id会跳过，不重复消耗请求；如需新一轮应先改用独立输出目录。diagnose_corpus_random_100.py为独立服务诊断脚本；原诊断在HF_HUB_OFFLINE=1及TRANSFORMERS_OFFLINE=1下执行，只读取现有本地模型缓存。报告脚本只读取结果、语料和只读Qdrant元信息。",
        "",
        "本报告、逐题详单、summary.json、review.jsonl、运行配置、源代码哈希和诊断结果均在本目录。所有100条结果已检查唯一ID、与输入顺序一致、服务端trace匹配及对应源文档存在。",
    ]
    (OUT / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    for path in [
        ROOT / "scripts/eval_corpus_random_100_live.py",
        ROOT / "scripts/diagnose_corpus_random_100.py",
        Path(__file__),
    ]:
        ast.parse(path.read_text(encoding="utf-8-sig"))
    assert (OUT / "case_details.md").read_text(encoding="utf-8").count("\n## ") == 100
    assert not citation_audit["invalid_checklist_reference_ids"]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("REPORT", str((OUT / "report.md").resolve()))
    print("DETAIL_BYTES", (OUT / "case_details.md").stat().st_size)


if __name__ == "__main__":
    main()
