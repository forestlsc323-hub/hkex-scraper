"""阶段一 第二步：按日期范围抓公告列表，存 raw 表。

用法：
    python run_listing.py

中断了直接重跑，已完成的时间块会自动跳过。
"""

import logging

from hkexdb import config, listing, logsetup


def main() -> int:
    cfg = config.load("config.yaml")
    cfg.ensure_dirs()
    log_file = logsetup.setup(cfg.log_dir, cfg.log_level, run_name="listing")
    log = logging.getLogger("run_listing")
    log.info("日志写到 %s", log_file)

    csv_path, count = listing.run(cfg)
    print(f"\nraw 表：{csv_path}（{count} 行）")
    print("这是未清洗的原始列表。分类与抽取是阶段二、三的事。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
