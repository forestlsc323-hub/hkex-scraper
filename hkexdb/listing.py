"""按日期范围拉取公告列表。

阶段一的主力脚本。做的事很窄：把检索接口的分页结果**原样**取回来交给 storage。
不判断公告类型、不解析 PDF、不去重——那些都是后面阶段的事。
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

from .config import Config, Query
from .http_client import PoliteSession
from .storage import RawStore, row_uid

log = logging.getLogger(__name__)

BASE = "https://www1.hkexnews.hk"
SEARCH_PAGE = f"{BASE}/search/titlesearch.xhtml"

# ⚠️ 下面两项由 run_probe.py 实测确认。若勘察报告的结论与此不符，改这里。
SERVLET = f"{BASE}/search/titleSearchServlet.do"
DATE_PARAM_NAMES = ("fromDate", "toDate")

XHR_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": SEARCH_PAGE + "?lang=zh",
}


def month_chunks(start: date, end: date, months: int = 1) -> list[tuple[date, date]]:
    """把日期范围切成若干块。

    切块的两个理由：
    1) 单次查询命中太多会撞上服务端的返回条数上限，切小就不会漏。
    2) 断点续跑的粒度更细——中断后只需重跑没做完的那一块。
    """
    if start > end:
        return []

    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        # 往后推 months 个月，落到"下一块的第一天"
        year, month = cursor.year, cursor.month + months
        year += (month - 1) // 12
        month = (month - 1) % 12 + 1
        next_start = date(year, month, 1)
        chunk_end = min(end, date.fromordinal(next_start.toordinal() - 1))
        chunks.append((cursor, chunk_end))
        cursor = next_start
    return chunks


def _parse_payload(text: str) -> tuple[list[dict], bool]:
    """接口把结果放在 result 字段里，且常常是"JSON 字符串套 JSON"。"""
    payload = json.loads(text)
    rows = payload.get("result")
    if isinstance(rows, str):
        rows = json.loads(rows) if rows.strip() else []
    has_next = str(payload.get("hasNextRow", "")).lower() == "true"
    return rows or [], has_next


def _build_params(cfg: Config, query: Query, chunk: tuple[date, date],
                  page_no: int) -> dict:
    from_key, to_key = DATE_PARAM_NAMES
    params = {
        "sortDir": "0",
        "sortByOptions": "DateTime",
        "category": "0",
        "market": "SEHK",
        "stockId": "-1",
        "documentType": "-1",
        from_key: chunk[0].strftime("%Y%m%d"),
        to_key: chunk[1].strftime("%Y%m%d"),
        "title": "",
        "searchType": "1",
        "t1code": "-1",
        "t2Gcode": "-1",
        "t2code": "-1",
        "rowRange": str(cfg.row_range),
        "lang": query.lang,
        "pageNo": str(page_no),
    }
    # config.yaml 里 profile 自己写的参数优先，可覆盖上面任何一项
    params.update({k: str(v) for k, v in query.params.items()})
    return params


def fetch_chunk(session: PoliteSession, store: RawStore, cfg: Config,
                query: Query, chunk: tuple[date, date]) -> int:
    """抓一个 (查询 × 时间块) 的全部分页，返回记录数。"""
    unit = f"{query.name}|{chunk[0].isoformat()}_{chunk[1].isoformat()}"

    if store.is_done(unit):
        log.info("跳过（已完成）%s", unit)
        return 0

    total = 0
    for page_no in range(1, cfg.max_pages_per_chunk + 1):
        params = _build_params(cfg, query, chunk, page_no)
        resp = session.get(SERVLET, params=params, headers=XHR_HEADERS)

        try:
            rows, has_next = _parse_payload(resp.text)
        except json.JSONDecodeError:
            log.error("%s 第 %d 页返回的不是 JSON，前 200 字：%r",
                      unit, page_no, resp.text[:200])
            raise

        store.save_page_json(unit, page_no, resp.text)

        enriched = []
        for position, row in enumerate(rows):
            # 原样保留接口返回的所有字段，只追加以 _ 开头的出处信息
            record = dict(row)
            record["_row_uid"] = row_uid(query.name, chunk[0].isoformat(),
                                         page_no, position,
                                         str(row.get("FILE_LINK", "")))
            record["_query_name"] = query.name
            record["_lang"] = query.lang
            record["_chunk_from"] = chunk[0].isoformat()
            record["_chunk_to"] = chunk[1].isoformat()
            record["_page_no"] = page_no
            record["_fetched_at"] = resp.fetched_at
            record["_source_url"] = SERVLET
            enriched.append(record)

        store.append_rows(enriched)
        total += len(enriched)

        log.info("%s 第 %d 页：%d 条%s%s", unit, page_no, len(rows),
                 "（缓存）" if resp.from_cache else "",
                 "" if has_next else "（末页）")

        if not has_next or not rows:
            break
    else:
        log.warning("%s 翻到了 max_pages_per_chunk=%d 上限仍未结束，"
                    "可能是分页判断有误，请检查。", unit, cfg.max_pages_per_chunk)

    store.mark_done(unit)
    return total


def run(cfg: Config) -> tuple[Path, int]:
    """阶段一入口：按配置抓完所有 (查询 × 时间块)，生成 raw CSV。"""
    session = PoliteSession(
        user_agent=cfg.user_agent,
        cache_dir=cfg.cache_dir,
        min_interval_seconds=cfg.min_interval_seconds,
        timeout_seconds=cfg.timeout_seconds,
        max_retries=cfg.max_retries,
        backoff_base_seconds=cfg.backoff_base_seconds,
        cache_enabled=cfg.cache_enabled,
    )
    store = RawStore(cfg.raw_dir)
    chunks = month_chunks(cfg.date_from, cfg.date_to, cfg.chunk_months)

    log.info("日期范围 %s ~ %s，切成 %d 块；查询 profile %d 个",
             cfg.date_from, cfg.date_to, len(chunks), len(cfg.queries))

    grand_total = 0
    for query in cfg.queries:
        for chunk in chunks:
            grand_total += fetch_chunk(session, store, cfg, query, chunk)

    log.info("本次新增 %d 条；网络请求 %d 次，缓存命中 %d 次，重试 %d 次",
             grand_total, session.stats["network"], session.stats["cache"],
             session.stats["retries"])

    return store.rebuild_csv()
