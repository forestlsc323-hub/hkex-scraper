"""从公告 PDF 链接直接读，定位关键段落。

用法（链接进，结果出，没有手工下载这一步）：

    python run_pdf.py https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0615/2026061500123.pdf

链接从 data/raw/listing_raw.csv 的 FILE_LINK 列拿（run_listing.py 抓的）。
可以一次给多个链接。
"""

import logging
import sys

from hkexdb import config, console, listing, logsetup
from hkexdb.pdf_source import full_url, open_pdf

# 要在公告里定位的锚点。命中即打印页码和原文行 —— 铁律三要的 page 出处。
ANCHORS = [
    "價值比較", "要約價的價值比較",
    "溢價", "折讓",
    "要約價", "每股要約股份",
    "最高現金代價", "現金代價總額", "總代價",
    "財務資源",
    "最後交易日", "未受干擾日",
]


def main(argv: list[str]) -> int:
    console.init()
    if not argv:
        print(__doc__)
        return 2

    cfg = config.load("config.yaml")
    cfg.ensure_dirs()
    logsetup.setup(cfg.log_dir, cfg.log_level, run_name="pdf")
    log = logging.getLogger("run_pdf")

    pdf_cfg = cfg.raw.get("pdf", {})
    from pathlib import Path
    cache_dir = Path(pdf_cfg.get("cache_dir", "data/cache/pdf"))

    # 与检索共用一个会话：PDF 路径可能依赖检索页种下的 cookie
    session = listing.make_session(cfg)
    listing.warm_up_session(session)

    for raw_link in argv:
        url = full_url(raw_link)     # 直接粘 FILE_LINK 相对路径也认
        print("=" * 78)
        print(url)
        print("=" * 78)
        try:
            doc = open_pdf(url, cache_dir, session=session.session,
                           user_agent=cfg.user_agent,
                           max_pages=int(pdf_cfg.get("max_pages", 0)))
        except Exception as exc:
            log.error("打不开：%s", exc)
            continue

        print(f"共 {doc.page_count} 页 | 文本层：{'有' if doc.has_text_layer else '无（需 OCR）'}"
              f" | {'本地副本' if doc.from_cache else '刚从网上取回'}")
        if not doc.has_text_layer:
            print(f"{console.WARN} 扫描件，本工具取不到文本，须走 OCR 并强制人工复核。")
            continue

        for anchor in ANCHORS:
            hits = doc.find(anchor)
            if not hits:
                continue
            print(f"\n--- 「{anchor}」命中 {len(hits)} 处 ---")
            for page_no, line in hits[:6]:
                print(f"  p{page_no:02d}| {line[:96]}")
            if len(hits) > 6:
                print(f"  …… 其余 {len(hits) - 6} 处")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
