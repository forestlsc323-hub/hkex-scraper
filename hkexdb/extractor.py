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
from functools import lru_cache

MGO, VGO, PO = "MGO", "VGO", "PO"
PREMIUM, DISCOUNT = "premium", "discount"

# ---------------------------------------------------------------- 要约类型
#
# 教训（docs/02）：3336 全文出现 11 次「無條件」，没有一次在描述要约类型
# —— 多是风险警示语「要約未必會成為無條件要約」。全文词频会得出相反答案。
# 所以类型只在**标题**和**明确的类型短语**里判。

# ⚠️ 「部份」和「部分」在港交所公告里是混用的，后者是简体习惯，
# 前者才是港式繁体的常见写法。只认「部分」会把一单部分要约判成别的类型 ——
# 09638 法拉帝实跑被判成 VGO（应为 PO），标题写的是「部份」。
_TYPE_PHRASES = [
    # 顺序即优先级：部分要约最特殊，先判
    (PO, re.compile(r"部[分份](?:收購)?要約|partial\s+offer", re.I)),
    (MGO, re.compile(r"強制性[^，。；]{0,10}要約|強制[^，。；]{0,6}現金要約|"
                     r"mandatory\s+(?:unconditional\s+)?(?:cash\s+)?offer", re.I)),
    (VGO, re.compile(r"自願性?[^，。；]{0,12}要約|voluntary\s+(?:conditional\s+)?"
                     r"(?:cash\s+)?offer", re.I)),
]

# 「強制性」和「部分」不可能同时成立：收購守則規則 26 要求的强制要约
# 必须向**全体**股东、就**全部**股份提出；部分要约要执行人员同意，
# 本质上是自願的。两个词一起出现时，「強制性」是那个说了算的 ——
# 「部分」多半出现在别处（例如「部分股東已承諾接納」）。
# 01796 实跑被判成 PO，答案是 MGO。
_MANDATORY = _TYPE_PHRASES[1][1]

# ⚠️ 但「強制性全面要約」这个词组最常见的出处其实是**清洗豁免**：
# 「申請豁免…須提出強制性全面要約的責任」。那种句子说的是这单
# **不必**做强制要约，拿它去压过「部分要约」正好压反。
# 所以只有在附近没有豁免字样时，「強制性」才算数。
_WAIVER = re.compile(r"豁免|免除|清洗|寬免|whitewash", re.I)

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
                   "30d": "前30日均价", "60d": "前60日均价",
                   "180d": "前180日均价", "nav": ""}
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


_SPACES = re.compile(r"[ \t　]+")


@lru_cache(maxsize=512)
def _flat(text: str) -> str:
    """PDF 的换行会把一个句子劈开，先拼回去；顺手去掉页脚。

    ⚠️ 这是全篇最热的一个函数：十几个 extract_* 各自遍历一遍 pages，
    一份 60 页的公告要调它 800 多次，占整个抽取层七成时间。
    它是纯函数（同一页永远得同一个结果），所以加记忆就够，
    不必去改十几个调用方的结构。
    """
    flat = _SPACES.sub(" ", text.replace("\n", ""))
    return _FOOTER.sub("", flat)


def _decimals(num: str) -> int:
    return len(num.split(".")[1]) if "." in num else 0


def _find_page(pages: dict[int, str], needle: str) -> int:
    for page in sorted(pages):
        if needle in _flat(pages[page]):
            return page
    return 0


# ---------------------------------------------------------------- 类型

def _waived(text: str, m) -> bool:
    """这个「強制性…要約」是不是正被豁免掉的那个。

    清洗豁免公告里满篇都是「申請豁免根據規則26.1提出強制性全面要約的
    責任」—— 句子说的是这单**不必**做强制要约，照字面读正好读反。
    """
    return bool(_WAIVER.search(text[max(0, m.start() - 30):m.end()]))


def _phrase_type(text: str):
    """在一段文字里找类型短语。返回 (类型, match) 或 (None, None)。"""
    for kind, pattern in _TYPE_PHRASES:
        m = pattern.search(text)
        if not m:
            continue
        # 「強制性」压过「部分」：规则 26 的强制要约必须就全部股份提出，
        # 不可能同时是部分要约。见 _MANDATORY 处的说明。
        if kind is PO:
            hard = _MANDATORY.search(text)
            if hard and not _waived(text, hard):
                return MGO, hard
        elif kind is MGO and _waived(text, m):
            continue          # 被豁免掉的那个不算，接着看下一档
        return kind, m
    return None, None


def extract_offer_type(title: str, pages: dict[int, str]) -> tuple[str, Evidence]:
    """证据从硬到软排三档：标题 → 規則26.1 → 正文首几页的类型短语。

    绝不做全文词频 —— 3336 那单会被判成相反的类型。

    **規則26.1 排在正文短语前面**，这是这一版改的。
    26.1 是强制性全面要约的法律依据，一份公告写下它就等于说
    「这单是规则 26 触发的强制要约」；而正文里蹦出来一个「部分」，
    可能只是「部分股東已承諾接納」「部分代價以股份支付」。
    法条比措辞硬 —— 实跑里 01980 / 01796 / 02362 三单被正文的「部分」
    判成 PO，答案都是 MGO。

    但标题仍然排在最前：标题写明「部分收購要約」是最权威的，
    而几乎每份收购文件的释义节都会顺带提到 26.1。
    """
    kind, m = _phrase_type(title or "")
    if kind:
        return kind, Evidence(0, m.group(0))

    head = _flat("".join(pages.get(p, "") for p in sorted(pages)[:3]))
    whole = _flat("".join(pages.values()))
    rule = _RULE_26.search(whole)
    if rule and not _WAIVER.search(whole[max(0, rule.start() - 40):rule.end()]):
        return MGO, Evidence(_find_page(pages, rule.group(0)),
                             whole[max(0, rule.start() - 40):rule.end() + 40].strip())

    kind, m = _phrase_type(head)
    if kind:
        start = max(0, m.start() - 30)
        return kind, Evidence(_find_page(pages, m.group(0)) or 1,
                              head[start:m.end() + 30].strip())

    return "", Evidence()


# ---------------------------------------------------------------- 要约价

# ⚠️ 「面值」是这里最阴险的陷阱。港股公司章程里满篇都是
# 「每股面值 0.01 港元」，而它和要约价长得一模一样：都是「每股 X 港元」。
#
# 02362 金川國際实跑抽出要约价 0.01、溢价 -98.38% —— 那不是要约价，
# 是股份面值。数字本身「自洽」（0.01 对 0.617 确实是 -98.4%），所以
# 校验器一个都拦不住，人看报表也只觉得这单折让离谱而已。
# 这正是铁律二说的静默污染：错得很像对的。
#
# 所以宽松的那几条必须逐字符挡住「面值」，不能只在开头挡 ——
# 「要約價…較每股面值0.01港元」这种写法会从中间绕过去。
_NOT_PAR = r"(?:(?!面值|票面|股本面值)[^。；])"

_OFFER_PRICE = [
    re.compile(r"「要約價」[^0-9]{0,40}?每股要約股份\s*([\d.]+)\s*港元"),
    re.compile(rf"「要約價」{_NOT_PAR}{{0,60}}?([\d.]+)\s*港元"),
    re.compile(r"要約價為?每股要約股份\s*([\d.]+)\s*港元"),
    re.compile(rf"每股要約股份{_NOT_PAR}{{0,12}}?([\d.]+)\s*港元"),
    re.compile(rf"要約價{_NOT_PAR}{{0,20}}?([\d.]+)\s*港元"),
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


# 交易规模至少得是「要约价 × 这么多股」。
# 港股最小的要约也涉及百万级股数，1000 股留了三个数量级的余量 ——
# 这道闸不是用来判断规模对不对的，只用来拦住「总额等于每股价」这种
# 定义上就不可能的数：02362 实跑抽出要约价 0.01、交易规模 0.01。
_MIN_SHARES_IN_A_DEAL = 1000


def _num_or_none(value):
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# 要约价最多能比六个月最高价高这么多倍。
# 溢价 100%~200% 的单子真实存在（08413 亞洲富思 +136%），所以闸要开得松；
# 它防的不是「溢价高」，是「数量级错了」。
_PRICE_SANITY_MULTIPLE = 10


def _drop_impossible_offer_price(result) -> None:
    """要约价高出六个月最高价一个数量级 —— 那不是要约价，是别的数字。

    ⚠️ 这条防的不是正则写得松，是**PDF 的文字顺序乱了**。
    09929 澳達控股那份把数字放在独立的文字层，pdfplumber 顺序读出来是：

        按每股要約股0.11份 港元的要約價計算
        按每股要約股份220.0港元的要約價計算，本公司的已發行股本總額將為 百萬港元

    真实句子是「每股要約股份 0.11 港元…已發行股本總額將為 220.0 百萬港元」。
    数字被搬到了错的位置，于是 220.0（股本总额，单位百万）被当成了要约价。
    00834 康大食品同一个病。

    正则救不了排版错乱，但**算术能**：这家公司六个月最高价 0.116 港元，
    要约价 220 是它的一千九百倍。留一个这样的数比留空坏得多。
    """
    price = _num_or_none(result.offer_price)
    # 参照物有两个来源，谁在就用谁：六个月最高价，或价值比较里最高的
    # 那个基准价。09929 那份排版错乱到连六个月最高价都没抽到，
    # 只靠一个来源这道闸就形同虚设。
    refs = [_num_or_none(result.six_month_high)]
    refs += [_num_or_none(c.benchmark) for c in result.comparisons]
    refs = [r for r in refs if r and r > 0]
    if not price or not refs:
        return
    high = max(refs)
    if price > high * _PRICE_SANITY_MULTIPLE:
        result.notes.append(
            f"抽到的要约价 {result.offer_price} 是参照价 {high} 的 "
            f"{price / high:.0f} 倍（数量级不对，多半是 PDF 里数字与文字错位），"
            f"已作废，需人工读原文")
        result.offer_price = ""
        result.offer_price_evidence = Evidence()


def _drop_impossible_deal_size(result) -> None:
    """总代价不可能等于每股价。对不上就把规模清掉，绝不留一个假数。

    铁律三：没有出处的字段一律留空。抽到一个**定义上就不可能**的数字，
    比留空坏得多 —— 留空会被人补上，错数会被人直接粘进底稿。
    """
    try:
        price = float(result.offer_price)
        size = float(result.deal_size)
    except (TypeError, ValueError):
        return
    if price <= 0 or size <= 0:
        return
    if size < price * _MIN_SHARES_IN_A_DEAL:
        result.notes.append(
            f"抽到的交易规模 {result.deal_size} 与每股 {result.offer_price} "
            f"港元不相称（总代价不可能这么接近每股价），已作废，需人工读原文")
        result.deal_size = ""
        result.deal_size_evidence = Evidence()
        return

    # 天花板：要约只买公众股东手上那部分，付出去的钱不可能超过
    # 「按要约价把整家公司买下来」。超了就是抓到了别的东西 ——
    # 2025 实测 03626 / 01747 / 01489 三单都超了，不用看原文就知道错。
    shares = _num_or_none(result.total_shares)
    if shares and size > price * shares * 1.01:
        whole = price * shares
        result.notes.append(
            f"抽到的交易规模 {result.deal_size} 超过按要约价计的全部股本估值 "
            f"{whole:,.0f}（要约买不到比整家公司还多），已作废，需人工读原文")
        result.deal_size = ""
        result.deal_size_evidence = Evidence()


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
# 「每股」不是唯一写法。实跑里这三种都出现过，只认第一种就整单落空：
#     …收市價每股0.115港元            （08413，最常见）
#     …每一股份收市價12.00港元        （03389 亨得利）
#     …的股份平均收市價0.119港元      （03389 亨得利，第 3 条）
# 03389 那单五条比较一条都没抽出来，就是因为它通篇不写「每股」。
_PER_SHARE = r"(?:每股|每一股份|股份)"
_BENCHMARK = re.compile(
    rf"(約)?\s*{_PER_SHARE}[^0-9%]{{0,24}}?(約)?\s*([\d,]+\.?\d*)\s*(?:的)?港元")

# 「溢價／折讓」和百分比的**前后顺序两种都有**：
#     折讓約14.13%          （词在前，最常见）
#     有大約0.125%之溢價    （数在前 —— 03389 全篇都是这种）
# 只认词在前的话，03389 那五条比较全部匹配不上，整单溢价率留空。
_PCT_WORD_FIRST = re.compile(r"(溢價|折讓|折價)\s*(?:約)?(?:為)?\s*([\d.]+)\s*%")
_PCT_NUM_FIRST = re.compile(r"([\d.]+)\s*%\s*(?:之|的)?\s*(溢價|折讓|折價)")


class _Pct:
    """一处百分比：词、数、以及它在句子里的位置。"""

    __slots__ = ("word", "number", "start", "end")

    def __init__(self, word: str, number: str, start: int, end: int):
        self.word, self.number = word, number
        self.start, self.end = start, end

    @property
    def direction(self) -> str:
        return PREMIUM if self.word == "溢價" else DISCOUNT


def _pcts(text: str) -> list:
    """句子里所有的「溢價/折讓 X%」，两种词序都认，按出现先后排。"""
    found = []
    for m in _PCT_WORD_FIRST.finditer(text):
        found.append(_Pct(m.group(1), m.group(2), m.start(), m.end()))
    for m in _PCT_NUM_FIRST.finditer(text):
        # 「折讓約 14.13%」在两个正则下都会命中一次，位置重叠的算一处
        if any(f.start < m.end() and m.start < f.end for f in found):
            continue
        found.append(_Pct(m.group(2), m.group(1), m.start(), m.end()))
    found.sort(key=lambda f: f.start)
    return found


# 「分別」是并列句的标记：三个基准价列完再列三个百分比，一一对应关系
# 靠语序而不是靠位置（01310 香港寬頻）。这种句子拆不安全 ——
#     「…分別約每股3.228港元、每股2.892港元及每股2.701港元分別溢價約
#       57.20%、75.48%及87.87%」
# 按「最近的那个基准价」去配，第二、三条会配到错的基准上。
# 所以只取并列开始前的那一条，其余标出来让人补（铁律二：不静默瞎配）。
_PARALLEL = re.compile(r"分別|分别")

_PARALLEL_NOTE = ("价值比较里有「分別…」并列句，只取了并列开始前的那一条，"
                  "其余口径请人工补（拆不准就不硬拆）")

_ANCHORS = [
    # ⚠️「未受干擾日」和「不受干擾日期」两种写法都有 —— 一字之差。
    # 08439 新百利用的是「不」，于是那 5 条全被判成「最後交易日」，
    # 而锚点判错不会报错，只会让主值溢价率取到另一条，看着完全正常。
    ("undisturbed", re.compile(r"[未不]受干擾日")),
    ("pre_rule37", re.compile(r"規則\s*3\.7")),
    ("nav", re.compile(r"資產淨值|淨資產|資產價值")),
    ("last_trading_day", re.compile(r"最後交易日")),
]

# ⚠️ 表格排版会把「…的三十(30)個交易日」拦腰劈开，扁平化后变成
# 「…的三 0.356 129.8% 157.9%十(30)個交易日」—— 中文数字和括号数字之间
# 插进了三列数据。所以括号里那个数字必须能**单独**认出来，
# 不能只认「三十(30)個」这种连在一起的完整写法。
# 08439 新百利那张表里 5 条比较的窗口全判成了「收市价」，就是这么来的。
_WINDOWS = [
    ("180d", re.compile(r"一百八十|\(?\s*180\s*\)?\s*個")),
    ("60d", re.compile(r"六十|\(?\s*60\s*\)?\s*個")),
    ("30d", re.compile(r"三十|\(?\s*30\s*\)?\s*個")),
    ("10d", re.compile(r"十\s*\(?10\)?\s*個|\(?\s*10\s*\)?\s*個|十個")),
    ("5d", re.compile(r"五\s*\(?5\)?\s*個|\(?\s*5\s*\)?\s*個|五個")),
]


# 条目编号：(i) (ii) … 或 1. 2. …
# ⚠️ 港交所公告的价值比较用**三种**编号，不是两种：
#     (i)(ii)…      1417、00195、02362
#     1. 2. 3.      3336
#     (a)(b)(c)…    01875 東曜藥業   ← 这一种漏了
# 漏一种的后果不是少抽几条，是整节切不开：01875 那节有 12 条比较，
# 只抽出来 1 条（第一条），主值溢价率因此取到 99.00%（未受干扰日收市价）
# 而不是 114.67%（未受干扰日前 30 日均价）—— 差 15 个百分点，
# 而且看起来完全正常。
#
# 罗马数字和字母序号在 (i) 上撞车：i 既是罗马数字 1，也是字母表第 9 个。
# 靠「必须连号」化解 —— 见 _split_items。
_MARK = re.compile(r"\(\s*(?:[ivx]+|[a-z]|\d{1,2})\s*\)|(?<![\d.])\d{1,2}\s*\.\s"
                   # 00372 保德用的是不带括号的「i. ii. iii. … viii.」。
                   # 不认它的话整节切不开，一整节当成一条 —— 于是
                   # 第一个数字（要约价自己）成了基准价。
                   r"|(?<![A-Za-z])[ivx]{1,4}\s*\.\s")

_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7,
          "viii": 8, "ix": 9, "x": 10, "xi": 11, "xii": 12}
_LETTER = {chr(ord("a") + i): i + 1 for i in range(26)}


def _mark_value(text: str, alphabetic: bool = False) -> int | None:
    """把 (iv) / 7. / (c) 这样的编号转成序号。认不出返回 None。

    `alphabetic` 决定单个字母按哪套算：(i) 在罗马编号里是 1，
    在字母编号里是第 9 个。两种解释都试一遍，谁能连成序列就用谁。
    """
    core = text.strip().strip("().． ").strip().lower()
    if core.isdigit():
        return int(core)
    if alphabetic and len(core) == 1:
        return _LETTER.get(core)
    return _ROMAN.get(core)


def _consecutive_marks(section: str, alphabetic: bool) -> list:
    """找出真正连号的那串编号。接不上序列的一律不算。"""
    marks, expected = [], None
    for m in _MARK.finditer(section):
        value = _mark_value(m.group(0), alphabetic)
        if value is None:
            continue
        if expected is None:
            if value != 1:          # 条目总是从 1 / i / a 开始
                continue
            marks.append(m)
            expected = 2
        elif value == expected:
            marks.append(m)
            expected += 1
    return marks


def _split_items(section: str) -> list[str]:
    """把「价值比较」一节按编号拆成一条条。

    实测三种编号：(i)(ii)…（1417 / 00195 / 02362）、1. 2. 3.（3336）、
    (a)(b)(c)…（01875 東曜藥業）。

    ⚠️ 条文内部本身就带括号数字，会伪装成条目编号：
        「前五(5)個連續交易日」「前三十(30)個連續交易日」
        「（按(i)於本聯合公告日期…及(ii)…計算）」
    劈错的后果不只是漏抽 —— 两条粘在一起时锚点会判成错的那个，
    产出一个看起来正常的错数字。

    判据用「必须连号」：真条目是 1,2,3… 或 i,ii,iii… 或 a,b,c…，逐个
    递增；夹在句子里的 (5) (10) (30) (i) (ii) 接不上序列，一律不算。

    罗马和字母两套解释在 (i) 上撞车（罗马的 1 ＝ 字母的第 9 个），
    所以两套各切一遍，谁切出来的条目多就用谁 —— 连号本身就是判据，
    接不上的那套自然切不出东西。
    """
    best: list = []
    for alphabetic in (False, True):
        marks = _consecutive_marks(section, alphabetic)
        if len(marks) > len(best):
            best = marks

    if not best:
        return [section]
    marks = best
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


def extract_comparisons(pages: dict[int, str],
                        notes: list | None = None) -> list[Comparison]:
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
        return _comparisons_from_scan(joined, page_of, notes)
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
    parallel = False
    for item in _split_items(section):
        pos = section.find(item, cursor)
        cursor = pos + len(item)
        found, skipped = _comparisons_in(item, page_at(pos))
        out.extend(found)
        parallel = parallel or skipped

    # 小标题在、但一条都没切出来 → 多半是表格排版（08439 就是），
    # 换一套读法再试一次。绝不放着一个「有小标题却零条比较」的结果不管。
    if not out:
        out = _comparisons_from_table(section, page_at(0))
    if parallel and notes is not None:
        notes.append(_PARALLEL_NOTE)
    return out


def _note_alternative_prices(result) -> None:
    """一份公告给了**两套要约价**时，说出来。

    06808 高鑫零售同时列了两个价：1.58 港元（部分遞延結算替代方案下的
    最高代价）和 1.38 港元（全額預付替代方案），于是同一套锚点／窗口
    各出现两遍，百分比一套一个样。程序按先出现的那套取值（1.58），
    而你的答案取的是 1.38 那套。

    只有一份样本，定不出「该取哪一套」的规则，所以不猜 —— 但也不能
    装作没看见：把重复的那一套摆到备注里，看表的人一眼知道这单有分叉。

    ⚠️ 判据必须是「**整条梯子**重复了一遍」，不是「有两个数字撞了」。
    按后者写的第一版在 12 单里报了 8 单，全是误报：
      · 每股净资产天然有两条（经审核 + 未经审核，两个结算日）；
      · 窗口认不出来的那几条（120日均价、最後實際可行日期）都落进
        「收市价」，于是和真正的收市价撞车。
    一条 12 单里响 8 次的提醒，只会教会人忽略「备注」这一列。
    """
    seen: dict[tuple[str, str], str] = {}
    clash: list[str] = []
    for c in result.comparisons:
        if c.anchor == "nav":
            continue          # 经审核/未经审核两条净资产是常态，不算分叉
        key = (c.anchor, c.window)
        if key in seen and seen[key] != c.stated_pct:
            if not any(x.startswith(c.label) for x in clash):
                clash.append(f"{c.label} {seen[key]}% / {c.stated_pct}%")
        else:
            seen.setdefault(key, c.stated_pct)
    if len(clash) >= 3:
        result.notes.append(
            "本单像是列了两套要约价（替代方案）：整条梯子出现了两遍 —— "
            + "、".join(clash[:3]) + "。主值取的是先出现的那套，请核原文")


def _comparisons_in(item: str, page: int) -> tuple[list[Comparison], bool]:
    """一条（可能含多项）比较文字 → 若干 Comparison。

    ⚠️ 基准价取的是**百分比左边最近的那一个**，不是这段话里的第一个。
    01310 香港寬頻那句话以「經調整要約價每股5.075港元較…」开头 ——
    按「第一个」取，要约价自己成了基准价，溢价率算在自己头上，
    而结果看起来完全正常。

    返回 (抽到的, 有没有因为并列句而放弃的)。
    """
    out: list[Comparison] = []
    benches = list(_BENCHMARK.finditer(item))
    if not benches:
        return out, False

    pcts = _pcts(item)
    skipped = False
    prev_end = 0
    for p in pcts:
        left = [b for b in benches if b.end() <= p.start]
        if not left:
            continue
        bench = left[-1]
        # 「分別」＝并列句，基准价和百分比按语序一一对应，位置配不准
        if _PARALLEL.search(item[bench.end():p.start]):
            skipped = True
            continue
        # 一条里只有一处百分比时按整条判锚点（沿用原来的行为）；
        # 有多处时各判各的，否则后面那条的「30個交易日」会污染前面那条。
        scope = item if len(pcts) == 1 else item[prev_end:p.end]
        prev_end = p.end
        anchor, window = _classify(scope)
        number = bench.group(3).replace(",", "")
        approx = bench.group(1) is not None or bench.group(2) is not None
        out.append(Comparison(
            anchor=anchor, window=window,
            benchmark=number, benchmark_decimals=_decimals(number),
            benchmark_is_exact=not approx,   # 「約」在哪一侧都算约整值
            stated_pct=p.number, stated_direction=p.direction,
            page=page, quote=item.strip()[:220]))
    return out, skipped


# 表格形式的价值比较。08439 新百利就是这么排的 —— 一张表，不是一句句话。
# 扁平化之后长这样（列头和折行的字都混在里面）：
#
#   (vi) 於2026年4月28日（最後交易日） 0.860 (4.9)% 6.7%
#   (ix) 直至及包括最後交易日的三十(30)個交易日 0.581 40.8% 58.0%
#
# 三处和句子形式不一样，缺一条都抽不出来：
#   · 基准价**不带「港元」**，就是个光秃秃的数字；
#   · 没有「溢價／折讓」两个字，靠**括号**表示负数：(4.9)% 是折让 4.9%；
#   · 后面还跟着第二个百分比（计入特别股息后的口径），要的是**第一个**。
_TABLE_ROW = re.compile(
    # ⚠️ 基准价后面必须紧跟空白或左括号。少了这个前瞻，「0.125%」会被
    # 拆成基准 0.12 ＋ 百分比 5 —— 一条凭空捏出来的比较，且看着很正常。
    r"(?P<bench>\d+\.\d{2,4})(?=[\s（(])"
    r"\s*(?P<neg>[（(])?\s*(?P<pct>\d+\.?\d*)\s*[)）]?\s*%")


def _comparisons_from_table(section: str, page: int) -> list[Comparison]:
    """把表格排版的价值比较捞出来。"""
    out: list[Comparison] = []
    for item in _split_items(section):
        m = _TABLE_ROW.search(item)
        if not m:
            continue
        anchor, window = _classify(item)
        if anchor == "unknown" and window == "unknown":
            continue
        bench = m.group("bench")
        out.append(Comparison(
            anchor=anchor, window=window,
            benchmark=bench, benchmark_decimals=_decimals(bench),
            benchmark_is_exact=True,          # 表格里印的就是精确值
            stated_pct=m.group("pct"),
            stated_direction=DISCOUNT if m.group("neg") else PREMIUM,
            page=page, quote=item.strip()[:220]))
    return out


def _comparisons_from_scan(joined: str, page_of: dict[int, int],
                           notes: list | None = None) -> list[Comparison]:
    """兜底：没有小标题时按形状全文捞。

    ⚠️ 并列句的提醒在这条路上也要发出来。01310 香港寬頻恰恰是
    「没有小标题」**且**「一句话里塞了四条比较」—— 两个毛病同时犯，
    而只在有小标题那条路上写备注的话，正好漏掉它。
    """
    def page_at(pos: int) -> int:
        best = 0
        for at, page in sorted(page_of.items()):
            if at <= pos:
                best = page
        return best

    out: list[Comparison] = []
    seen: set[tuple[str, str, str]] = set()
    parallel = False
    for pos, clause in _scan_whole_document(joined):
        found, skipped = _comparisons_in(clause, page_at(pos))
        parallel = parallel or skipped
        for cmp_ in found:
            # 同一条比较可能在摘要和正文各出现一次，去重
            key = (cmp_.anchor, cmp_.window, cmp_.stated_pct)
            if key in seen:
                continue
            seen.add(key)
            out.append(cmp_)
    if parallel and notes is not None:
        notes.append(_PARALLEL_NOTE)
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
                "offeror", "the offeror", "有限公司", "公司",
                # 08220 比高集團实跑抽出「認購人可能」——「認購人」和
                # 「要約人」一样是通称，配股清洗豁免那类公告里满篇都是。
                "認購人", "认购人", "收購方", "投資者", "賣方", "卖方"}
# 要约人段的右边界：接下来必然是动词、助动词或介词。
# 「可能」是从 08220 那单补的：标题写「…代表認購人可能須提出…」，
# 不挡住它就会把助动词粘进公司名里。
_OFFEROR_RIGHT = re.compile(r"就|提出|作出|向|對|以|可能|須|需|，|。|,")

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


# 通称几乎都长这样：两三个字 + 「人」（要約人/認購人/受益人/承配人/
# 獨立第三方…）。08220 实跑抽出「益人」—— 一个被切了半截的通称，
# 比整个通称更像名字，也就更危险。真名字要么带「有限公司/Limited」，
# 要么带「先生/女士」，要么长得多。
_SHORT_ROLE = re.compile(r"^.{0,3}人$")


def _is_placeholder(name: str) -> bool:
    text = name.strip()
    if text.lower() in _PLACEHOLDER:
        return True
    return bool(_SHORT_ROLE.match(text))


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
# ⚠️⚠️ 这一段是这个文件里最反复踩的坑，值得写清楚。
#
# 「每股 X 港元」和「總代價 X 港元」在正则眼里长得几乎一样，而**总额和
# 单价差着上亿倍**。抽错了不会报错，只会在表里留一个荒谬但看着正常的数。
# 实跑里已经栽过三次，每次都是同一个病，只是换了个措辞：
#
#   02362  「要約的總價值根據要約價每股要約股份0.01港元計算」  → 抽成 0.01
#   08439  「計及…最高金額後每股要約股份應收總額0.918港元」    → 抽成 0.918
#   02362  「倘要約獲悉數接納，按每股要約股份0.01港元計算」      → 抽成 0.01
#
# 前两次我只在**出事的那一条**正则上加了 (?!每股)，于是下一条措辞又中招。
# 这次改成：所有正则共用同一个「不许跨过每股」的间隔件 _GAP。
# 一条总代价的措辞里出现「每股」，那个数就一定是单价，不是总额 ——
# 这是口径决定的，不是个案。
_GAP = r"(?:(?!每股|每份)[^0-9。；])"

# 名词那一坨的写法在实跑里至少有四种排列，硬按顺序写死会一直漏：
#     現金代價總額 / 現金總代價 / 總現金代價 / 代價總額
# 所以把「現金」「總」当可选前缀各允许一次，名词本体单列。
_MONEY_NOUN = r"(?:現金)?(?:總)?(?:現金)?(?:代價|金額|價值|款項)(?:總額|總代價)?"

# 「為」前面可能还垫着「約」「將」「將約」。
#     現金總代價**約為**320,581,945港元          （03389 亨得利）
#     現金代價總額**將約為**194,867,400港元      （08413 亞洲富思）
# 少这一个「約」字，这两单的交易规模就是空的 —— 而溢价率都抽对了，
# 说明 PDF 读到了，纯粹是措辞没认。
_ABOUT_IS = r"(?:將)?\s*(?:約|大約)?\s*(?:將)?為"

_DEAL_SIZE = [
    # 「須支付的最高現金代價約為5,440萬港元」
    # 「應付之最高現金金額為1,905,849,908.60港元」
    # 「最高代價（…一长串括号…）金額約為469.85百萬港元」（01980 天鴿互動）
    #   ⚠️ 间隔放到 70：名词和数字之间常常插一整个括号补充说明
    #   （「不包括非接納股份及要約人、洪女士…已擁有之股份」足足 55 字），
    #   而 _GAP 本身不许跨过数字、句号分号和「每股」，放宽是安全的。
    re.compile(rf"最高{_MONEY_NOUN}{_GAP}{{0,70}}?{_AMOUNT}"),
    # 「須支付的最高總代價」这种把「最高」和名词拆开的写法。
    # 「需要支付最高達52,007,328港元」（01633）—— 認「需要／須要」，
    # 只写「需支付」会漏掉多一个「要」字的那种。
    re.compile(rf"(?:應付|須支付|需支付|須要支付|需要支付|支付|須付|所需)"
               rf"{_GAP}{{0,12}}?最高{_GAP}{{0,24}}?{_AMOUNT}"),
    # 「須支付的現金代價總額為92,000,000港元」
    # 「應付的總現金代價將為7,000,000港元」（02362）
    re.compile(rf"{_MONEY_NOUN}{_ABOUT_IS}{_GAP}{{0,12}}?{_AMOUNT}"),
    # 「倘要約獲悉數接納，應付總額約為…」
    re.compile(rf"(?:悉數接納|全數接納|全部接納){_GAP}{{0,60}}?{_AMOUNT}"),
    # 「要約項下之總代價約為…」
    re.compile(rf"要約(?:項下)?(?:之|的)?(?:總代價|總價值)"
               rf"{_GAP}{{0,24}}?{_AMOUNT}"),
]


def _amount_of(m) -> str:
    """把一处匹配换算成整数金额字符串。

    有「萬」「億」才换算，而这是**单位换算不是计算**（铁律一），
    依据留在 quote 里可复核；没有单位就原样保留，取整会丢掉角分。
    """
    raw = m.group("num").replace(",", "")
    unit_char = m.group("unit") or ""
    return raw if not unit_char else f"{float(raw) * _UNIT[unit_char]:.0f}"


# 「这笔钱付给谁？」—— 你手册里的判别口诀，这里是它的机器实现。
#
# 一份要约公告里最大的那个数字，往往是**买卖协议项下收购控股权**的对价，
# 那笔钱付给特定卖方，不是付给接纳要约的公众股东，所以不是交易规模。
#
# 2025 全年 58 单实测，比值（程序÷答案）里有四单**正好 3.00**：
#     03928 中國新零售  222,800,000 ÷ 74,268,000 = 3.000
#     02442 怡俊集團    230,000,000 ÷ 76,673,400 = 3.000
#     01757 俊裕地基     80,000,000 ÷ 26,700,000 = 2.996
#     03626 HSSP       195,000,000 ÷ 65,044,000 = 2.998
# 三比一 —— 要约人先买走 75%，剩下 25% 才是要约的对象。
# 那个 3 倍不是巧合，是「收购控股权的钱」和「要约的钱」之比。
_SELLER_SIDE = re.compile(
    # 「收購股份」「購買股份」在这些公告里是**定义词**，指协议项下从卖方
    # 手上买走的那批股份 —— 01980 天鴿互動写「收購股份之總代價為
    # 13,597,870港元」，那是买 1.80% 股权的钱，被当成了整单的交易规模
    # （答案是 4.69 亿，差 34 倍）。
    # 注意这不会误伤真要约句：下面 _paid_to_the_seller 要求同一段话里
    # **没有**「要約項下／根據要約」之类的字样才算卖方那一笔。
    r"銷售股份|待售股份|買賣協議|賣方|出售股份|轉讓股份|收購事項項下|"
    r"收購股份|購買股份")
_OFFER_SIDE = re.compile(r"要約項下|根據要約|接納要約|要約獲|要約的最高|要約應付")


# 「全部已發行股本的價值」＝把整家公司按要约价折算一遍，是**估值**不是规模。
# 口径写得很清楚：交易规模是要约项下应付给公众股东的钱，不是 100% 股本估值。
# 01980 天鴿互動写「本公司全部已發行股本的價值約為754.39百萬港元」，
# 正好等于 0.68 × 11.09 亿股 —— 拿它当交易规模会把一单 4.7 亿的要约记成 7.5 亿。
_EQUITY_VALUE = re.compile(
    r"全部已發行股[本份](?:的)?(?:價值|總值)|已發行股本(?:的)?(?:價值|總值)")


def _is_equity_value(flat: str, at: int) -> bool:
    """这处金额讲的是不是「整家公司值多少」。

    ⚠️ 窗口要**跨过匹配起点**再往后取几个字：匹配是从「價值」这个词
    开头的，而标记词「全部已發行股本的價值」正好骑在起点上 ——
    只往前看就永远看不见它（第一版就是这么静默失效的）。
    """
    return bool(_EQUITY_VALUE.search(flat[max(0, at - 60):at + 10]))


def _paid_to_the_seller(flat: str, at: int) -> bool:
    """这处金额讲的是不是「付给卖方」那一笔。

    看它前面那段话：提到销售股份／买卖协议／卖方，而又没有说这是要约
    项下的，那就是控股权转让的对价 —— 口径完全不同，不能当交易规模。
    """
    before = flat[max(0, at - 90):at]
    return bool(_SELLER_SIDE.search(before)) and not _OFFER_SIDE.search(before)


def deal_size_candidates(pages: dict[int, str]) -> list[tuple[str, int, str]]:
    """一份公告里所有像「交易规模」的数。返回 [(金额, 页码, 引文)]。

    一份要约公告里通常同时印着好几个大额数字：控股权转让的对价、
    全部已发行股本的估值、要约项下应付给公众股东的最高代价……
    你的手册里那句判别口诀说的就是它们的区别 ——「这笔钱付给谁？」

    2025 全年 58 单实测：交易规模只对了 12 单（23.5%），而错的 35 单
    比值从 0.019 到 3.201 **连续分布** —— 不是稳定地取错了某一个口径，
    而是每份公告里候选不同、先撞上哪个就用哪个。12 单正好对上说明
    正则本身找得到，问题在**选**。

    所以先把候选**全部**列出来。选哪个仍按原来的优先级（改口径要有
    依据，不能拍脑袋），但候选多于一个时会写进备注 ——
    让「这里有得选」这件事在结果里看得见，而不是静默地选了一个。
    """
    out: list[tuple[str, int, str]] = []
    seen: set[str] = set()
    for pattern in _DEAL_SIZE:
        for page in sorted(pages):
            flat = _flat(pages[page])
            for m in pattern.finditer(flat):
                amount = _amount_of(m)
                if amount in seen:
                    continue
                seen.add(amount)
                if _paid_to_the_seller(flat, m.start()):
                    continue          # 付给卖方的，不是要约规模
                if _is_equity_value(flat, m.start()):
                    continue          # 整家公司的估值，不是要约规模
                start = max(0, m.start() - 40)
                out.append((amount, page, flat[start:m.end() + 10].strip()))
    return out


def extract_deal_size(pages: dict[int, str]) -> tuple[str, Evidence]:
    """取「要约项下最高现金代价」。

    口径来自你三单人工答案的反推（见 selectors.py）：
    不是控股权转让对价，也不是 100% 股本估值。
    """
    picks = deal_size_candidates(pages)
    if not picks:
        return "", Evidence()
    amount, page, quote = picks[0]
    return amount, Evidence(page, quote)


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
    result.comparisons = extract_comparisons(pages, result.notes)
    result.six_month_low, result.six_month_high, result.six_month_evidence = \
        extract_six_month(pages)
    # 候选算一遍就够 —— 主值和备注都从这一份里取。
    # （原来 extract_deal_size 里算一遍、写备注时又算一遍，
    #   而这个函数是整个抽取层第二贵的一项。）
    others = deal_size_candidates(pages)
    if others:
        amount, page, quote = others[0]
        result.deal_size, result.deal_size_evidence = amount, Evidence(page, quote)

    # 候选不止一个时说出来。2025 全年实测交易规模只对 12/51，而错的那些
    # 比值连续散布在 0.019~3.201 —— 说明不是稳定取错了某个口径，
    # 是每份公告里候选不同。把候选摆出来，「这里有得选」才看得见。
    if len(others) > 1:
        rest = "、".join(a for a, _, _ in others[1:5])
        result.notes.append(
            f"交易规模有 {len(others)} 个候选，取了 {result.deal_size}，"
            f"其余：{rest}{'…' if len(others) > 5 else ''}（口径存疑请核原文）")
    result.total_shares, result.total_shares_evidence = extract_total_shares(pages)
    result.is_conditional, result.is_conditional_evidence = \
        extract_conditionality(title, pages)
    result.debt_conversion, result.debt_conversion_evidence = \
        extract_debt_conversion(pages)

    _drop_impossible_offer_price(result)
    _drop_impossible_deal_size(result)

    if not result.offer_type:
        result.notes.append("要约类型未识别，需人工判定")
    if not result.comparisons:
        result.notes.append("未找到「價值比較」一节，溢价率无法抽取")
    _note_alternative_prices(result)
    if not result.deal_size:
        result.notes.append("交易规模未识别")
    if not result.offeror:
        result.notes.append("要约方未识别，需人工从公告首页读取")
    return result
