"""HKEXnews title-search client."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from datetime import date

import requests

BASE = "https://www1.hkexnews.hk"
SERVLET = f"{BASE}/search/titleSearchServlet.do"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{BASE}/search/titlesearch.xhtml?lang=zh",
}


@dataclass(frozen=True)
class Announcement:
    news_id: str
    date_time: str
    stock_code: str
    stock_name: str
    title: str
    pdf_url: str
    file_info: str

    def as_dict(self) -> dict:
        return asdict(self)


def _absolute(link: str) -> str:
    link = (link or "").strip()
    if not link:
        return ""
    if link.startswith("http"):
        return link
    return BASE + link


def _parse_result(payload: dict) -> list[dict]:
    result = payload.get("result")
    if isinstance(result, str):
        return json.loads(result) if result.strip() else []
    return result or []


def search_page(
    session: requests.Session,
    *,
    title: str,
    date_from: date,
    date_to: date,
    lang: str,
    page_no: int,
    row_range: int = 100,
    timeout: int = 60,
) -> tuple[list[dict], bool]:
    params = {
        "sortDir": "0",
        "sortByOptions": "DateTime",
        "category": "0",
        "market": "SEHK",
        "stockId": "-1",
        "documentType": "-1",
        "fromDate": date_from.strftime("%Y%m%d"),
        "toDate": date_to.strftime("%Y%m%d"),
        "title": title,
        "searchType": "1",
        "t1code": "-1",
        "t2Gcode": "-1",
        "t2code": "-1",
        "rowRange": str(row_range),
        "lang": lang,
        "pageNo": str(page_no),
    }
    resp = session.get(SERVLET, params=params, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()
    rows = _parse_result(payload)
    has_next = str(payload.get("hasNextRow", "")).lower() == "true"
    return rows, has_next


def search(
    keywords: list[str],
    *,
    date_from: date,
    date_to: date,
    lang: str = "ZH",
    delay: float = 1.0,
    max_pages: int = 50,
    session: requests.Session | None = None,
) -> list[Announcement]:
    """Run one title search per keyword and return de-duplicated announcements."""
    session = session or requests.Session()
    seen: dict[str, Announcement] = {}

    for keyword in keywords:
        for page_no in range(1, max_pages + 1):
            rows, has_next = search_page(
                session,
                title=keyword,
                date_from=date_from,
                date_to=date_to,
                lang=lang,
                page_no=page_no,
            )
            for row in rows:
                pdf_url = _absolute(row.get("FILE_LINK", ""))
                if not pdf_url.lower().endswith(".pdf"):
                    continue
                ann = Announcement(
                    news_id=str(row.get("NEWS_ID", "")).strip(),
                    date_time=str(row.get("DATE_TIME", "")).strip(),
                    stock_code=str(row.get("STOCK_CODE", "")).strip(),
                    stock_name=str(row.get("STOCK_NAME", "")).strip(),
                    title=str(row.get("TITLE", "")).strip(),
                    pdf_url=pdf_url,
                    file_info=str(row.get("FILE_INFO", "")).strip(),
                )
                seen.setdefault(ann.news_id or pdf_url, ann)
            if not has_next or not rows:
                break
            time.sleep(delay)
        time.sleep(delay)

    return sorted(seen.values(), key=lambda a: (a.date_time, a.stock_code))
