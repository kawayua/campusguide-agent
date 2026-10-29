"""Apply the 2026-10-28 100-case diagnosis (R9) to the frozen gold set.

Rules from the report:
  * separate necessary checks from optional ones; optional items never fail a case;
  * correct two wrong reference expectations (014 approval unit, 041 comparison object);
  * keep the original verdict and store the re-review under review_v2 (never overwrite history);
  * the disputed 061/065/077/078 verdicts are revised only in review_v2.

Run once; the script is idempotent for the listed cases.
"""

from __future__ import annotations

import hashlib
import io
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
GOLD = ROOT / "data/eval/answer_quality/gold_v1.jsonl"
BACKUP = ROOT / "data/eval/answer_quality/frozen_100_20261028/gold_v1.pre_r9.jsonl"
SOURCE = "2026-10-28 逐题诊断报告 R9"

# case -> (necessary checks, optional checks, review_v2 status, note)
PATCHES: dict[int, dict] = {
    12: {
        "required": ["每周原则不超过8小时", "每月原则不超过40小时", "岗位申请程序"],
        "optional": ["岗位类别（学校文件未分类时可略）"],
        "note": "岗位类别非本题必答；8/40小时与申请程序为必答。",
    },
    14: {
        "required": [
            "教学单位提出调停补课申请",
            "主管教学负责人签署意见",
            "本科生院教学运行科审批",
            "获批后通知学生",
        ],
        "optional": [],
        "note": "审批对象按原文改为本科生院教学运行科，原“教学院长审批”为参考错误。",
    },
    33: {
        "required": ["学校临时困难补助适用情形", "学院帮扶或助学金措施"],
        "optional": ["具体申请入口或表单（语料未给时从宽）"],
        "note": "申请入口不作硬性要求，只要求说明适用情形与学院措施。",
    },
    41: {
        "required": [
            "2026级农业工程（农业电气化与自动化方向）培养目标与课程设置",
            "2025—2026级工业工程与管理培养目标与课程设置",
            "两者培养目标与课程设置的对比",
        ],
        "optional": [],
        "note": "比较对象按题面改为工业工程与管理，原参考误写软件工程学硕。",
    },
    61: {
        "required": [
            "计算机科学与技术学硕研究方向",
            "计算机技术专硕研究方向",
            "两类硕士的教学实践与专业实践要求",
        ],
        "optional": [],
        "status": "partial",
        "note": "研究方向已有实质回答，实践要求缺失，应为部分回答而非有据未答。",
    },
    65: {
        "required": ["2022版毕业总学分", "2024版毕业总学分", "两版核心课程集合及变化"],
        "optional": ["显式作差句式"],
        "status": "complete",
        "note": "两版总分与核心课均已给出，未显式作差不构成漏答。",
    },
    77: {
        "required": [
            "2024级数据科学与大数据技术培养目标与核心课程",
            "2024级大数据管理与应用培养目标与核心课程",
        ],
        "optional": ["技术与管理侧重对比", "学位类别"],
        "status": "complete",
        "note": "两个专业的培养目标与核心课均已答出；侧重对比与学位类别为题外要求。",
    },
    78: {
        "required": ["辅修专业申请资格及最低18学分", "辅修学士学位申请资格及最低36学分"],
        "optional": ["毕业证书与学位证书区别"],
        "status": "complete",
        "note": "题面未要求证书，条件与18/36学分差别已答出。",
    },
    83: {
        "required": ["处分办法第二十三条、第二十八条要求决定书载明申诉途径与期限"],
        "optional": ["完整申诉操作程序（申诉处理办法未收录时应披露边界）"],
        "note": "完整程序属可选项；只援引办法名而不展开仍是实质缺口，不得编造时限。",
    },
    91: {
        "required": ["梁云属于数学与信息学院", "梁云属于计算机科学与工程系"],
        "optional": ["名册列为教授"],
        "note": "职称非本题所问。",
    },
}


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    if not BACKUP.exists():
        BACKUP.write_bytes(GOLD.read_bytes())
    before = sha256(BACKUP)
    rows = [json.loads(line) for line in GOLD.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    changed = []
    for row in rows:
        number = int(row["id"].rsplit("_", 1)[-1])
        patch = PATCHES.get(number)
        if patch is None:
            continue
        row["required_checks"] = patch["required"]
        row["optional_checks"] = patch["optional"]
        row.setdefault("review_history", []).append(
            {
                "date": "2026-10-28",
                "source": SOURCE,
                "previous_required_checks": row.get("baseline_required_checks")
                or patch.get("previous_required"),
                "previous_baseline_review": row.get("baseline_review"),
            }
        )
        if patch.get("status"):
            row["review_v2"] = {
                "status": patch["status"],
                "note": patch["note"],
                "source": SOURCE,
                "date": "2026-10-28",
                "does_not_overwrite": "baseline_review",
            }
        elif patch.get("note"):
            row.setdefault("review_notes", []).append(
                {"date": "2026-10-28", "source": SOURCE, "note": patch["note"]}
            )
        changed.append(number)
    GOLD.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "changed": sorted(changed),
                "sha256_before": before,
                "sha256_after": sha256(GOLD),
                "backup": str(BACKUP),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()