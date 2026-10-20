from __future__ import annotations

import json
from pathlib import Path

import pymupdf

from campusguide.ingest.parse import parse_corpus, parse_html_file, parse_pdf_file


def test_parse_html_extracts_content_and_table(repo_root: Path) -> None:
    fixture = repo_root / "tests" / "fixtures" / "html" / "sample_policy.html"
    parsed = parse_html_file(fixture, title_hint="fallback")

    assert parsed.title == "华南农业大学本科学生学籍管理实施细则"
    assert "第一条" in parsed.text
    assert "第三条" in parsed.text
    assert "转专业申请表 | 1" in parsed.text
    assert "网站首页" not in parsed.text
    assert "分享到微信" not in parsed.text
    assert "版权所有" not in parsed.text
    assert len(parsed.tables) == 1


def test_parse_html_keeps_inline_numbers_and_heading_spacing(tmp_path: Path) -> None:
    path = tmp_path / "inline-spans.html"
    path.write_text(
        """
<html><body><div class="wp_articlecontent">
  <p><span>第一条</span><span>依据教育部令第</span><span>41</span><span>号制定。</span></p>
  <p><span>华南农办〔</span><span>2021</span><span>〕</span><span>68</span><span>号</span></p>
  <p><span>第二章</span><span>总</span><span>则</span></p>
  <p>2015</p>
</div></body></html>
""",
        encoding="utf-8",
    )
    parsed = parse_html_file(path)

    assert "第一条 依据教育部令第41号制定。" in parsed.text
    assert "华南农办〔2021〕68号" in parsed.text
    assert "第二章 总则" in parsed.text
    assert "\n2015\n" in f"\n{parsed.text}\n"


def test_parse_html_unwraps_layout_table_and_keeps_articles(tmp_path: Path) -> None:
    path = tmp_path / "layout-table.html"
    path.write_text(
        """
<html><body><div class="wp_articlecontent"><table><tr><td>
  <p>华南农业大学某管理办法</p>
  <p>第一章 总则</p>
  <p>第一条 为规范管理，制定本办法。</p>
  <p>第二条 本办法适用于全体学生。</p>
</td></tr></table></div></body></html>
""",
        encoding="utf-8",
    )
    parsed = parse_html_file(path)

    assert "第一条 为规范管理" in parsed.text
    assert "第二条 本办法适用于全体学生" in parsed.text
    assert parsed.text.count("\n") >= 3
    assert any("布局表格" in warning for warning in parsed.warnings)


def test_parse_html_falls_back_to_body_with_warning(tmp_path: Path) -> None:
    path = tmp_path / "plain.html"
    path.write_text(
        "<html><body><h1>某制度</h1><p>第一条 " + "正文内容。" * 40 + "</p></body></html>",
        encoding="utf-8",
    )
    parsed = parse_html_file(path)
    assert "第一条" in parsed.text
    assert any("回退到 body" in warning for warning in parsed.warnings)


def _make_pdf(path: Path) -> None:
    document = pymupdf.open()
    pages = [
        ["第一章 总则", "第一条 为规范本科学生学籍管理，制定本细则。"],
        ["第二章 学籍异动", "第二条 学生申请转专业，应当符合学校规定。"],
        ["第三章 附则", "第三条 本细则由教务处负责解释。"],
    ]
    for index, lines in enumerate(pages, start=1):
        page = document.new_page()
        page.insert_text(
            (72, 50),
            "华南农业大学本科学生学籍管理实施细则",
            fontname="china-s",
            fontsize=10,
        )
        y = 80
        for line in lines:
            page.insert_text((72, y), line, fontname="china-s", fontsize=11)
            y += 24
        page.insert_text((72, 780), f"- {index} -", fontname="china-s", fontsize=9)
    document.save(path)
    document.close()


def test_parse_pdf_removes_repeated_header_and_page_numbers(tmp_path: Path) -> None:
    path = tmp_path / "policy.pdf"
    _make_pdf(path)
    parsed = parse_pdf_file(path)

    assert parsed.page_count == 3
    assert "第一章 总则" in parsed.text
    assert "第三条 本细则由教务处负责解释。" in parsed.text
    assert "华南农业大学本科学生学籍管理实施细则" not in parsed.text
    assert "- 1 -" not in parsed.text
    assert any("页眉/页脚" in warning for warning in parsed.warnings)


def test_parse_corpus_writes_parsed_json(tmp_path: Path) -> None:
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "html" / "sample_policy.html"
    version_dir = tmp_path / "raw" / "status-rules" / "v2025"
    version_dir.mkdir(parents=True)
    (version_dir / "source.html").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    import hashlib

    digest = hashlib.sha256((version_dir / "source.html").read_bytes()).hexdigest()
    (version_dir / "meta.json").write_text(
        json.dumps(
            {
                "doc_id": "status-rules",
                "version_id": "v2025",
                "title": "本科学生学籍管理实施细则",
                "format": "html",
                "source_file": "source.html",
                "sha256": digest,
                "content_type": "text/html; charset=utf-8",
                "fetched_at": "2026-10-19T00:00:00+00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    results = parse_corpus(raw_dir=tmp_path / "raw", processed_dir=tmp_path / "processed")
    assert len(results) == 1
    assert results[0].status == "parsed"
    assert results[0].output_path is not None
    payload = json.loads(results[0].output_path.read_text(encoding="utf-8"))
    assert payload["doc_id"] == "status-rules"
    assert "第一条" in payload["text"]
