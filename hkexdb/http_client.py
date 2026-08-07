"""礼貌爬取层：限速、重试、磁盘缓存。

所有对外网的请求都必须走这里，不要在别处直接 requests.get()。
这样"对方站点看到的行为"只有一处定义，改一个地方就全局生效。

缓存的一个重要副作用：**幂等**。
响应连同抓取时间一起存盘，重跑时从缓存读，产出的 CSV 逐字节一致。
这满足工程要求里的"同输入同输出"。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)


@dataclass
class Response:
    """一次请求的结果。from_cache=True 表示这次没有真的联网。"""

    url: str
    params: dict
    status: int
    text: str
    fetched_at: str          # ISO 时间戳；来自首次抓取，缓存命中时原样带出
    from_cache: bool


def cache_key(url: str, params: dict | None) -> str:
    """URL + 参数 → 稳定的缓存文件名。

    参数先排序再序列化，保证 {a:1,b:2} 和 {b:2,a:1} 落到同一个 key。
    """
    payload = json.dumps(
        {"url": url, "params": params or {}}, sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


class PoliteSession:
    """带限速、重试、缓存的 HTTP 会话。"""

    def __init__(self, *, user_agent: str, cache_dir: Path,
                 min_interval_seconds: float = 1.5, timeout_seconds: int = 60,
                 max_retries: int = 4, backoff_base_seconds: float = 2.0,
                 cache_enabled: bool = True):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
        })
        self.cache_dir = Path(cache_dir)
        self.min_interval = min_interval_seconds
        self.timeout = timeout_seconds
        self.max_retries = max_retries
        self.backoff_base = backoff_base_seconds
        self.cache_enabled = cache_enabled

        self._last_request_at = 0.0
        self.stats = {"network": 0, "cache": 0, "retries": 0}

    # ---------- 缓存读写 ----------

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def _read_cache(self, key: str) -> Response | None:
        path = self._cache_path(key)
        if not path.exists():
            return None
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("缓存文件损坏，忽略并重新抓取 %s: %s", path.name, exc)
            return None
        return Response(
            url=blob["url"], params=blob["params"], status=blob["status"],
            text=blob["text"], fetched_at=blob["fetched_at"], from_cache=True,
        )

    def _write_cache(self, key: str, resp: Response) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path(key).write_text(
            json.dumps({
                "url": resp.url, "params": resp.params, "status": resp.status,
                "fetched_at": resp.fetched_at, "text": resp.text,
            }, ensure_ascii=False),
            encoding="utf-8",
        )

    # ---------- 限速 ----------

    def _throttle(self) -> None:
        """确保两次真实网络请求之间至少隔 min_interval 秒。"""
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request_at = time.monotonic()

    # ---------- 对外接口 ----------

    def get(self, url: str, params: dict | None = None, *,
            headers: dict | None = None, use_cache: bool = True) -> Response:
        """带重试的 GET。返回 Response；重试用尽后抛异常。"""
        key = cache_key(url, params)

        if self.cache_enabled and use_cache:
            cached = self._read_cache(key)
            if cached is not None:
                self.stats["cache"] += 1
                log.debug("缓存命中 %s params=%s", url, params)
                return cached

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                raw = self.session.get(url, params=params, headers=headers,
                                       timeout=self.timeout)
                self.stats["network"] += 1

                # 429 / 5xx 属于"稍后可能就好了"，值得重试
                if raw.status_code == 429 or raw.status_code >= 500:
                    raise requests.HTTPError(
                        f"HTTP {raw.status_code}", response=raw)

                raw.raise_for_status()

                resp = Response(
                    url=url, params=params or {}, status=raw.status_code,
                    text=raw.text,
                    fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    from_cache=False,
                )
                if self.cache_enabled:
                    self._write_cache(key, resp)
                return resp

            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                # 4xx（429 除外）是我们自己请求写错了，重试没意义
                if status is not None and 400 <= status < 500 and status != 429:
                    log.error("请求被拒绝，不重试 %s params=%s -> HTTP %s",
                              url, params, status)
                    raise
                last_error = exc
            except requests.RequestException as exc:
                last_error = exc

            if attempt < self.max_retries:
                wait = self.backoff_base * (2 ** attempt)   # 2s, 4s, 8s, 16s
                self.stats["retries"] += 1
                log.warning("第 %d 次失败（%s），%.0f 秒后重试：%s",
                            attempt + 1, last_error, wait, url)
                time.sleep(wait)

        log.error("重试 %d 次后仍失败：%s params=%s", self.max_retries, url, params)
        raise RuntimeError(f"请求失败：{url} params={params}") from last_error
