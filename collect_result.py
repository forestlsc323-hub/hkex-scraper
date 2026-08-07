# -*- coding: utf-8 -*-
"""把这次运行的结果打包成一个文件，发给 Claude 就够了。

用法：python collect_result.py
产出：发给CLAUDE.txt
"""

import csv
import json
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "发给CLAUDE.txt"
LIMIT = 60          # 每段最多贴多少行


def section(title):
    return f"\n{'=' * 64}\n{title}\n{'=' * 64}\n"


def tail(path: Path, n: int) -> str:
    if not path.exists():
        return f"（没有 {path.name}）"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        return f"（读不了：{exc}）"
    return "\n".join(lines[-n:]) or "（空）"


def csv_summary(path: Path, sample: int = 10) -> str:
    if not path.exists():
        return f"（没有 {path.name}）"
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return f"{path.name}：0 行"
    out = [f"{path.name}：{len(rows)} 行", f"列：{list(rows[0].keys())}", "", "前几行："]
    for r in rows[:sample]:
        out.append("  " + " | ".join(
            f"{k}={str(r.get(k, ''))[:40]}"
            for k in list(r)[:6]))
    return "\n".join(out)


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except Exception:
            pass

    parts = [
        section("环境"),
        f"系统：{platform.platform()}",
        f"Python：{sys.version}",
        f"目录：{ROOT}",
    ]

    cfgp = ROOT / "config.yaml"
    if cfgp.exists():
        try:
            import yaml
            cfg = yaml.safe_load(cfgp.read_text(encoding="utf-8"))
            parts += [section("配置"),
                      f"日期范围：{cfg['date_range']['from']} ~ {cfg['date_range']['to']}",
                      f"翻页步长：{cfg['listing']['row_range_step']}",
                      f"请求间隔：{cfg['http']['min_interval_seconds']} 秒"]
        except Exception as exc:
            parts += [section("配置"), f"（读不了：{exc}）"]

    logs = ROOT / "logs"
    parts += [section("抓取日志（最后 %d 行）" % LIMIT),
              tail(logs / "vendor.log", LIMIT)]

    raw = ROOT / "data" / "raw"
    parts += [section("抓到的公告列表"),
              csv_summary(raw / "vendor_listing.csv")]

    scr = ROOT / "data" / "screening"
    parts += [section("筛查结果"), csv_summary(scr / "screened.csv", sample=5)]

    qc = scr / "QC_REPORT.md"
    if qc.exists():
        text = qc.read_text(encoding="utf-8", errors="replace")
        parts += [section("质控报告（前 120 行）"),
                  "\n".join(text.splitlines()[:120])]

    # 最有价值的两块：人工复核桶 + 矛盾行
    sc = scr / "screened.csv"
    if sc.exists():
        with sc.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        manual = [r for r in rows if r.get("bucket") == "manual"]
        parts += [section(f"人工复核桶（{len(manual)} 条，最多列 {LIMIT} 条）")]
        for r in manual[:LIMIT]:
            parts.append(f"  {r.get('date','')} {r.get('code','')} "
                         f"{r.get('name','')} | {r.get('title','')[:80]}")

        retained = [r for r in rows if r.get("bucket") == "retained"]
        parts += [section(f"留存桶（{len(retained)} 条，最多列 {LIMIT} 条）")]
        for r in retained[:LIMIT]:
            parts.append(f"  {r.get('date','')} {r.get('code','')} "
                         f"{r.get('name','')} | {r.get('title','')[:80]}")

    OUT.write_text("\n".join(str(p) for p in parts), encoding="utf-8")
    print(f"诊断已打包：{OUT}")
    print(f"大小：{OUT.stat().st_size / 1024:.0f} KB —— 把里面的内容发给 Claude")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
