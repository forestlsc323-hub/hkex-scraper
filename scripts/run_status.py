"""看流水线跑到哪一步了，下一步该干什么。

用法：
    python run_status.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import sys

from hkexdb import config, console, pipeline


def main() -> int:
    console.init()
    cfg = config.load("config.yaml")
    steps = pipeline.status(cfg)
    print(f"\n工作目录：{cfg.config_path.parent}")
    print(f"日期范围：{cfg.date_from} ~ {cfg.date_to}")
    print(pipeline.format_status(steps))
    print()
    for step in steps:
        print(f"  {step.n}. {step.name:<10} {step.command:<34} → {step.produces}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
