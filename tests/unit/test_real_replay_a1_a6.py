"""Replay of the report's real failure samples (报告 A1/A2/A3/A6 acceptance).

Every text here is copied from the historical run: the claim wording from the
recorded answer and the quote from the recorded evidence block. No synthetic
fixture is allowed to stand in for these cases, because the review showed the
synthetic versions passed while the real ones still failed.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from campusguide.agents.question_units import decompose_question
from campusguide.config import load_organization
from campusguide.domain.models import Citation, Claim
from campusguide.qa.merge import merge_claims
from campusguide.retrieval.profile_expand import ProfileLexicon
from campusguide.verification.conditions import check_conditions

ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------- case 001
# Real claim: the answer inverted “开考30分钟后禁止迟到考生入场”.
CASE_001_CLAIM = "开考后30分钟内，监考员禁止迟到的考生入场。"
CASE_001_QUOTE = (
    "开考30 分钟后，监考员禁止迟到的考生入场，再次核对考生证件，并要求考生在“考试座次表”上签名。"
    "开考后30 分钟内，一律不允许考生离开考场。"
)
CASE_001_CORRECT = "学生迟到30分钟者，不得参加该课程考试，并作缺考论。"

# ---------------------------------------------------------------- case 098
# Real page text: three demonstration centres followed by ordinary labs without
# any punctuation between the names.
CASE_098_QUOTE = (
    "基于云平台的计算机实验教学示范中心信息数学与应用统计实验教学示范中心"
    "数学建模实验教学示范中心计算机软件实验室计算机硬件实验室计算机系统实验室"
    "计算机网络实验室云计算与大数据实验室物联网实验室IT项目综合训练实验室"
    "ERP沙盘模拟综合实验室工业工程实验室生产运作与管理实验室、信息技术与管理实验室、"
    "管理系统仿真实验室"
)
CASE_098_BAD = (
    "数学与信息学院的实验教学示范中心包括基于云平台的计算机实验教学示范中心、"
    "信息数学与应用统计实验教学示范中心、数学建模实验教学示范中心、计算机软件实验室、"
    "计算机硬件实验室、计算机系统实验室、计算机网络实验室、云计算与大数据实验室、"
    "物联网实验室、IT项目综合训练实验室、ERP沙盘模拟综合实验室、工业工程实验室、"
    "生产运作与管理实验室、信息技术与管理实验室、管理系统仿真实验室等。"
)
CASE_098_GOOD = (
    "数学与信息学院的实验教学示范中心包括基于云平台的计算机实验教学示范中心、"
    "信息数学与应用统计实验教学示范中心、数学建模实验教学示范中心；"
    "学院另有云计算与大数据实验室。"
)

# ---------------------------------------------------------------- case 035
# Real 2026-09-14 supplement: the exception is a supervisor-affiliation branch,
# without words such as 除外/不适用/例外.
CASE_035_SUPPLEMENT = (
    "根据联培研究生第一导师的人事归属，将科研成果署名单位认定分为以下两类情形：\n"
    "（一）第一导师为我校教师的情形。此类联培研究生的科研成果，第一署名单位必须为“华南农业大学”。\n"
    "（二）第一导师为联合培养单位人员的情形。此类联培研究生的科研成果，实行“双单位认可制”，"
    "其科研成果的第一署名单位可为“华南农业大学”或“联合培养单位”。"
)
CASE_035_BAD = "研究生老生的参评科研成果必须以华南农业大学为第一署名单位。"
CASE_035_BRANCH_SCHOOL = "第一导师为我校教师的联培研究生，第一署名单位必须为华南农业大学。"
CASE_035_BRANCH_JOINT = (
    "第一导师为联合培养单位人员的联培研究生，第一署名单位可为华南农业大学或联合培养单位。"
)

# ---------------------------------------------------------------- case 039/084
CASE_039_MISSING = "微专业完成后的证书情况证据未提及。"
CASE_039_RULE = (
    "微专业完成后的证书情况：学生在校期间完成微专业全部课程，获得规定学分，"
    "经学院审核，报本科生院审定，发放学校统一制作的微专业结业证书。"
)
CASE_084_MISSING = "其余各系主任未列出。"
CASE_084_RULE = "作物科学技术系主任为张艳霞，副主任为连腾祥。"

# ---------------------------------------------------------------- case 041
CASE_041_QUESTION = (
    "2026级农业工程中农业电气化与自动化方向，与2025—2026级工业工程与管理专业的"
    "培养目标和课程设置有什么区别？"
)


def _claim(claim_id: str, text: str, quote: str, title: str) -> Claim:
    return Claim(
        claim_id=claim_id,
        text=text,
        citations=[
            Citation(
                article_id=f"{title}#1",
                quote=quote,
                quote_start=0,
                quote_end=len(quote),
                doc_title=title,
                article_no="第一条",
                effective_from=date(2025, 9, 1),
                source_url="https://www.scau.edu.cn/x.htm",
            )
        ],
    )


def test_case_001_real_claim_is_rejected() -> None:
    findings = check_conditions(CASE_001_CLAIM, [CASE_001_QUOTE])
    assert [finding.kind for finding in findings] == ["threshold_direction"]
    assert findings[0].blocking is True


def test_case_001_correct_claim_passes() -> None:
    assert check_conditions(CASE_001_CORRECT, [CASE_001_QUOTE]) == []


def test_case_098_real_page_rejects_the_enumerated_labs() -> None:
    findings = check_conditions(CASE_098_BAD, [CASE_098_QUOTE])
    assert [finding.kind for finding in findings] == ["category"]


def test_case_098_correct_three_centres_are_accepted() -> None:
    assert check_conditions(CASE_098_GOOD, [CASE_098_QUOTE]) == []


def test_case_098_real_text_with_normal_punctuation_also_rejects() -> None:
    punctuated = (
        "基于云平台的计算机实验教学示范中心，信息数学与应用统计实验教学示范中心，"
        "数学建模实验教学示范中心，计算机软件实验室，云计算与大数据实验室"
    )
    findings = check_conditions(CASE_098_BAD, [punctuated])
    assert [finding.kind for finding in findings] == ["category"]


def test_case_035_real_supplement_blocks_the_universal_rule() -> None:
    findings = check_conditions(CASE_035_BAD, [CASE_035_SUPPLEMENT])
    assert [finding.kind for finding in findings] == ["exception_dropped"]
    assert findings[0].blocking is True


def test_case_035_correct_branches_pass() -> None:
    assert check_conditions(CASE_035_BRANCH_SCHOOL, [CASE_035_SUPPLEMENT]) == []
    assert check_conditions(CASE_035_BRANCH_JOINT, [CASE_035_SUPPLEMENT]) == []


def test_case_035_unknown_supervisor_affiliation_is_blocked() -> None:
    claim = "联培研究生的科研成果第一署名单位必须为华南农业大学。"
    assert [finding.kind for finding in check_conditions(claim, [CASE_035_SUPPLEMENT])] == [
        "exception_dropped"
    ]


def test_case_039_real_claims_no_longer_contradict() -> None:
    result = merge_claims(
        [
            _claim("c1", CASE_039_MISSING, "微专业办法", "华南农业大学微专业建设管理办法（试行）"),
            _claim("c2", CASE_039_RULE, "微专业办法", "华南农业大学微专业建设管理办法（试行）"),
        ]
    )
    assert all("未提及" not in claim.text for claim in result.claims)
    assert any(note.kind == "stale_missing" for note in result.notes)


def test_case_084_real_claims_no_longer_contradict() -> None:
    for pair in (
        [CASE_084_MISSING, CASE_084_RULE],
        [CASE_084_RULE, CASE_084_MISSING],
    ):
        result = merge_claims(
            [
                _claim("c1", pair[0], "农学院师资", "农学院师资队伍"),
                _claim("c2", pair[1], "农学院师资", "农学院师资队伍"),
            ]
        )
        assert all("未列出" not in claim.text for claim in result.claims)


def test_case_041_units_keep_both_objects_and_their_ranges() -> None:
    org = load_organization(ROOT / "configs/organization.yaml")
    lexicon = ProfileLexicon.from_organization(org)
    units = decompose_question(CASE_041_QUESTION, lexicon)
    objects = {unit.subject for unit in units}
    assert objects == {"农业工程", "工业工程与管理"}
    aspects = {unit.requested_aspects[0] for unit in units}
    assert aspects == {"培养目标", "课程设置"}
    versions = {unit.version for unit in units}
    assert versions == {"2026", "2025-2026"}
    assert all("定义区别" not in unit.requested_aspects for unit in units)