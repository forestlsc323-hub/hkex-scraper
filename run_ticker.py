"""按股票代码检索某家公司的全部历史公告。

用途：手册第 2 层「公司级完备性对账」—— 四层防漏网里**最强的一层**。
对某家公司，把它的全部公告拉出来，才能确认它的 T0 落在
「主清单／窗口前／特殊品种／流产案」四个桶的哪一个，做到数量守恒。

用法：
    python run_ticker.py 1417 3336 00195
    python run_ticker.py 1417 --from 2026-01-01 --to 2026-08-07

产出 data/ticker/<代码>.csv，含该公司全部公告（含已除牌证券）。
"""

import argparse
import csv
import datetime as dt
import logging
import sys
from pathlib import Path

from hkexdb import config, console, listing, logsetup, stocks


def main(argv: list[str]) -> int:
    console.init()
    parser = argparse.ArgumentParser(prog="run_ticker", description=__doc__)
    parser.add_argument("tickers", nargs="+", help="股票代码，如 1417 0700.HK")
    parser.add_argument("--from", dest="date_from", help="起始日期 YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", help="结束日期 YYYY-MM-DD")
    args = parser.parse_args(argv)

    cfg = config.load("config.yaml")
    cfg.ensure_dirs()
    logsetup.setup(cfg.log_dir, cfg.log_level, run_name="ticker")
    log = logging.getLogger("run_ticker")

    def as_date(text):
        return dt.datetime.strptime(text, "%Y-%m-%d").date() if text else None

    session = listing.make_session(cfg)
    listing.warm_up_session(session)

    map_cache = cfg.data_dir / "stock_id_map.json"
    stock_map = stocks.load_cached_map(map_cache)
    if not stock_map:
        log.info("首次运行，下载证券清单（活跃 + 已除牌）…")
        stock_map = stocks.load_stock_id_map(session, map_cache)
    if not stock_map:
        print("拿不到证券清单，无法按代码检索。", file=sys.stderr)
        return 1
    print(f"证券清单 {len(stock_map)} 条（含已除牌）")

    out_dir = cfg.data_dir / "ticker"
    out_dir.mkdir(parents=True, exist_ok=True)

    for ticker in args.tickers:
        try:
            rows = listing.fetch_ticker_history(
                session, cfg, ticker, stock_map,
                date_from=as_date(args.date_from), date_to=as_date(args.date_to))
        except ValueError as exc:
            print(f"{console.FAIL} {ticker}：{exc}", file=sys.stderr)
            continue
        except listing.Cancelled:
            print("已停止", file=sys.stderr)
            return 130

        if not rows:
            print(f"{console.WARN} {ticker}：没有公告")
            continue

        header = sorted({k for r in rows for k in r})
        path = out_dir / f"{stocks.normalize_code(ticker)}.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        span = f"{rows[-1].get('DATE_TIME', '?')} ~ {rows[0].get('DATE_TIME', '?')}"
        print(f"{console.OK} {ticker}：{len(rows)} 条（{span}）→ {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
