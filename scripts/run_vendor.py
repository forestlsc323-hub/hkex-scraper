# -*- coding: utf-8 -*-
"""直接用 vendor/hkex_client.py（你那份实战客户端，一字未改）抓数据。

用法：
    python run_vendor.py                       # 用 config.yaml 里的日期范围
    python run_vendor.py 2026-01-01 2026-06-30 # 或直接指定

产出：
    data/raw/vendor_listing.csv    抓到的全部公告
    data/raw/vendor_raw.json       原始记录（未经任何加工）

这条路子绕开我写的所有封装，只用你那份客户端。
它抓到东西就说明网络和接口都没问题；抓不到，错误也来自你自己的代码，
排查范围一下子小很多。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import csv
import datetime as dt
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "vendor"))

ROOT = Path(__file__).resolve().parent


def main(argv):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stderr),
                  logging.FileHandler(ROOT / "logs" / "vendor.log",
                                      encoding="utf-8")])

    if len(argv) >= 2:
        d1 = dt.datetime.strptime(argv[0], "%Y-%m-%d").date()
        d2 = dt.datetime.strptime(argv[1], "%Y-%m-%d").date()
    else:
        import yaml
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        rng = cfg["date_range"]
        d1 = rng["from"] if isinstance(rng["from"], dt.date) else \
            dt.datetime.strptime(str(rng["from"]), "%Y-%m-%d").date()
        d2 = rng["to"] if isinstance(rng["to"], dt.date) else \
            dt.datetime.strptime(str(rng["to"]), "%Y-%m-%d").date()

    from hkex_client import HKEXClient        # noqa: E402  vendor 目录里的

    print(f"日期范围 {d1} ~ {d2}（共 {(d2 - d1).days + 1} 天）")
    print("正在建立会话并抓取，进度见下方日志…\n")

    done = [0]

    def progress(day, total_days, count):
        done[0] += 1
        print(f"  [{done[0]}/{total_days}] {day}　累计 {count} 条", flush=True)

    client = HKEXClient()
    records = client.search(d1, d2, progress_cb=progress)

    out_dir = ROOT / "data" / "raw"
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "vendor_raw.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")

    if records:
        cols = sorted({k for r in records for k in r})
        preferred = ["NEWS_ID", "DATE_TIME", "STOCK_CODE", "STOCK_NAME",
                     "TITLE", "FILE_LINK", "FILE_INFO"]
        header = [c for c in preferred if c in cols] + \
                 [c for c in cols if c not in preferred]
        with (out_dir / "vendor_listing.csv").open(
                "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
            w.writeheader()
            w.writerows(records)

    print(f"\n抓到 {len(records)} 条")
    print(f"  {out_dir / 'vendor_listing.csv'}")
    print(f"  {out_dir / 'vendor_raw.json'}")
    if records:
        print("\n前 3 条：")
        for r in records[:3]:
            print(f"  {r.get('DATE_TIME','')}  {r.get('STOCK_CODE','')}  "
                  f"{str(r.get('TITLE',''))[:60]}")
    return 0 if records else 1


if __name__ == "__main__":
    (ROOT / "logs").mkdir(exist_ok=True)
    raise SystemExit(main(sys.argv[1:]))
