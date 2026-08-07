"""统一的日志配置。

工程要求里的"完整日志"从这里起步：屏幕上看进度，文件里留全量记录。
每次运行写一个带时间戳的独立日志文件，方便事后追查某一次抓取。
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-7s %(name)-22s %(message)s"


def setup(log_dir: Path, level: str = "INFO", run_name: str = "run") -> Path:
    """配置根 logger，返回本次运行的日志文件路径。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"{run_name}_{stamp}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # 重复调用 setup() 时先清掉旧 handler，避免日志打印多份
    for handler in list(root.handlers):
        root.removeHandler(handler)

    # 文件永远记 DEBUG，屏幕按 config.yaml 的 level
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(console)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return log_file
