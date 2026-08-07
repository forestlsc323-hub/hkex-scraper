"""读取 config.yaml，并把里面的路径变成好用的 Path 对象。

设计原则（工程要求）：所有可调参数都来自 config.yaml，代码里不写死。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import yaml

# 仓库根目录 = 本文件的上一级。用它兜底，这样从任何工作目录启动都能找到
# config.yaml / screening_rules.yaml（GUI 启动器常常不在仓库根目录跑 python）。
REPO_ROOT = Path(__file__).resolve().parent.parent


def resolve_file(name: str | Path) -> Path:
    """按「当前目录 → 仓库根目录」的顺序找文件。都找不到就报当前目录那个，
    让错误信息指向用户以为它该在的地方。"""
    path = Path(name)
    if path.is_absolute():
        return path
    for base in (Path.cwd(), REPO_ROOT):
        candidate = base / path
        if candidate.exists():
            return candidate.resolve()
    return (Path.cwd() / path).resolve()


@dataclass(frozen=True)
class Query:
    """一组检索参数。对应 config.yaml 里 listing.queries 的一项。"""

    name: str
    lang: str
    params: dict


@dataclass(frozen=True)
class Config:
    raw: dict                      # 原始 YAML，需要时可以直接查
    config_path: Path

    # paths
    data_dir: Path
    raw_dir: Path
    cache_dir: Path
    probe_dir: Path
    log_dir: Path

    # date_range
    date_from: date
    date_to: date

    # http
    user_agent: str
    min_interval_seconds: float
    timeout_seconds: int
    max_retries: int
    backoff_base_seconds: float
    cache_enabled: bool

    # listing
    row_range_step: int
    max_rounds_per_day: int
    category_t1code: str
    category_t2codes: list[str]

    # logging
    log_level: str

    def ensure_dirs(self) -> None:
        """第一次运行时把需要的目录建出来。"""
        for path in (self.data_dir, self.raw_dir, self.cache_dir,
                     self.probe_dir, self.log_dir):
            path.mkdir(parents=True, exist_ok=True)


def _as_date(value) -> date:
    """YAML 里写 2026-01-01 会被解析成 date；写成字符串也要能吃下。"""
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def load(config_path: str | Path = "config.yaml") -> Config:
    config_path = resolve_file(config_path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"找不到配置文件 {config_path}。\n"
            f"当前工作目录：{Path.cwd()}\n"
            f"仓库根目录：{REPO_ROOT}\n"
            f"请在仓库根目录下运行，或把 config.yaml 放到上述任一位置。")
    with config_path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    # 数据/日志目录一律相对 config.yaml 所在目录，而不是相对当前工作目录。
    # 否则从别处启动会把 data/ 建到意想不到的地方，下一步就找不到上一步的产物。
    anchor = config_path.parent

    paths = raw["paths"]
    date_range = raw["date_range"]
    http = raw["http"]
    listing = raw["listing"]

    return Config(
        raw=raw,
        config_path=config_path,
        data_dir=anchor / paths["data_dir"],
        raw_dir=anchor / paths["raw_dir"],
        cache_dir=anchor / paths["cache_dir"],
        probe_dir=anchor / paths["probe_dir"],
        log_dir=anchor / paths["log_dir"],
        date_from=_as_date(date_range["from"]),
        date_to=_as_date(date_range["to"]),
        user_agent=http["user_agent"],
        min_interval_seconds=float(http["min_interval_seconds"]),
        timeout_seconds=int(http["timeout_seconds"]),
        max_retries=int(http["max_retries"]),
        backoff_base_seconds=float(http["backoff_base_seconds"]),
        cache_enabled=bool(http["cache_enabled"]),
        row_range_step=int(listing["row_range_step"]),
        max_rounds_per_day=int(listing["max_rounds_per_day"]),
        category_t1code=str(listing.get("category_t1code", "10000")),
        category_t2codes=[str(c) for c in listing.get("category_t2codes", [])],
        log_level=str(raw.get("logging", {}).get("level", "INFO")),
    )
