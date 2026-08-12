"""一单为什么没出来 —— 沿着流水线往回查。

准确率报告能告诉你「09666 漏了」，但漏在哪一层它不说。而这四层
里，每一层漏掉的含义完全不同、修法也完全不同：

    索引层漏  → 关键词没搜到这条公告（搜法的问题，最严重）
    筛查层漏  → 搜到了，但标题被判成「非要约」灰掉了（规则的问题）
    抽取层漏  → 留存了，但 PDF 没打开／没抽出要约字段（解析的问题）
    合并层漏  → 抽到了，但被当成镜像/同单重复合并进另一行了（不是漏）

没有这个工具时，我只能猜，而猜错的代价是去修一个不存在的问题 ——
准确率那次「13 单漏检」里有 8 单其实抽到了，就是这么浪费掉两轮的。

三条铁律里的第二条（分类层的错误是静默污染）要求每一层都留痕，
而这个程序确实每一层都写了盘：

    data/store/listing.csv       索引存档（跨次累积，不删）
    data/screening/screened.csv  筛查结果（含被灰掉的，含命中的规则）
    data/store/deals.csv         成品存档

所以答案本来就在硬盘上，缺的只是一个把三张表串起来问话的入口。
这个模块不重跑、不联网、不改任何数据，纯读盘。

命令行：
    python -m hkexdb.trace 09666 2025-04-28
"""

from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

from .scoring import codes_in, normalise_code

# 你答案表记的是 T0，程序留存的可能是几天后的后续公告 —— 和配对时
# 用的是同一个窗口，否则「追踪说找到了、评分说漏了」会自相矛盾。
NEAR_DAYS = 45

INDEX = "index"
SCREEN = "screen"
EXTRACT = "extract"

_BUCKET_LABEL = {
    "retained": "留存（进抽取）", "manual": "人工复核",
    "excluded": "已灰（后续/程序公告）", "special": "特殊品种",
    "superseded": "被取代", "irrelevant": "题材无关",
}


def _date(text: str):
    try:
        return dt.date.fromisoformat(str(text or "").strip()[:10])
    except ValueError:
        return None


def _near(row_date: str, want: dt.date | None, near_days: int) -> bool:
    if want is None:
        return True
    got = _date(row_date)
    if got is None:
        return False
    return abs((got - want).days) <= near_days


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


@dataclass
class Hit:
    """某一层里命中的一条记录。"""
    stage: str
    date: str
    title: str
    detail: str = ""

    def line(self) -> str:
        head = f"{self.date}　{self.title[:60]}"
        return f"{head}\n      {self.detail}" if self.detail else head


@dataclass
class Trace:
    code: str
    date: str
    index: list[Hit] = field(default_factory=list)
    screened: list[Hit] = field(default_factory=list)
    deals: list[Hit] = field(default_factory=list)
    verdict: str = ""
    layer: str = ""

    def text(self) -> str:
        out = [f"{self.code}　{self.date or '（不限日期）'}　→　{self.verdict}"]
        for label, hits in (("索引", self.index), ("筛查", self.screened),
                            ("成品", self.deals)):
            if not hits:
                out.append(f"    {label}：无")
                continue
            for hit in hits:
                out.append(f"    {label}：{hit.line()}")
        return "\n".join(out)


class Snapshot:
    """三张盘上的表，读一次给多次追踪用（答案表有 80 多单）。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.listing = self._listing()
        self.screening = _read_csv(self.root / "data" / "screening" / "screened.csv")
        self.deals = self._deals()

    def _listing(self) -> list[dict]:
        from . import store
        archived = store.load_listing(self.root)
        if archived:
            return list(archived.values())
        # 存档还没建起来时退回本次跑的原始索引
        return _read_csv(self.root / "data" / "raw" / "vendor_listing.csv")

    def _deals(self) -> list[dict]:
        from . import store
        archived = store.load_deals(self.root)
        if archived:
            return list(archived.values())
        return _read_csv(self.root / "data" / "deals.csv")

    @property
    def ready(self) -> bool:
        """跑过没有。看**文件在不在**，不是看有没有行 ——
        跑完一轮而某一层一条都没有，本身就是个结论，不能说成「还没跑」。"""
        return any(p.exists() for p in (
            self.root / "data" / "store" / "listing.csv",
            self.root / "data" / "raw" / "vendor_listing.csv",
            self.root / "data" / "screening" / "screened.csv",
            self.root / "data" / "store" / "deals.csv",
            self.root / "data" / "deals.csv"))


def trace(code: str, date: str = "", *, root: Path | None = None,
          snapshot: Snapshot | None = None, near_days: int = NEAR_DAYS) -> Trace:
    """这一单走到哪一层没了。"""
    from .runner import ROOT

    snap = snapshot or Snapshot(root or ROOT)
    want_code = normalise_code(code)
    want_date = _date(date)
    result = Trace(code=want_code, date=str(date or "").strip()[:10])

    from . import store
    for row in snap.listing:
        if normalise_code(row.get("STOCK_CODE", "")) != want_code:
            continue
        iso = row.get("DATE_ISO") or store.row_date(row)
        if not _near(iso, want_date, near_days):
            continue
        result.index.append(Hit(INDEX, iso, row.get("TITLE", "")))

    for row in snap.screening:
        if normalise_code(row.get("code", "")) != want_code:
            continue
        if not _near(row.get("date", ""), want_date, near_days):
            continue
        bucket = row.get("bucket", "")
        detail = _BUCKET_LABEL.get(bucket, bucket)
        rules = row.get("matched_exclude") or row.get("matched_retain") or ""
        if rules:
            detail += f"｜命中：{rules}"
        result.screened.append(Hit(SCREEN, row.get("date", ""),
                                   row.get("title", ""), detail))

    for row in snap.deals:
        if want_code not in codes_in(row.get("股票代码", "")):
            continue
        if not _near(row.get("公告日期", ""), want_date, near_days):
            continue
        detail = row.get("判定", "")
        for extra in (row.get("要约类型", ""), row.get("备注", "")):
            if str(extra).strip():
                detail += f"｜{str(extra).strip()[:80]}"
        result.deals.append(Hit(EXTRACT, row.get("公告日期", ""),
                                row.get("受要约方", "") or row.get("公告标题", ""),
                                detail))

    result.layer, result.verdict = _verdict(result, snap)
    return result


def _verdict(result: Trace, snap: Snapshot) -> tuple[str, str]:
    """判断卡在哪一层。顺序是从后往前 —— 越靠后的证据越有力。"""
    if result.deals:
        kept = [h for h in result.deals if h.detail.startswith("要约")]
        if kept:
            return "", "已出成品（不算漏检；多半是日期对不上或字段配对没配上）"
        labels = "、".join(sorted({h.detail.split("｜")[0] for h in result.deals}))
        return EXTRACT, f"抽到了但判定为「{labels}」——（合并/灰掉，不是没抓到）"

    if result.screened:
        buckets = {h.detail.split("｜")[0] for h in result.screened}
        if "留存（进抽取）" in buckets:
            return EXTRACT, "筛查留存了，但没出成品 —— 卡在抽取层（PDF 没开成／没认出要约字段）"
        return SCREEN, f"被筛查层挡下：{'、'.join(sorted(buckets))} —— 规则的问题"

    if result.index:
        return SCREEN, "索引里有这条，但筛查表里没有 —— 不在本次跑的日期范围内"

    if not snap.ready:
        return "", "盘上还没有数据 —— 先跑一次抓取"
    return INDEX, "索引里就没有这条公告 —— 关键词没搜到它（最严重的一层）"


def missing_report(got_rows: list[dict], answer_rows: list[dict], *,
                   root: Path | None = None, near_days: int = NEAR_DAYS) -> list[str]:
    """答案表里没配上的那些单，逐单说明漏在哪一层。

    这段直接接在准确率报告后面 —— 「漏了 4 单」和「这 4 单分别漏在
    索引层/筛查层」是两种不同的信息，后者才指得动下一步干什么。
    """
    from .runner import ROOT
    from .scoring import KEY_COLUMNS, pair_up

    _pairs, misses, _extra = pair_up(got_rows, answer_rows, near_days=near_days)
    misses = [r for r in misses
              if any(str(v).strip() for k, v in r.items() if k not in KEY_COLUMNS)]
    if not misses:
        return []

    snap = Snapshot(root or ROOT)
    lines = ["", "── 漏检追踪：这些单卡在哪一层 ──"]
    tally: dict[str, int] = {}
    for want in misses:
        result = trace(want.get("股票代码", ""), want.get("公告日期", ""),
                       snapshot=snap, near_days=near_days)
        tally[result.layer or "other"] = tally.get(result.layer or "other", 0) + 1
        lines.append(result.text())
    lines.append("")
    label = {INDEX: "索引层（没搜到）", SCREEN: "筛查层（规则灰掉）",
             EXTRACT: "抽取层（没抽出字段）", "other": "其他"}
    lines.append("合计：" + "，".join(f"{label.get(k, k)} {v} 单"
                                     for k, v in sorted(tally.items())))
    return lines


def main(argv: list[str] | None = None) -> int:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("用法：python -m hkexdb.trace <股票代码> [公告日期]")
        return 2
    print(trace(args[0], args[1] if len(args) > 1 else "").text())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
