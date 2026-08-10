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
BATS = ["RUN.bat", "一键运行.bat", "一键更新.bat"]


# ---------------------------------------------------------------- 启动器

@pytest.mark.parametrize("name", BATS)
def test_bat_files_use_crlf(name):
    """LF 换行会让 Windows cmd 解析崩掉，双击后闪一下就关（真踩过）。"""
    raw = (ROOT / name).read_bytes()
    assert b"\r\n" in raw
    assert raw.count(b"\n") == raw.count(b"\r\n"), f"{name} 里有裸 LF"


def test_launcher_keeps_a_console_so_errors_are_visible():
    """pythonw.exe 没有控制台：界面若在启动阶段崩掉，用户什么都看不到。"""
    bat = (ROOT / "RUN.bat").read_bytes().decode("utf-8")
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
