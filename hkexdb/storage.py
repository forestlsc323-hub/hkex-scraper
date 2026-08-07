"""raw 层存储：把检索接口返回的列表**原样**落盘。

这一层刻意不做任何清洗——不去空格、不转类型、不去重、不改字段名。
理由：raw 是所有下游的地基，一旦在这里"顺手清理"，
后面发现清理逻辑错了就再也回不去了。清洗放到后面的阶段做。

三个文件各司其职：
  listing_rows.jsonl  追加写。每行一条记录 + 出处信息。断点续跑靠它。
  checkpoint.json     已完成的 (查询, 时间块) 清单。中断后重跑跳过已完成的。
  listing_raw.csv     从 jsonl 重新生成，排序固定 → 同输入同输出（幂等）。
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# 出处字段一律以 _ 开头，和公告本身的字段区分开
PROVENANCE = [
    "_row_uid", "_query_name", "_lang", "_chunk_from", "_chunk_to",
    "_page_no", "_fetched_at", "_source_url",
]

# 这几个字段放 CSV 最前面，方便你用 Excel 打开就能看
PREFERRED_ORDER = [
    "NEWS_ID", "DATE_TIME", "STOCK_CODE", "STOCK_NAME", "TITLE",
    "LONG_TEXT", "FILE_LINK", "FILE_INFO", "FILE_TYPE",
]


def row_uid(query_name: str, chunk_from: str, page_no: int,
            position: int, file_link: str) -> str:
    """一条记录的稳定身份。

    重跑同一段时间必然产生同样的 uid，所以重复追加的行能在生成 CSV 时去掉。
    注意：这是"同一次抓取里的同一行"，不是"同一宗交易"——
    中英双语两版是两条不同的记录，raw 层不合并（附录 C 第 8 项）。
    """
    payload = f"{query_name}|{chunk_from}|{page_no}|{position}|{file_link}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


class RawStore:
    def __init__(self, raw_dir: Path):
        self.raw_dir = Path(raw_dir)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.rows_path = self.raw_dir / "listing_rows.jsonl"
        self.checkpoint_path = self.raw_dir / "checkpoint.json"
        self.csv_path = self.raw_dir / "listing_raw.csv"
        self.pages_dir = self.raw_dir / "pages"

    # ---------- 断点续跑 ----------

    def _load_checkpoint(self) -> set[str]:
        if not self.checkpoint_path.exists():
            return set()
        try:
            return set(json.loads(self.checkpoint_path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("checkpoint 读取失败，当作全新开始：%s", exc)
            return set()

    def is_done(self, unit: str) -> bool:
        return unit in self._load_checkpoint()

    def mark_done(self, unit: str) -> None:
        done = self._load_checkpoint()
        done.add(unit)
        self.checkpoint_path.write_text(
            json.dumps(sorted(done), ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------- 写入 ----------

    def save_page_json(self, unit: str, page_no: int, text: str) -> None:
        """接口返回的原始 JSON 也留一份，出问题时可以回到最源头核对。"""
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        safe = unit.replace("|", "_").replace("/", "_")
        (self.pages_dir / f"{safe}_p{page_no:03d}.json").write_text(
            text, encoding="utf-8")

    def append_rows(self, rows: list[dict]) -> None:
        with self.rows_path.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    # ---------- 生成 CSV ----------

    def rebuild_csv(self) -> tuple[Path, int]:
        """从 jsonl 重新生成 CSV。去掉重复 uid，排序固定，因此结果幂等。"""
        if not self.rows_path.exists():
            log.warning("还没有任何抓取结果：%s 不存在", self.rows_path)
            return self.csv_path, 0

        by_uid: dict[str, dict] = {}
        with self.rows_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                by_uid[row["_row_uid"]] = row      # 后写的覆盖先写的

        rows = sorted(
            by_uid.values(),
            key=lambda r: (str(r.get("DATE_TIME", "")), str(r.get("STOCK_CODE", "")),
                           r["_row_uid"]),
        )

        # 表头 = 常用字段 + 其余公告字段（字母序）+ 出处字段
        seen: set[str] = set()
        for row in rows:
            seen.update(row.keys())
        extras = sorted(k for k in seen
                        if k not in PREFERRED_ORDER and k not in PROVENANCE)
        header = ([k for k in PREFERRED_ORDER if k in seen] + extras
                  + [k for k in PROVENANCE if k in seen])

        # utf-8-sig：Excel 打开中文才不乱码
        with self.csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

        log.info("raw 表已生成：%s（%d 行，%d 列）", self.csv_path, len(rows), len(header))
        return self.csv_path, len(rows)
