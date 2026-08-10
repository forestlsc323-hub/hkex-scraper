"""网页报告的测试。"""

from __future__ import annotations

import json
import re


from hkexdb import report



def _records():
    return [
        {"row_id": "u1", "date": "2026-06-15", "code": "1417", "name": "浦江中國",
         "bucket": "retained", "title": "聯合公告 (1) 完成出售及購買 (2) 強制性無條件現金要約",
         "pdf_url": "/x/a.pdf", "reasons": "保留层命中 ['恢復…買賣']",
         "matched_retain": "恢復…買賣", "matched_exclude": "", "species": "",
         "manual_flags": "", "rules_version": "screening-2026-08-A"},
        {"row_id": "u2", "date": "2026-06-20", "code": "1417", "name": "浦江中國",
         "bucket": "excluded", "title": "寄發綜合文件", "pdf_url": "/x/b.pdf",
         "reasons": "排除层命中 ['寄發']", "matched_exclude": "寄發",
         "matched_retain": "", "species": "", "manual_flags": "",
         "rules_version": "screening-2026-08-A"},
        {"row_id": "u3", "date": "2026-07-05", "code": "0007", "name": "庚公司",
         "bucket": "manual", "title": "董事會會議日期", "pdf_url": "",
         "reasons": "两层均未命中，转人工复核", "matched_exclude": "",
         "matched_retain": "", "species": "", "manual_flags": "",
         "rules_version": "screening-2026-08-A"},
    ]


def _payload(html: str) -> dict:
    m = re.search(r"const DATA=(\{.*?\});", html, re.S)
    assert m, "页面里找不到嵌入的数据"
    return json.loads(m.group(1))


# ---------------------------------------------------------------- 自包含

def test_page_has_no_external_dependencies():
    """必须能双击打开、断网可用 —— 不许有 CDN、外链样式或脚本。"""
    html = report.build_html(_records())
    assert "<link" not in html
    assert not re.search(r'<script[^>]+src=', html)
    for bad in ("cdn.", "googleapis", "unpkg", "jsdelivr", "http://"):
        assert bad not in html, f"页面引用了外部资源：{bad}"


def test_page_embeds_every_row():
    data = _payload(report.build_html(_records()))
    assert len(data["rows"]) == 3
    assert {r["code"] for r in data["rows"]} == {"1417", "0007"}


def test_every_row_carries_its_verdict_reason():
    """铁律三：判定必须能追溯。页面上点开每行都要看得到依据。"""
    for row in _payload(report.build_html(_records()))["rows"]:
        assert row["reasons"].strip()


def test_bucket_counts_appear_as_cards():
    html = report.build_html(_records())
    # 三个桶各一张卡片
    assert html.count('class="card"') == 3
    assert "人工复核" in html and "留存" in html and "已灰" in html


def test_notes_render_as_a_warning_block():
    html = report.build_html(_records(), notes=["人工复核桶有 2 条，必须逐条看完"])
    assert 'class="warn"' in html
    assert "必须逐条看完" in html


def test_embedded_data_cannot_break_out_of_the_script_tag():
    """公告标题是外部数据，不能让它从 <script> 里逃逸出来。

    直接 json.dumps 嵌进 <script> 时，标题里的 `</script>` 会让浏览器
    提前闭合脚本标签，后面的内容按 HTML 解析 —— 这是真实可利用的注入点。
    """
    rec = dict(_records()[0])
    rec["title"] = '</script><img src=x onerror=alert(1)>甲'
    html = report.build_html([rec])

    # 页面里只应有我们自己的两个 script 标签（内联 DATA + 逻辑），
    # 数据段里不得出现任何字面的 < 或 >
    body = html.split("const DATA=", 1)[1]
    payload = body.split(";", 1)[0]
    assert "<" not in payload and ">" not in payload

    # 但数据本身必须一字不差 —— 转义只是传输形式
    assert _payload(html)["rows"][0]["title"] == '</script><img src=x onerror=alert(1)>甲'


def test_ampersand_survives_the_round_trip():
    """`&amp;` 在公告标题里很常见，转义不能改坏它。"""
    rec = dict(_records()[0])
    rec["title"] = "甲 & 乙 <公司>"
    assert _payload(report.build_html([rec]))["rows"][0]["title"] == "甲 & 乙 <公司>"


def test_report_written_to_disk(tmp_path):
    out = report.write_report(_records(), tmp_path / "sub" / "r.html",
                              rules_version="v1", source="x.csv")
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "v1" in text and "x.csv" in text
