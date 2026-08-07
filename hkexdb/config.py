"""读取 config.yaml，并把里面的路径变成好用的 Path 对象。

设计原则（工程要求）：所有可调参数都来自 config.yaml，代码里不写死。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import yaml


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
    chunk_months: int
    row_range: int
    max_pages_per_chunk: int
    queries: list[Query]

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
    config_path = Path(config_path)
    with config_path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    paths = raw["paths"]
    date_range = raw["date_range"]
    http = raw["http"]
    listing = raw["listing"]

    queries = [
        Query(name=q["name"], lang=q.get("lang", "ZH"), params=dict(q.get("params", {})))
        for q in listing["queries"]
    ]

    return Config(
        raw=raw,
        config_path=config_path,
        data_dir=Path(paths["data_dir"]),
        raw_dir=Path(paths["raw_dir"]),
        cache_dir=Path(paths["cache_dir"]),
        probe_dir=Path(paths["probe_dir"]),
        log_dir=Path(paths["log_dir"]),
        date_from=_as_date(date_range["from"]),
        date_to=_as_date(date_range["to"]),
        user_agent=http["user_agent"],
        min_interval_seconds=float(http["min_interval_seconds"]),
        timeout_seconds=int(http["timeout_seconds"]),
        max_retries=int(http["max_retries"]),
        backoff_base_seconds=float(http["backoff_base_seconds"]),
        cache_enabled=bool(http["cache_enabled"]),
        chunk_months=int(listing["chunk_months"]),
        row_range=int(listing["row_range"]),
        max_pages_per_chunk=int(listing["max_pages_per_chunk"]),
        queries=queries,
        log_level=str(raw.get("logging", {}).get("level", "INFO")),
    )
