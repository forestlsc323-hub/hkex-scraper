"""流程执行器：把整条流水线包成一个可被界面调用的东西。

界面（app.py）只负责画窗口和转发消息，真正的逻辑全在这里 ——
这样界面那层薄到几乎不可能出错，而这里可以离线测试。

对外只有一个 `run()`：
    · 通过 `on_log` 回调把每一行输出交给界面
    · 通过 `on_step` 回调报告当前进度
    · `cancel_event` 置位后尽快停下
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

STEPS = ["抓取公告列表", "质控筛查", "抽取要约字段", "生成网页", "打包诊断"]


class Cancelled(Exception):
    """用户点了停止。"""


@dataclass
class Deal:
    """一单要约的最终结果 —— 这就是你要的那张表的一行。"""

    # 当事方 —— 做 precedent 时第一眼看的就是「谁买谁、谁做的 FA」
    code: str = ""                    # 受要约方股票代码
    name: str = ""                    # 受要约方（披露易归属的简称）
    target_full: str = ""             # 受要约方全称（标题里写了才有）
    offeror: str = ""                 # 要约方
    offeror_fa: str = ""              # 要约方财务顾问
    date: str = ""                    # 首次公告日期（T0）
    last_trading_day: str = ""        # 停牌前最后交易日
    # 条款
    offer_type: str = ""
    consideration: str = ""           # 现金 / 证券 / 现金＋证券
    offer_price: str = ""
    premium_pct: str = ""             # 主值溢价率
    premium_basis: str = ""           # 主值口径 —— 没有它这个数字没意义
    premium_ladder: dict = field(default_factory=dict)   # 全部比较项
    deal_size: str = ""
    listing_intent: str = ""          # 拟维持上市 / 拟撤销上市
    # 出处与复核
    confidence: str = ""
    checks: str = ""
    pdf_url: str = ""
    title: str = ""
    notes: str = ""
    evidence: dict = field(default_factory=dict)


@dataclass
class Result:
    ok: bool = False
    fetched: int = 0
    screened: int = 0
    deals: list = field(default_factory=list)
    report_path: Path | None = None
    diagnostic_path: Path | None = None
    log_path: Path | None = None
    error: str = ""
    buckets: dict = field(default_factory=dict)
    qc_notes: list = field(default_factory=list)


def _fetch(d1: dt.date, d2: dt.date, log, on_step, cancel_event) -> list[dict]:
    """用 vendor/hkex_client.py（用户提供的实战客户端，一字未改）抓列表。"""
    import sys
    sys.path.insert(0, str(ROOT / "vendor"))
    from hkex_client import HKEXClient          # noqa: E402

    n_days = (d2 - d1).days + 1
    log(f"日期范围 {d1} ~ {d2}（{n_days} 天）")
    log("正在访问检索页建立会话…")

    client = HKEXClient()
    cookies = sorted(c.name for c in client.session.cookies)
    log(f"会话 cookie：{cookies if cookies else '（服务端未下发）'}")

    done = [0]

    def progress(day, total_days, count):
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled()
        done[0] += 1
        log(f"  [{done[0]}/{total_days}] {day}　累计 {count} 条")
        on_step(0, done[0] / max(1, total_days))

    return client.search(d1, d2, progress_cb=progress, cancel_event=cancel_event)


def _write_listing(records: list[dict], log) -> Path:
    out_dir = ROOT / "data" / "raw"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "vendor_raw.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")

    csv_path = out_dir / "vendor_listing.csv"
    if records:
        preferred = ["NEWS_ID", "DATE_TIME", "STOCK_CODE", "STOCK_NAME",
                     "TITLE", "FILE_LINK", "FILE_INFO"]
        cols = sorted({k for r in records for k in r})
        header = [c for c in preferred if c in cols] + \
                 [c for c in cols if c not in preferred]
        with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
            w.writeheader()
            w.writerows(records)
        log(f"已保存 {csv_path.name}")
    return csv_path


def run(date_from: dt.date, date_to: dt.date, *,
        on_log=None, on_step=None, cancel_event: threading.Event | None = None,
        fetch=_fetch, open_pdf=None) -> Result:
    """跑完整条流水线。

    `fetch` 和 `open_pdf` 都可替换 —— 测试里换成假的，就不会真的联网。
    """
    lines: list[str] = []

    def log(text: str = "") -> None:
        lines.append(str(text))
        if on_log:
            on_log(str(text))

    def step(index: int, frac: float = 0.0) -> None:
        if on_step:
            on_step(index, frac)

    result = Result()
    log_path = ROOT / "run_log.txt"
    started = dt.datetime.now()
    log(f"开始 {started:%Y-%m-%d %H:%M:%S}")

    try:
        step(0)
        log(f"\n【1/5】{STEPS[0]}")
        records = fetch(date_from, date_to, log, step, cancel_event)
        result.fetched = len(records)
        csv_path = _write_listing(records, log)
        log(f"抓到 {len(records)} 条")

        if records:
            step(1, 0.0)
            log(f"\n【2/5】{STEPS[1]}")
            rows, rules = _screen(records, result, log)
            step(1, 1.0)

            step(2, 0.0)
            log(f"\n【3/5】{STEPS[2]}")
            result.deals = _extract_deals(rows, log, step, cancel_event,
                                          open_pdf=open_pdf)
            _write_deals(result.deals, log)
            step(2, 1.0)

            step(3, 0.0)
            log(f"\n【4/5】{STEPS[3]}")
            result.report_path = _write_report(rows, result, rules, csv_path, log)
            step(3, 1.0)
            result.ok = True
        else:
            log("\n抓到 0 条，后面几步跳过。")

        step(4, 0.0)
        log(f"\n【5/5】{STEPS[4]}")
        result.diagnostic_path = _write_diagnostic(lines, result)
        log(f"已生成 {result.diagnostic_path.name}")
        step(4, 1.0)

    except Cancelled:
        log("\n已停止。")
        result.error = "用户停止"
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        log("\n出错了，完整堆栈：\n" + traceback.format_exc())
        try:
            result.diagnostic_path = _write_diagnostic(lines, result)
        except Exception:
            pass

    elapsed = (dt.datetime.now() - started).total_seconds()
    log(f"\n用时 {elapsed / 60:.1f} 分钟")
    log_path.write_text("\n".join(lines), encoding="utf-8")
    result.log_path = log_path
    return result


def _screen(records, result: Result, log):
    """标题层筛选。返回 (行, 规则)。"""
    from . import screening as S

    rules = S.load_rules("screening_rules.yaml")
    recs = [{
        "row_id": r.get("NEWS_ID", f"r{i}"),
        "date": (r.get("DATE_TIME") or "").split()[0],
        "code": r.get("STOCK_CODE", ""),
        "name": r.get("STOCK_NAME", ""),
        "title": r.get("TITLE", ""),
        "pdf_url": r.get("FILE_LINK", ""),
    } for i, r in enumerate(records)]

    report_obj = S.screen(recs, rules)
    result.buckets = dict(report_obj.counts)
    result.qc_notes = list(report_obj.notes)
    result.screened = len(recs)

    scr_dir = ROOT / "data" / "screening"
    scr_dir.mkdir(parents=True, exist_ok=True)
    with (scr_dir / "screened.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["row_id", "date", "code", "name", "bucket", "species",
                    "matched_exclude", "matched_retain", "manual_flags",
                    "reasons", "title", "pdf_url", "rules_version"])
        for r in recs:
            v = r["verdict"]
            w.writerow([r["row_id"], r["date"], r["code"], r["name"], v.bucket,
                        v.species, "／".join(v.matched_exclude),
                        "／".join(v.matched_retain), "；".join(v.manual_flags),
                        "；".join(v.reasons), r["title"], r["pdf_url"], rules.version])

    labels = {S.RETAINED: "留存（进抽取）", S.MANUAL: "人工复核",
              S.EXCLUDED: "已灰（后续/程序公告）", S.SPECIAL: "特殊品种",
              S.SUPERSEDED: "被取代", S.IRRELEVANT: "题材无关"}
    for bucket, n in sorted(result.buckets.items(), key=lambda kv: -kv[1]):
        log(f"  {labels.get(bucket, bucket)}: {n}")
    log(f"数量校验：{'平' if report_obj.reconciled else '不平 —— 需人工检查'}")
    for note in report_obj.notes:
        log(f"  · {note}")
    return recs, rules


def _extract_deals(rows, log, on_step, cancel_event, open_pdf=None) -> list[Deal]:
    """对留存桶里的公告逐份打开 PDF，抽要约字段。

    这一步才产出你真正要的东西：要约类型、要约价、溢价率、交易规模。
    留存桶通常只有十几条，所以逐份下载 PDF 是划算的。
    """
    from . import extractor, pdf_source, selectors, validators

    targets = [r for r in rows if r["verdict"].bucket == "retained"]
    if not targets:
        log("  留存桶为空，没有要抽的公告。")
        return []

    log(f"  留存桶 {len(targets)} 条，逐份打开 PDF…")
    cache = ROOT / "data" / "cache" / "pdf"
    opener = open_pdf or (lambda url: pdf_source.open_pdf(url, cache))
    deals: list[Deal] = []

    for i, row in enumerate(targets, 1):
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled()
        on_step(2, i / len(targets))

        deal = Deal(code=row["code"], name=row["name"], date=row["date"],
                    title=row["title"], pdf_url=pdf_source.full_url(row["pdf_url"]))
        try:
            doc = opener(deal.pdf_url)
            if not doc.has_text_layer:
                deal.notes = "扫描件无文本层，需 OCR 并人工复核"
                deal.confidence = "low"
                deals.append(deal)
                log(f"    [{i}/{len(targets)}] {deal.code} 扫描件，跳过")
                continue

            ex = extractor.extract(row["title"], doc.pages)
            deal.offeror = ex.offeror
            deal.offeror_fa = ex.offeror_fa
            # 标题没写受要约方全称时退回披露易给的简称 —— 那是它自己的归属，
            # 比从正文里猜可靠
            deal.target_full = ex.target or row["name"]
            deal.last_trading_day = ex.last_trading_day
            deal.offer_type = ex.offer_type
            deal.consideration = ex.consideration
            deal.offer_price = ex.offer_price
            deal.deal_size = ex.deal_size
            deal.listing_intent = ex.listing_intent
            deal.confidence = ex.confidence
            deal.notes = "；".join(ex.notes)

            comps = [{"anchor": c.anchor, "window": c.window, "label": c.label,
                      "stated_pct": c.stated_pct,
                      "stated_direction": c.stated_direction,
                      "page": c.page, "quote": c.quote} for c in ex.comparisons]
            pick = selectors.select_primary_premium(comps)
            if pick:
                deal.premium_pct = str(pick.signed_pct)
                deal.premium_basis = pick.label

            # 整条溢价梯子都留着：投行看可比不会只看一个口径，
            # 而且下一个人可能要按「最后交易日收市价」重排
            deal.premium_ladder = {
                c.label: ("-" if c.stated_direction == "discount" else "")
                         + c.stated_pct for c in ex.comparisons}

            deal.checks = _run_checks(ex, validators)
            deal.evidence = {
                "当事方": [ex.parties_evidence.page, ex.parties_evidence.quote],
                "要约类型": [ex.offer_type_evidence.page, ex.offer_type_evidence.quote],
                "要约价": [ex.offer_price_evidence.page, ex.offer_price_evidence.quote],
                "交易规模": [ex.deal_size_evidence.page, ex.deal_size_evidence.quote],
                "溢价率": [pick.page, pick.source_quote] if pick else [0, ""],
                "上市意向": [ex.listing_intent_evidence.page,
                             ex.listing_intent_evidence.quote],
            }
            log(f"    [{i}/{len(targets)}] {deal.code} {deal.name}　"
                f"← {deal.offeror or '要约方未识别'}　"
                f"{deal.offer_type}　{deal.offer_price}　"
                f"{deal.premium_pct}%　{deal.deal_size}")
        except Exception as exc:
            deal.notes = f"抽取失败：{type(exc).__name__}: {exc}"
            deal.confidence = "low"
            log(f"    [{i}/{len(targets)}] {deal.code} 失败：{exc}")
        deals.append(deal)
    return deals


def _run_checks(ex, validators) -> str:
    """跑 V4/V5/V6，把结果压成一行。铁律一：算术全在这里，不在抽取层。"""
    from decimal import Decimal

    if not ex.comparisons or not ex.offer_price:
        return ""
    offer = Decimal(ex.offer_price)
    comparisons = [validators.PriceComparison(
        label=c.label, benchmark=Decimal(c.benchmark),
        benchmark_decimals=c.benchmark_decimals,
        benchmark_is_exact=c.benchmark_is_exact,
        stated_pct=Decimal(c.stated_pct), stated_direction=c.stated_direction,
        page=c.page, source_quote=c.quote) for c in ex.comparisons]

    low = Decimal(ex.six_month_low) if ex.six_month_low else Decimal(0)
    high = Decimal(ex.six_month_high) if ex.six_month_high else Decimal("9" * 12)
    nonmarket = frozenset(c.label for c in ex.comparisons if c.anchor == "nav")
    findings = validators.run_price_comparisons(offer, comparisons, low, high,
                                                nonmarket_labels=nonmarket)
    failed = [f"{f.code}:{f.subject}" for f in findings if not f.passed]
    return "全部通过" if not failed else "未通过 " + "；".join(failed[:3])


# 溢价梯子的固定列。做可比表时人人都要按同一口径横向对齐，
# 所以列是固定的：某一单没有这个口径就留空，绝不用别的口径顶上。
LADDER_COLUMNS = [
    "最后交易日收市价", "最后交易日前5日均价", "最后交易日前10日均价",
    "最后交易日前30日均价", "最后交易日前180日均价",
    "未受干扰日收市价", "未受干扰日前5日均价", "未受干扰日前10日均价",
    "未受干扰日前30日均价", "未受干扰日前180日均价",
    "3.7公告前收市价",
    "每股净资产",
]

# 兜底列：公告用了固定列以外的口径时，原样写在这里。
# 没有这一列，那几条比较就悄悄消失了 —— 1417 的「規則3.7 公告前」
# 和 3336 的「前180日均价」当初就是这么丢的。
LADDER_OTHER = "其他比较项"

DEAL_COLUMNS = [
    "公告日期", "股票代码", "受要约方", "受要约方全称", "要约方", "要约方财务顾问",
    "要约类型", "对价形式", "要约价(HKD)", "主值溢价率(%)", "主值口径",
    "交易规模(HKD)", "上市地位意向", "停牌前最后交易日",
    *[f"较{c}(%)" for c in LADDER_COLUMNS], LADDER_OTHER,
    "置信度", "复算校验", "备注", "公告标题", "PDF链接",
]


def _deal_row(d: Deal) -> list[str]:
    other = "；".join(f"较{k} {v}%" for k, v in d.premium_ladder.items()
                      if k not in LADDER_COLUMNS)
    return [d.date, d.code, d.name, d.target_full, d.offeror, d.offeror_fa,
            d.offer_type, d.consideration, d.offer_price, d.premium_pct,
            d.premium_basis, d.deal_size, d.listing_intent, d.last_trading_day,
            *[d.premium_ladder.get(c, "") for c in LADDER_COLUMNS], other,
            d.confidence, d.checks, d.notes, d.title, d.pdf_url]


def _write_deals(deals: list[Deal], log) -> None:
    if not deals:
        return
    out = ROOT / "data" / "deals.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(DEAL_COLUMNS)
        for d in deals:
            w.writerow(_deal_row(d))

    # 出处单独存：CSV 塞不下整段引文，而重启后没有出处就违背铁律三
    from . import dealsview
    dealsview.save_evidence(deals, ROOT / "data" / "deals_evidence.json")
    log(f"  已保存 {out.name}（{len(deals)} 单）")


def _write_report(rows, result: Result, rules, csv_path, log) -> Path:
    from . import report as R

    scr_dir = ROOT / "data" / "screening"
    with (scr_dir / "screened.csv").open(encoding="utf-8-sig", newline="") as fh:
        table = list(csv.DictReader(fh))
    manual = sum(1 for r in table if r.get("bucket") == "manual")
    notes = [f"人工复核桶有 {manual} 条，须逐条看完（铁律二）。"] if manual else []
    notes += list(result.qc_notes)
    path = R.write_report(table, scr_dir / "report.html",
                          rules_version=rules.version, source=str(csv_path),
                          notes=notes, deals=result.deals)
    log(f"已生成 {path.name}")
    return path


def _write_diagnostic(lines: list[str], result: Result) -> Path:
    """把这次运行压成一个文件，发给 Claude 就够了。"""
    import platform
    import sys

    out = ROOT / "SEND_TO_CLAUDE.txt"
    parts = [
        "=" * 64, "环境", "=" * 64,
        f"系统：{platform.platform()}",
        f"Python：{sys.version}",
        f"目录：{ROOT}",
        "", "=" * 64, "结果", "=" * 64,
        f"抓到：{result.fetched} 条",
        f"筛查：{result.screened} 条",
        f"判定桶：{result.buckets}",
        f"错误：{result.error or '（无）'}",
        "", "=" * 64, "运行日志", "=" * 64,
        *lines[-400:],
    ]
    out.write_text("\n".join(str(p) for p in parts), encoding="utf-8")
    return out
