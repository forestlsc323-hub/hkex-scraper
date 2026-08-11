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
import time
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
    from hkexdb.config import section

    rng = section("config.yaml", key="date_range", root=ROOT)
    today = dt.date.today()
    try:
        return str(rng["from"]), str(rng["to"])
    except KeyError:
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

    from hkexdb import dealsview

    root = tk.Tk()
    root.title("披露易要约公告抓取工具")
    root.geometry("1200x740")
    root.minsize(900, 560)

    msgs: queue.Queue = queue.Queue()
    cancel = threading.Event()
    state = {"running": False, "result": None, "activity": "",
             "frame": 0, "started": 0.0, "moved": 0.0,
             "progress": 0.0, "pulsing": False}

    tabs = ttk.Notebook(root)
    tabs.pack(fill="both", expand=True)
    page_run = ttk.Frame(tabs)
    page_deals = ttk.Frame(tabs)
    tabs.add(page_deals, text="  要约明细  ")
    tabs.add(page_run, text="  抓取  ")

    # ================================================================
    # 要约明细页 —— 抓出来的东西在这里看
    # ================================================================
    dstate = {"rows": [], "view": [], "evidence": {},
              "sort": "公告日期", "reverse": True}

    dtop = ttk.Frame(page_deals, padding=(12, 10, 12, 4))
    dtop.pack(fill="x")

    ttk.Label(dtop, text="搜索").pack(side="left")
    e_search = ttk.Entry(dtop, width=28)
    e_search.pack(side="left", padx=(6, 16))

    ttk.Label(dtop, text="类型").pack(side="left")
    cb_type = ttk.Combobox(dtop, width=10, state="readonly",
                           values=dealsview.TYPE_CHOICES)
    cb_type.current(0)
    cb_type.pack(side="left", padx=(6, 16))

    btn_reload = ttk.Button(dtop, text="重新载入")
    btn_reload.pack(side="left", padx=4)
    btn_csv = ttk.Button(dtop, text="打开 deals.csv")
    btn_csv.pack(side="left", padx=4)
    btn_score = ttk.Button(dtop, text="对答案（准确率）")
    btn_score.pack(side="left", padx=4)

    dsum = ttk.Label(page_deals, text="", foreground="#666", padding=(12, 0))
    dsum.pack(fill="x")

    dmid = ttk.Frame(page_deals, padding=(12, 6))
    dmid.pack(fill="both", expand=True)

    tree = ttk.Treeview(dmid, columns=[c[0] for c in dealsview.COLUMNS],
                        show="headings", height=14, selectmode="browse")
    for field, heading, width, anchor in dealsview.COLUMNS:
        tree.heading(field, text=heading,
                     command=lambda f=field: sort_by(f))
        tree.column(field, width=width, anchor=anchor, stretch=False)
    tsb = ttk.Scrollbar(dmid, command=tree.yview)
    tree.configure(yscrollcommand=tsb.set)
    tsb.pack(side="right", fill="y")
    tree.pack(fill="both", expand=True)

    # 置信度低的整行标黄 —— 这几单必须人看过才能用
    tree.tag_configure("low", background="#fff4d6")
    tree.tag_configure("notoffer", foreground="#9aa0a6")

    # 明细区：左边正文，右边一列动作按钮。
    # 按钮单独放一边、离表格远远的 —— 原来双击表格行就直接开浏览器，
    # 想看明细手一抖就弹出去了。现在双击表格什么也不做。
    dbot = ttk.Frame(page_deals, padding=(12, 0, 12, 12))
    dbot.pack(fill="both", expand=False)

    detail = tk.Text(dbot, height=15, wrap="word", state="disabled",
                     font=("Consolas" if sys.platform.startswith("win")
                           else "monospace", 9))
    detail.pack(side="left", fill="both", expand=True)

    actions = ttk.Frame(dbot, padding=(10, 0, 0, 0))
    actions.pack(side="right", fill="y")

    lbl_which = ttk.Label(actions, text="未选中", width=20, wraplength=150,
                          foreground="#666")
    lbl_which.pack(anchor="w", pady=(0, 6))
    btn_pdf = ttk.Button(actions, text="打开原文 PDF", state="disabled", width=18)
    btn_pdf.pack(pady=2)
    btn_copy = ttk.Button(actions, text="复制 PDF 链接", state="disabled", width=18)
    btn_copy.pack(pady=2)
    lbl_copied = ttk.Label(actions, text="", foreground="#1f7a4d")
    lbl_copied.pack(anchor="w", pady=(4, 0))

    def show_detail(text: str) -> None:
        detail.configure(state="normal")
        detail.delete("1.0", "end")
        detail.insert("1.0", text)
        detail.configure(state="disabled")

    def refresh_tree() -> None:
        rows = dealsview.filter_rows(dstate["rows"], e_search.get(),
                                     cb_type.get())
        rows = dealsview.sort_rows(rows, dstate["sort"], dstate["reverse"])
        dstate["view"] = rows
        tree.delete(*tree.get_children())
        for i, row in enumerate(rows):
            # 正文层判定不是要约的整行灰掉；是要约但置信度低的标黄
            if row.get("判定") != "要约":
                tags = ("notoffer",)
            else:
                tags = ("low",) if row.get("置信度") == "low" else ()
            tree.insert("", "end", iid=str(i), tags=tags,
                        values=[dealsview.display(row, f)
                                for f, *_ in dealsview.COLUMNS])
        dsum.configure(text=dealsview.summary(rows))
        _clear_selection()
        show_detail(dealsview.detail_text({}))

    def _clear_selection() -> None:
        lbl_which.configure(text="未选中")
        lbl_copied.configure(text="")
        btn_pdf.configure(state="disabled")
        btn_copy.configure(state="disabled")

    def sort_by(field: str) -> None:
        dstate["reverse"] = not dstate["reverse"] if dstate["sort"] == field else False
        dstate["sort"] = field
        refresh_tree()

    def load_deals() -> None:
        """优先读跨次运行的存档 —— 那是一个会长大的库，本次跑的只是其中一段。

        存档为空（第一次用）才退回单次产物 data/deals.csv。
        """
        from hkexdb import store
        archived = store.load_deals(ROOT)
        if archived:
            dstate["rows"] = list(archived.values())
            dstate["evidence"] = store.load_evidence(ROOT)
        else:
            dstate["rows"] = dealsview.load_rows(ROOT / "data" / "deals.csv")
            dstate["evidence"] = dealsview.load_evidence(
                ROOT / "data" / "deals_evidence.json")
        refresh_tree()

    def _selected() -> dict:
        sel = tree.selection()
        return dstate["view"][int(sel[0])] if sel else {}

    def on_pick(_event=None) -> None:
        row = _selected()
        if not row:
            return
        show_detail(dealsview.detail_text(row, dstate["evidence"]))
        lbl_which.configure(
            text=f"{row.get('股票代码', '')} {row.get('受要约方', '')}")
        lbl_copied.configure(text="")
        has_url = bool(row.get("PDF链接"))
        btn_pdf.configure(state="normal" if has_url else "disabled")
        btn_copy.configure(state="normal" if has_url else "disabled")

    def open_pdf() -> None:
        url = _selected().get("PDF链接", "")
        if url:
            import webbrowser
            webbrowser.open(url)

    def copy_link() -> None:
        url = _selected().get("PDF链接", "")
        if not url:
            return
        root.clipboard_clear()
        root.clipboard_append(url)
        lbl_copied.configure(text="已复制")

    tree.bind("<<TreeviewSelect>>", on_pick)
    e_search.bind("<KeyRelease>", lambda _e: refresh_tree())
    cb_type.bind("<<ComboboxSelected>>", lambda _e: refresh_tree())
    btn_reload.configure(command=load_deals)
    btn_pdf.configure(command=open_pdf)
    btn_copy.configure(command=copy_link)
    btn_csv.configure(command=lambda: _open_file(ROOT / "data" / "deals.csv"))

    def run_scoring() -> None:
        """拿抽出来的和人工答案表逐字段对。没有标准答案就量不出准确率。"""
        path = runner.score_against_answer_key()
        _open_file(Path(path))

    btn_score.configure(command=run_scoring)

    # ================================================================
    # 抓取页
    # ================================================================
    # ---------------- 顶部：日期 + 按钮 ----------------
    top = ttk.Frame(page_run, padding=12)
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

    force_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(top, text="忽略存档，重新抓取", variable=force_var).grid(
        row=0, column=6, padx=(14, 0))

    ttk.Label(top, text="格式 2026-06-01　·　第一次建议先抓一周试试",
              foreground="#888").grid(row=1, column=0, columnspan=6,
                                      sticky="w", pady=(6, 0))

    # ---------------- 中部：进度 + 日志 ----------------
    mid = ttk.Frame(page_run, padding=(12, 0))
    mid.pack(fill="both", expand=True)

    status = ttk.Label(mid, text="就绪")
    status.pack(anchor="w")

    # 一条进度条，两种状态：
    #   进度在动 → determinate，老老实实走完成比例
    #   进度卡住 → 自动切成 indeterminate 来回跑，说明「还活着，在等」
    # ttk 的进度条不能同时又走比例又有动画，所以用切模式实现，
    # 切换点是「进度连续 2 秒没动」。
    bar = ttk.Progressbar(mid, mode="determinate", maximum=100)
    bar.pack(fill="x", pady=(4, 2))

    activity = ttk.Label(mid, text="", foreground="#666")
    activity.pack(anchor="w", pady=(0, 6))

    logbox = tk.Text(mid, wrap="none", height=20,
                     font=("Consolas" if sys.platform.startswith("win")
                           else "monospace", 10))
    sb = ttk.Scrollbar(mid, command=logbox.yview)
    logbox.configure(yscrollcommand=sb.set, state="disabled")
    sb.pack(side="right", fill="y")
    logbox.pack(fill="both", expand=True)

    # ---------------- 底部：结果按钮 ----------------
    bottom = ttk.Frame(page_run, padding=12)
    bottom.pack(fill="x")
    btn_report = ttk.Button(bottom, text="打开结果网页", state="disabled")
    btn_report.pack(side="left", padx=(0, 8))
    btn_diag = ttk.Button(bottom, text="打开诊断文件（发给 Claude）", state="disabled")
    btn_diag.pack(side="left", padx=8)
    btn_folder = ttk.Button(bottom, text="打开文件夹")
    btn_folder.pack(side="left", padx=8)
    btn_check = ttk.Button(bottom, text="自检（对比两种抓法）")
    btn_check.pack(side="right", padx=8)
    btn_cat = ttk.Button(bottom, text="勘察类别码")
    btn_cat.pack(side="right", padx=4)
    btn_cache = ttk.Button(bottom, text="清理临时文件…")
    btn_cache.pack(side="right", padx=4)
    btn_update = ttk.Button(bottom, text="检查更新")
    btn_update.pack(side="right", padx=4)

    # ---------------- 消息泵 ----------------
    def append(text: str) -> None:
        logbox.configure(state="normal")
        logbox.insert("end", text + "\n")
        logbox.see("end")
        logbox.configure(state="disabled")

    # 转圈的那个小符号 —— 纯 ASCII，cp936 控制台也编得出来
    SPINNER = "|/-\\"

    STALL_SECONDS = 2.0        # 进度不动多久就切成动画

    def set_progress(value: float) -> None:
        """进度动了：切回真进度条，并记下这一刻。"""
        if state["pulsing"]:
            bar.stop()
            bar.configure(mode="determinate", maximum=100)
            state["pulsing"] = False
        bar["value"] = value
        state["progress"] = value
        state["moved"] = time.monotonic()

    def tick() -> None:
        """每 200 毫秒走一格：转圈、已运行多久、进度卡住就让进度条动起来。"""
        if state["running"]:
            state["frame"] += 1
            now = time.monotonic()
            secs = int(now - state["started"])
            mins, rest = divmod(secs, 60)
            elapsed = f"{mins} 分 {rest} 秒" if mins else f"{rest} 秒"
            spin = SPINNER[state["frame"] % len(SPINNER)]
            note = state["activity"]
            activity.configure(
                text=f"{spin}  已运行 {elapsed}" + (f"　·　{note}" if note else ""))

            # 进度条本身也得动起来 —— 否则最后一份卡在死链上重试的那
            # 一两分钟，界面看上去和死机一模一样。
            if not state["pulsing"] and now - state["moved"] > STALL_SECONDS:
                bar.configure(mode="indeterminate")
                bar.start(25)
                state["pulsing"] = True
        root.after(200, tick)

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
                set_progress(min(100, (index + frac) / len(runner.STEPS) * 100))
                status.configure(text=f"[{index + 1}/{len(runner.STEPS)}] "
                                      f"{runner.STEPS[index]}")
            elif kind == "activity":
                state["activity"] = payload
            elif kind == "done":
                finish(payload)
            elif kind == "update":
                show_update(payload)
            elif kind == "checked":
                state["running"] = False
                btn_run.configure(state="normal")
                btn_check.configure(state="normal")
                btn_stop.configure(state="disabled")
                status.configure(text="自检结束，看下面的日志")
        root.after(120, pump)

    def finish(result) -> None:
        state["running"] = False
        state["result"] = result
        bar.stop()
        bar.configure(mode="determinate", maximum=100)
        state["pulsing"] = False
        secs = int(time.monotonic() - state["started"])
        activity.configure(text=f"用时 {secs // 60} 分 {secs % 60} 秒")
        btn_run.configure(state="normal")
        btn_stop.configure(state="disabled")
        bar["value"] = 100 if result.ok else state["progress"]
        if result.error:
            status.configure(text=f"结束：{result.error}")
        else:
            # 用户要的是「几单要约」，公告条数只是过程量 —— 所以它排第一
            with_price = sum(1 for d in result.deals if d.offer_price)
            parts = [f"抓到要约 {len(result.deals)} 单（其中 {with_price} 单抽到要约价）",
                     f"公告 {result.fetched} 条"]
            names = {"retained": "留存", "manual": "待人工看", "excluded": "已灰",
                     "special": "特殊品种", "superseded": "被取代",
                     "irrelevant": "题材无关"}
            if result.buckets:
                parts.append("　".join(f"{names.get(k, k)} {v}"
                                       for k, v in result.buckets.items()))
            status.configure(text="完成　" + "　·　".join(parts))
        if result.report_path:
            btn_report.configure(state="normal")
        if result.diagnostic_path:
            btn_diag.configure(state="normal")

        # 跑完直接把人送到明细页 —— 那才是他要看的东西
        if result.deals:
            dstate["rows"] = dealsview.rows_from_deals(result.deals)
            dstate["evidence"] = {d.pdf_url: d.evidence for d in result.deals}
            refresh_tree()
            tabs.select(page_deals)

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
        state["started"] = time.monotonic()
        state["activity"] = ""
        btn_run.configure(state="disabled")
        btn_stop.configure(state="normal")
        btn_report.configure(state="disabled")
        btn_diag.configure(state="disabled")
        state["pulsing"] = False
        bar.stop()
        bar.configure(mode="determinate")
        bar["value"] = 0
        state["moved"] = time.monotonic()

        # ⚠️ tkinter 的变量只能在主线程读。原来 force_var.get() 写在
        # work() 里，那是工作线程 —— 轻则读到脏值，重则直接
        # RuntimeError: main thread is not in main loop。
        # 在这里（主线程）读成普通 bool，再传进去。
        force = bool(force_var.get())

        def work() -> None:
            result = runner.run(
                d1, d2,
                on_log=lambda t: msgs.put(("log", t)),
                on_step=lambda i, f: msgs.put(("step", (i, f))),
                cancel_event=cancel, force_refetch=force,
                on_activity=lambda t: msgs.put(("activity", t)))
            msgs.put(("done", result))

        threading.Thread(target=work, daemon=True).start()

    def start_self_check() -> None:
        """关键词模式快，但它对不对取决于服务端怎么理解 title 参数 ——
        那是离线验证不了的。所以给一个按钮：同一段日期两种抓法各跑一遍，
        把关键词模式漏掉的逐条列出来。只对一两周跑，全量那侧本来就慢。"""
        if state["running"]:
            return
        try:
            d1 = dt.date.fromisoformat(e_from.get().strip())
            d2 = dt.date.fromisoformat(e_to.get().strip())
        except ValueError:
            append("日期格式不对，应该像 2026-06-01")
            return
        if (d2 - d1).days > 30:
            append("自检请只选一两周 —— 全量那一侧很慢，一个月要跑很久。")
            return

        cancel.clear()
        state["running"] = True
        btn_run.configure(state="disabled")
        btn_check.configure(state="disabled")
        btn_stop.configure(state="normal")

        def work() -> None:
            try:
                path = runner.self_check(d1, d2,
                                         on_log=lambda t: msgs.put(("log", t)),
                                         cancel_event=cancel)
                msgs.put(("log", f"\n自检报告已保存：{path}"))
            except Exception as exc:
                msgs.put(("log", f"自检失败：{type(exc).__name__}: {exc}"))
            msgs.put(("checked", None))

        threading.Thread(target=work, daemon=True).start()

    def probe_categories() -> None:
        """把披露易自己的公告分类树读出来。拿到「收購及合併」那个码，
        服务端就能直接给要约公告，关键词模式带回的普通交易公告就没了。"""
        if state["running"]:
            return
        append("正在勘察分类码…")

        def work() -> None:
            try:
                path = runner.probe_categories(
                    on_log=lambda t: msgs.put(("log", t)))
                msgs.put(("log", f"报告：{path}"))
            except Exception as exc:
                msgs.put(("log", f"勘察失败：{type(exc).__name__}: {exc}"))

        threading.Thread(target=work, daemon=True).start()

    def manage_cache() -> None:
        """跑出来的临时文件占多少地方，要不要删。

        存档（data\\store）不在名单上 —— 那是这个工具的本体：公告列表、
        抽出来的字段、每个数字的出处引文都在里面，删了就要从头再抓一遍。
        这里能删的都是「删了只是要重跑一次」的东西。
        """
        from tkinter import messagebox
        rows = runner.disposable_report()
        if not rows:
            messagebox.showinfo("清理", "没有可清理的临时文件。")
            return

        total = sum(size for _, _, _, size, _ in rows)
        lines = [f"　{label}：{n} 个文件，{runner.human_size(size)}\n"
                 f"　　　{rel}　（删了：{cost}）"
                 for rel, label, n, size, cost in rows]
        if messagebox.askyesno(
                "清理临时文件",
                f"一共占 {runner.human_size(total)}：\n\n"
                + "\n\n".join(lines)
                + "\n\n存档（data\\store）不动 —— 公告列表、抽出来的字段、"
                  "\n出处引文都在里面，删了要从头再抓一遍。\n\n要现在全删吗？"):
            gone, freed = runner.clear_disposable([r[0] for r in rows])
            append(f"已删除 {gone} 个文件，腾出 {runner.human_size(freed)}")
            messagebox.showinfo(
                "清理", f"删了 {gone} 个文件，腾出 {runner.human_size(freed)}。")

    def check_update() -> None:
        """从 GitHub 拉最新版覆盖本文件夹。

        原来这是 一键更新.bat 干的，但那串「cmd 调 PowerShell 下载并
        覆盖文件」的动作和下载器木马一模一样，卡巴斯基把它当成
        PDM:Trojan.Win32.Generic.nblk 删掉了。所以改成程序自己更新自己。
        """
        from tkinter import messagebox
        from hkexdb import updater

        btn_update.configure(state="disabled", text="更新中…")
        append("正在从 GitHub 取最新版…")

        def work():
            def note_retry(attempt, total, exc):
                msgs.put(("log", f"      连接被重置（第 {attempt}/{total} 次），"
                                 f"稍后重试：{type(exc).__name__}"))

            result = updater.update(ROOT, on_retry=note_retry)
            msgs.put(("update", result))

        threading.Thread(target=work, daemon=True).start()

    def show_update(result) -> None:
        from tkinter import messagebox
        btn_update.configure(state="normal", text="检查更新")
        if not result.ok:
            append(f"更新失败：{result.error}")
            messagebox.showerror("检查更新", result.error)
            return
        if not result.written:
            append("已经是最新版。")
            messagebox.showinfo("检查更新", "已经是最新版，没有文件需要更新。")
            return
        append(f"已更新 {len(result.written)} 个文件：")
        for rel in result.written[:20]:
            append(f"      {rel}")
        messagebox.showinfo(
            "检查更新",
            f"更新了 {len(result.written)} 个文件。\n\n"
            "关掉这个窗口重新打开，新版才生效。\n"
            "（你的数据和存档没有被动过。）")

    btn_run.configure(command=start)
    btn_cat.configure(command=probe_categories)
    btn_cache.configure(command=manage_cache)
    btn_update.configure(command=check_update)
    btn_check.configure(command=start_self_check)
    btn_stop.configure(command=lambda: (cancel.set(), append("正在停止…")))
    btn_report.configure(
        command=lambda: _open_file(state["result"].report_path))
    btn_diag.configure(
        command=lambda: _open_file(state["result"].diagnostic_path))
    btn_folder.configure(command=lambda: _open_file(ROOT))

    append("准备就绪。改好日期后点「开始抓取」。")
    append("第一次跑建议只抓一周，确认能通再放大范围。")

    load_deals()                      # 上次的结果直接摆出来，不用重跑
    if not dstate["rows"]:
        tabs.select(page_run)         # 还没有数据，先去抓取页

    root.after(120, pump)
    root.after(200, tick)
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
    # Windows 上子进程是 spawn 出来的 —— 它会把这个文件重新 import 一遍。
    # 没有这一句，被 spawn 的子进程会再开一个界面窗口，然后它自己再 spawn，
    # 无限套娃直到内存耗尽。解析挪进子进程之后这句就是必需的。
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(_crash_guard())
