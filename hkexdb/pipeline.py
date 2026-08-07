"""流水线状态：每一步知道自己的前置做完没有，并指出下一步。

对应 GUI 上那五个按钮。做这个的理由很实际：
各步之间有严格的先后依赖，前一步没做完就跑下一步，
拿到的往往不是报错而是「0 条」或「空报告」—— 又是静默失败。
把依赖显式化，让每一步在开跑前就能说清楚缺什么。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import Config


@dataclass
class Step:
    n: int
    key: str
    name: str
    command: str
    produces: str          # 产物的相对描述
    why: str               # 这一步为什么不能跳
    done: bool
    detail: str


def _count_csv_rows(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open(encoding="utf-8-sig") as fh:
        return max(0, sum(1 for _ in fh) - 1)


def status(cfg: Config) -> list[Step]:
    probe = cfg.probe_dir / "PROBE_REPORT.md"
    raw_csv = cfg.raw_dir / "listing_raw.csv"
    checkpoint = cfg.raw_dir / "checkpoint.json"
    screened = cfg.data_dir / "screening" / "screened.csv"
    report = cfg.data_dir / "screening" / "report.html"

    # 勘察报告存在但顶部有「本次勘察无效」横幅的，不算做完
    probe_ok = False
    probe_detail = "未运行"
    if probe.exists():
        head = probe.read_text(encoding="utf-8")[:600]
        if "本次勘察无效" in head:
            probe_ok, probe_detail = False, "跑过但一个请求都没发出去，结论无效"
        else:
            probe_ok, probe_detail = True, f"已生成 {probe}"

    days_done = 0
    if checkpoint.exists():
        import json
        try:
            days_done = len(json.loads(checkpoint.read_text(encoding="utf-8")))
        except Exception:
            days_done = 0
    total_days = (cfg.date_to - cfg.date_from).days + 1

    raw_rows = _count_csv_rows(raw_csv)
    screened_rows = _count_csv_rows(screened)

    return [
        Step(1, "probe", "勘察接口", "python run_probe.py",
             "data/probe/PROBE_REPORT.md",
             "确认接口地址与参数名。跳过它，第 2 步可能抓到 0 条却不报错。",
             probe_ok, probe_detail),
        Step(2, "listing", "抓取列表", "python run_listing.py",
             "data/raw/listing_raw.csv",
             "按天抓全量公告。中断可直接重跑，已完成的天会跳过。",
             days_done >= total_days and raw_rows > 0,
             f"已完成 {days_done}/{total_days} 天，raw 表 {raw_rows} 行"
             if days_done else "未运行"),
        Step(3, "screening", "质控筛查", "python run_screening.py",
             "data/screening/screened.csv + QC_REPORT.md",
             "T0 标题层筛选。软删除，一行不少。跑完必须人工过一遍再进抽取（铁律二）。",
             screened_rows > 0,
             f"已筛 {screened_rows} 行" if screened_rows else "未运行"),
        Step(4, "report", "生成网页", "python run_report.py",
             "data/screening/report.html",
             "把筛查结果做成可筛选的网页，一屏过完人工复核桶和矛盾行。",
             report.exists(),
             f"已生成 {report}" if report.exists() else "未运行"),
        Step(5, "pdf", "读公告原文", "python run_pdf.py <FILE_LINK>",
             "按需，链接进文本出",
             "从 raw 表的 FILE_LINK 列取链接，直接读 PDF 定位段落。",
             False, "按需运行，不计完成状态"),
    ]


def format_status(steps: list[Step]) -> str:
    from .console import OK, WARN

    lines = ["", "流水线状态", "=" * 62]
    nxt = None
    for step in steps:
        mark = OK if step.done else WARN
        lines.append(f"{mark} {step.n}. {step.name:<10} {step.detail}")
        if not step.done and nxt is None and step.key != "pdf":
            nxt = step
    lines.append("=" * 62)
    if nxt:
        lines.append(f"下一步：{nxt.command}")
        lines.append(f"        {nxt.why}")
    else:
        lines.append("主流程已跑完。人工复核桶和矛盾行看过了吗？（铁律二）")
    return "\n".join(lines)
