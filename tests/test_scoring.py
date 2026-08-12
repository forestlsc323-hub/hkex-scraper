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
    """仓库里那份答案表要能直接读，并且自检能跑。

    现在是 83 单：2026 上半年 25 单（用户手工核的）+ 2025 全年 58 单
    （从用户的表格截图转录的）。数量会随他继续录入而涨，所以不写死。
    """
    from hkexdb import runner, scoring as S
    path = runner.Path(__file__).parent.parent / "data" / "answer_key.csv"
    if not path.exists():
        import pytest
        pytest.skip("答案表不在（本地 data/ 被清过）")
    rows = S.load(path)
    assert len(rows) >= 25
    assert rows[0]["股票代码"].startswith("0"), "代码列前导零被吞了"
    assert any("01875" in p for p in S.sanity_check(rows))


# ------------------------------------------------- 配对：日期差几天还是同一单

def _row(code, date, **kw):
    return {"股票代码": code, "公告日期": date, **kw}


def test_the_same_deal_filed_a_few_days_apart_still_counts_as_matched():
    """实跑那份报告把 13 单算成「漏检」，其中至少 8 单程序明明抽到了 ——
    只是日期差几天：

        01780  答案 05-15，程序 05-07（差 8 天）
        01953  答案 04-22，程序 04-24（差 2 天）

    用户记的是 T0，程序打开的是那一单里最早的**留存**公告，未必同一天。
    结果同一单被同时记成一次漏检和一次多余，分母凭空缩水一半。
    量错了的准确率比没有准确率更糟：它会指挥人去修不存在的问题。
    """
    got = [_row("01780", "2026-05-07", **{"要约类型": "MGO"})]
    answer = [_row("01780", "2026-05-15", **{"要约类型": "MGO"})]

    report = scoring.score(got, answer)

    assert not report.missing_deals, "同一单被算成漏检了"
    assert not report.extra_deals, "同一单又被算成多余了"
    assert report.scores[0].right == 1


def test_a_loose_pairing_is_always_reported():
    """会自己放宽的比对器，如果不说，比严格的更危险。"""
    got = [_row("01780", "2026-05-07", **{"要约类型": "MGO"})]
    answer = [_row("01780", "2026-05-15", **{"要约类型": "MGO"})]

    report = scoring.score(got, answer)

    assert report.loose_pairs and report.loose_pairs[0][2] == 8
    assert "差 8 天" in report.text()


def test_two_separate_deals_on_one_company_are_paired_one_to_one():
    """02362 金川同期有 MGO 和 PO 两单 —— 绝不能让一行去顶两个答案。"""
    got = [_row("02362", "2026-03-26", **{"要约类型": "MGO"}),
           _row("02362", "2026-05-22", **{"要约类型": "PO"})]
    answer = [_row("02362", "2026-03-02", **{"要约类型": "MGO"}),
              _row("02362", "2026-05-27", **{"要约类型": "PO"})]

    report = scoring.score(got, answer)

    assert not report.missing_deals and not report.extra_deals
    assert report.scores[0].right == 2, "两单各配各的，都该对上"


def test_a_date_far_away_is_not_forced_into_a_pair():
    """半年前的另一单不是同一单 —— 硬配上就是把准确率做假。"""
    got = [_row("01780", "2025-11-01", **{"要约类型": "MGO"})]
    answer = [_row("01780", "2026-05-15", **{"要约类型": "MGO"})]

    report = scoring.score(got, answer)

    assert report.missing_deals and report.extra_deals


def test_a_genuinely_missing_deal_is_still_reported_as_missing():
    """放宽配对不能把真漏检也一起放过 —— 那才是最该看见的东西。"""
    report = scoring.score([], [_row("00195", "2026-05-29",
                                     **{"要约类型": "PO"})])
    assert len(report.missing_deals) == 1


# ------------------------------------------------- 差法本身有名字的，就说出来

def test_a_sign_only_difference_is_called_out():
    """08031 抽到 -13.58、答案 13.58 —— 数值一样符号相反。

    这类差法一眼能定性，直接标出来能省掉一次翻原文：用户已经确认过
    自己答案表里 01875 那条就是正负号写反了。
    """
    report = scoring.score(
        [_row("08031", "2026-01-21", **{"主值溢价率(%)": "-13.58"})],
        [_row("08031", "2026-01-21", **{"主值溢价率(%)": "13.58"})])
    assert "符号相反" in report.text()


def test_a_single_wrong_digit_is_called_out():
    """08439：程序 48,180,952.56，答案 18,180,952.56 —— 只差第一位。
    用户后来确认是答案表打错的，正是这一类。"""
    hint = scoring._hint("48180952", "18180952")
    assert "第 1 位" in hint and "按错一个键" in hint


def test_a_transposed_number_is_called_out():
    assert "换了位" in scoring._hint("1243", "1234")


def test_a_factor_of_ten_is_called_out():
    assert "10 倍" in scoring._hint("1000", "100")
    assert "10 倍" in scoring._hint("100", "1000")


def test_an_ordinary_difference_gets_no_made_up_explanation():
    """看不出名堂就别瞎猜 —— 一条错误的提示比没有提示更浪费时间。"""
    assert scoring._hint("266329230", "66833690") == ""
    assert scoring._hint("", "123") == ""
    assert scoring._hint("MGO", "PO") == ""


# ------------------------------------------------- 答案表：认得你自己的叫法

def test_your_own_column_names_are_accepted(tmp_path):
    """你那张 2025 年的表用的是投行习惯的列名，deals.csv 用的是程序的叫法。

    两边对不上就一条都配不上，而报告只会显示「一单都没对上」——
    看不出是列名的问题。与其让你每次导出都手工改表头，不如程序认得。
    """
    path = tmp_path / "answer_key.csv"
    path.write_text(
        "股份代码,公司简称,首次公告日期,要约类,溢价率,交易规模\n"
        "6808,高鑫零售,2025-01-01,MGO,2.86%,3236298104\n",
        encoding="utf-8-sig")

    rows = scoring.load(path)

    assert rows[0]["股票代码"] == "06808"        # 补到 5 位
    assert rows[0]["公告日期"] == "2025-01-01"
    assert rows[0]["要约类型"] == "MGO"
    assert rows[0]["主值溢价率(%)"] == "2.86"    # 百分号去掉
    assert rows[0]["交易规模(HKD)"] == "3236298104"


def test_a_code_written_three_ways_is_one_company():
    """同一家公司在你表里可能写 195 / 0195.HK / 00195。"""
    assert {scoring.normalise_code(x) for x in
            ("195", "0195.HK", "00195", " 195 ")} == {"00195"}


def test_the_program_side_codes_are_normalised_too():
    """deals.csv 里有「01117<br/>01432」这种双代码行（镜像归档）。"""
    got = [_row("01117<br/>01432", "2025-10-30", **{"要约类型": "MGO"})]
    answer = [_row("1432", "2025-10-30", **{"要约类型": "MGO"})]
    report = scoring.score(got, [scoring.normalise_row(r) for r in answer])
    assert not report.missing_deals


def test_an_official_column_is_not_clobbered_by_an_alias(tmp_path):
    """表里同时有「股票代码」和「股份代码」时，正式那列说了算。"""
    row = scoring.normalise_row({"股票代码": "00195", "股份代码": "999"})
    assert row["股票代码"] == "00195"
