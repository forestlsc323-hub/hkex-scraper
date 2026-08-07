"""从 HKEXnews 的 PDF 链接**直接**取文本。

用法就是你要的那样：手上有公告列表里的那个 PDF 链接，直接喂进来，
拿到带页码的文本。没有「先下载文件、再打开文件」这一步。

    doc = open_pdf(session, "https://www1.hkexnews.hk/listedco/.../xxx.pdf")
    print(doc.pages[10])          # 第 11 页的文本
    print(doc.find("價值比較"))    # 定位到某节在第几页

关于缓存要说清楚一句：
函数内部会把取回的 PDF 字节存一份到 data/cache/pdf/。
这**不是**让你手工下载 —— 你看不到这一步，链接进、文本出。
留这份副本是因为你自己的工程要求写着「原始文件永久保留，
解析与抽取幂等可重跑」：没有副本，公告一旦被替换或撤下，
就再也无法复现当初抽出来的数字，审计链就断了。
重跑时从副本读，同一个链接永远得到同一份文本。
"""

from __future__ import annotations

import hashlib
import io
import logging
from dataclasses import dataclass
from pathlib import Path

import requests

log = logging.getLogger(__name__)

HEADERS = {"Accept": "application/pdf,*/*"}

BASE = "https://www1.hkexnews.hk"


def full_url(file_link: str) -> str:
    """把 raw 表的 `FILE_LINK` 拼成可下载的完整地址。

    接口给的是相对路径，形如 `/listedco/listconews/sehk/2026/0615/2026061500123.pdf`。
    移植自实战客户端的 `full_url`。
    """
    link = (file_link or "").strip()
    if not link:
        raise ValueError("FILE_LINK 为空，无法拼出 PDF 地址")
    if link.startswith("http"):
        return link
    return BASE + ("" if link.startswith("/") else "/") + link


@dataclass
class PdfDoc:
    """一份公告的解析结果。页码从 1 起，和 PDF 阅读器显示的一致。"""

    url: str
    pages: dict[int, str]        # {页码: 该页文本}
    page_count: int
    has_text_layer: bool
    from_cache: bool
    extractor: str = ""          # 实际产出文本的解析器，出处的一部分
    extractor_note: str = ""     # 两个解析器分歧时的说明

    @property
    def text(self) -> str:
        return "\n".join(self.pages[p] for p in sorted(self.pages))

    def find(self, needle: str) -> list[tuple[int, str]]:
        """找出现 needle 的页码和所在行。铁律三要的 page 出处就从这里来。"""
        hits = []
        for page_no in sorted(self.pages):
            for line in self.pages[page_no].splitlines():
                if needle in line:
                    hits.append((page_no, line.strip()))
        return hits

    def page_of(self, needle: str) -> int | None:
        """needle 首次出现在第几页。找不到返回 None，绝不猜。"""
        hits = self.find(needle)
        return hits[0][0] if hits else None


def _cache_path(cache_dir: Path, url: str) -> Path:
    return cache_dir / f"{hashlib.sha256(url.encode()).hexdigest()[:20]}.pdf"


def fetch_bytes(url: str, cache_dir: Path, *,
                session: requests.Session | None = None,
                user_agent: str = "hkex-precedent-db/0.1",
                timeout: int = 180) -> tuple[bytes, bool]:
    """取 PDF 字节。返回 (内容, 是否来自本地副本)。

    ⚠️ `session` 应当传入**已访问过检索页的那个会话**。
    实战客户端的 `download_pdf` 用的就是 `self.session` —— 和检索共用一个，
    带着检索页种下的 cookie。这里如果新建一个裸 session，
    在需要 cookie 的路径上会拿不到文件，而表现可能只是一个 403 或一段 HTML。
    """
    path = _cache_path(cache_dir, url)
    if path.exists():
        log.debug("PDF 副本命中 %s", url)
        return path.read_bytes(), True

    if session is None:
        log.warning("未传入已建立会话的 session，改用裸会话下载 %s —— "
                    "若失败请传入检索时用的那个 session", url)
        session = requests.Session()
    headers = dict(HEADERS, **{"User-Agent": user_agent})
    resp = session.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()

    content = resp.content
    if not content.startswith(b"%PDF"):
        raise ValueError(
            f"这个链接返回的不是 PDF（开头是 {content[:16]!r}）：{url}")

    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    log.info("已取回 PDF %s（%.1f KB）", url, len(content) / 1024)
    return content, False


def _extract_pdfplumber(data: bytes, max_pages: int) -> tuple[dict[int, str], int]:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        total = len(pdf.pages)
        limit = min(max_pages, total) if max_pages else total
        return {i + 1: (pdf.pages[i].extract_text() or "") for i in range(limit)}, total


def _extract_pypdf(data: bytes, max_pages: int) -> tuple[dict[int, str], int]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    total = len(reader.pages)
    limit = min(max_pages, total) if max_pages else total
    return {i + 1: (reader.pages[i].extract_text() or "") for i in range(limit)}, total


def _chars(pages: dict[int, str]) -> int:
    return sum(len(t.strip()) for t in pages.values())


# pypdf 要比 pdfplumber 多出这么多字符，才值得改用它。
# 定成 1.2 而不是「谁多用谁」：3336 那单 pypdf 只多 1.6%（空白与连字处理差异），
# 这种噪声级差异翻转解析器会让结果随库版本漂移，破坏「同输入同输出」。
# 只有 pdfplumber 明显失手时才切换。
_SWITCH_RATIO = 1.2


def extract_pages(data: bytes, max_pages: int = 0, *,
                  primary=_extract_pdfplumber, secondary=_extract_pypdf,
                  switch_ratio: float = _SWITCH_RATIO,
                  ) -> tuple[dict[int, str], int, str, str]:
    """把 PDF 字节拆成 {页码: 文本}。返回 (页字典, 总页数, 用了哪个解析器, 备注)。

    **pdfplumber 是主解析器，不是回退。** 这一条是拿三份真实公告试出来的：

        文件        pypdf    pdfplumber   pypdf 保留率
        1417 MGO    7,564      19,235        39.3%
        3336 VGO   20,795      20,466       101.6%
        00195 PO    8,418      18,303        46.0%

    在 1417 和 00195 上，pypdf 丢掉的部分包含**全部**关键锚点：
    價值比較、溢價、折讓、要約價、最高現金代價、財務資源、最後交易日。

    最危险的是它丢得「不彻底」—— 剩下的七八千字看起来像正常文本，
    总量远超任何「文本太少就回退」的阈值。三单里有两单会静默产出空抽取，
    而日志上一切正常。这正是铁律二说的静默污染。

    所以现在的策略是：pdfplumber 出结果，同时用 pypdf 复核一遍。
    谁的字符多用谁，并在两者差异显著时留下记录 —— 解析器选择本身也是出处。
    """
    pages: dict[int, str] = {}
    total = 0
    note = ""
    used = ""

    try:
        pages, total = primary(data, max_pages)
        used = "pdfplumber"
    except Exception as exc:
        log.warning("pdfplumber 解析失败：%s", exc)
        note = f"pdfplumber 失败({exc})"

    try:
        alt_pages, alt_total = secondary(data, max_pages)
    except Exception as exc:
        log.debug("pypdf 复核失败（不影响主结果）：%s", exc)
        return pages, total, used or "none", note

    primary_chars, alt_chars = _chars(pages), _chars(alt_pages)

    if alt_chars > primary_chars * switch_ratio or (not primary_chars and alt_chars):
        note = (f"pdfplumber 只取到 {primary_chars:,} 字符，pypdf 取到 {alt_chars:,}，"
                f"已改用 pypdf —— 请人工抽查这份公告")
        log.warning(note)
        return alt_pages, alt_total, "pypdf", note

    if primary_chars and alt_chars < primary_chars * 0.8:
        note = (f"pypdf 仅取到 {alt_chars:,} 字符，为 pdfplumber 的 "
                f"{alt_chars / primary_chars * 100:.0f}%；以 pdfplumber 为准")
        log.debug(note)

    return pages, total, used, note


def open_pdf(url: str, cache_dir: Path, *,
             session: requests.Session | None = None,
             user_agent: str = "hkex-precedent-db/0.1",
             max_pages: int = 0, min_text_chars: int = 500) -> PdfDoc:
    """链接进，带页码的文本出。这是本模块唯一需要调用的函数。"""
    data, from_cache = fetch_bytes(url, cache_dir, session=session,
                                   user_agent=user_agent)
    pages, page_count, extractor, note = extract_pages(data, max_pages)
    has_text = _chars(pages) >= min_text_chars

    if not has_text:
        log.warning("这份 PDF 没有文本层（疑似扫描件），需 OCR 并强制人工复核：%s", url)

    return PdfDoc(url=url, pages=pages, page_count=page_count,
                  has_text_layer=has_text, from_cache=from_cache,
                  extractor=extractor, extractor_note=note)
