"""控制台输出的跨平台保护。

背景：Windows 中文版的控制台默认编码是 cp936（GBK），
中文没问题，但 ✅ ⚠️ ❌ ⛔ → 这些符号**编不出来**，
Python 会直接抛 UnicodeEncodeError 把脚本打死 ——
脚本本身跑得好好的，只是最后一行打印把它干掉了。

所以两件事：
1. `init()` 把 stdout/stderr 设成遇到编不出的字符就替换，绝不抛异常。
2. 面向控制台的提示一律用 ASCII 标记（下面这几个常量），
   emoji 只留给写进 .md / .csv 的内容 —— 那些文件都显式用 utf-8 写，
   什么符号都装得下。
"""

from __future__ import annotations

import sys

# 控制台专用标记。别在这里放 emoji。
OK = "[OK]"
WARN = "[!]"
FAIL = "[X]"
ARROW = "->"


def init() -> None:
    """让控制台输出永不因编码问题崩溃。入口脚本第一件事就调它。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:      # 被重定向成不支持的对象时跳过
            continue
        try:
            # 保持原编码（控制台该显示什么还显示什么），
            # 只把编不出的字符换成 ? ，不让它抛异常
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass
