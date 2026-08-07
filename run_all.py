# -*- coding: utf-8 -*-
"""一键跑完整个流程，全程留痕。

批处理窗口一关输出就没了 —— 所以真正的流程放在这里，用 Python 跑，
屏幕和 `运行日志.txt` 同时写。不管跑成什么样，日志一定还在。

用法：
    python run_all.py                        # 用 config.yaml 的日期范围
    python run_all.py 2026-06-01 2026-06-30  # 或直接指定
"""

from __future__ import annotations

import datetime as dt
import io
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TRANSCRIPT = ROOT / "run_log.txt"   # 英文名：中文名在部分环境下会乱码


class Tee:
    """同时写屏幕和文件。屏幕编码撑不住的字符换成 ?，绝不让打印把程序打死。"""

    def __init__(self, stream, fh):
        self.stream, self.fh = stream, fh

    def write(self, text):
        try:
            self.stream.write(text)
        except UnicodeEncodeError:
            self.stream.write(text.encode("ascii", "replace").decode("ascii"))
        self.fh.write(text)
        self.fh.flush()
        return len(text)

    def flush(self):
        try:
            self.stream.flush()
        except Exception:
            pass
        self.fh.flush()

    def isatty(self):
        return False


def banner(n: int, total: int, title: str) -> None:
    print(f"\n{'=' * 62}\n[{n}/{total}] {title}\n{'=' * 62}", flush=True)


def read_dates(argv: list[str]) -> tuple[dt.date, dt.date]:
    if len(argv) >= 2:
        return (dt.datetime.strptime(argv[0], "%Y-%m-%d").date(),
                dt.datetime.strptime(argv[1], "%Y-%m-%d").date())
    import yaml
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    rng = cfg["date_range"]

    def to_date(v):
        return v if isinstance(v, dt.date) else \
            dt.datetime.strptime(str(v), "%Y-%m-%d").date()

    return to_date(rng["from"]), to_date(rng["to"])


def step_fetch(d1: dt.date, d2: dt.date) -> int:
    """用 vendor/hkex_client.py（你那份客户端，一字未改）抓列表。"""
    import csv
    import json
    import logging

    sys.path.insert(0, str(ROOT / "vendor"))

    logging.basicConfig(
        level=logging.INFO, force=True,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)])

    from hkex_client import HKEXClient          # noqa: E402

    n_days = (d2 - d1).days + 1
    print(f"日期范围 {d1} ~ {d2}（{n_days} 天）")
    print("先访问检索页建立会话，再逐日抓取。\n")

    done = [0]

    def progress(day, total_days, count):
        done[0] += 1
        print(f"  [{done[0]}/{total_days}] {day}　累计 {count} 条", flush=True)

    client = HKEXClient()
    cookies = sorted(c.name for c in client.session.cookies)
    print(f"会话 cookie：{cookies or '（服务端未下发 —— 记下这一点告诉 Claude）'}\n")

    records = client.search(d1, d2, progress_cb=progress)

    out_dir = ROOT / "data" / "raw"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "vendor_raw.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")

    if records:
        preferred = ["NEWS_ID", "DATE_TIME", "STOCK_CODE", "STOCK_NAME",
                     "TITLE", "FILE_LINK", "FILE_INFO"]
        cols = sorted({k for r in records for k in r})
        header = [c for c in preferred if c in cols] + \
                 [c for c in cols if c not in preferred]
        with (out_dir / "vendor_listing.csv").open(
                "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
            w.writeheader()
            w.writerows(records)

    print(f"\n抓到 {len(records)} 条 → {out_dir / 'vendor_listing.csv'}")
    for r in records[:3]:
        print(f"  {r.get('DATE_TIME','')}  {r.get('STOCK_CODE','')}  "
              f"{str(r.get('TITLE',''))[:56]}")
    return len(records)


def main(argv: list[str]) -> int:
    TRANSCRIPT.parent.mkdir(parents=True, exist_ok=True)
    fh = TRANSCRIPT.open("w", encoding="utf-8")
    sys.stdout = Tee(sys.__stdout__, fh)
    sys.stderr = Tee(sys.__stderr__, fh)

    started = dt.datetime.now()
    print(f"开始 {started:%Y-%m-%d %H:%M:%S}")
    print(f"目录 {ROOT}")
    print(f"Python {sys.version.split()[0]}")

    ok = False
    try:
        d1, d2 = read_dates(argv)

        banner(1, 4, "抓取公告列表（vendor/hkex_client.py）")
        count = step_fetch(d1, d2)

        if count:
            banner(2, 4, "质控筛查")
            import run_screening
            run_screening.main([str(ROOT / "data" / "raw" / "vendor_listing.csv")])

            banner(3, 4, "生成网页")
            import run_report
            run_report.main([])
            ok = True
        else:
            print("\n抓到 0 条，跳过后面两步。")

        banner(4, 4, "打包诊断")
        import collect_result
        collect_result.main()

    except KeyboardInterrupt:
        print("\n\n已被用户中断（Ctrl+C）。")
    except Exception:
        print("\n\n出错了，完整堆栈如下：\n")
        traceback.print_exc(file=sys.stdout)
        print("\n把 run_log.txt 整个发给 Claude —— 这份堆栈是关键。")

    elapsed = (dt.datetime.now() - started).total_seconds()
    print(f"\n{'=' * 62}")
    print(f"结束，用时 {elapsed / 60:.1f} 分钟")
    print(f"\n完整日志：{TRANSCRIPT}")
    print(f"发给 Claude：{ROOT / 'SEND_TO_CLAUDE.txt'}")
    if ok:
        print(f"结果网页：{ROOT / 'data' / 'screening' / 'report.html'}")
    print("=" * 62)

    # 先把 stdout/stderr 换回来再关文件 —— 反过来的话，
    # 之后任何一次 print 都会撞上「I/O operation on closed file」。
    sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
    fh.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
