"""阶段一 第一步：勘察数据源。

用法：
    python run_probe.py

跑完看 data/probe/PROBE_REPORT.md。
这一步只发几个请求，很轻，可以放心重跑（有缓存）。
"""

import logging

from hkexdb import config, logsetup, probe
from hkexdb.http_client import PoliteSession


def main() -> int:
    cfg = config.load("config.yaml")
    cfg.ensure_dirs()
    log_file = logsetup.setup(cfg.log_dir, cfg.log_level, run_name="probe")
    log = logging.getLogger("run_probe")
    log.info("日志写到 %s", log_file)

    session = PoliteSession(
        user_agent=cfg.user_agent,
        cache_dir=cfg.cache_dir,
        min_interval_seconds=cfg.min_interval_seconds,
        timeout_seconds=cfg.timeout_seconds,
        max_retries=cfg.max_retries,
        backoff_base_seconds=cfg.backoff_base_seconds,
        cache_enabled=cfg.cache_enabled,
    )

    # 勘察只用一小段日期试探接口，不需要跑全范围
    report = probe.run(
        session, cfg.probe_dir,
        date_from=cfg.date_from.strftime("%Y%m%d"),
        date_to=min(cfg.date_to, cfg.date_from.replace(day=28)).strftime("%Y%m%d"),
    )
    print(f"\n勘察报告：{report}\n请先读它，再决定 config.yaml 怎么配。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
