"""找配置文件、读配置文件。就这两件事。

工程要求：所有可调参数都来自 config.yaml，代码里不写死。
这里只提供读取入口，不定义参数含义 —— 含义写在 config.yaml 的注释里，
那才是唯一真相源。
"""

from __future__ import annotations

from pathlib import Path

import yaml

# 仓库根目录 = 本文件的上一级。用它兜底，这样从任何工作目录启动都能找到
# config.yaml / screening_rules.yaml —— .bat 双击时的工作目录未必是这里。
REPO_ROOT = Path(__file__).resolve().parent.parent


def resolve_file(name: str | Path) -> Path:
    """按「当前目录 → 仓库根目录」的顺序找文件。

    都找不到就报当前目录那个，让错误信息指向用户以为它该在的地方。
    """
    path = Path(name)
    if path.is_absolute():
        return path
    for base in (Path.cwd(), REPO_ROOT):
        candidate = base / path
        if candidate.exists():
            return candidate.resolve()
    return (Path.cwd() / path).resolve()


def read(name: str | Path = "config.yaml", root: Path | None = None) -> dict:
    """读一个 YAML 配置。读不到、或者内容坏了，返回空字典。

    配置坏了不该让整个程序起不来 —— 各处调用方自带默认值，
    退回默认值继续跑，比在启动阶段崩掉有用得多。

    原来 app.py、runner 里两处、各自写了一遍 try/except + safe_load，
    四份几乎一样的代码。合成这一处。
    """
    path = (root / name) if root is not None else resolve_file(name)
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def section(name: str, *, key: str = "listing", root: Path | None = None) -> dict:
    """读 config.yaml 里的某一节。"""
    return read(name, root=root).get(key, {}) or {}
