"""网页报告与流水线状态的测试。"""

from __future__ import annotations

import datetime as dt
import json
import re

import pytest

from hkexdb import pipeline, report
from hkexdb.config import Config

from test_listing import _make_config


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


def test_manual_bucket_is_listed_first():
    """要人看的排前面 —— 人工复核桶是铁律二要求的动作。"""
    assert pipeline is not None
    order = report.BUCKET_ORDER
    assert order[0] == "manual"
    assert order.index("manual") < order.index("excluded")


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


# ---------------------------------------------------------------- 流水线状态

def _cfg(tmp_path) -> Config:
    cfg = _make_config(tmp_path)
    return Config(**{**cfg.__dict__,
                     "date_from": dt.date(2026, 1, 1),
                     "date_to": dt.date(2026, 1, 3)})


def test_fresh_project_points_at_step_one(tmp_path):
    steps = pipeline.status(_cfg(tmp_path))
    assert not any(s.done for s in steps)
    text = pipeline.format_status(steps)
    assert "run_probe.py" in text


def test_invalid_probe_report_does_not_count_as_done(tmp_path):
    """跑过但一个请求都没发出去的勘察，结论无效，不能算完成。

    否则流水线会指着第 2 步说「可以走了」，而参数其实一个都没验证过。
    """
    cfg = _cfg(tmp_path)
    cfg.probe_dir.mkdir(parents=True, exist_ok=True)
    (cfg.probe_dir / "PROBE_REPORT.md").write_text(
        "# 报告\n\n> # ⛔ 本次勘察无效：一个请求都没有发出去\n", encoding="utf-8")

    probe_step = next(s for s in pipeline.status(cfg) if s.key == "probe")
    assert not probe_step.done
    assert "无效" in probe_step.detail


def test_valid_probe_report_counts_as_done(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.probe_dir.mkdir(parents=True, exist_ok=True)
    (cfg.probe_dir / "PROBE_REPORT.md").write_text(
        "# 报告\n\n结论：使用 titleSearchServlet.do\n", encoding="utf-8")
    assert next(s for s in pipeline.status(cfg) if s.key == "probe").done


def test_partial_listing_is_not_done(tmp_path):
    """只抓了一部分天数不算完成 —— 否则会带着缺口往下走。"""
    cfg = _cfg(tmp_path)                       # 共 3 天
    cfg.raw_dir.mkdir(parents=True, exist_ok=True)
    (cfg.raw_dir / "checkpoint.json").write_text(
        json.dumps(["2026-01-01"]), encoding="utf-8")
    (cfg.raw_dir / "listing_raw.csv").write_text("a\n1\n", encoding="utf-8")

    step = next(s for s in pipeline.status(cfg) if s.key == "listing")
    assert not step.done
    assert "1/3" in step.detail


def test_complete_listing_is_done(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.raw_dir.mkdir(parents=True, exist_ok=True)
    (cfg.raw_dir / "checkpoint.json").write_text(
        json.dumps(["2026-01-01", "2026-01-02", "2026-01-03"]), encoding="utf-8")
    (cfg.raw_dir / "listing_raw.csv").write_text("a\n1\n2\n", encoding="utf-8")
    assert next(s for s in pipeline.status(cfg) if s.key == "listing").done


def test_status_names_the_next_command(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.probe_dir.mkdir(parents=True, exist_ok=True)
    (cfg.probe_dir / "PROBE_REPORT.md").write_text("结论：ok", encoding="utf-8")
    text = pipeline.format_status(pipeline.status(cfg))
    assert "run_listing.py" in text


def test_pdf_step_never_blocks_the_pipeline():
    """第 5 步是按需的，不该让流水线永远显示「没做完」。"""
    steps = pipeline.status(_cfg(__import__("pathlib").Path("/tmp")))
    pdf = next(s for s in steps if s.key == "pdf")
    assert "按需" in pdf.detail
