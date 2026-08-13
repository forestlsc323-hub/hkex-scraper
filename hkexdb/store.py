"""跨次运行的存档：抓过的别再抓，抽过的别再抽。

你的想法完全成立，而且比省时间更重要 —— 它把这个工具从「每次重头来」
变成「一个会长大的库」。

存三样东西，各有各的失效规则：

  listing.csv    抓回来的公告列表（NEWS_ID / 日期 / 代码 / 标题 / 链接）
                 **永不失效**。披露易发过的公告不会变，这一层存下来，
                 下次抓 2025 年时 2026 年那段一个请求都不用发。

  coverage.json  哪些日子已经抓过了，按「抓取方式＋关键词」分开记。
                 关键词模式抓过的日子，不等于全量模式也抓过 ——
                 口径不同，覆盖范围就不同，混在一起会造成静默漏检。

  deals.csv      抽出来的要约字段，**带抽取器版本号**。
                 版本一致就直接复用，连 PDF 都不用打开；
                 版本变了（我改了正则）就重抽 —— 这是你工程要求里
                 「规则变更重跑，不手改产出」那条的机器实现。

日期覆盖用「一天一个字符串」记，不做区间合并。一年才 365 条，
而区间合并的边界错误是那种能静默吞掉几天数据的 bug，不值得冒。
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
from pathlib import Path

# 抽取器版本。**改了抽取/校验逻辑就要改这个号**，否则旧结果会被当成
# 新结果复用，而你根本看不出来 —— 那正是铁律二说的静默污染。
EXTRACTOR_VERSION = "2026-08-Z"   # Z：03938 真原文验收打包加总；「已發行股份將為N股」
# Y：窗口和条目编号认全角括号（「連續五（5）個交易日」）
# X：并列句按窗口个数拆开；月度窗口；双重上市取港交所那套
# W：受要约股数≠已发行股本；天花板加第二个参照物；协议每股价与 V21
# V：中文被空格拆开时的归一化；V16；干扰值按类归开
# U：股数不再抓买卖协议的股数（它是交易规模天花板的分母）
# T：算术闸改成筛子（换下一个候选，不再清空）；「代價上限約為」
# S：多要约打包加总（②）＋情境间取合计最大（③）＋V9
# R：口径裁定落地 —— 类型拆两个维度；替代方案取即期价；V15
# Q：交易规模 ——「約為」；名词四种排列；整家公司的估值不算
# P：2025 那份原文摘录带出的五个病（词序/每股/编号/最近基准/退档）
# O：主值溢价率的符号跟自己的两个数字对一遍
# N：同一单的两份文件互相补空格
# M：要约价数量级闸也用价值比较的基准价当参照
# L：同单重复合并；要约价数量级闸（PDF 数字与文字错位）
# K：交易规模 —— 付给卖方的不算；超过全部股本估值的作废；候选写进备注
# J：拿到四份真实措辞后的一轮 —— 字母编号 / 表格排版 / 不受干擾 / 每股陷阱
# I：規則26.1 排到正文措辞前面（被豁免的那个不算）
# H：认「部份」；強制性压过部分（清洗豁免除外）；半截通称不算名字
# G：面值不再当要约价；总代价不可能等于每股价；要约方通称再补一批
# F：分段解析（前几页看不出要约迹象就不再往下翻）
# E：每股价不再当成交易规模；要约方名字不再粘上释义表的邻居
# D：公告日期改成 ISO，旧存档里那些 DD/MM/YYYY 必须重建

STORE_DIR = "data/store"
LISTING_FILE = "listing.csv"
COVERAGE_FILE = "coverage.json"
DEALS_FILE = "deals.csv"
EVIDENCE_FILE = "evidence.json"

# DATE_ISO 是**加工层**：原始的 DATE_TIME 一字不动地留着，另开一列存
# 规范化后的日期。原始层不动、加工层另开 —— 你手册里那条铁律。
LISTING_COLUMNS = ["NEWS_ID", "DATE_TIME", "DATE_ISO", "STOCK_CODE",
                   "STOCK_NAME", "TITLE", "FILE_LINK", "FIRST_SEEN"]


def normalise_date(value: str) -> str:
    """披露易的日期 → ISO（YYYY-MM-DD）。认不出就返回空字符串。

    ⚠️ 接口给的是 **DD/MM/YYYY**（界面上显示的「04/06/2026」就是它），
    不是 ISO。我当初直接把斜杠换成横杠就拿去比大小 ——
    「04-06-2026」按字符串排在「2026-01-01」前面，于是 2594 条公告
    一条都过不了日期筛子，抓得好好的数据被全部丢掉，日志上还显示
    「新增 2594 条进存档」，只有下一行的「本次范围内共 0 条」露了馅。

    日期格式这种东西绝不能靠字符串替换糊弄，必须真解析。
    """
    text = str(value or "").split()[0].strip() if value else ""
    if not text:
        return ""
    text = text.replace(".", "/").replace("-", "/")
    parts = [p for p in text.split("/") if p]
    if len(parts) != 3:
        return ""
    try:
        if len(parts[0]) == 4:                     # YYYY/MM/DD
            y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
        else:                                       # DD/MM/YYYY（披露易用这个）
            d, m, y = int(parts[0]), int(parts[1]), int(parts[2])
        return dt.date(y, m, d).isoformat()
    except (ValueError, TypeError):
        return ""


def row_date(row: dict) -> str:
    """一行公告的 ISO 日期。老存档没有 DATE_ISO 列，就现算。"""
    return (row.get("DATE_ISO") or "").strip() or normalise_date(
        row.get("DATE_TIME", ""))


def _write_csv(path: Path, columns: list[str], rows) -> None:
    """先写临时文件，再原子改名。

    存档动辄几千行，重写一遍有个真实的时间窗口。写到一半被杀（关窗口、
    断电、任务管理器），原地写就会留下半截 CSV —— 下次读出来少一半数据，
    而且完全无声。os.replace 在 Windows 和 POSIX 上都是原子的。
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)
    os.replace(tmp, path)


def _dir(root: Path) -> Path:
    path = root / STORE_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------- 覆盖范围

def coverage_key(mode: str, keywords: list[str]) -> str:
    """抓取口径的指纹。

    关键词模式抓过 6 月，不代表全量模式也抓过 6 月 —— 后者会多出
    一大批标题不含关键词的公告。口径不同就分开记，绝不混。
    """
    if mode == "keyword":
        return "keyword:" + ",".join(sorted(keywords))
    return mode


def load_coverage(root: Path) -> dict[str, set[str]]:
    path = _dir(root) / COVERAGE_FILE
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return {k: set(v) for k, v in raw.items() if isinstance(v, list)}


def _write_json(path: Path, payload) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, path)


def save_coverage(root: Path, coverage: dict[str, set[str]]) -> None:
    _write_json(_dir(root) / COVERAGE_FILE,
                {k: sorted(v) for k, v in coverage.items()})


def _days(d1: dt.date, d2: dt.date) -> list[dt.date]:
    return [d1 + dt.timedelta(days=i) for i in range((d2 - d1).days + 1)]


def missing_days(root: Path, d1: dt.date, d2: dt.date, key: str) -> list[dt.date]:
    """这段日期里还没抓过的那些天。"""
    done = load_coverage(root).get(key, set())
    return [d for d in _days(d1, d2) if d.isoformat() not in done]


def to_ranges(days: list[dt.date]) -> list[tuple[dt.date, dt.date]]:
    """把零散的天并回连续区间，好一段一段去抓。

    例：抓过 2026 全年后再要 2025-01-01~今天，缺的是
    2025 一整年 + 2026 剩下那几天，合成两段，而不是发几百个请求。
    """
    if not days:
        return []
    days = sorted(days)
    out, start, prev = [], days[0], days[0]
    for day in days[1:]:
        if (day - prev).days == 1:
            prev = day
            continue
        out.append((start, prev))
        start = prev = day
    out.append((start, prev))
    return out


def mark_covered(root: Path, d1: dt.date, d2: dt.date, key: str) -> None:
    coverage = load_coverage(root)
    coverage.setdefault(key, set()).update(d.isoformat() for d in _days(d1, d2))
    save_coverage(root, coverage)


# ---------------------------------------------------------------- 公告列表

def load_listing(root: Path) -> dict[str, dict]:
    path = _dir(root) / LISTING_FILE
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return {r["NEWS_ID"]: r for r in csv.DictReader(fh) if r.get("NEWS_ID")}


def merge_listing(root: Path, records: list[dict]) -> tuple[int, int]:
    """把新抓到的合并进存档。返回 (新增, 存档总数)。

    以 NEWS_ID 为准，已有的不覆盖 —— 存档里那条是当初真正见到的样子，
    后来的重复请求没有理由改写它。
    """
    store = load_listing(root)
    today = dt.date.today().isoformat()
    added = 0
    for rec in records:
        nid = str(rec.get("NEWS_ID", "")).strip()
        if not nid or nid in store:
            continue
        store[nid] = {
            "NEWS_ID": nid,
            "DATE_TIME": rec.get("DATE_TIME", ""),      # 原始层：一字不动
            "DATE_ISO": normalise_date(rec.get("DATE_TIME", "")),
            "STOCK_CODE": rec.get("STOCK_CODE", ""),
            "STOCK_NAME": rec.get("STOCK_NAME", ""),
            "TITLE": rec.get("TITLE", ""),
            "FILE_LINK": rec.get("FILE_LINK", ""),
            "FIRST_SEEN": today,
        }
        added += 1

    # 按**真实日期**排，不是按 DATE_TIME 字符串 ——
    # DD/MM/YYYY 按字符串排等于乱排：04/06 会跑到 18/05 前面。
    _write_csv(_dir(root) / LISTING_FILE, LISTING_COLUMNS,
               sorted(store.values(), key=row_date, reverse=True))
    return added, len(store)


def listing_between(root: Path, d1: dt.date, d2: dt.date) -> list[dict]:
    """从存档里取这段日期的公告，格式和刚抓回来的一模一样。"""
    lo, hi = d1.isoformat(), d2.isoformat()
    out = [row for row in load_listing(root).values()
           if lo <= row_date(row) <= hi]
    out.sort(key=row_date, reverse=True)
    return out


# ---------------------------------------------------------------- 抽取结果

def load_deals(root: Path) -> dict[str, dict]:
    """存档里的抽取结果，按 NEWS_ID 索引。"""
    path = _dir(root) / DEALS_FILE
    if not path.exists():
        return {}
    from .dealsview import load_rows
    return {r["NEWS_ID"]: r for r in load_rows(path) if r.get("NEWS_ID")}


def reusable(row: dict, version: str | None = None) -> bool:
    """这条存档还能不能直接用？版本对不上就不能。

    我改了正则却复用旧结果，表面一切正常，数字却是旧逻辑抽的 ——
    这正是铁律二说的静默污染，所以宁可重抽。

    ⚠️ 版本号在函数体内读，不能写成默认参数 —— 默认参数在模块导入时
    就把值定死了，之后改 EXTRACTOR_VERSION 根本不生效。也就是说我以为
    自己「升了版本会自动重抽」，实际一直在复用旧结果。
    这个错是 test_a_new_extractor_version_forces_a_re_extract 抓到的。
    """
    want = EXTRACTOR_VERSION if version is None else version
    return str(row.get("抽取器版本", "")).strip() == want


def save_deals(root: Path, rows: list[dict], columns: list[str]) -> None:
    _write_csv(_dir(root) / DEALS_FILE, ["NEWS_ID", "抽取器版本", *columns], rows)


def merge_deals(root: Path, new_rows: list[dict], columns: list[str]) -> int:
    """把这次抽的并进存档，同 NEWS_ID 以新的为准。返回存档总数。"""
    store = load_deals(root)
    for row in new_rows:
        nid = str(row.get("NEWS_ID", "")).strip()
        if nid:
            store[nid] = row
    save_deals(root, list(store.values()), columns)
    return len(store)


def load_evidence(root: Path) -> dict:
    path = _dir(root) / EVIDENCE_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def merge_evidence(root: Path, new_evidence: dict) -> None:
    """出处引文单独存。

    原件用完即弃时，这份引文就是审计链上唯一剩下的东西 ——
    「这个数字出自第几页哪句话」比 PDF 本身更该留住。
    """
    store = load_evidence(root)
    store.update(new_evidence)
    _write_json(_dir(root) / EVIDENCE_FILE, store)
