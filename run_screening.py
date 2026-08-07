"""阶段二：T0 Screening —— 抓 PDF 之前的标题层筛选。

用法：
    python run_screening.py                    # 读 data/raw/listing_raw.csv
    python run_screening.py 我的清单.csv        # 或指定别的 CSV

产出：
    data/screening/screened.csv     全量，含判定桶与依据（软删除，一行不少）
    data/screening/QC_REPORT.md     四层防漏网的质控报告

铁律二：这一层跑完要**人工过一遍**再进抽取。先看 QC_REPORT.md 的
矛盾行清单和人工复核桶，确认没有误杀，再往下走。
"""

import csv
import sys
from pathlib import Path

from hkexdb import config, logsetup, screening as S


def load_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    # raw 表的列名来自披露易接口；这里只做「取用」，不改 raw 本身
    out = []
    for i, r in enumerate(rows):
        out.append({
            "row_id": r.get("_row_uid") or f"row{i}",
            "date": (r.get("DATE_TIME") or "").split()[0] if r.get("DATE_TIME") else "",
            "code": r.get("STOCK_CODE", ""),
            "name": r.get("STOCK_NAME", ""),
            "title": r.get("TITLE", ""),
            "pdf_url": r.get("FILE_LINK", ""),
            "file_info": r.get("FILE_INFO", ""),
        })
    return out


def write_report(report: S.QualityReport, rules: S.Rules, out_dir: Path,
                 records: list[dict]) -> Path:
    lines = [
        "# T0 Screening 质控报告",
        "",
        f"规则版本：`{rules.version}`　样本 {len(records)} 条",
        "",
        "## 判定桶分布",
        "",
        "| 桶 | 条数 | 含义 |",
        "| --- | --- | --- |",
    ]
    meaning = {
        S.RETAINED: "命中 T0 特征，进入抽取",
        S.EXCLUDED: "后续／程序公告，软删除（行还在）",
        S.SPECIAL: "非三种要约品种，剔除但可分辨（坑⑬）",
        S.MANUAL: "两层均未命中，**必须逐条人工看**（手册第三步）",
        S.SUPERSEDED: "被取代的旧标题行（坑⑫）",
    }
    for b, n in sorted(report.counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {b} | {n} | {meaning.get(b, '')} |")

    lines += ["", "## 底部数量校验", "",
              "记录数＝代号数＝简称数＝标题行数＝判定桶合计", "",
              "```"]
    for k, v in report.reconciliation.items():
        lines.append(f"{k:<12} {v}")
    lines += ["```", "",
              ("✅ 平" if report.reconciled else
               "❌ **不平 —— 先查勘误行与归属继承（坑⑫）再往下走**"), ""]

    lines += ["## 第 1 层：矛盾行自检（防误杀，最强的一层）", "",
              f"被灰的行里含「{'／'.join(rules.contradiction_terms)}」的共 "
              f"**{len(report.contradictions)}** 条，逐条看是不是误杀：", ""]
    if report.contradictions:
        lines += ["| 代号 | 简称 | 命中词 | 标题 |", "| --- | --- | --- | --- |"]
        for r in report.contradictions:
            lines.append(f"| {r['code']} | {r['name']} | "
                         f"{'／'.join(r['contradiction_terms'])} | {r['title'][:70]} |")
    else:
        lines.append("（无）")

    manual = [r for r in records if r["verdict"].bucket == S.MANUAL]
    lines += ["", "## 人工复核桶（手册第三步）", "",
              f"两层均未命中的 **{len(manual)}** 条，必须逐条看：", ""]
    if manual:
        lines += ["| 代号 | 简称 | 标题 |", "| --- | --- | --- |"]
        for r in manual:
            lines.append(f"| {r['code']} | {r['name']} | {r['title'][:80]} |")
    else:
        lines.append("（无）")

    flagged = [r for r in records if r["verdict"].manual_flags]
    if flagged:
        lines += ["", "## 人工标记（坑②：不可自动排除的词）", ""]
        for r in flagged:
            lines.append(f"- `{r['code']}` {r['title'][:60]} → "
                         f"{'；'.join(r['verdict'].manual_flags)}")

    mirrors = S.find_mirror_pairs(records)
    if mirrors:
        lines += ["", "## 镜像归档（坑⑨）：需人工指定受要约方后合并记一单", ""]
        for m in mirrors:
            lines.append(f"- 代号 {m.codes}　简称 {m.names}　{m.title[:60]}")

    lines += ["", f"## 第 4 层：随机抽查 {len(report.random_sample)} 条"
                  f"（种子 {rules.audit_seed}，可复现）", ""]
    for r in report.random_sample:
        lines.append(f"- `{r['code']}` [{r['verdict'].bucket}] {r['title'][:70]}")

    lines += ["", "## 还需人工完成的两层", "",
              "- **第 2 层 公司级完备性对账（最强）**：每家公司的 T0 必须落在"
              "「主清单／窗口前／特殊品种／流产案」四选一，数量守恒。",
              "- **第 3 层 外部对数**：与 SFC Takeovers Bulletin 或 Wind 的期内单数核总。",
              "",
              "这两层需要外部数据，本工具不做 —— 交表前必须手工跑完。"]

    if report.notes:
        lines += ["", "## 提示", ""] + [f"- {n}" for n in report.notes]

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "QC_REPORT.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main(argv: list[str]) -> int:
    cfg = config.load("config.yaml")
    cfg.ensure_dirs()
    logsetup.setup(cfg.log_dir, cfg.log_level, run_name="screening")

    src = Path(argv[0]) if argv else cfg.raw_dir / "listing_raw.csv"
    if not src.exists():
        print(f"找不到 {src}，先跑 run_listing.py")
        return 1

    rules = S.load_rules("screening_rules.yaml")
    records = load_csv(src)
    report = S.screen(records, rules)

    out_dir = cfg.data_dir / "screening"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 软删除：全量写出，一行不少，只加判定列
    with (out_dir / "screened.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["row_id", "date", "code", "name", "bucket", "species",
                    "matched_exclude", "matched_retain", "is_bundled",
                    "manual_flags", "reasons", "title", "pdf_url", "rules_version"])
        for r in records:
            v = r["verdict"]
            w.writerow([r["row_id"], r["date"], r["code"], r["name"], v.bucket,
                        v.species, "／".join(v.matched_exclude),
                        "／".join(v.matched_retain), v.is_bundled,
                        "；".join(v.manual_flags), "；".join(v.reasons),
                        r["title"], r["pdf_url"], rules.version])

    path = write_report(report, rules, out_dir, records)
    print(f"\n全量结果：{out_dir / 'screened.csv'}（{len(records)} 行，软删除，一行不少）")
    print(f"质控报告：{path}")
    print(f"\n判定桶：{report.counts}")
    print(f"数量校验：{'✅ 平' if report.reconciled else '❌ 不平'}")
    print(f"矛盾行 {len(report.contradictions)} 条、"
          f"人工复核桶 {report.counts.get(S.MANUAL, 0)} 条 —— 先看完再进抽取（铁律二）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
