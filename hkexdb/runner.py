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

STEPS = ["抓取公告列表", "质控筛查", "生成网页", "打包诊断"]


class Cancelled(Exception):
    """用户点了停止。"""


@dataclass
class Result:
    ok: bool = False
    fetched: int = 0
    screened: int = 0
    report_path: Path | None = None
    diagnostic_path: Path | None = None
    log_path: Path | None = None
    error: str = ""
    buckets: dict = field(default_factory=dict)


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
        fetch=_fetch) -> Result:
    """跑完整条流水线。`fetch` 可替换，方便离线测试。"""
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
        log(f"\n【1/4】{STEPS[0]}")
        records = fetch(date_from, date_to, log, step, cancel_event)
        result.fetched = len(records)
        csv_path = _write_listing(records, log)
        log(f"抓到 {len(records)} 条")

        if records:
            step(1, 0.0)
            log(f"\n【2/4】{STEPS[1]}")
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
            result.screened = len(recs)

            scr_dir = ROOT / "data" / "screening"
            scr_dir.mkdir(parents=True, exist_ok=True)
            with (scr_dir / "screened.csv").open(
                    "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                w.writerow(["row_id", "date", "code", "name", "bucket", "species",
                            "matched_exclude", "matched_retain", "manual_flags",
                            "reasons", "title", "pdf_url", "rules_version"])
                for r in recs:
                    v = r["verdict"]
                    w.writerow([r["row_id"], r["date"], r["code"], r["name"],
                                v.bucket, v.species, "／".join(v.matched_exclude),
                                "／".join(v.matched_retain), "；".join(v.manual_flags),
                                "；".join(v.reasons), r["title"], r["pdf_url"],
                                rules.version])
            for bucket, n in sorted(result.buckets.items(), key=lambda kv: -kv[1]):
                log(f"  {bucket}: {n}")
            log(f"数量校验：{'平' if report_obj.reconciled else '不平 —— 需人工检查'}")
            step(1, 1.0)

            step(2, 0.0)
            log(f"\n【3/4】{STEPS[2]}")
            from . import report as R
            with (scr_dir / "screened.csv").open(encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))
            manual = sum(1 for r in rows if r.get("bucket") == "manual")
            notes = [f"人工复核桶有 {manual} 条，必须逐条看完再进抽取（铁律二）。"] \
                if manual else []
            result.report_path = R.write_report(
                rows, scr_dir / "report.html",
                rules_version=rules.version, source=str(csv_path), notes=notes)
            log(f"已生成 {result.report_path.name}")
            step(2, 1.0)
            result.ok = True
        else:
            log("\n抓到 0 条，跳过筛查与网页。")

        step(3, 0.0)
        log(f"\n【4/4】{STEPS[3]}")
        result.diagnostic_path = _write_diagnostic(lines, result)
        log(f"已生成 {result.diagnostic_path.name}")
        step(3, 1.0)

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
