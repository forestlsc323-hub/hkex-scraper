"""Windows 上跑得起来 —— 这些坑全是真踩过的。

用户的机器是 Windows 10 + 中文环境（控制台 cp936），从
C:\\Users\\Judy Shi\\Downloads\\... 双击 .bat 启动。
这一路上每一个坑都在这里钉死，因为它们的共同点是：
出事时界面上什么都看不见。
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BATS = ["一键运行.bat", "一键更新.bat"]


# ---------------------------------------------------------------- 启动器

@pytest.mark.parametrize("name", BATS)
def test_bat_files_use_crlf(name):
    """LF 换行会让 Windows cmd 解析崩掉，双击后闪一下就关（真踩过）。"""
    raw = (ROOT / name).read_bytes()
    assert b"\r\n" in raw
    assert raw.count(b"\n") == raw.count(b"\r\n"), f"{name} 里有裸 LF"


def test_launcher_keeps_a_console_so_errors_are_visible():
    """pythonw.exe 没有控制台：界面若在启动阶段崩掉，用户什么都看不到。"""
    bat = (ROOT / "一键运行.bat").read_bytes().decode("cp936")
    live = [ln for ln in bat.splitlines()
            if ln.strip() and not ln.strip().upper().startswith("REM")]
    assert not any("pythonw" in ln for ln in live)
    assert any("python.exe app.py" in ln for ln in live)


# ---------------------------------------------------------------- 编码

def test_no_emoji_anywhere_that_reaches_a_cp936_console():
    """cp936 控制台打不出 emoji，直接抛 UnicodeEncodeError 把程序带崩。

    界面里的中文没问题（Tk 用 UTF-16），但 print 到控制台的必须是
    cp936 能编码的字符。
    """
    for path in [ROOT / "app.py", *(ROOT / "hkexdb").glob("*.py")]:
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "print(" not in line and "log(" not in line:
                continue
            try:
                line.encode("cp936")
            except UnicodeEncodeError as exc:
                bad = line[exc.start:exc.end]
                pytest.fail(f"{path.name} 有 cp936 编不出来的字符 {bad!r}：\n  {line.strip()}")


def test_crash_guard_writes_a_file_when_startup_fails(tmp_path, monkeypatch):
    """双击后「什么都没发生」是最难排查的失败 —— 必须留下痕迹。"""
    import app
    monkeypatch.setattr(app, "ROOT", tmp_path)
    monkeypatch.setattr(app, "main", lambda: (_ for _ in ()).throw(
        RuntimeError("启动就炸")))

    assert app._crash_guard() == 1
    crash = tmp_path / "app_crash.txt"
    assert crash.exists(), "崩溃了却没留下任何文件"
    text = crash.read_text(encoding="utf-8")
    assert "RuntimeError" in text and "启动就炸" in text


def test_crash_guard_passes_through_success(tmp_path, monkeypatch):
    import app
    monkeypatch.setattr(app, "main", lambda: 0)
    assert app._crash_guard() == 0


# ---------------------------------------------------------------- 工作目录

def test_rule_files_are_found_from_any_working_directory(tmp_path, monkeypatch):
    """.bat 双击时的工作目录未必是仓库根目录，配置照样要找得到。"""
    from hkexdb.config import resolve_file

    monkeypatch.chdir(tmp_path)
    assert resolve_file("screening_rules.yaml").exists()
    assert resolve_file("config.yaml").exists()


def test_screening_rules_load_from_any_working_directory(tmp_path, monkeypatch):
    from hkexdb import screening

    monkeypatch.chdir(tmp_path)
    rules = screening.load_rules("screening_rules.yaml")
    assert rules.version and rules.exclude_terms


# ---------------------------------------------------------------- 干净启动

def test_app_imports_without_tkinter_and_falls_back():
    """没有 tkinter 的机器上也要能 import，然后退回命令行。"""
    import importlib

    import app
    importlib.reload(app)
    assert callable(app.main) and callable(app.run_console)


def test_every_shipped_module_compiles():
    """删了一堆死代码之后，确认剩下的都还能编译。"""
    files = [ROOT / "app.py", *(ROOT / "hkexdb").glob("*.py")]
    out = subprocess.run([sys.executable, "-m", "py_compile", *map(str, files)],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


def test_no_module_imports_something_that_was_deleted():
    """死代码删干净了没有 —— 残留的 import 会在用户机器上炸。"""
    gone = {"hkexdb.listing", "hkexdb.http_client", "hkexdb.storage",
            "hkexdb.stocks", "hkexdb.probe", "hkexdb.pipeline",
            "hkexdb.console", "hkexdb.logsetup"}
    for path in [ROOT / "app.py", *(ROOT / "hkexdb").glob("*.py")]:
        text = path.read_text(encoding="utf-8")
        for dead in gone:
            mod = dead.split(".")[1]
            assert f"from .{mod} import" not in text, f"{path.name} 还在 import {mod}"
            assert f"from hkexdb import {mod}" not in text, f"{path.name} 还在 import {mod}"


# ---------------------------------------------------------------- 线程边界

def test_no_tkinter_variable_is_read_from_a_worker_thread():
    """tkinter 的变量只能在主线程读。

    原来 force_var.get() 写在 work() 里 —— 那是工作线程，
    轻则读到脏值，重则 RuntimeError: main thread is not in main loop
    直接把整个抓取线程干掉，而界面上只看到进度条不动。
    主线程先读成普通 bool，再传进工作线程。
    """
    text = (ROOT / "app.py").read_text(encoding="utf-8")
    work = text.split("def work() -> None:", 1)
    assert len(work) == 2, "找不到工作线程函数"
    body = work[1].split("threading.Thread", 1)[0]
    for forbidden in ("_var.get()", "force_var", "e_from.get()", "e_to.get()",
                      "cb_type.get()", "tree.selection()"):
        assert forbidden not in body, \
            f"工作线程里读了界面控件：{forbidden}"


def test_there_is_exactly_one_progress_bar_on_the_run_page():
    """两条进度条并成一条：进度在动就走比例，卡住就自己动起来。"""
    text = (ROOT / "app.py").read_text(encoding="utf-8")
    created = [ln for ln in text.splitlines()
               if "ttk.Progressbar(" in ln and not ln.strip().startswith("#")]
    assert len(created) == 1, f"抓取页应该只有一条进度条，找到 {len(created)} 条"


def test_the_bar_animates_itself_when_progress_stalls():
    """85/86 之后一声不吭地等几分钟，和卡死没有区别。

    进度条必须能从「走比例」切成「来回跑」，切回来时还要恢复真实进度。
    """
    text = (ROOT / "app.py").read_text(encoding="utf-8")
    assert 'configure(mode="indeterminate")' in text, "卡住时进度条不会动"
    assert 'mode="determinate"' in text, "动完不切回真进度"
    assert "bar.start(" in text and "bar.stop()" in text
    assert "STALL_SECONDS" in text, "切换阈值应该是个有名字的常量"
    assert "已运行" in text, "不显示已运行时长"


def test_the_spinner_is_ascii_only():
    """转圈符号也要 cp936 编得出来 —— 它会进日志。"""
    text = (ROOT / "app.py").read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if "SPINNER" in ln and "=" in ln)
    line.encode("cp936")
    assert all(ord(c) < 128 for c in line.split("=")[1].strip().strip('"'))


def test_the_bar_never_loses_the_real_progress_when_it_animates():
    """切成动画再切回来，不能把已完成的比例清零 ——
    那会让人以为白跑了。所以真实进度另存一份。"""
    text = (ROOT / "app.py").read_text(encoding="utf-8")
    assert 'state["progress"]' in text, "没有单独保存真实进度"
    finish = text.split("def finish(result)", 1)[1].split("def ", 1)[0]
    assert 'state["progress"]' in finish, "结束时没有用回真实进度"


# ------------------------------------------- .bat 的编码：踩过一次，钉死

@pytest.mark.parametrize("name", BATS)
def test_bat_files_are_stored_in_the_console_code_page(name):
    """.bat 必须存成 GBK（cp936）—— 中文 Windows 的控制台就是这个码页。

    存成 UTF-8 再靠 `chcp 65001` 转，会踩 cmd 的一个老 bug：
    它按**字节偏移**记住自己读到文件哪儿了，中途换码页会让偏移错位，
    于是从后面某一行的**中间**接着读。实跑真的出过：

        '执行文件」正是下载器木马的行为特征' is not recognized as an
        internal or external command

    那一行本来只是条中文注释。更新其实成功了，但用户看到的是一堆
    像出错的红字 —— 而且内容还是「木马」两个字。
    """
    raw = (ROOT / name).read_bytes()
    raw.decode("cp936")          # 解不开就说明存错编码了
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    else:
        raise AssertionError(f"{name} 看起来还是 UTF-8，中文 cmd 会读乱")


@pytest.mark.parametrize("name", BATS)
def test_bat_files_never_switch_the_code_page(name):
    """理由同上：文件本来就是控制台的码页，不需要也不能换。"""
    text = (ROOT / name).read_bytes().decode("cp936")
    live = [ln for ln in text.splitlines()
            if ln.strip() and not ln.strip().upper().startswith("REM")]
    assert not any(ln.strip().lower().startswith("chcp") for ln in live), \
        f"{name} 里还有 chcp —— 换码页会让 cmd 的读取位置错位"
