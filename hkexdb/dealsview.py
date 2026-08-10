"""要约明细表的数据层。

界面（app.py）只负责摆控件，取数、筛选、排序、拼出处全在这里 ——
这样这一层能离线测试，而 tkinter 在测试环境里根本装不上。

列选得有讲究。做港股 precedent transactions 时，一张可比表上真正
会被引用的是这几样：

    谁买谁（要约方 / 受要约方）· 谁做的 FA · 什么类型（MGO/VGO/PO）
    · 每股多少钱 · 相对什么口径溢价多少 · 这单多大 · 买完想不想退市

「相对什么口径」和溢价率数字同样重要 —— 一个 -15.45% 不写明是
「未受干扰日前 30 日均价」，放进可比表就是污染，因为隔壁那单可能
用的是最后交易日收市价。所以口径永远和数字同列显示，不许拆开。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

# (字段名, 表头, 宽度, 对齐)  —— 表头与 runner.DEAL_COLUMNS 保持一致
COLUMNS = [
    ("判定", "判定", 56, "center"),
    ("公告日期", "公告日期", 88, "center"),
    ("股票代码", "代码", 58, "center"),
    ("受要约方", "受要约方", 150, "w"),
    ("要约方", "要约方", 190, "w"),
    ("要约类型", "类型", 52, "center"),
    ("对价形式", "对价", 60, "center"),
    ("要约价(HKD)", "要约价", 74, "e"),
    ("主值溢价率(%)", "溢价率%", 74, "e"),
    ("主值口径", "溢价口径", 140, "w"),
    ("交易规模(HKD)", "交易规模", 120, "e"),
    ("要约方财务顾问", "要约方FA", 150, "w"),
    ("置信度", "置信度", 56, "center"),
]

# 数值列：排序要按数字，不能按字符串（否则 9 会排在 1,905,849,908 后面）
_NUMERIC = {"要约价(HKD)", "主值溢价率(%)", "交易规模(HKD)"}

_CONFIDENCE_LABEL = {"high": "高", "medium": "中", "low": "低"}


def _num(value: str) -> float | None:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def thousands(value: str) -> str:
    """只加千分位，数字本身一个字符都不动（这是显示，不是计算）。"""
    n = _num(value)
    if n is None or not str(value).strip():
        return str(value or "")
    whole, _, frac = str(value).strip().partition(".")
    try:
        whole = f"{int(whole):,}"
    except ValueError:
        return str(value)
    return f"{whole}.{frac}" if frac else whole


def display(row: dict, field: str) -> str:
    """一个单元格显示成什么样。"""
    raw = str(row.get(field, "") or "")
    if field == "交易规模(HKD)":
        return thousands(raw)
    if field == "置信度":
        return _CONFIDENCE_LABEL.get(raw, raw)
    return raw


# ---------------------------------------------------------------- 取数

def rows_from_deals(deals) -> list[dict]:
    """内存里的 Deal → 行字典。和 CSV 读出来的结构必须完全一致，
    否则界面会因为「刚跑完」和「重启后」两条路径而出现两套行为。"""
    from . import runner
    return [dict(zip(runner.DEAL_COLUMNS, runner._deal_row(d))) for d in deals]


def load_rows(path: str | Path) -> list[dict]:
    """从 data/deals.csv 读回来 —— 不重跑也能翻看上次的结果。"""
    path = Path(path)
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def load_evidence(path: str | Path) -> dict:
    """出处存在 CSV 旁边的 JSON 里，按 PDF 链接索引。

    铁律三要的是「这个数字是从哪一页哪句话来的」，重启一次就丢了
    等于没有。CSV 塞不下整段引文，所以单独存。
    """
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def save_evidence(deals, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {d.pdf_url: d.evidence for d in deals if d.pdf_url and d.evidence}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                    encoding="utf-8")


# ---------------------------------------------------------------- 筛选与排序

# 类型下拉的选项。「只看要约」放第一位 —— 打开程序最常做的就是这件事。
TYPE_CHOICES = ["只看要约", "全部", "MGO", "VGO", "PO"]


def filter_rows(rows: list[dict], query: str, offer_type: str = "") -> list[dict]:
    """搜索框对整行做子串匹配；类型下拉单独筛。

    「只看要约」筛的是正文层判定，不是要约类型 —— 类型没抽出来的
    真要约也要留在里面，否则就成了拿抽取失败去掩盖数据。
    """
    out = rows
    if offer_type == "只看要约":
        # 判定为空 = 这份 deals.csv 是旧版本跑的，没有这一列。
        # 那种情况下**全部显示** —— 打开程序看到一张空表，
        # 用户只会以为程序坏了，而数据其实好好地躺在文件里。
        out = [r for r in out if r.get("判定") in ("要约", "", None)]
    elif offer_type and offer_type != "全部":
        out = [r for r in out if r.get("要约类型", "") == offer_type]
    q = (query or "").strip().lower()
    if q:
        out = [r for r in out
               if any(q in str(v).lower() for v in r.values())]
    return out


def sort_rows(rows: list[dict], field: str, reverse: bool = False) -> list[dict]:
    """数值列按数字排，其余按字符串。空值永远排在最后 ——
    抽不到的那几单不该因为排序跑到表头去误导人。"""
    if field in _NUMERIC:
        def key(r):
            n = _num(r.get(field, ""))
            return (n is None, -(n or 0) if reverse else (n or 0))
    else:
        def key(r):
            v = str(r.get(field, "") or "")
            return (not v, v)
    ordered = sorted(rows, key=key)
    if reverse and field not in _NUMERIC:
        ordered.reverse()
    return ordered


# ---------------------------------------------------------------- 明细

# 明细面板的分组，顺序即阅读顺序
_DETAIL_GROUPS = [
    ("判定", ["判定", "判定理由"]),
    ("当事方", ["公告日期", "股票代码", "受要约方", "受要约方全称",
                "要约方", "要约方财务顾问"]),
    ("交易条款", ["要约类型", "对价形式", "要约价(HKD)", "交易规模(HKD)",
                  "上市地位意向", "停牌前最后交易日"]),
    ("溢价／折让（公告原文口径）", None),      # None = 展开整条梯子
    ("复核", ["置信度", "复算校验", "备注"]),
]


def detail_text(row: dict, evidence: dict | None = None) -> str:
    """明细面板的文字。

    溢价梯子整条列出来，不只给主值：可比表的下一个使用者可能要按
    「最后交易日收市价」重排，主值只是我们按规则选的那一条。
    """
    if not row:
        return "（在上面的表里点一行，这里显示这一单的全部字段和原文出处）"

    lines = [f"{row.get('公告标题', '')}".strip(), ""]
    for group, fields in _DETAIL_GROUPS:
        lines.append(f"── {group} " + "─" * max(0, 46 - len(group)))
        if fields is None:
            # 列名形如「较未受干扰日前30日均价(%)」：去掉头一个「较」和尾巴「(%)」
            ladder = [(k[1:-len("(%)")], v) for k, v in row.items()
                      if k.startswith("较") and k.endswith("(%)") and str(v).strip()]
            if not ladder:
                lines.append("  （公告里没找到「價值比較」一节）")
            for label, value in ladder:
                star = "  ←主值" if label == row.get("主值口径", "") else ""
                lines.append(f"  较{label:<18}{value:>8} %{star}")
            other = str(row.get("其他比较项", "") or "").strip()
            if other:
                lines.append(f"  其他口径：{other}")
        else:
            for field in fields:
                value = display(row, field)
                lines.append(f"  {field:<14}{value if value else '—（未抽到）'}")
        lines.append("")

    quotes = (evidence or {}).get(row.get("PDF链接", ""), {})
    if quotes:
        lines.append("── 原文出处（铁律三：有值必有出处）" + "─" * 12)
        for field, pair in quotes.items():
            page, quote = (pair + [0, ""])[:2] if isinstance(pair, list) else (0, "")
            if quote:
                # 页码 0 = 这个字段是从标题剪出来的，不在正文里
                where = f"第 {page} 页" if page else "公告标题"
                lines.append(f"  【{field}】{where}")
                lines.append(f"    {quote}")
        lines.append("")

    lines.append(f"PDF：{row.get('PDF链接', '')}")
    return "\n".join(lines)


def summary(rows: list[dict]) -> str:
    """表头上的一行汇总。

    先说「几单是真要约」，再说别的。原来只报「共 15 单」而其中 13 单
    是普通停复牌公告，表看着就像坏了。
    """
    if not rows:
        return "没有要约记录。先在「抓取」页跑一次，或确认 data/deals.csv 存在。"

    offers = [r for r in rows if r.get("判定") == "要约"]
    other = len(rows) - len(offers)
    kinds: dict[str, int] = {}
    for r in offers:
        key = r.get("要约类型") or "类型未识别"
        kinds[key] = kinds.get(key, 0) + 1
    parts = "　".join(f"{k} {v}" for k, v in sorted(kinds.items())) or "无"
    tail = f"　·　另有 {other} 条标题像要约、正文不是（判定列已标出）" if other else ""
    return f"{len(offers)} 单要约　·　{parts}{tail}"
