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

from hkexdb import config, console, pipeline, logsetup, screening as S


# 演示样本：三条是真实公告标题（1417 / 3336 / 00195，已人工核对），
# 其余是按手册十三个坑构造的，用来演示报告长什么样。
# ⚠️ 绝不是真实的披露易检索结果。
DEMO_ROWS = [
    ("2026-06-15", "1417", "浦江中國", "聯合公告 (1) 完成出售及購買浦江中國控股有限公司擬出售股份 (2) 由力高證券有限公司為並代表YOMI.SUN HOLDING LIMITED作出強制性無條件現金要約 及 (3) 恢復股份買賣", "真实"),
    ("2026-05-18", "3336", "巨騰國際", "聯合公告 (1)有關本公司已發行股份總數約27.81%的買賣協議 (2)具有前置條件之自願性有條件全面現金要約 (3)須予披露交易 及 (4)復牌", "真实"),
    ("2026-05-18", "6613", "藍思科技", "聯合公告 (1)有關本公司已發行股份總數約27.81%的買賣協議 (2)具有前置條件之自願性有條件全面現金要約 (3)須予披露交易 及 (4)復牌", "真实-镜像"),
    ("2026-06-15", "00195", "綠科科技", "公告 由華富建業企業融資有限公司代表YELLOWSTONE INTERNATIONAL LIMITED提出附帶先決條件的自願現金部分收購要約", "真实"),
    ("2026-06-20", "1417", "浦江中國", "寄發綜合文件", "构造"),
    ("2026-06-25", "1417", "浦江中國", "強制性無條件現金要約之要約結果", "构造"),
    ("2026-07-01", "0002", "乙公司", "每月最新資料", "构造-坑③"),
    ("2026-07-02", "00195", "綠科科技", "達成先決條件之公告", "构造-坑④"),
    ("2026-07-03", "0005", "戊公司", "建議以協議安排方式將公司私有化及撤銷上市地位", "构造-坑②⑬"),
    ("2026-07-04", "0006", "己公司", "延遲寄發綜合文件－訂立買賣協議之後續安排", "构造-矛盾行"),
    ("2026-07-05", "0007", "庚公司", "董事會會議日期", "构造-人工桶"),
    ("2026-07-06", "0008", "辛公司", "就要約委任獨立財務顧問", "构造-坑④"),
    ("2026-07-07", "0009", "壬公司", "聯合公告 - 可能強制性無條件現金要約及恢復買賣 (取消－標題已被取代及更換)", "构造-坑⑫"),
    ("2026-07-07", "0009", "壬公司", "聯合公告 - 可能強制性無條件現金要約及恢復買賣 (修改後標題)", "构造-坑⑫"),
    ("2026-07-08", "0010", "癸公司", "有關強制收購剩餘股份之公告", "构造-坑②"),
]


def demo_records() -> list[dict]:
    return [{"row_id": f"demo{i}", "date": d, "code": c, "name": n,
             "title": t, "pdf_url": "", "file_info": "", "_origin": o}
            for i, (d, c, n, t, o) in enumerate(DEMO_ROWS)]


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
            # 优先用清洗后的标题：接口返回的 TITLE 是 HTML 片段，
            # 带 &amp; / <br/> / tooltip 残留，直接拿去匹配词表会静默失配。
            "title": r.get("TITLE_CLEAN") or r.get("TITLE", ""),
            "title_raw": r.get("TITLE", ""),
            "pdf_url": r.get("FILE_LINK", ""),
            "file_info": r.get("FILE_INFO", ""),
        })
    return out


def write_report(report: S.QualityReport, rules: S.Rules, out_dir: Path,
                 records: list[dict], *, is_demo: bool = False,
                 source: str = "") -> Path:
    lines = ["# T0 Screening 质控报告", ""]
    if is_demo:
        lines += [
            "> # ⚠️ 演示数据，不是真实检索结果",
            ">",
            "> 本报告由 `run_screening.py --demo` 生成，用于演示报告格式。",
            "> 15 条样本中只有 4 条是真实公告标题（1417 / 3336 / 6613 / 00195），",
            "> 其余按手册十三个坑构造。**不得用于任何分析或对账。**",
            ">",
            "> 真实报告需先跑 `python run_listing.py` 抓到披露易的检索结果。",
            "",
        ]
    lines += [
        f"规则版本：`{rules.version}`　样本 {len(records)} 条"
        + (f"　数据源：`{source}`" if source else ""),
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
    console.init()
    cfg = config.load("config.yaml")
    cfg.ensure_dirs()
    logsetup.setup(cfg.log_dir, cfg.log_level, run_name="screening")

    rules = S.load_rules("screening_rules.yaml")
    is_demo = "--demo" in argv
    argv = [a for a in argv if not a.startswith("--")]

    if is_demo:
        records = demo_records()
        source = "演示数据（非真实）"
        print(f"{console.WARN} 演示模式：15 条样本中仅 4 条为真实公告标题，其余为构造样本。\n")
    else:
        src = Path(argv[0]) if argv else cfg.raw_dir / "listing_raw.csv"
        if not src.exists():
            print(f"找不到 {src}")
            print("先跑：python run_listing.py")
            print("只想看报告长什么样：python run_screening.py --demo")
            return 1
        records = load_csv(src)
        source = str(src)

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

    path = write_report(report, rules, out_dir, records,
                        is_demo=is_demo, source=source)
    print(f"\n全量结果：{out_dir / 'screened.csv'}（{len(records)} 行，软删除，一行不少）")
    print(f"质控报告：{path}")
    print(f"\n判定桶：{report.counts}")
    print(f"数量校验：{console.OK + ' 平' if report.reconciled else console.FAIL + ' 不平'}")
    print(f"矛盾行 {len(report.contradictions)} 条、"
          f"人工复核桶 {report.counts.get(S.MANUAL, 0)} 条 —— 先看完再进抽取（铁律二）")
    print(pipeline.format_status(pipeline.status(cfg)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
