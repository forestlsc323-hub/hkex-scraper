"""把筛查结果生成一张能直接看的网页。

用法：
    python run_report.py                # 读 data/screening/screened.csv
    python run_report.py 别的.csv

产出 data/screening/report.html —— 单个自包含文件，双击就能看，断网也行。
点卡片筛选判定桶、点表头排序、点行展开判定依据。
"""

import csv
import sys
from pathlib import Path

from hkexdb import config, console, logsetup, pipeline, report


def main(argv: list[str]) -> int:
    console.init()
    cfg = config.load("config.yaml")
    cfg.ensure_dirs()
    logsetup.setup(cfg.log_dir, cfg.log_level, run_name="report")

    src = Path(argv[0]) if argv else cfg.data_dir / "screening" / "screened.csv"
    if not src.exists():
        print(f"找不到 {src}")
        print(pipeline.format_status(pipeline.status(cfg)))
        return 1

    with src.open(encoding="utf-8-sig", newline="") as fh:
        records = list(csv.DictReader(fh))
    if not records:
        print(f"{console.WARN} {src} 是空的")
        return 1

    version = records[0].get("rules_version", "")
    notes = []
    manual = sum(1 for r in records if r.get("bucket") == "manual")
    if manual:
        notes.append(f"人工复核桶有 {manual} 条，必须逐条看完再进抽取（铁律二）。")
    if any("演示" in (r.get("row_id") or "") or (r.get("row_id") or "").startswith("demo")
           for r in records):
        notes.append("⚠️ 这份数据来自 --demo 演示样本，不是真实检索结果。")

    out = cfg.data_dir / "screening" / "report.html"
    report.write_report(records, out, rules_version=version,
                        source=str(src), notes=notes)

    print(f"\n网页已生成：{out}")
    print("双击打开即可。点卡片筛选判定桶、点表头排序、点行展开判定依据。")
    print(pipeline.format_status(pipeline.status(cfg)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
