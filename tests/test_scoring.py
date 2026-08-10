"""准确率打分的测试。

没有标准答案，「跑通了」和「跑对了」区分不开 —— 这一层就是那把尺。
"""

from __future__ import annotations

from hkexdb import scoring


def row(**kw):
    base = {"股票代码": "03336", "公告日期": "2026-05-18",
            "要约类型": "VGO", "要约价(HKD)": "2.20",
            "主值溢价率(%)": "-15.45"}
    base.update(kw)
    return base


def test_blank_cells_in_the_answer_key_are_not_graded():
    """留空＝这个字段没有标准答案，不是「答案是空」。

    这样你可以只填有把握的那几列，先把类型和要约价的准确率量出来。
    """
    r = scoring.score([row()], [row(要约类型="VGO", **{"要约价(HKD)": "",
                                                        "主值溢价率(%)": ""})])
    graded = {s.field: s.graded for s in r.scores}
    assert graded == {"要约类型": 1}


def test_a_wrong_value_is_reported_with_both_sides():
    r = scoring.score([row(要约类型="MGO")], [row(要约类型="VGO")])
    s = next(s for s in r.scores if s.field == "要约类型")
    assert s.right == 0 and s.graded == 1
    assert s.wrong[0][1] == "MGO" and s.wrong[0][2] == "VGO"
    assert "应为：VGO" in r.text()


def test_printing_rounding_does_not_count_as_wrong():
    """公告印的是四舍五入值，尾差不是错。"""
    r = scoring.score([row(**{"主值溢价率(%)": "-15.4500"})],
                      [row(**{"主值溢价率(%)": "-15.45"})])
    assert all(s.right == s.graded for s in r.scores)


def test_a_real_numeric_difference_still_counts_as_wrong():
    r = scoring.score([row(**{"要约价(HKD)": "2.20"})],
                      [row(**{"要约价(HKD)": "2.30"})])
    s = next(s for s in r.scores if s.field == "要约价(HKD)")
    assert s.right == 0


def test_a_deal_the_program_never_found_is_called_out_as_a_miss():
    """漏检比抽错严重 —— 抽错看得见，漏检看不见。"""
    r = scoring.score([], [row()])
    assert r.missing_deals == [("03336", "2026-05-18")]
    assert "漏检" in r.text()


def test_same_company_two_offers_are_scored_separately():
    """同一家公司被不同要约人先后发要约要分开记，绝不按代码去重。"""
    a = row(公告日期="2026-01-10", 要约类型="MGO")
    b = row(公告日期="2026-05-18", 要约类型="VGO")
    r = scoring.score([a, b], [a, b])
    s = next(s for s in r.scores if s.field == "要约类型")
    assert s.graded == 2 and s.right == 2


def test_extra_deals_are_listed_but_not_counted_wrong():
    """程序多抽到的不算错 —— 可能只是答案表还没填到。"""
    r = scoring.score([row()], [])
    assert r.extra_deals == [("03336", "2026-05-18")]
    assert all(s.graded == 0 for s in r.scores)


def test_report_sorts_worst_fields_first():
    """最该修的排最前面。"""
    rows_got = [row(股票代码="1", 要约类型="MGO", **{"要约价(HKD)": "2.20"}),
                row(股票代码="2", 要约类型="VGO", **{"要约价(HKD)": "9.99"})]
    rows_want = [row(股票代码="1", 要约类型="VGO", **{"要约价(HKD)": "2.20"}),
                 row(股票代码="2", 要约类型="VGO", **{"要约价(HKD)": "2.20"})]
    text = scoring.score(rows_got, rows_want).text()
    assert text.index("要约类型") < text.index("主值溢价率")


def test_template_has_the_same_columns_as_deals_csv(tmp_path):
    """模板列对不上 deals.csv，填完了也对不起来。"""
    from hkexdb import runner
    path = scoring.write_template(runner.DEAL_COLUMNS, tmp_path / "t.csv")
    header = path.read_text(encoding="utf-8-sig").splitlines()[0]
    assert header.split(",")[0] == runner.DEAL_COLUMNS[0]
    assert "股票代码" in header


def test_scoring_a_run_without_an_answer_key_explains_what_to_do(tmp_path, monkeypatch):
    from hkexdb import runner
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    logs = []
    runner.score_against_answer_key(on_log=logs.append)
    text = "\n".join(logs)
    assert "answer_key.csv" in text
    assert (tmp_path / "data" / "answer_key_template.csv").exists()


def test_the_answer_key_itself_is_sanity_checked():
    """答案表也会有错 —— 折让不可能超过 100%（价格最低只能到 0）。

    你那份手工表里東曜藥業写的是 -114.67%，而你自己的材料里说这单是
    「东曜+114%」，是正负号写反了。程序不该默默按错的答案打分。
    """
    from hkexdb import scoring as S
    problems = S.sanity_check([
        {"股票代码": "01875", "公告日期": "2026-01-13", "主值溢价率(%)": "-114.67"}])
    assert len(problems) == 1
    assert "01875" in problems[0] and "不可能" in problems[0]


def test_a_normal_discount_is_not_flagged():
    from hkexdb import scoring as S
    assert not S.sanity_check([{"股票代码": "01417", "主值溢价率(%)": "-55.57"}])
    assert not S.sanity_check([{"股票代码": "09638", "主值溢价率(%)": "27.20"}])


def test_the_shipped_answer_key_loads_and_is_checked():
    """仓库里那份 25 单的答案表要能直接读，并且自检能跑。"""
    from hkexdb import runner, scoring as S
    path = runner.Path(__file__).parent.parent / "data" / "answer_key.csv"
    if not path.exists():
        import pytest
        pytest.skip("答案表不在（本地 data/ 被清过）")
    rows = S.load(path)
    assert len(rows) == 25
    assert rows[0]["股票代码"].startswith("0"), "代码列前导零被吞了"
    assert any("01875" in p for p in S.sanity_check(rows))
