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
from pathlib import Path

# 抽取器版本。**改了抽取/校验逻辑就要改这个号**，否则旧结果会被当成
# 新结果复用，而你根本看不出来 —— 那正是铁律二说的静默污染。
EXTRACTOR_VERSION = "2026-08-C"

STORE_DIR = "data/store"
LISTING_FILE = "listing.csv"
COVERAGE_FILE = "coverage.json"
DEALS_FILE = "deals.csv"
EVIDENCE_FILE = "evidence.json"

LISTING_COLUMNS = ["NEWS_ID", "DATE_TIME", "STOCK_CODE", "STOCK_NAME",
                   "TITLE", "FILE_LINK", "FIRST_SEEN"]


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


def save_coverage(root: Path, coverage: dict[str, set[str]]) -> None:
    payload = {k: sorted(v) for k, v in coverage.items()}
    (_dir(root) / COVERAGE_FILE).write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


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
            "DATE_TIME": rec.get("DATE_TIME", ""),
            "STOCK_CODE": rec.get("STOCK_CODE", ""),
            "STOCK_NAME": rec.get("STOCK_NAME", ""),
            "TITLE": rec.get("TITLE", ""),
            "FILE_LINK": rec.get("FILE_LINK", ""),
            "FIRST_SEEN": today,
        }
        added += 1

    path = _dir(root) / LISTING_FILE
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=LISTING_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for row in sorted(store.values(),
                          key=lambda r: r.get("DATE_TIME", ""), reverse=True):
            w.writerow(row)
    return added, len(store)


def listing_between(root: Path, d1: dt.date, d2: dt.date) -> list[dict]:
    """从存档里取这段日期的公告，格式和刚抓回来的一模一样。"""
    lo, hi = d1.isoformat(), d2.isoformat()
    out = []
    for row in load_listing(root).values():
        day = (row.get("DATE_TIME") or "").split()[0].replace("/", "-")
        if lo <= day <= hi:
            out.append(row)
    out.sort(key=lambda r: r.get("DATE_TIME", ""), reverse=True)
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
    path = _dir(root) / DEALS_FILE
    header = ["NEWS_ID", "抽取器版本", *columns]
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)


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
    (_dir(root) / EVIDENCE_FILE).write_text(
        json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
