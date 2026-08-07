"""Scrape HKEXnews takeover-offer announcements and extract premium / discount rates."""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime
from pathlib import Path

import requests

from . import config, extract, pdf
from .search import Announcement, search

FIELDS = [
    "stock_code",
    "stock_name",
    "date",
    "offer_type",
    "premium_pct",
    "discount_pct",
    "benchmark",
    "title",
    "pdf_url",
    "status",
    "context",
]


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def build_parser() -> argparse.ArgumentParser:
    today = date.today()
    parser = argparse.ArgumentParser(prog="hkex-offers", description=__doc__)
    parser.add_argument("--from", dest="date_from", type=_parse_date,
                        default=date(today.year, 1, 1), help="起始日期 YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", type=_parse_date, default=today,
                        help="结束日期 YYYY-MM-DD")
    parser.add_argument("--lang", choices=["ZH", "EN"], default="ZH")
    parser.add_argument("--keywords", nargs="+", help="覆盖默认标题搜索关键词")
    parser.add_argument("--out", type=Path, default=Path("offers.csv"))
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/pdf"))
    parser.add_argument("--max-pages", type=int, default=60,
                        help="每份 PDF 最多解析的页数，0 表示全部")
    parser.add_argument("--limit", type=int, help="只处理前 N 份公告（调试用）")
    parser.add_argument("--delay", type=float, default=1.0, help="请求间隔秒数")
    parser.add_argument("--list-only", action="store_true",
                        help="只输出公告列表，不下载 PDF")
    return parser


def shortlist(announcements: list[Announcement]) -> list[tuple[Announcement, str]]:
    out = []
    for ann in announcements:
        if config.EXCLUDE_TITLE.search(ann.title):
            continue
        offer_type = config.classify_offer_type(ann.title)
        if offer_type:
            out.append((ann, offer_type))
    return out


def process(ann: Announcement, offer_type: str, session: requests.Session,
            cache_dir: Path, max_pages: int) -> dict:
    row = {
        "stock_code": ann.stock_code,
        "stock_name": ann.stock_name,
        "date": ann.date_time,
        "offer_type": offer_type,
        "premium_pct": "",
        "discount_pct": "",
        "benchmark": "",
        "title": ann.title,
        "pdf_url": ann.pdf_url,
        "status": "",
        "context": "",
    }
    try:
        data = pdf.download(ann.pdf_url, cache_dir, session=session)
        text = pdf.extract_text(data, max_pages=max_pages)
    except Exception as exc:
        row["status"] = f"download_error: {exc}"
        return row

    if len(text.strip()) < 200:
        row["status"] = "no_text_layer"
        return row

    hit = extract.pick_primary(extract.find_hits(text))
    if hit is None:
        row["status"] = "no_match"
        return row

    if hit.kind == extract.PREMIUM:
        row["premium_pct"] = hit.pct
    else:
        row["discount_pct"] = hit.pct
    row["benchmark"] = hit.benchmark
    row["context"] = hit.context
    row["status"] = "ok"
    return row


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    keywords = args.keywords or config.DEFAULT_KEYWORDS[args.lang]

    print(f"搜索 {args.date_from} ~ {args.date_to}，关键词 {keywords}", file=sys.stderr)
    session = requests.Session()
    announcements = search(keywords, date_from=args.date_from, date_to=args.date_to,
                           lang=args.lang, delay=args.delay, session=session)
    print(f"命中 {len(announcements)} 份公告", file=sys.stderr)

    selected = shortlist(announcements)
    print(f"识别为 MGO/VGO/PO 的有 {len(selected)} 份", file=sys.stderr)
    if args.limit:
        selected = selected[: args.limit]

    rows = []
    for i, (ann, offer_type) in enumerate(selected, 1):
        if args.list_only:
            rows.append({**{f: "" for f in FIELDS},
                         "stock_code": ann.stock_code, "stock_name": ann.stock_name,
                         "date": ann.date_time, "offer_type": offer_type,
                         "title": ann.title, "pdf_url": ann.pdf_url,
                         "status": "list_only"})
            continue
        print(f"[{i}/{len(selected)}] {ann.stock_code} {ann.date_time} {offer_type}",
              file=sys.stderr)
        rows.append(process(ann, offer_type, session, args.cache_dir, args.max_pages))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    ok = sum(1 for r in rows if r["status"] == "ok")
    print(f"写入 {args.out}，成功提取 {ok}/{len(rows)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
