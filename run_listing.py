"""阶段一 第二步：按日期范围抓公告列表，存 raw 表。

用法：
    python run_listing.py

中断了直接重跑，已完成的时间块会自动跳过。
"""

import logging
import sys

from hkexdb import config, console, listing, logsetup


def main() -> int:
    console.init()
    cfg = config.load("config.yaml")
    cfg.ensure_dirs()
    log_file = logsetup.setup(cfg.log_dir, cfg.log_level, run_name="listing")
    log = logging.getLogger("run_listing")
    log.info("日志写到 %s", log_file)

    try:
        csv_path, count = listing.run(cfg)
    except RuntimeError as exc:
        # 网络不通是最常见的失败，别甩一堆 traceback 给用户
        log.error("%s", exc)
        print("\n抓取失败：连不上披露易。", file=sys.stderr)
        print("已抓到的时间块都记在 checkpoint 里，网络恢复后重跑本命令即可续跑。",
              file=sys.stderr)
        print("\n常见原因：", file=sys.stderr)
        print("  1. 本机网络/代理不通 —— 先在浏览器打开 "
              "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=zh 确认", file=sys.stderr)
        print("  2. 处在有出网白名单的环境（公司网络、云端沙箱）—— "
              "换一台能直连的机器", file=sys.stderr)
        print("  3. 接口参数变了 —— 跑 python run_probe.py 重新勘察", file=sys.stderr)
        return 1

    print(f"\nraw 表：{csv_path}（{count} 行）")
    print("这是未清洗的原始列表。分类与抽取是阶段二、三的事。")
    print("下一步：python run_screening.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
