"""跨平台可移植性测试。

这两条都是真实炸过的问题，且**在 Linux 上开发时看不见**：

1. Windows 中文控制台是 cp936，编不出 ✅ ⚠️ ❌ 这类符号，
   脚本跑得好好的，最后一行 print 把它打死。
2. GUI 启动器不一定在仓库根目录跑 python，相对路径的
   config.yaml / screening_rules.yaml 就找不到了。
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hkexdb import config, console, screening

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- 编码

def test_console_markers_are_pure_ascii():
    """控制台标记必须是纯 ASCII —— 任何终端编码都装得下。"""
    for marker in (console.OK, console.WARN, console.FAIL, console.ARROW):
        marker.encode("ascii")          # 编不出就抛异常
        marker.encode("cp936")


def test_entry_scripts_print_no_emoji_to_console():
    """入口脚本的 print() 里不许出现 cp936 编不出的字符。

    emoji 只能出现在写进 .md / .csv 的内容里 —— 那些文件显式用 utf-8 写。
    """
    offenders = []
    for script in sorted((REPO / "scripts").glob("run_*.py")) + [REPO / "app.py"]:
        for lineno, line in enumerate(script.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith("print"):
                continue
            try:
                line.encode("cp936")
            except UnicodeEncodeError as exc:
                bad = line[exc.start:exc.end]
                offenders.append(f"{script.name}:{lineno} 含 {bad!r}")
    assert not offenders, "控制台输出含 cp936 编不出的字符：\n" + "\n".join(offenders)


def test_console_init_survives_a_cp936_stream():
    """init() 之后，往 cp936 流里写 emoji 也不该抛异常。"""
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp936", errors="strict")
    with pytest.raises(UnicodeEncodeError):
        stream.write("✅")              # 修复前的行为

    stream.reconfigure(errors="replace")
    stream.write("✅ 数量校验")          # 修复后：替换成 ?，不崩
    stream.flush()


def test_scripts_run_under_cp936_console(tmp_path):
    """端到端：模拟 Windows 中文控制台跑 run_screening.py --demo。"""
    env = dict(os.environ, PYTHONIOENCODING="cp936")
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "run_screening.py"), "--demo"],
        env=env, capture_output=True, cwd=str(tmp_path), timeout=120)
    assert proc.returncode == 0, proc.stderr.decode("cp936", errors="replace")[-800:]
    assert b"UnicodeEncodeError" not in proc.stderr


# ---------------------------------------------------------------- 路径

def test_config_found_from_any_working_directory(tmp_path, monkeypatch):
    """GUI 启动器常常不在仓库根目录跑 python。"""
    monkeypatch.chdir(tmp_path)
    cfg = config.load("config.yaml")
    assert cfg.config_path == (REPO / "config.yaml").resolve()


def test_screening_rules_found_from_any_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rules = screening.load_rules("screening_rules.yaml")
    assert rules.exclude_terms


def test_data_dirs_anchor_to_the_repo_not_the_cwd(tmp_path, monkeypatch):
    """产物必须落在仓库目录，否则第 4 步找不到第 3 步的输出。"""
    monkeypatch.chdir(tmp_path)
    cfg = config.load("config.yaml")
    assert cfg.raw_dir.is_absolute()
    assert REPO in cfg.raw_dir.parents
    assert tmp_path not in cfg.raw_dir.parents


def test_missing_config_says_where_it_looked(tmp_path, monkeypatch):
    """找不到配置时要告诉用户去哪儿找了，而不是甩个 FileNotFoundError。"""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError) as exc:
        config.load("不存在的配置.yaml")
    message = str(exc.value)
    assert "当前工作目录" in message and "仓库根目录" in message


def test_absolute_config_path_is_respected(tmp_path):
    cfg = config.load(REPO / "config.yaml")
    assert cfg.config_path == REPO / "config.yaml"
