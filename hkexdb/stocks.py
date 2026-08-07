"""证券代码 → 披露易 stockId 的映射。

移植自实战客户端 `hkex_client.py` 的 `_load_stock_id_map` / `_resolve_stock_id`。

**为什么这个不起眼的模块很重要：**
手册第六节四层防漏网里，第 2 层「公司级完备性对账」被标为**最强的一层** ——
每家公司的 T0 必须落在「主清单／窗口前／特殊品种／流产案」四选一，数量守恒。
要做这个对账，就得能按公司代码把它的全部历史公告拉出来，
而按代码检索必须先把 `0700` 这种代码换成披露易内部的 `stockId`。
没有本模块，第 2 层质控做不了。

两份清单分别是活跃证券和已除牌证券。**已除牌的必须要**：
被要约收购成功的公司往往随后就退市了，只查活跃清单会漏掉一大批先例交易。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .http_client import PoliteSession

log = logging.getLogger(__name__)

# ⚠️ 注意主机名是 www.hkexnews.hk，**不带 1**，与检索接口的 www1 不同。
_STOCK_LIST_URLS = [
    # (来源, URL, category) —— category 会带进检索请求
    ("active", "https://www.hkexnews.hk/ncms/script/eds/activestock_sehk_e.json", "0"),
    ("inactive", "https://www.hkexnews.hk/ncms/script/eds/inactivestock_sehk_e.json", "1"),
]


def normalize_code(ticker: str) -> str:
    """`0700` / `700` / `0700.HK` / `00700` → `00700`（统一 5 位）。"""
    code = (ticker or "").upper().replace(".HK", "").strip()
    code = code.lstrip("0").zfill(5)
    if not code or code == "00000":
        raise ValueError(f"无法识别股票代码：{ticker!r}")
    return code


def load_stock_id_map(session: PoliteSession,
                      cache_path: Path | None = None) -> dict[str, tuple[str, str]]:
    """下载两份证券清单，返回 {5位代码: (stockId, category)}。

    ⚠️ **活跃证券优先**，这一条不能靠字典插入顺序来保证。

    实战客户端里是先读 inactive 再读 active，靠后写覆盖先写来实现「活跃优先」——
    能跑，但依赖 `_STOCK_LIST_URLS` 的字面顺序。谁把那两行调换一下，
    优先级就静默反过来了，而且不会报错，只会让已除牌的 category 用到活跃证券上。
    这里改成显式判断：已存在的活跃记录不被已除牌记录覆盖。
    """
    mapping: dict[str, tuple[str, str]] = {}
    active_codes: set[str] = set()

    for source, url, category in _STOCK_LIST_URLS:
        try:
            resp = session.get(url)
            items = json.loads(resp.text)
        except Exception as exc:
            log.warning("下载证券列表 %s 失败：%s", url, exc)
            continue

        added = 0
        for item in items:
            raw_code = str(item.get("c", "")).strip()
            if not raw_code:
                continue
            try:
                code = normalize_code(raw_code)
            except ValueError:
                continue

            # 显式优先级：活跃证券已登记的，不让已除牌的覆盖
            if source != "active" and code in active_codes:
                continue
            mapping[code] = (str(item.get("i", "")), category)
            if source == "active":
                active_codes.add(code)
            added += 1

        log.info("证券清单 %s：%d 条", source, added)

    if cache_path is not None and mapping:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({k: list(v) for k, v in sorted(mapping.items())},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
        log.info("stockId 映射已缓存：%s（%d 条）", cache_path, len(mapping))

    return mapping


def load_cached_map(cache_path: Path) -> dict[str, tuple[str, str]]:
    """从本地副本读映射，不联网。"""
    if not cache_path.exists():
        return {}
    blob = json.loads(cache_path.read_text(encoding="utf-8"))
    return {k: (v[0], v[1]) for k, v in blob.items()}


def resolve(ticker: str, mapping: dict[str, tuple[str, str]]) -> tuple[str, str]:
    """`0700` → `(stockId, category)`。找不到就抛异常，绝不返回一个猜的值。"""
    code = normalize_code(ticker)
    if code not in mapping:
        raise ValueError(
            f"披露易证券清单里找不到 {ticker}（代码归一化为 {code}）。"
            f"可能尚未上市、代码有误，或清单未更新。")
    return mapping[code]
