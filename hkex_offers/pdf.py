"""Download announcement PDFs and turn them into plain text."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def cache_path(cache_dir: Path, url: str) -> Path:
    digest = hashlib.sha1(url.encode()).hexdigest()[:16]
    return cache_dir / f"{digest}.pdf"


def download(url: str, cache_dir: Path, session: requests.Session | None = None,
             timeout: int = 120) -> bytes:
    path = cache_path(cache_dir, url)
    if path.exists():
        return path.read_bytes()
    session = session or requests.Session()
    resp = session.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_bytes(resp.content)
    return resp.content


def _text_pypdf(data: bytes, max_pages: int) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = reader.pages[:max_pages] if max_pages else reader.pages
    return "\n".join(page.extract_text() or "" for page in pages)


def _text_pdfplumber(data: bytes, max_pages: int) -> str:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = pdf.pages[:max_pages] if max_pages else pdf.pages
        return "\n".join(page.extract_text() or "" for page in pages)


def extract_text(data: bytes, max_pages: int = 60) -> str:
    """pypdf first (fast); fall back to pdfplumber when it yields almost nothing."""
    try:
        text = _text_pypdf(data, max_pages)
    except Exception:
        text = ""
    if len(text.strip()) >= 500:
        return text
    try:
        return _text_pdfplumber(data, max_pages) or text
    except Exception:
        return text
