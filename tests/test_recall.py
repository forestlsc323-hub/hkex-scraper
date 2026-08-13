# -*- coding: utf-8 -*-
"""孤儿单：一整单交易的公告全被灰掉了，于是这单凭空消失。

判据是**演绎**不是猜测：后续公告的存在，证明存在过一份 T0。
「寄發綜合要約文件」讲的是某一个要约的后续动作 —— 有子必有父。
"""

from __future__ import annotations

from pathlib import Path

from hkexdb import recall


def row(code, date, title, bucket, name=""):
    return {"code": code, "date": date, "title": title,
            "bucket": bucket, "name": name}


def test_a_deal_whose_every_filing_was_greyed_out_is_reported():
    """06113 UTS 的真实形态：索引里 4 条，条条都是后续，一条都没留存。"""
    rows = [
        row("06113", "2025-05-06",
            "聯合公告 延遲寄發有關…就收購UTS…之綜合要約及回應文件",
            "excluded", "UTS MARKETING"),
        row("06113", "2025-05-23",
            "聯合公告 寄發有關…就收購UTS…之綜合要約及回應文件", "excluded"),
        row("06113", "2025-06-13", "聯合公告 …要約結果", "excluded"),
    ]
    got = recall.find_orphans(rows)
    assert len(got) == 1
    assert got[0].code == "06113" and got[0].count == 3
    assert got[0].first == "2025-05-06" and got[0].last == "2025-06-13"
    assert "綜合要約" in got[0].evidence


def test_a_deal_with_one_retained_filing_is_not_an_orphan():
    """留存了 T0 就不是孤儿 —— 哪怕后面十条后续公告全被灰掉。"""
    rows = [
        row("01417", "2026-06-15", "聯合公告 強制性無條件現金要約", "retained"),
        row("01417", "2026-07-01", "寄發綜合文件", "excluded"),
        row("01417", "2026-08-01", "要約結果", "excluded"),
    ]
    assert recall.find_orphans(rows) == []


def test_follow_up_words_unrelated_to_an_offer_do_not_count():
    """「寄發二零二五年年報」也命中「寄發」—— 但它不是要约的后续。

    判据必须同时看得出这是**要约**的后续，否则一堆无关公告会被认成孤儿单，
    而一张全是噪音的表等于没有表。
    """
    rows = [row("00001", "2025-03-01", "寄發二零二四年年報", "excluded"),
            row("00001", "2025-04-01", "股東特別大會通告", "excluded")]
    # 「年報」「大會通告」既不是要约的后续，也不含「要約」二字
    assert recall.find_orphans(rows) == []


def test_two_deals_on_the_same_code_are_counted_separately():
    """同一家公司先后两单要约 —— 隔了 60 天以上就是两单，各判各的。"""
    rows = [
        row("02362", "2025-03-02", "聯合公告 寄發綜合要約文件", "excluded"),
        row("02362", "2026-08-20", "公告 自願現金部分要約", "retained"),
        row("02362", "2026-09-10", "公告 部分要約之要約結果", "excluded"),
    ]
    got = recall.find_orphans(rows)
    assert len(got) == 1, "前一单是孤儿，后一单留存了 T0"
    assert got[0].first == "2025-03-02"


def test_rows_without_a_stock_code_are_skipped_not_crashed():
    """披露易本身就有不给代码的行，归不到单上 —— 另有报告管它们。"""
    assert recall.find_orphans([row("", "2025-01-01", "寄發綜合要約文件",
                                    "excluded")]) == []


def test_the_report_lands_on_disk_with_every_title(tmp_path: Path):
    rows = [row("06113", "2025-05-06", "延遲寄發有關收購UTS之綜合要約文件", "excluded"),
            row("06113", "2025-05-23", "寄發有關收購UTS之綜合要約文件", "manual")]
    path = recall.write_report(recall.find_orphans(rows), tmp_path)
    text = path.read_text(encoding="utf-8-sig")
    assert "06113" in text
    assert "excluded／manual" in text
    assert "延遲寄發" in text and "寄發有關收購UTS" in text


def test_the_summary_says_zero_when_nothing_is_lost():
    assert "孤儿单：0" in recall.summary([])[0]


def test_the_summary_leads_with_the_count_and_the_reasoning():
    rows = [row("06113", "2025-05-06", "延遲寄發有關收購UTS之綜合要約文件",
                "excluded")]
    lines = recall.summary(recall.find_orphans(rows))
    assert "孤儿单 1 单" in lines[0]
    assert any("召回率的缺口" in x for x in lines)


# --------------------------- 第二个缺口：4921 条「人工复核」里有多少像 T0

def test_a_real_t0_title_is_counted_as_a_gap():
    """两层词表都没命中、但标题明明就是一份 T0。"""
    rows = [row("01417", "2026-06-15",
                "聯合公告 由力高證券有限公司代表要約人作出強制性無條件現金要約",
                "manual")]
    total, samples = recall.manual_bucket_gap(rows)
    assert total == 1 and "強制性" in samples[0]


def test_a_follow_up_title_is_never_counted_as_a_t0():
    rows = [row("01417", "2026-07-01", "寄發有關強制性無條件現金要約之綜合文件",
                "manual")]
    assert recall.manual_bucket_gap(rows)[0] == 0


def test_an_unrelated_title_is_not_counted():
    for title in ["寄發二零二四年年報", "董事名單與其角色及職能",
                  "須予披露交易 - 收購物業"]:
        assert not recall.looks_like_t0(title), title


def test_rows_already_retained_are_not_part_of_the_gap():
    rows = [row("01417", "2026-06-15", "聯合公告 強制性無條件現金要約", "retained")]
    assert recall.manual_bucket_gap(rows)[0] == 0


def test_the_gap_summary_reports_both_holes():
    rows = [row("06113", "2025-05-06", "延遲寄發有關收購UTS之綜合要約文件",
                "excluded"),
            row("01417", "2026-06-15", "聯合公告 強制性無條件現金要約", "manual")]
    lines = recall.gap_summary(rows, recall.find_orphans(rows))
    assert any("孤儿单 1 单" in x for x in lines)
    assert any("看起来像 T0" in x for x in lines)


# ---------------- 收紧：第一版实跑报了 105 单，逐条看只有 7 单像真漏检

def test_an_acquisition_of_assets_is_not_a_missed_offer():
    """「收購」在港股公告里太常见 —— 收资产、收股权、发通函都用它。

    第一版拿「要約|收購|私有化」当判据，9657 条报出 105 单，
    其中 75 单的证据标题里根本没有「要約」两个字。
    一张 105 行、94% 是噪音的表，等于没有表。
    """
    rows = [row("01468", "2025-01-07",
                "延遲寄發通函 內容有關 就收購目標公司80%股權 涉及根據特別授權"
                "發行代價股份的 主要交易", "excluded")]
    assert recall.find_orphans(rows) == []


def test_a_scheme_of_arrangement_is_out_of_scope_not_missed():
    """協議安排／第86條私有化不是 MGO/VGO/PO —— 本来就不收，不算漏。"""
    rows = [row("00933", "2025-01-02",
                "聯合公告(1)龍躍消費品有限公司建議根據公司法第99條以協議安排方式"
                "私有化堡獅龍國際集團有限公司；(2)寄發綜合文件 要約", "excluded")]
    assert recall.find_orphans(rows) == []


def test_a_buyback_and_a_whitewash_are_out_of_scope():
    for title in ["(1)代表本公司提出有條件自願性現金要約按每股1.20港元之價格回購股份；"
                  "(2)寄發綜合文件",
                  "進一步延遲寄發有關申請清洗豁免之通函 及 全面要約"]:
        assert recall.find_orphans([row("00001", "2025-01-06", title,
                                        "excluded")]) == []


def test_a_bond_tender_is_not_a_share_offer():
    """01030 新城發展「以現金購買其尚未償還的優先票據」—— 债券要约不是股份要约。"""
    rows = [row("01030", "2026-02-26",
                "新城環球有限公司 以現金購買其尚未償還的2027年到期11.88%"
                "有擔保優先票據之要約 及 寄發文件", "excluded")]
    assert recall.find_orphans(rows) == []


def test_a_tail_cluster_of_an_already_captured_deal_is_not_an_orphan():
    """01749 杉杉品牌：T0 在 6 月留存了，9 月那簇综合文件被 60 天间隔切开。

    那不是丢了一单，是同一单的尾巴。按代码找一找附近有没有留存过 T0，
    有就不算 —— 否则每一单长命的交易都会额外报一次假漏检。
    """
    rows = [row("01749", "2025-06-20", "聯合公告 強制性有條件現金要約", "retained"),
            row("01749", "2025-09-11", "綜合文件 - 提出強制性有條件現金要約", "excluded"),
            row("01749", "2025-10-03", "要約結果 - 強制性有條件現金要約", "excluded")]
    assert recall.find_orphans(rows) == []


def test_one_unrelated_title_does_not_kill_the_whole_cluster():
    """⚠️ 范围外只看**那条证据本身**，不看整簇。

    一簇里混进一条「購回股份」的无关公告，不该把整单交易一起判出局
    （01939 東京中央拍賣第一版就是这么被误杀的）。
    """
    rows = [row("01939", "2025-05-29", "根據股份購回授權購回股份之月報表", "excluded"),
            row("01939", "2025-05-29",
                "有關力高證券有限公司代表要約人提出強制性無條件現金要約之"
                "綜合文件", "excluded")]
    got = recall.find_orphans(rows)
    assert len(got) == 1 and "強制性無條件現金要約" in got[0].evidence
