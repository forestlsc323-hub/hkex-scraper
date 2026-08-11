"""从公告 PDF 里抽出要约字段。

抽的是四样你要的东西，外加复核所需的一切出处：

    要约类型（MGO / VGO / PO）· 要约价 · 溢价率 · 交易规模

**铁律一：这里只做定位与摘录，一个数都不算。**
百分比的复算、基数闭合交给 validators.py，主值选取交给 selectors.py。

**铁律三：每个字段都带 page + quote。** 抽不到就留空并标 confidence=low，
绝不填一个「看起来合理」的数。

正则全部是拿 1417 / 3336 / 00195 三份真实公告试出来的，
测试 `tests/test_extractor.py` 用那三份的原文守着。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MGO, VGO, PO = "MGO", "VGO", "PO"
PREMIUM, DISCOUNT = "premium", "discount"

# ---------------------------------------------------------------- 要约类型
#
# 教训（docs/02）：3336 全文出现 11 次「無條件」，没有一次在描述要约类型
# —— 多是风险警示语「要約未必會成為無條件要約」。全文词频会得出相反答案。
# 所以类型只在**标题**和**明确的类型短语**里判。

_TYPE_PHRASES = [
    # 顺序即优先级：部分要约最特殊，先判
    (PO, re.compile(r"部分(?:收購)?要約|partial\s+offer", re.I)),
    (MGO, re.compile(r"強制性[^，。；]{0,10}要約|強制[^，。；]{0,6}現金要約|"
                     r"mandatory\s+(?:unconditional\s+)?(?:cash\s+)?offer", re.I)),
    (VGO, re.compile(r"自願性?[^，。；]{0,12}要約|voluntary\s+(?:conditional\s+)?"
                     r"(?:cash\s+)?offer", re.I)),
]

# 规则 26.1 = 强制性全面要约的法律依据，比措辞更硬
_RULE_26 = re.compile(r"規則\s*26\.1|rule\s*26\.1", re.I)


@dataclass
class Evidence:
    """一个字段的出处。没有它，这个字段就不该存在（铁律三）。"""

    page: int = 0
    quote: str = ""


@dataclass
class Comparison:
    """价值比较里的一项。"""

    anchor: str            # undisturbed / last_trading_day / pre_rule37 / nav
    window: str            # spot / 5d / 10d / 30d / 180d / nav
    benchmark: str         # 印出来的基准价，原样保留字符串
    benchmark_decimals: int
    benchmark_is_exact: bool   # 印作「約X」= False；直接印数字 = True
    stated_pct: str
    stated_direction: str
    page: int
    quote: str

    @property
    def label(self) -> str:
        names = {"undisturbed": "未受干扰日", "last_trading_day": "最后交易日",
                 "pre_rule37": "3.7公告前", "nav": "每股净资产"}
        windows = {"spot": "收市价", "5d": "前5日均价", "10d": "前10日均价",
                   "30d": "前30日均价", "180d": "前180日均价", "nav": ""}
        return f"{names.get(self.anchor, self.anchor)}{windows.get(self.window, '')}"


@dataclass
class Extraction:
    offeror: str = ""                 # 要约方
    offeror_fa: str = ""              # 要约方财务顾问（标题里「由X代表」的X）
    target: str = ""                  # 受要约方（标题里写全称时）
    parties_evidence: Evidence = field(default_factory=Evidence)
    consideration: str = ""           # 现金 / 证券 / 现金＋证券
    listing_intent: str = ""          # 拟维持上市 / 拟撤销上市
    listing_intent_evidence: Evidence = field(default_factory=Evidence)
    last_trading_day: str = ""        # 停牌前最后交易日
    offer_type: str = ""
    offer_type_evidence: Evidence = field(default_factory=Evidence)
    offer_price: str = ""
    offer_price_evidence: Evidence = field(default_factory=Evidence)
    comparisons: list[Comparison] = field(default_factory=list)
    six_month_low: str = ""
    six_month_high: str = ""
    six_month_evidence: Evidence = field(default_factory=Evidence)
    deal_size: str = ""
    deal_size_evidence: Evidence = field(default_factory=Evidence)
    total_shares: str = ""
    total_shares_evidence: Evidence = field(default_factory=Evidence)
    is_conditional: str = ""          # 无条件 / 有条件 / 附先决条件
    is_conditional_evidence: Evidence = field(default_factory=Evidence)
    debt_conversion: bool = False     # 债转股被动触发 26.1 的痕迹（技术性要约）
    debt_conversion_evidence: Evidence = field(default_factory=Evidence)
    notes: list[str] = field(default_factory=list)

    @property
    def nav_per_share(self) -> str:
        """每股 NAV。它**不做**溢价率主值，但必须单独存 ——
        壳股估值看 P/B 不看 P/E，这个数是估值组的，不是溢价组的。"""
        for c in self.comparisons:
            if c.anchor == "nav":
                return c.benchmark
        return ""

    def spot(self, anchor: str) -> str:
        """某个锚点的收市价。用来算泄露证据（不受干扰→最后交易日涨幅）。"""
        for c in self.comparisons:
            if c.anchor == anchor and c.window == "spot":
                return c.benchmark
        return ""

    def verdict(self) -> tuple[str, str]:
        """这份公告到底是不是一单要约？返回 (判定, 理由)。

        实跑一周，留存桶 15 条里只有 2 条是真要约，其余是普通停复牌公告 ——
        标题层的「復牌／恢復買賣」在**已经筛过一遍的要约表**里是强特征，
        但在全市场里每天都有一堆无关的停复牌。

        表里摆 13 行空白，看起来像程序坏了。所以在正文层再判一次：
        正文连「要約價」和「價值比較」都没有的，就直接说它不像要约，
        而不是留一行空白让人猜。

        注意这是**判定**不是删除（铁律二：软删除）——行照样在表里，
        只是标出来，人一眼能跳过。
        """
        if self.offer_price and self.comparisons:
            return "offer", "正文有要约价与价值比较"
        if not self.offer_price and not self.comparisons:
            return "not_offer", "正文没有「要約價」也没有「價值比較」，不像要约公告"
        missing = "价值比较" if self.offer_price else "要约价"
        return "unclear", f"正文只找到一半：缺{missing}，需人工看一眼"

    @property
    def confidence(self) -> str:
        """有几个主字段抽到了。低置信度的必须人工复核。"""
        got = sum(bool(x) for x in
                  (self.offer_type, self.offer_price, self.deal_size))
        got += bool(self.comparisons)
        return {4: "high", 3: "medium"}.get(got, "low")


# ---------------------------------------------------------------- 工具

# 页脚形如「- 11 -」。拼页时它会卡在两个条目中间，让后一条切不出来，
# 两条粘成一条之后锚点判定必然出错 —— 抽错比抽不到更危险。
_FOOTER = re.compile(r"[-–—]\s*\d{1,4}\s*[-–—]")


def _flat(text: str) -> str:
    """PDF 的换行会把一个句子劈开，先拼回去；顺手去掉页脚。"""
    flat = re.sub(r"[ \t　]+", " ", text.replace("\n", ""))
    return _FOOTER.sub("", flat)


def _decimals(num: str) -> int:
    return len(num.split(".")[1]) if "." in num else 0


def _find_page(pages: dict[int, str], needle: str) -> int:
    for page in sorted(pages):
        if needle in _flat(pages[page]):
            return page
    return 0


# ---------------------------------------------------------------- 类型

def extract_offer_type(title: str, pages: dict[int, str]) -> tuple[str, Evidence]:
    """先看标题，标题判不出再看首页的类型短语。

    绝不做全文词频 —— 3336 那单会被判成相反的类型。
    """
    for kind, pattern in _TYPE_PHRASES:
        m = pattern.search(title or "")
        if m:
            return kind, Evidence(0, m.group(0))

    head = _flat("".join(pages.get(p, "") for p in sorted(pages)[:3]))
    for kind, pattern in _TYPE_PHRASES:
        m = pattern.search(head)
        if m:
            page = _find_page(pages, m.group(0)) or 1
            start = max(0, m.start() - 30)
            return kind, Evidence(page, head[start:m.end() + 30].strip())

    if _RULE_26.search(_flat("".join(pages.values()))):
        return MGO, Evidence(_find_page(pages, "規則26.1"), "收購守則規則26.1")
    return "", Evidence()


# ---------------------------------------------------------------- 要约价

_OFFER_PRICE = [
    re.compile(r"「要約價」[^0-9]{0,40}?每股要約股份\s*([\d.]+)\s*港元"),
    re.compile(r"「要約價」[^0-9]{0,60}?([\d.]+)\s*港元"),
    re.compile(r"要約價為?每股要約股份\s*([\d.]+)\s*港元"),
    re.compile(r"每股要約股份[^0-9]{0,12}?([\d.]+)\s*港元"),
    re.compile(r"要約價[^0-9]{0,20}?([\d.]+)\s*港元"),
]


# 「这份文件在讲一单要约吗」的粗筛。**只用来决定要不要继续往下解析**，
# 不参与任何判定。
#
# 为什么需要它：实跑那 26 份新公告，前 6 份里 5 份的结论是「正文没有
# 要約價也没有價值比較，不像要约公告」—— 而为了得出这句话，我们把一份
# 一两百页的文件从头到尾解析了一遍，每份八十多秒。整份文件解析完，
# 只为了确认它不值得解析。
#
# 所以先看前几页。要约公告的封面必然印着价钱（「每股 X.XX 港元」）
# 或至少印着要约的名目，这批词一个都不出现，就没有必要再往下翻。
#
# 这批词故意放得很松 —— 宁可多解析几份没用的，也不能漏掉一单。
# 松到什么程度可以从日志里看：每份都会报「翻了几页 / 共几页」。
_OFFER_HINT = re.compile(
    r"要約價|註銷價|收購價|價值比較|溢價|折讓|要約人|要約股份|"
    r"每股[^。；]{0,16}?[\d.]+\s*港元|"
    r"強制(?:性)?[^，。；]{0,14}要約|自願(?:性)?[^，。；]{0,14}要約|"
    r"部分要約|購股權要約|全面要約|綜合文件|收購守則")


def looks_like_offer(pages: dict[int, str]) -> bool:
    """前几页里有没有「这是一单要约」的迹象。

    一票否决用的，不是判定 —— 说「有」只代表值得把整份文件解析完，
    真正的判定还是 Extraction.verdict()，口径一点没变。
    """
    return any(_OFFER_HINT.search(_flat(t)) for t in pages.values())


def extract_offer_price(pages: dict[int, str]) -> tuple[str, Evidence]:
    """按模式优先级扫，而不是按页码。

    定义节（「要約價」指…）往往在文末，正文里则会出现 2.2 这种省略写法。
    先扫页会让宽松模式在前面的页上抢先命中，抽到 2.2 而不是 2.20。
    """
    flats = {page: _flat(pages[page]) for page in sorted(pages)}
    for pattern in _OFFER_PRICE:
        for page, flat in flats.items():
            m = pattern.search(flat)
            if m:
                start = max(0, m.start() - 20)
                return m.group(1), Evidence(page, flat[start:m.end() + 20].strip())
    return "", Evidence()


# ---------------------------------------------------------------- 价值比较

_SECTION_START = re.compile(r"(?:要約價的)?價值比較")
_SECTION_END = re.compile(r"最高(?:與|及)最低股價|財務資源|可動用財務資源|"
                          r"要約(?:的)?總代價|確認具備充足財務資源")

# 一项比较里要抓的三样：基准价、方向、百分比
# 「每股」和数字之间可能隔着一长串修饰语，例如
#   每股經審計合併淨資產價值約0.348的港元
# 所以中间允许非数字若干字；「約」出现在数字前就说明这是约整值。
# 「約」可能落在「每股」的**两侧**，两种写法都见过：
#     平均收市價每股約1.168港元          （1417、00195）
#     平均收市價約每股股份3.18港元        （3336）
# 只认后一种会把 3336 的 8 个基准全判成精确值，V4 的区间检验退化成等式检验，
# 于是 11 项里 8 项报假警报 —— 这正是当初设计区间检验要避免的事。
_BENCHMARK = re.compile(
    r"(約)?\s*每股[^0-9%]{0,24}?(約)?\s*([\d,]+\.?\d*)\s*(?:的)?港元")
_PCT = re.compile(r"(溢價|折讓|折價)\s*(?:約)?(?:為)?\s*([\d.]+)\s*%")

_ANCHORS = [
    ("undisturbed", re.compile(r"未受干擾日")),
    ("pre_rule37", re.compile(r"規則\s*3\.7")),
    ("nav", re.compile(r"資產淨值|淨資產|資產價值")),
    ("last_trading_day", re.compile(r"最後交易日")),
]

_WINDOWS = [
    ("180d", re.compile(r"180\s*個|一百八十")),
    ("30d", re.compile(r"三十\s*\(?30\)?\s*個|30\s*個|三十個")),
    ("10d", re.compile(r"十\s*\(?10\)?\s*個|10\s*個|十個")),
    ("5d", re.compile(r"五\s*\(?5\)?\s*個|5\s*個|五個")),
]


# 条目编号：(i) (ii) … 或 1. 2. …
_MARK = re.compile(r"\(\s*(?:[ivx]+|\d{1,2})\s*\)|(?<![\d.])\d{1,2}\s*\.\s")

_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7,
          "viii": 8, "ix": 9, "x": 10, "xi": 11, "xii": 12}


def _mark_value(text: str) -> int | None:
    """把 (iv) / 7. 这样的编号转成序号。认不出返回 None。"""
    core = text.strip().strip("().． ").strip()
    if core.isdigit():
        return int(core)
    return _ROMAN.get(core.lower())


def _split_items(section: str) -> list[str]:
    """把「价值比较」一节按编号拆成一条条。

    三份样本用了两种编号：(i)(ii)…（1417、00195）和 1. 2. 3.（3336）。

    ⚠️ 条文内部本身就带括号数字，会伪装成条目编号：
        「前五(5)個連續交易日」「前三十(30)個連續交易日」
        「（按(i)於本聯合公告日期…及(ii)…計算）」
    劈错的后果不只是漏抽 —— 两条粘在一起时锚点会判成错的那个，
    产出一个看起来正常的错数字。

    判据用「必须连号」：真条目是 1,2,3… 或 i,ii,iii…，逐个递增；
    夹在句子里的 (5) (10) (30) (i) (ii) 接不上序列，一律不算。
    """
    candidates = [(m, _mark_value(m.group(0))) for m in _MARK.finditer(section)]

    marks, expected = [], None
    for m, value in candidates:
        if value is None:
            continue
        if expected is None:
            if value != 1:          # 条目总是从 1 或 i 开始
                continue
            marks.append(m)
            expected = 2
        elif value == expected:
            marks.append(m)
            expected += 1

    if not marks:
        return [section]
    items = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(section)
        items.append(section[m.start():end])
    return items


def _classify(item: str) -> tuple[str, str]:
    """一条比较只描述一个锚点，取**最靠左**出现的那个。

    按列表顺序取会出事：万一两个锚点因为切分失误落进同一条，
    列表靠前的会赢，而正确答案是文中先出现的那个。
    """
    hits = [(m.start(), name) for name, p in _ANCHORS
            for m in [p.search(item)] if m]
    anchor = min(hits)[1] if hits else ""
    if anchor == "nav":
        return "nav", "nav"
    window = next((name for name, p in _WINDOWS if p.search(item)), "spot")
    return anchor or "last_trading_day", window


# 一条比较项长什么样：句子里同时有「溢價/折讓 X%」和「每股…港元」。
# 这个形状比小标题稳得多 —— 小标题各家律所写法不一，形状是收购守则
# 规则 3.5 要求披露的内容，不会变。
_LOOKS_LIKE_COMPARISON = re.compile(
    r"(?=[^；;。]*(?:溢價|折讓|折價)\s*(?:約)?(?:為)?\s*[\d.]+\s*%)"
    r"(?=[^；;。]*每股)")

# 假的价值比较表：认购价/配售价/供股价也有一模一样的表（你陷阱清单里那条）。
# 兜底扫描时必须把它们排掉，否则会把募资价当成要约价的比较。
_FAKE_COMPARISON = re.compile(r"認購價|配售價|供股價|發行價|轉換價|行使價")


def _split_clauses(text: str) -> list[tuple[int, str]]:
    """按分句符切开，返回 [(起始位置, 句子)]。"""
    out, start = [], 0
    for m in re.finditer(r"[；;。]", text):
        out.append((start, text[start:m.end()]))
        start = m.end()
    if start < len(text):
        out.append((start, text[start:]))
    return out


def _scan_whole_document(joined: str) -> list[tuple[int, str]]:
    """找不到小标题时，直接在全文里按「形状」捞比较项。

    实跑年初至今 24 单，有 11 单（46%）是因为 `_SECTION_START` 那个
    「價值比較」小标题没出现而整单落空 —— 有的写「要約價較…」，
    有的干脆没有小标题，直接一段话列下来。要求小标题存在是我定错了。

    形状判据：一句里同时有「溢價/折讓 X%」和「每股…港元」。
    """
    out = []
    for pos, clause in _split_clauses(joined):
        if not _LOOKS_LIKE_COMPARISON.search(clause):
            continue
        if _FAKE_COMPARISON.search(clause):
            continue          # 认购价/配售价的同款表，不是要约价的
        out.append((pos, clause))
    return out


def extract_comparisons(pages: dict[int, str]) -> list[Comparison]:
    """抽「价值比较」。

    两条路：
      1. 有「價值比較」小标题 → 按小标题切出整节，再按编号切条（最精确）；
      2. 没有小标题 → 全文按「形状」捞（兜底，覆盖 46% 的漏抽）。
    """
    joined, page_of = "", {}
    for page in sorted(pages):
        flat = _flat(pages[page])
        page_of[len(joined)] = page
        joined += flat

    start = _SECTION_START.search(joined)
    if not start:
        return _comparisons_from_scan(joined, page_of)
    rest = joined[start.end():]
    end = _SECTION_END.search(rest)
    section = rest[:end.start()] if end else rest[:2500]
    offset = start.end()

    def page_at(pos: int) -> int:
        best = 0
        for at, page in sorted(page_of.items()):
            if at <= offset + pos:
                best = page
        return best

    out: list[Comparison] = []
    cursor = 0
    for item in _split_items(section):
        pos = section.find(item, cursor)
        cursor = pos + len(item)

        pct = _PCT.search(item)
        bench = _BENCHMARK.search(item)
        if not pct or not bench:
            continue
        anchor, window = _classify(item)
        number = bench.group(3).replace(",", "")
        approx = bench.group(1) is not None or bench.group(2) is not None
        out.append(Comparison(
            anchor=anchor, window=window,
            benchmark=number, benchmark_decimals=_decimals(number),
            benchmark_is_exact=not approx,   # 「約」在哪一侧都算约整值
            stated_pct=pct.group(2),
            stated_direction=PREMIUM if pct.group(1) == "溢價" else DISCOUNT,
            page=page_at(pos), quote=item.strip()[:220]))
    return out


def _build(clause: str, page: int) -> Comparison | None:
    """一句 → 一条比较项。抽不齐就返回 None，绝不半拉子入表。"""
    pct = _PCT.search(clause)
    bench = _BENCHMARK.search(clause)
    if not pct or not bench:
        return None
    number = bench.group(3).replace(",", "")
    approx = bench.group(1) is not None or bench.group(2) is not None
    anchor, window = _classify(clause)
    return Comparison(
        anchor=anchor, window=window,
        benchmark=number, benchmark_decimals=_decimals(number),
        benchmark_is_exact=not approx,
        stated_pct=pct.group(2),
        stated_direction=PREMIUM if pct.group(1) == "溢價" else DISCOUNT,
        page=page, quote=clause.strip()[:220])


def _comparisons_from_scan(joined: str, page_of: dict[int, int]) -> list[Comparison]:
    """兜底：没有小标题时按形状全文捞。"""
    def page_at(pos: int) -> int:
        best = 0
        for at, page in sorted(page_of.items()):
            if at <= pos:
                best = page
        return best

    out: list[Comparison] = []
    seen: set[tuple[str, str, str]] = set()
    for pos, clause in _scan_whole_document(joined):
        cmp_ = _build(clause, page_at(pos))
        if cmp_ is None:
            continue
        # 同一条比较可能在摘要和正文各出现一次，去重
        key = (cmp_.anchor, cmp_.window, cmp_.stated_pct)
        if key in seen:
            continue
        seen.add(key)
        out.append(cmp_)
    return out


# ---------------------------------------------------------------- 当事方
#
# 港股要约公告的标题几乎是固定句式，把三方都写在里面：
#
#   由 [要约人财务顾问] （為並）代表 [要约人] 就收購 [受要约方] 全部已發行股份 作出…
#
# 三份样本各是一种写法，但结构一致。做投行 precedent 时，
# 「谁买谁」和「谁做的 FA」是第一眼要看的东西，比溢价率还先看。
#
# 这里仍然只做摘录：从标题里**剪**出这几段字，一个字都不改写。

_AGENT = re.compile(r"為並代表|為代表|代表")
# FA 段的左边界：**编号**括号、「由」、连接词。
#
# ⚠️ 只认编号括号（里面纯数字或罗马数字），不能认所有括号 ——
# 券商名字里带括号是常态：中國銀河國際證券(香港)有限公司、
# 建銀國際(控股)有限公司。按任意括号切，FA 会被切成「有限公司」。
# 实测 01657 那单就是这么错的。
_FA_LEFT = re.compile(r"[(（]\s*(?:\d{1,2}|[ivxIVX]{1,4})\s*[)）]|由|及|and\s", re.I)

# 这些不是名字，是公告里的通称。抽到它们等于没抽到 ——
# 表里出现一个叫「要約人」的要约方，比留空更糟：它看着像抽到了。
_PLACEHOLDER = {"要約人", "要约人", "本公司", "該公司", "买方", "買方",
                "offeror", "the offeror", "有限公司", "公司"}
# 要约人段的右边界：接下来必然是动词或介词
_OFFEROR_RIGHT = re.compile(r"就|提出|作出|向|對|以|，|。|,")

# 标题里常有两处「收購」——「部分收購要約以收購綠科科技…」。
# 非贪婪从第一处起匹配会把「要約以收購」一起吃进公司名里，
# 所以名字段里明确不许再出现「收購」「要約」。
_TARGET = re.compile(
    r"收購\s*((?:(?!收購|要約)[^，。；]){2,40}?)\s*(?:之|的)?"
    r"(?:全部已發行股份|全部已發行股本|不超過|全部股份)")

# 释义节里的兜底：「要約人」 指 XXX
#
# ⚠️ 释义表是**两栏排版**，扁平化成一行后，左栏的词条和右栏的定义直接
# 连在一起，前后两行之间也没有边界。于是有两种脏法，实跑都见过：
#   左边漏进来：「要約人」或「買方」 Sky Links Group Limited  →  08439
#   右边漏进来：「要約人」 楊敬堯先生「海外股東」              →  01780
# 所以词条那段要把别名一起吃掉，名字那段则明确不许出现「」——
# 公司名和人名从不带这对引号，见到就是越过了栏或行的边界。
_OFFEROR_DEF = re.compile(r"「要約人」(?:\s*(?:或|及|、)\s*「[^」]{1,24}」)*"
                          r"\s*(?:指|指的是)?\s*([^，。；、「」]{2,60}?)"
                          r"\s*(?:，|。|；|指|一家|之|「)")


def _cut_fa(before: str) -> str:
    """标题里 FA 的名字夹在编号和「代表」之间，从右往左找左边界。"""
    left = 0
    for m in _FA_LEFT.finditer(before):
        left = m.end()
    return before[left:].strip(" 　-–—")


def _is_placeholder(name: str) -> bool:
    return name.strip().lower() in _PLACEHOLDER


def extract_parties(title: str, pages: dict[int, str]) -> dict:
    """从标题剪出 要约人 / 要约人财务顾问 / 受要约方。

    抽不到就留空 —— 受要约方还能退回列表层的 STOCK_NAME（那是披露易
    自己给的归属，比猜可靠），要约人和 FA 没有退路，宁可空着。
    """
    title = title or ""
    out = {"offeror": "", "offeror_fa": "", "target": "",
           "parties_evidence": Evidence()}

    m = _AGENT.search(title)
    if m:
        fa = _cut_fa(title[:m.start()])
        rest = title[m.end():]
        cut = _OFFEROR_RIGHT.search(rest)
        offeror = (rest[:cut.start()] if cut else rest).strip(" 　")
        if 2 <= len(fa) <= 40 and not _is_placeholder(fa):
            out["offeror_fa"] = fa
        if 2 <= len(offeror) <= 60 and not _is_placeholder(offeror):
            out["offeror"] = offeror
            out["parties_evidence"] = Evidence(0, title[max(0, m.start() - 30):
                                                        m.end() + 60].strip())

    t = _TARGET.search(title)
    if t:
        out["target"] = t.group(1).strip(" 　")

    # 标题里写的是通称（「代表要約人提出…」）或压根没写，退到释义节找真名
    if not out["offeror"]:
        for page in sorted(pages):
            flat = _flat(pages[page])
            d = _OFFEROR_DEF.search(flat)
            if d and not _is_placeholder(d.group(1)):
                out["offeror"] = d.group(1).strip()
                out["parties_evidence"] = Evidence(page, d.group(0)[:200])
                break
    return out


# ---------------------------------------------------------------- 交易条款

# 对价形式：现金 / 证券 / 混合。港股要约绝大多数是纯现金，
# 但「證券交換要約」估值口径完全不同，必须能一眼分辨。
_CASH = re.compile(r"現金要約|以現金(?:方式)?(?:支付|作出)|現金代價")
_SECURITIES = re.compile(r"證券交換要約|以.{0,6}股份.{0,4}支付|換股要約")

# 要约人对上市地位的意向：维持 vs 撤销。这决定这单能不能当私有化可比。
_KEEP_LISTING = re.compile(r"維持[^。；]{0,20}上市地位|保持[^。；]{0,20}上市地位")
_DELIST = re.compile(r"撤(?:銷|回)[^。；]{0,20}上市地位|私有化")

# 停牌前最后交易日：两种写法都见过
_LAST_TRADING_DAY = [
    re.compile(r"(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)\s*[（(]\s*即\s*最後交易日"),
    re.compile(r"最後(?:一個)?交易日\s*[，,]?\s*即\s*(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)"),
    re.compile(r"最後(?:一個)?交易日\s*(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)"),
]


def extract_terms(title: str, pages: dict[int, str]) -> dict:
    """对价形式、上市地位意向、停牌前最后交易日。

    三项都是「有就摘、没有就空」，不做任何推断 ——
    比如「没写撤销上市」不等于「维持上市」，那样的默认值就是编造。
    """
    out = {"consideration": "", "listing_intent": "", "last_trading_day": "",
           "listing_intent_evidence": Evidence()}
    whole = title or ""
    flats = {p: _flat(pages[p]) for p in sorted(pages)}
    whole += "".join(flats.values())

    has_cash = bool(_CASH.search(whole))
    has_sec = bool(_SECURITIES.search(whole))
    out["consideration"] = ("现金＋证券" if has_cash and has_sec
                            else "现金" if has_cash
                            else "证券" if has_sec else "")

    for page, flat in flats.items():
        m = _DELIST.search(flat) or _KEEP_LISTING.search(flat)
        if m:
            out["listing_intent"] = ("拟撤销上市" if _DELIST.match(m.group(0))
                                     else "拟维持上市")
            out["listing_intent_evidence"] = Evidence(page, m.group(0)[:200])
            break

    for page, flat in flats.items():
        for pattern in _LAST_TRADING_DAY:
            m = pattern.search(flat)
            if m:
                out["last_trading_day"] = re.sub(r"\s+", "", m.group(1))
                break
        if out["last_trading_day"]:
            break
    return out


# ---------------------------------------------------------------- 估值组

# 已发行股数。乘上要约价就是隐含股权价值（equity value）。
#
# 「全部已發行股本估值」不是垃圾，只是**不能当 deal size** ——
# 它是估值指标，投行做倍数时正要用它。所以它归估值组，不归规模组。
# 抽的是股数，乘法交给 Python（铁律一）。
_TOTAL_SHARES = [
    re.compile(r"已發行股份總數為?\s*([\d,]+)\s*股"),
    re.compile(r"合共\s*([\d,]+)\s*股股份"),
    re.compile(r"已發行\s*([\d,]+)\s*股(?:股份)?"),
    re.compile(r"([\d,]{9,})\s*股已發行股份"),
]


def extract_total_shares(pages: dict[int, str]) -> tuple[str, Evidence]:
    for page in sorted(pages):
        flat = _flat(pages[page])
        for pattern in _TOTAL_SHARES:
            m = pattern.search(flat)
            if m:
                start = max(0, m.start() - 40)
                return (m.group(1).replace(",", ""),
                        Evidence(page, flat[start:m.end() + 10].strip()))
    return "", Evidence()


# ---------------------------------------------------------------- 条件

# 无条件 MGO ＝ 已成事实；有条件 VGO ＝ 还要判断能不能成。
# 这个区分决定这单在可比表里怎么用，标题里就写着。
_CONDITIONAL = [
    ("附先决条件", re.compile(r"附帶先決條件|具有前置條件|附有先決條件")),
    ("无条件", re.compile(r"無條件")),
    ("有条件", re.compile(r"有條件")),
]


def extract_conditionality(title: str, pages: dict[int, str]) -> tuple[str, Evidence]:
    for label, pattern in _CONDITIONAL:
        m = pattern.search(title or "")
        if m:
            return label, Evidence(0, (title or "")[max(0, m.start() - 20):
                                                    m.end() + 20].strip())
    head = _flat("".join(pages.get(p, "") for p in sorted(pages)[:2]))
    for label, pattern in _CONDITIONAL:
        m = pattern.search(head)
        if m:
            return label, Evidence(1, head[max(0, m.start() - 30):m.end() + 30])
    return "", Evidence()


# ---------------------------------------------------------------- 技术性要约

# 债转股被动触发规则 26.1 → 要约价＝换股价，走程序保上市地位。
# 这类单的溢价率和真收购完全不是一回事，混进中位数就是废数据。
_DEBT_CONVERSION = re.compile(
    r"可換股債券|可轉換債券|債務轉[換股]|換股價|轉換價|"
    r"以股代債|資本化.{0,8}債務|PSCS|優先股.{0,6}轉換")


def extract_debt_conversion(pages: dict[int, str]) -> tuple[bool, Evidence]:
    for page in sorted(pages):
        flat = _flat(pages[page])
        m = _DEBT_CONVERSION.search(flat)
        if m:
            start = max(0, m.start() - 60)
            return True, Evidence(page, flat[start:m.end() + 60].strip())
    return False, Evidence()


# ---------------------------------------------------------------- 六个月区间

# ⚠️ 中间可能夹着日期：「最高收市價為**於2026年6月8日的**每股1.980港元」。
# 原来写成 [^0-9]{0,40} 不许出现数字，被这个日期整段挡掉 ——
# 1417 和 00195 的六个月区间就是这么丢的，而丢了 V6 就没得校验。
_SIX_MONTH = re.compile(
    r"最高收市價.{0,60}?每股\s*(?:約)?\s*([\d.]+)\s*港元.{0,160}?"
    r"最低收市價.{0,60}?每股\s*(?:約)?\s*([\d.]+)\s*港元")


def extract_six_month(pages: dict[int, str]) -> tuple[str, str, Evidence]:
    for page in sorted(pages):
        flat = _flat(pages[page])
        m = _SIX_MONTH.search(flat)
        if m:
            return m.group(2), m.group(1), Evidence(page, m.group(0)[:220])
    return "", "", Evidence()


# ---------------------------------------------------------------- 交易规模

# 港式中文的量词。少了「百萬」这一个，「約66.8百萬港元」就抽不出来 ——
# 而这是香港公告里最常见的写法之一。
_UNIT = {"萬": 10_000, "万": 10_000, "億": 100_000_000, "亿": 100_000_000,
         "百萬": 1_000_000, "百万": 1_000_000,
         "千萬": 10_000_000, "千万": 10_000_000}

# 量词要按长度倒序排进正则，否则「百萬」会被「萬」先吃掉一半
_UNIT_ALT = "|".join(sorted(_UNIT, key=len, reverse=True))
_AMOUNT = rf"(?P<num>[\d,]+\.?\d*)\s*(?P<unit>{_UNIT_ALT})?\s*港元"

# 实跑年初至今，12 单里有 6 单交易规模是空的 —— 原来只认三种措辞太窄。
# 顺序即优先级：越明确写「最高」的越靠前。
#
# 口径始终是同一个（你那句判别口诀）：这笔钱付给谁？
# 付给接纳要约的公众股东的才算，付给特定卖方的不算。
_DEAL_SIZE = [
    # 「須支付的最高現金代價約為5,440萬港元」
    # 「應付之最高現金金額為1,905,849,908.60港元」
    re.compile(rf"最高(?:現金)?(?:總)?(?:代價|金額|價值|款項|總額)"
               rf"[^0-9]{{0,24}}?{_AMOUNT}"),
    # 「須支付的最高總代價」这种把「最高」和名词拆开的写法
    re.compile(rf"(?:應付|須支付|需支付|須付|所需)[^0-9]{{0,12}}?最高"
               rf"[^0-9]{{0,24}}?{_AMOUNT}"),
    # 「須支付的現金代價總額為92,000,000港元」
    re.compile(rf"(?:現金)?(?:代價|款項)總額[^0-9]{{0,24}}?{_AMOUNT}"),
    # 「倘要約獲悉數接納，應付總額約為…」
    #
    # ⚠️ 这条最松，必须挡住「每股」——「倘要約獲悉數接納，按每股要約股份
    # 0.01港元計算」会被它抓成交易规模 0.01。实跑里 02362 金川國際那单
    # 的交易规模就是这么变成 0.01 的（答案是 7,000,000）。
    # 每股价永远不是交易规模：一个是单价，一个是总额。
    re.compile(rf"(?:悉數接納|全數接納|全部接納)"
               rf"((?:(?!每股)[^。；]){{0,60}}?){_AMOUNT}"),
    # 「要約項下之總代價約為…」
    re.compile(rf"要約(?:項下)?(?:之|的)?(?:總代價|總價值)"
               rf"[^0-9]{{0,24}}?{_AMOUNT}"),
]


def extract_deal_size(pages: dict[int, str]) -> tuple[str, Evidence]:
    """取「要约项下最高现金代价」。

    口径来自你三单人工答案的反推（见 selectors.py）：
    不是控股权转让对价，也不是 100% 股本估值。

    中文数字单位（萬/億）在这里换算成整数 —— 这是**单位换算不是计算**，
    换算依据写进 quote 里可复核。
    """
    for page in sorted(pages):
        flat = _flat(pages[page])
        for pattern in _DEAL_SIZE:
            m = pattern.search(flat)
            if m:
                raw = m.group("num").replace(",", "")
                unit_char = m.group("unit") or ""
                start = max(0, m.start() - 40)
                evidence = Evidence(page, flat[start:m.end() + 10].strip())
                if not unit_char:
                    # 公告直接印了完整数字，原样保留 —— 取整会丢掉角分
                    return raw, evidence
                # 有「萬」「億」才换算。这是单位换算，不是计算；依据留在 quote 里。
                return f"{float(raw) * _UNIT[unit_char]:.0f}", evidence
    return "", Evidence()


# ---------------------------------------------------------------- 总入口

def extract(title: str, pages: dict[int, str]) -> Extraction:
    """一份公告 → 结构化字段。抽不到的留空，绝不猜。"""
    result = Extraction()

    parties = extract_parties(title, pages)
    result.offeror = parties["offeror"]
    result.offeror_fa = parties["offeror_fa"]
    result.target = parties["target"]
    result.parties_evidence = parties["parties_evidence"]

    terms = extract_terms(title, pages)
    result.consideration = terms["consideration"]
    result.listing_intent = terms["listing_intent"]
    result.listing_intent_evidence = terms["listing_intent_evidence"]
    result.last_trading_day = terms["last_trading_day"]

    result.offer_type, result.offer_type_evidence = extract_offer_type(title, pages)
    result.offer_price, result.offer_price_evidence = extract_offer_price(pages)
    result.comparisons = extract_comparisons(pages)
    result.six_month_low, result.six_month_high, result.six_month_evidence = \
        extract_six_month(pages)
    result.deal_size, result.deal_size_evidence = extract_deal_size(pages)
    result.total_shares, result.total_shares_evidence = extract_total_shares(pages)
    result.is_conditional, result.is_conditional_evidence = \
        extract_conditionality(title, pages)
    result.debt_conversion, result.debt_conversion_evidence = \
        extract_debt_conversion(pages)

    if not result.offer_type:
        result.notes.append("要约类型未识别，需人工判定")
    if not result.comparisons:
        result.notes.append("未找到「價值比較」一节，溢价率无法抽取")
    if not result.deal_size:
        result.notes.append("交易规模未识别")
    if not result.offeror:
        result.notes.append("要约方未识别，需人工从公告首页读取")
    return result
