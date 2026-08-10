# -*- coding: utf-8 -*-
"""披露易抓取工具 —— 单窗口界面。

这一层刻意做得很薄：只画窗口、转发消息。真正的流程在 hkexdb/runner.py，
那边可以离线测试。界面里逻辑越少，出错的机会越少。

没有 tkinter 的环境会自动退回命令行模式。
"""

from __future__ import annotations

import datetime as dt
import queue
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def _open_file(path: Path) -> None:
    """用系统默认程序打开文件。三大平台都照顾到。"""
    if not path or not Path(path).exists():
        return
    path = str(path)
    if sys.platform.startswith("win"):
        import os
        os.startfile(path)                      # noqa: S606  Windows 专有
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def default_dates() -> tuple[str, str]:
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        rng = cfg["date_range"]
        return str(rng["from"]), str(rng["to"])
    except Exception:
        today = dt.date.today()
        return str(today - dt.timedelta(days=6)), str(today)


def run_console() -> int:
    """没有 tkinter 时的退路。"""
    from hkexdb import runner

    d1, d2 = default_dates()
    print("（未检测到 tkinter，改用命令行模式）\n")
    result = runner.run(dt.date.fromisoformat(d1), dt.date.fromisoformat(d2),
                        on_log=print)
    print(f"\n日志：{result.log_path}")
    print(f"发给 Claude：{result.diagnostic_path}")
    if result.report_path:
        print(f"结果网页：{result.report_path}")
    return 0 if result.ok else 1


def main() -> int:
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        return run_console()

    from hkexdb import runner

    root = tk.Tk()
    root.title("披露易要约公告抓取工具")
    root.geometry("880x600")
    root.minsize(720, 480)

    msgs: queue.Queue = queue.Queue()
    cancel = threading.Event()
    state = {"running": False, "result": None}

    # ---------------- 顶部：日期 + 按钮 ----------------
    top = ttk.Frame(root, padding=12)
    top.pack(fill="x")

    d1_default, d2_default = default_dates()
    ttk.Label(top, text="起始日期").grid(row=0, column=0, sticky="w")
    e_from = ttk.Entry(top, width=14)
    e_from.insert(0, d1_default)
    e_from.grid(row=0, column=1, padx=(6, 18))

    ttk.Label(top, text="结束日期").grid(row=0, column=2, sticky="w")
    e_to = ttk.Entry(top, width=14)
    e_to.insert(0, d2_default)
    e_to.grid(row=0, column=3, padx=(6, 18))

    btn_run = ttk.Button(top, text="开始抓取")
    btn_run.grid(row=0, column=4, padx=4)
    btn_stop = ttk.Button(top, text="停止", state="disabled")
    btn_stop.grid(row=0, column=5, padx=4)

    ttk.Label(top, text="格式 2026-06-01　·　第一次建议先抓一周试试",
              foreground="#888").grid(row=1, column=0, columnspan=6,
                                      sticky="w", pady=(6, 0))

    # ---------------- 中部：进度 + 日志 ----------------
    mid = ttk.Frame(root, padding=(12, 0))
    mid.pack(fill="both", expand=True)

    status = ttk.Label(mid, text="就绪")
    status.pack(anchor="w")
    bar = ttk.Progressbar(mid, mode="determinate", maximum=100)
    bar.pack(fill="x", pady=(4, 8))

    logbox = tk.Text(mid, wrap="none", height=20,
                     font=("Consolas" if sys.platform.startswith("win")
                           else "monospace", 10))
    sb = ttk.Scrollbar(mid, command=logbox.yview)
    logbox.configure(yscrollcommand=sb.set, state="disabled")
    sb.pack(side="right", fill="y")
    logbox.pack(fill="both", expand=True)

    # ---------------- 底部：结果按钮 ----------------
    bottom = ttk.Frame(root, padding=12)
    bottom.pack(fill="x")
    btn_report = ttk.Button(bottom, text="打开结果网页", state="disabled")
    btn_report.pack(side="left", padx=(0, 8))
    btn_diag = ttk.Button(bottom, text="打开诊断文件（发给 Claude）", state="disabled")
    btn_diag.pack(side="left", padx=8)
    btn_folder = ttk.Button(bottom, text="打开文件夹")
    btn_folder.pack(side="left", padx=8)

    # ---------------- 消息泵 ----------------
    def append(text: str) -> None:
        logbox.configure(state="normal")
        logbox.insert("end", text + "\n")
        logbox.see("end")
        logbox.configure(state="disabled")

    def pump() -> None:
        while True:
            try:
                kind, payload = msgs.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                append(payload)
            elif kind == "step":
                index, frac = payload
                bar["value"] = min(100, (index + frac) / len(runner.STEPS) * 100)
                status.configure(text=f"[{index + 1}/{len(runner.STEPS)}] "
                                      f"{runner.STEPS[index]}")
            elif kind == "done":
                finish(payload)
        root.after(120, pump)

    def finish(result) -> None:
        state["running"] = False
        state["result"] = result
        btn_run.configure(state="normal")
        btn_stop.configure(state="disabled")
        bar["value"] = 100 if result.ok else bar["value"]
        if result.error:
            status.configure(text=f"结束：{result.error}")
        else:
            parts = [f"抓到 {result.fetched} 条"]
            if result.buckets:
                parts.append("　".join(f"{k} {v}" for k, v in result.buckets.items()))
            status.configure(text="完成　" + "　·　".join(parts))
        if result.report_path:
            btn_report.configure(state="normal")
        if result.diagnostic_path:
            btn_diag.configure(state="normal")

    def start() -> None:
        if state["running"]:
            return
        try:
            d1 = dt.date.fromisoformat(e_from.get().strip())
            d2 = dt.date.fromisoformat(e_to.get().strip())
        except ValueError:
            append("日期格式不对，应该像 2026-06-01")
            return
        if d1 > d2:
            append("起始日期比结束日期还晚")
            return

        logbox.configure(state="normal")
        logbox.delete("1.0", "end")
        logbox.configure(state="disabled")
        cancel.clear()
        state["running"] = True
        btn_run.configure(state="disabled")
        btn_stop.configure(state="normal")
        btn_report.configure(state="disabled")
        btn_diag.configure(state="disabled")
        bar["value"] = 0

        def work() -> None:
            result = runner.run(
                d1, d2,
                on_log=lambda t: msgs.put(("log", t)),
                on_step=lambda i, f: msgs.put(("step", (i, f))),
                cancel_event=cancel)
            msgs.put(("done", result))

        threading.Thread(target=work, daemon=True).start()

    btn_run.configure(command=start)
    btn_stop.configure(command=lambda: (cancel.set(), append("正在停止…")))
    btn_report.configure(
        command=lambda: _open_file(state["result"].report_path))
    btn_diag.configure(
        command=lambda: _open_file(state["result"].diagnostic_path))
    btn_folder.configure(command=lambda: _open_file(ROOT))

    append("准备就绪。改好日期后点「开始抓取」。")
    append("第一次跑建议只抓一周，确认能通再放大范围。")
    root.after(120, pump)
    root.mainloop()
    return 0


def _crash_guard() -> int:
    """启动阶段的兜底。

    界面程序最怕「双击了、什么都没发生」—— 用户无从下手，我也拿不到线索。
    所以不管出什么错，都做三件事：写进 app_crash.txt、打到控制台、
    再尽量弹个窗。三条路总有一条能让人看见。
    """
    import traceback
    try:
        return main()
    except Exception:
        detail = traceback.format_exc()
        crash = ROOT / "app_crash.txt"
        try:
            crash.write_text(
                "程序启动失败。请把这个文件发给 Claude。\n\n"
                f"Python: {sys.version}\n"
                f"平台: {sys.platform}\n"
                f"目录: {ROOT}\n\n{detail}",
                encoding="utf-8")
        except Exception:
            pass

        print("\n程序启动失败：\n", file=sys.stderr)
        print(detail, file=sys.stderr)
        print(f"\n详情已写入 {crash}，把这个文件发给 Claude。", file=sys.stderr)

        try:
            import tkinter.messagebox as mb
            mb.showerror("启动失败",
                         f"程序启动失败，详情已写入：\n{crash}\n\n"
                         "请把这个文件发给 Claude。")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(_crash_guard())
