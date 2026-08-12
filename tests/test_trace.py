"""漏检追踪：一单没出来，是漏在哪一层。

⚠️ 这里的假数据必须和真表的**列名**一致 —— 索引用 HKEX 的大写列
（STOCK_CODE/DATE_ISO/TITLE），筛查用 screened.csv 的小写列，成品用
deals.csv 的中文列。列名对不上时追踪会一律报「索引里就没有」，
那是最糟的一种错：它看起来像一个真结论。
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from hkexdb import trace as T


def _write(path: Path, columns: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    (tmp_path / "data" / "store").mkdir(parents=True)
    return tmp_path


def put_index(root: Path, rows: list[dict]) -> None:
    _write(root / "data" / "store" / "listing.csv",
           ["NEWS_ID", "DATE_TIME", "DATE_ISO", "STOCK_CODE", "STOCK_NAME",
            "TITLE", "FILE_LINK"], rows)


def put_screening(root: Path, rows: list[dict]) -> None:
    _write(root / "data" / "screening" / "screened.csv",
           ["row_id", "date", "code", "name", "bucket", "matched_exclude",
            "matched_retain", "reasons", "title", "pdf_url"], rows)


def put_deals(root: Path, rows: list[dict]) -> None:
    for i, row in enumerate(rows):
        # ⚠️ 存档按 NEWS_ID 索引，没有这一列的行会被整行丢掉。
        row.setdefault("NEWS_ID", f"n{i}")
    _write(root / "data" / "store" / "deals.csv",
           ["NEWS_ID", "判定", "公告日期", "股票代码", "受要约方", "要约类型",
            "备注", "公告标题"], rows)


def test_no_index_row_means_the_search_never_saw_it(root):
    put_index(root, [])
    got = T.trace("09666", "2025-04-28", root=root)
    assert got.layer == T.INDEX
    assert "索引里就没有" in got.verdict


def test_greyed_out_by_screening_names_the_rule(root):
    put_index(root, [{"NEWS_ID": "1", "DATE_ISO": "2025-04-28",
                      "STOCK_CODE": "9666", "TITLE": "月報表"}])
    put_screening(root, [{"row_id": "1", "date": "2025-04-28", "code": "09666",
                          "bucket": "excluded", "matched_exclude": "月報表",
                          "title": "月報表"}])
    got = T.trace("09666", "2025-04-28", root=root)
    assert got.layer == T.SCREEN
    assert "已灰" in got.verdict
    assert "月報表" in got.text()


def test_retained_but_no_deal_row_is_an_extraction_miss(root):
    put_index(root, [{"NEWS_ID": "1", "DATE_ISO": "2025-04-28",
                      "STOCK_CODE": "9666", "TITLE": "要約"}])
    put_screening(root, [{"row_id": "1", "date": "2025-04-28", "code": "09666",
                          "bucket": "retained", "matched_retain": "强制性…要约",
                          "title": "要約"}])
    got = T.trace("09666", "2025-04-28", root=root)
    assert got.layer == T.EXTRACT
    assert "抽取层" in got.verdict


def test_a_produced_row_is_not_a_miss_at_all(root):
    put_deals(root, [{"判定": "要约", "公告日期": "2025-04-30",
                      "股票代码": "09666", "要约类型": "MGO"}])
    got = T.trace("09666", "2025-04-28", root=root)
    assert got.layer == ""
    assert "已出成品" in got.verdict


def test_merged_away_rows_are_reported_as_merged_not_missing(root):
    """同单重复合并掉的那一行不是漏检 —— 说成漏检会指挥我去修抓取。"""
    put_deals(root, [{"判定": "同单重复", "公告日期": "2025-04-28",
                      "股票代码": "09666", "备注": "与另一份文件同一单"}])
    got = T.trace("09666", "2025-04-28", root=root)
    assert got.layer == T.EXTRACT
    assert "同单重复" in got.verdict


def test_a_far_away_filing_of_the_same_code_does_not_count(root):
    """同一家公司去年那单不能拿来顶今年这单。"""
    put_deals(root, [{"判定": "要约", "公告日期": "2024-01-05",
                      "股票代码": "09666", "要约类型": "MGO"}])
    got = T.trace("09666", "2025-04-28", root=root)
    assert got.deals == []
    assert got.layer == T.INDEX


def test_code_is_normalised_on_both_sides(root):
    put_index(root, [{"NEWS_ID": "1", "DATE_ISO": "2025-04-28",
                      "STOCK_CODE": "9666", "TITLE": "要約"}])
    got = T.trace("9666.HK", "2025-04-28", root=root)
    assert got.index and got.index[0].title == "要約"


def test_mirror_rows_with_two_codes_are_found_under_either(root):
    put_deals(root, [{"判定": "要约", "公告日期": "2025-04-28",
                      "股票代码": "01117<br/>01432", "要约类型": "MGO"}])
    assert "已出成品" in T.trace("01432", "2025-04-28", root=root).verdict


def test_empty_disk_says_so_instead_of_blaming_the_search(root):
    got = T.trace("09666", "2025-04-28", root=root)
    assert got.layer == ""
    assert "先跑一次抓取" in got.verdict


def test_missing_report_only_covers_rows_scoring_could_not_pair(root):
    put_screening(root, [{"row_id": "1", "date": "2025-05-06", "code": "06113",
                          "bucket": "excluded", "matched_exclude": "翌日披露報表",
                          "title": "翌日披露報表"}])
    got_rows = [{"股票代码": "00195", "公告日期": "2025-03-01",
                 "要约类型": "MGO"}]
    answers = [{"股票代码": "00195", "公告日期": "2025-03-01", "要约类型": "MGO"},
               {"股票代码": "06113", "公告日期": "2025-05-06", "要约类型": "MGO"}]
    lines = T.missing_report(got_rows, answers, root=root)
    text = "\n".join(lines)
    assert "06113" in text and "00195" not in text
    assert "筛查层（规则灰掉） 1 单" in text


def test_missing_report_is_silent_when_nothing_is_missing(root):
    rows = [{"股票代码": "00195", "公告日期": "2025-03-01", "要约类型": "MGO"}]
    assert T.missing_report(rows, rows, root=root) == []


def test_cli_prints_one_trace(root, capsys, monkeypatch):
    put_index(root, [{"NEWS_ID": "1", "DATE_ISO": "2025-04-28",
                      "STOCK_CODE": "9666", "TITLE": "要約"}])
    monkeypatch.setattr("hkexdb.runner.ROOT", root)
    assert T.main(["09666", "2025-04-28"]) == 0
    assert "09666" in capsys.readouterr().out


def test_cli_without_arguments_explains_itself(capsys):
    assert T.main([]) == 2
    assert "用法" in capsys.readouterr().out
