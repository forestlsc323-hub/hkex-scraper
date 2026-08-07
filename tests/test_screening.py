"""T0 Screening 的回归测试。

《作业手册》第五节那十三个坑，每一个都是被真实漏网逼出来的补丁。
补丁没有测试守着，下次改词表就会把它改回去。所以逐条钉死。
"""

from __future__ import annotations

import pytest

from hkexdb import screening as S

RULES = S.load_rules("screening_rules.yaml")


def bucket(title: str) -> str:
    return S.classify_title(title, RULES).bucket


# ================================================================
# 启动自检：规则文件自身不能有病
# ================================================================

def test_rules_pass_self_validation():
    RULES.validate()          # 不抛异常即通过


def test_no_exclude_term_matches_a_verified_t0_title():
    """语料反测：排除词命中任何一条已核实的真实 T0 标题就是漏检。

    这比人肉护栏可靠 —— 手册说「词表要按真实数据迭代」，这就是它的
    机器化形式。每核实一单新 T0，就把标题加进 t0_title_corpus，
    词表的护栏随样本一起变强。
    """
    assert RULES.t0_corpus, "语料为空，这道关等于没设"
    for term in RULES.exclude_terms:
        for t0 in RULES.t0_corpus:
            assert term not in t0, f"排除词「{term}」会误杀：{t0[:60]}…"


def test_guard_is_exact_match_not_substring():
    """「要約結果」含「要約」但合法（名词＋阶段词，坑⑤）。

    护栏若写成「包含」，会把整张合法词表判死；真正的地雷是裸词。
    """
    assert "要約結果" in RULES.exclude_terms
    assert "要約" in RULES.never_exclude


def test_adding_a_bare_landmine_term_raises():
    """裸词「強制」进排除列 —— 必须报错。用它当子串会团灭 MGO 的 T0（坑②）。"""
    bad = S.Rules(**{**RULES.__dict__, "exclude_terms": ["強制"]})
    with pytest.raises(S.RuleError, match="護欄|护栏"):
        bad.validate()


def test_term_that_hits_the_corpus_raises():
    """「全面現金要約」看着像后续公告用词，但它在真实 T0 标题里 —— 拒绝。"""
    bad = S.Rules(**{**RULES.__dict__, "exclude_terms": ["全面現金要約"]})
    with pytest.raises(S.RuleError, match="漏檢|漏检"):
        bad.validate()


def test_bare_noun_exclude_term_raises():
    """坑⑤：单取名词必误杀，必须名词＋动词成对。"""
    bad = S.Rules(**{**RULES.__dict__, "exclude_terms": ["要約"]})
    with pytest.raises(S.RuleError):
        bad.validate()


# ================================================================
# 坑①：T0 的法律学名不得被排除
# ================================================================

@pytest.mark.parametrize("title", [
    "根據收購守則規則3.5作出的公告",
    "有關收購本公司全部已發行股份之確定意圖公告",
    "聯合公告 - 可能強制性無條件現金要約",
    "聯合公告 - 可能自願性有條件現金要約及恢復買賣",
])
def test_t0_legal_names_survive(title):
    assert bucket(title) == S.RETAINED, f"T0 学名被误杀：{title}"


def test_maybe_offer_pattern_needs_regex_not_substring():
    """「可能…要約」中间隔着字，固定子串匹配不到，必须走正则。"""
    v = S.classify_title("聯合公告 - 可能強制性無條件現金要約", RULES)
    assert "可能…要約" in v.matched_retain


# ================================================================
# 坑②：強制收購 / 撤銷上市 只打人工标记，不自动排除
# ================================================================

def test_mandatory_acquisition_is_flagged_not_excluded():
    v = S.classify_title("有關強制收購剩餘股份之公告", RULES)
    assert v.bucket != S.EXCLUDED
    assert any("強制收購" in f for f in v.manual_flags)


def test_delisting_in_privatisation_t0_is_not_excluded():
    """私有化 T0 标题常含「建議撤銷上市地位」（伟工、储能）。"""
    v = S.classify_title("聯合公告 - 建議撤銷上市地位及可能全面要約", RULES)
    assert v.bucket != S.EXCLUDED
    assert any("撤銷上市" in f for f in v.manual_flags)


# ================================================================
# 坑③：词太短会误杀，词太长会漏网
# ================================================================

def test_short_terms_would_have_misfired():
    """「接納」「結束」太宽 —— 词表里必须是加长版。"""
    assert "接納" not in RULES.exclude_terms
    assert "結束" not in RULES.exclude_terms
    assert "接納程度" in RULES.exclude_terms
    assert "要約結束" in RULES.exclude_terms


@pytest.mark.parametrize("title", [
    "每月最新資料", "每月最新情況", "月度更新", "更新公告",
    "每月更新 - 要約進展",
])
def test_monthly_update_variants_all_caught(title):
    """同类公告不同律所起草，措辞必有变体 —— 四种变体都要抓到。"""
    assert bucket(title) == S.EXCLUDED, f"漏网：{title}"


# ================================================================
# 坑④：双轨词，同一个词两种命运
# ================================================================

def test_precondition_standalone_is_excluded():
    """独立成篇＝后续公告（绿科、金川、中国数智）。"""
    assert bucket("達成先決條件之公告") == S.EXCLUDED


def test_precondition_bundled_in_joint_t0_is_not_excluded():
    """作为 T0 联合公告的打包项＝不影响收录（圣牧那条）。"""
    title = ("聯合公告 (1) 有條件買賣協議 (2) 購股協議項下先決條件之達成 "
             "(3) 可能強制性現金要約 及 (4) 恢復買賣")
    v = S.classify_title(title, RULES)
    assert v.is_bundled
    assert v.bucket != S.EXCLUDED


def test_ifa_appointment_standalone_is_procedural():
    """单独成篇的一两百 KB 短公告＝T0 后数日的程序公告。"""
    assert bucket("就要約委任獨立財務顧問") == S.EXCLUDED


def test_ifa_appointment_bundled_is_a_t0_signal():
    """多编号联合公告里的「委任獨立財務顧問」＝T0 强特征。"""
    title = ("聯合公告 (1) 訂立買賣協議 (2) 可能強制性無條件現金要約 "
             "(3) 委任獨立財務顧問 及 (4) 恢復買賣")
    v = S.classify_title(title, RULES)
    assert v.is_bundled
    assert v.bucket == S.RETAINED


def test_bundled_detection_needs_two_or_more_numbers():
    assert S.is_bundled_announcement("聯合公告 (1) 訂立買賣協議 (2) 可能要約")
    assert S.is_bundled_announcement("公告 （1）甲 （2）乙 （3）丙")
    assert not S.is_bundled_announcement("就要約委任獨立財務顧問")
    assert not S.is_bundled_announcement("公告 (1) 只有一项")


# ================================================================
# 排除优先于保留（手册第三步）
# ================================================================

def test_exclusion_beats_retention():
    """后续公告标题几乎都会把要约全称复述一遍。

    这条标题同时含「寄發綜合文件」（排除）和「強制性無條件現金要約」
    （听起来像 T0）—— 必须判排除，否则会大量误留。
    """
    title = "寄發綜合文件 - 強制性無條件現金要約"
    v = S.classify_title(title, RULES)
    assert v.bucket == S.EXCLUDED
    assert "綜合文件" in v.matched_exclude or "寄發" in v.matched_exclude


def test_offer_result_announcement_is_excluded():
    assert bucket("強制性無條件現金要約之要約結果") == S.EXCLUDED


# ================================================================
# 坑⑬：非三种要约的品种要认得出来
# ================================================================

@pytest.mark.parametrize("title,species", [
    ("建議以協議安排方式將公司私有化", "協議安排私有化"),
    ("有關股份之回購要約", "回購要約"),
    ("申請清洗豁免之公告", "清洗豁免"),
    ("諒解備忘錄失效", "規則3.7失效"),
])
def test_special_species_are_separable(title, species):
    v = S.classify_title(title, RULES)
    assert v.bucket == S.SPECIAL
    assert v.species == species


def test_special_species_are_grey_but_distinguishable():
    """剔除但要能一眼分辨 —— 不能和后续程序公告混作一堆。"""
    v = S.classify_title("建議以協議安排方式將公司私有化", RULES)
    assert v.is_grey
    assert v.bucket != S.EXCLUDED      # 单独成桶


# ================================================================
# 坑⑫：标题勘误行
# ================================================================

def test_superseded_title_is_greyed():
    marker = RULES.superseded_marker
    v = S.classify_title(f"聯合公告 - 可能要約 {marker}", RULES)
    assert v.bucket == S.SUPERSEDED
    assert v.is_grey


def test_replacement_title_is_judged_normally():
    """取修改后版本 —— 它要正常参与判定，不能被勘误标记带偏。"""
    marker = RULES.replacement_marker
    assert bucket(f"聯合公告 - 訂立買賣協議及恢復買賣 {marker}") == S.RETAINED


def test_correction_pair_is_labelled():
    records = [
        {"title": f"甲公告 {RULES.superseded_marker}"},
        {"title": f"甲公告（更正） {RULES.replacement_marker}"},
        {"title": "乙公告"},
    ]
    S.resolve_title_corrections(records, RULES)
    assert [r["title_correction"] for r in records] == \
        ["superseded", "replacement", ""]


# ================================================================
# 坑⑨：镜像去重 vs 一司多约，方向相反
# ================================================================

def test_mirror_filing_is_detected_not_auto_merged():
    """要约方也是上市公司时，同一份联合公告在双方代码下各归档一次。

    自动猜「哪个是受要约方」会把整单记到错误公司名下，
    所以只挑出来交人工。
    """
    records = [
        {"row_id": "a", "date": "2026-05-18", "code": "3336",
         "name": "巨騰國際", "title": "聯合公告 - 自願性有條件全面現金要約"},
        {"row_id": "b", "date": "2026-05-18", "code": "6613",
         "name": "藍思科技", "title": "聯合公告 - 自願性有條件全面現金要約"},
    ]
    pairs = S.find_mirror_pairs(records)
    assert len(pairs) == 1
    assert pairs[0].codes == ["3336", "6613"]


def test_same_target_different_offerors_are_never_merged():
    """反方向：同一家公司被不同要约人先后发要约（绿科×2、金川×2、
    天鸽 MGO+PO）—— 按「要约人×标的」计数，分开记。
    """
    a = S.deal_key("Yellowstone International", "00195")
    b = S.deal_key("另一要約人", "00195")
    assert a != b


def test_deal_key_is_stable_across_whitespace():
    assert S.deal_key(" Yellowstone ", " 00195 ") == \
        S.deal_key("Yellowstone", "00195")


# ================================================================
# 坑⑪：块头只能当旁证
# ================================================================

def test_file_size_never_drives_the_verdict():
    """RIMBACO 的「寄發綜合文件」有 3MB —— 大不等于 T0。"""
    assert RULES.raw["size_hint"]["is_evidence"] is False
    assert bucket("寄發綜合文件") == S.EXCLUDED     # 不管多大都排除


# ================================================================
# 坑⑦：回指措辞标题层判不了
# ================================================================

def test_backreference_is_documented_as_body_level_only():
    """「茲提述…日期為XXX之公告」在正文首段，标题层拿不到。

    规则文件里明确写了这一点，防止有人误以为标题层已经处理了。
    """
    note = RULES.raw["backreference_note"]
    assert "標題層做不了" in note or "标题层做不了" in note
    assert RULES.raw["backreference_patterns"]


# ================================================================
# 四层防漏网
# ================================================================

def _sample_records() -> list[dict]:
    titles = [
        ("1417", "浦江中國", "聯合公告 (1) 完成出售及購買 (2) 強制性無條件現金要約 及 (3) 恢復股份買賣"),
        ("3336", "巨騰國際", "聯合公告 - 具有前置條件之自願性有條件全面現金要約及復牌"),
        ("00195", "綠科科技",
         "由華富建業企業融資有限公司代表YELLOWSTONE INTERNATIONAL LIMITED"
         "提出附帶先決條件的自願現金部分收購要約"),
        ("0001", "甲公司", "寄發綜合文件"),
        ("0002", "乙公司", "要約結果公告"),
        ("0003", "丙公司", "每月最新資料"),
        ("0004", "丁公司", "達成先決條件之公告"),
        ("0005", "戊公司", "建議以協議安排方式將公司私有化"),
        ("0006", "己公司", "延遲寄發綜合文件 - 訂立買賣協議之後續"),
        ("0007", "庚公司", "董事會會議日期"),
    ]
    return [{"row_id": f"r{i}", "date": "2026-06-15", "code": c,
             "name": n, "title": t}
            for i, (c, n, t) in enumerate(titles)]


def test_screen_produces_all_four_layers():
    report = S.screen(_sample_records(), RULES)
    assert report.reconciled, report.reconciliation
    assert report.counts[S.RETAINED] >= 3      # 三单真实 T0 都留住了
    assert report.counts[S.EXCLUDED] >= 4
    assert report.counts[S.SPECIAL] == 1
    assert report.counts[S.MANUAL] >= 1        # 「董事會會議日期」两层都没中


def test_contradiction_check_catches_a_possible_misfire():
    """第 1 层：被灰的行里含「訂立」的，要挑出来人工看。

    「延遲寄發綜合文件 - 訂立買賣協議之後續」被排除是对的，
    但它含「訂立」，必须进矛盾清单等人工确认，不能默默灰掉。
    """
    records = _sample_records()
    S.screen(records, RULES)
    flagged = S.contradiction_check(records, RULES)
    titles = [r["title"] for r in flagged]
    assert any("訂立" in t for t in titles)


def test_count_reconciliation_fails_loudly_on_missing_code():
    """底部数量校验：记录数＝代号数＝简称数＝标题行数＝判定桶合计。

    归属继承列是防错位的命根子 —— 少一个代号就要立刻报不平。
    """
    records = _sample_records()
    records[0]["code"] = ""            # 模拟归属继承断了
    S.screen(records, RULES)
    counts, ok = S.reconcile_counts(records)
    assert not ok
    assert counts["有代号数"] == counts["记录数"] - 1


def test_random_audit_is_reproducible():
    """固定种子 —— 抽查结果可复现，否则复核不可审计。"""
    r1, r2 = _sample_records(), _sample_records()
    S.screen(r1, RULES)
    S.screen(r2, RULES)
    assert [r["row_id"] for r in S.random_audit(r1, RULES)] == \
           [r["row_id"] for r in S.random_audit(r2, RULES)]


def test_grey_rows_are_soft_deleted_not_removed():
    """铁律：软删除不物理删。被灰的行必须还在，否则跑不了矛盾行自检。"""
    records = _sample_records()
    S.screen(records, RULES)
    assert len(records) == 10
    assert any(r["verdict"].is_grey for r in records)


def test_every_verdict_carries_a_reason():
    """铁律三：每个判定必须有依据。"""
    records = _sample_records()
    S.screen(records, RULES)
    for rec in records:
        assert rec["verdict"].reasons, f"无判定依据：{rec['title']}"


# ================================================================
# 三单真实 T0 必须被留住（端到端）
# ================================================================

@pytest.mark.parametrize("title", [
    "聯合公告 (1) 完成出售及購買浦江中國控股有限公司擬出售股份 (2) 強制性無條件現金要約 及 (3) 恢復股份買賣",
    "聯合公告 (1) 有關本公司已發行股份總數約27.81%的買賣協議 (2) 自願性有條件全面現金要約 (3) 須予披露交易 及 (4) 復牌",
    "由華富建業企業融資有限公司代表提出附帶先決條件的自願現金部分收購要約",
])
def test_the_three_verified_deals_are_not_screened_out(title):
    """1417 / 3336 / 00195 —— 三单人工已核对过的 T0，一条都不能漏。"""
    assert bucket(title) != S.EXCLUDED, f"真实 T0 被误杀：{title}"
