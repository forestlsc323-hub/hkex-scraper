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
import re
import threading
import time
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
    # 实际翻了几页。小于 page_count 就说明前几页看不出要约迹象、
    # 提前收工了 —— 这件事必须能被看见，不能悄悄发生（铁律二）。
    pages_parsed: int = 0
    stopped_early: bool = False

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
    suffix = ".htm" if is_html_link(url) else ".pdf"
    return cache_dir / f"{hashlib.sha256(url.encode()).hexdigest()[:20]}{suffix}"


def is_html_link(url: str) -> bool:
    """披露易的短公告发的是 .htm，不是 PDF。

    实测那一周 15 份留存公告里有 4 份是 .htm，全部抽取失败 ——
    27% 的留存桶就这么没了，而日志上只是一行「不是 PDF」。
    """
    return url.split("?")[0].lower().endswith((".htm", ".html"))


class RateLimiter:
    """跨线程限速。抄你 asso 那份客户端的 `_RateLimiter`，一模一样的做法。

    他在列表层用了它，注释写着「并发的目的是让网络延迟重叠，不是提高
    对服务器的请求频率」；但他自己的 `download_pdf` 绕过了它 ——
    那是他文件里我早就记下的第 9 号 bug。

    我把下载改成 4 路并发时，把这个 bug 一起继承了：56 份公告不限速地
    并发拉，披露易直接掐连接（10054 远程主机强迫关闭了一个现有的连接）。
    所以下载也必须走同一把闸。
    """

    def __init__(self, min_interval: float = 1.0):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def acquire(self) -> None:
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()


# 会被对方掐掉的那几种错，重试往往就好了；其余的直接抛，别白等。
_TRANSIENT = (requests.ConnectionError, requests.Timeout)


class DownloadTooSlow(requests.Timeout):
    """这份公告拖太久了，放弃它，别拖住整批。"""


# 一次下载最多允许占用多少秒（墙上时钟，不是读超时）。
BUDGET_SECONDS = 30.0


def _read_within(resp, budget: float, clock=time.monotonic) -> bytes:
    """边收边看表，超预算就掐断。

    ⚠️ requests 的 timeout=(10, 25) **不是总时长**：
        10 = 建立连接最多等多久
        25 = 两次收到数据之间最多等多久
    服务端只要每 24 秒吐一个字节，读超时就永远不触发，这次请求可以
    拖到天荒地老。我原先说的「3 次 × 25 秒 = 80 秒封顶」是错的 ——
    那只在对方彻底不响应时成立，而「连上了但挤牙膏」恰恰是最常见的那种。

    所以真正的上界只能自己拿秒表卡：流式读，超预算就断开。
    """
    deadline = clock() + budget
    chunks = []
    for chunk in resp.iter_content(65536):
        if chunk:
            chunks.append(chunk)
        if clock() > deadline:
            resp.close()
            raise DownloadTooSlow(
                f"下载超过 {budget:.0f} 秒仍未取完（已收 "
                f"{sum(len(c) for c in chunks) / 1024:.0f} KB）")
    return b"".join(chunks)


def _get_with_retry(session, url, headers, timeout, limiter,
                    attempts: int = 3, backoff: float = 1.5, sleeper=time.sleep,
                    on_retry=None, budget: float = BUDGET_SECONDS):
    """带退避重试、**带墙上时钟预算**的 GET。返回字节。

    偶发网络错误不该让那一单永久丢数据，重试一次通常就回来了；
    但一份公告最多占用 attempts × budget 秒，到点就放弃，
    那一行标成失败，剩下的接着跑 —— 一份烂链接不该拖住整批。

    `on_retry(第几次, 共几次, 异常)` 让上层把重试说出来。
    """
    last = None
    for attempt in range(attempts):
        if limiter is not None:
            limiter.acquire()
        try:
            resp = session.get(url, headers=headers, timeout=timeout,
                               stream=True)
            resp.raise_for_status()
            return _read_within(resp, budget)
        except _TRANSIENT as exc:
            last = exc
            if attempt < attempts - 1:
                if on_retry:
                    on_retry(attempt + 1, attempts, exc)
                log.warning("下载失败第 %d 次，退避后重试：%s", attempt + 1, exc)
                sleeper(backoff * (2 ** attempt))
    raise last


def fetch_bytes(url: str, cache_dir: Path, *,
                session: requests.Session | None = None,
                user_agent: str = "hkex-precedent-db/0.1",
                limiter: "RateLimiter | None" = None,
                timeout: tuple = (10, 25),
                on_retry=None) -> tuple[bytes, bool]:
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
    content = _get_with_retry(session, url, headers, timeout, limiter,
                              on_retry=on_retry)

    if not is_html_link(url) and not content.startswith(b"%PDF"):
        raise ValueError(
            f"这个链接返回的不是 PDF（开头是 {content[:16]!r}）：{url}")

    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    log.info("已取回 %s（%.1f KB）", url, len(content) / 1024)
    return content, False


def discard_cached(url: str, cache_dir: Path) -> bool:
    """删掉某个链接的本地副本。用完即弃模式下由调用方在解析完成后调用。"""
    path = _cache_path(cache_dir, url)
    try:
        path.unlink()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- HTML 公告

# 换页符：披露易的 .htm 公告用它分页，正好对应 PDF 的页码
_PAGE_BREAK = re.compile(r"page-break|pagebreak", re.I)


def extract_html_pages(data: bytes) -> dict[int, str]:
    """把 .htm 公告拆成 {页码: 文本}。

    这些公告本来就是同一份文件的另一种发布格式，正文措辞和 PDF 版
    一模一样，所以抽取层的正则原样适用 —— 只要把标签去干净。
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(data, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    # 有分页标记就按标记分页，没有就整篇算第 1 页。
    # 页码是出处的一部分（铁律三），宁可全算第 1 页，也不能编一个页码。
    chunks, current = [], []
    for element in soup.body.descendants if soup.body else soup.descendants:
        name = getattr(element, "name", None)
        if name in ("hr", "div", "p", "br"):
            klass = " ".join(element.get("class", []) or []) if hasattr(
                element, "get") else ""
            style = element.get("style", "") if hasattr(element, "get") else ""
            if _PAGE_BREAK.search(f"{klass} {style}"):
                chunks.append("".join(current))
                current = []
        if isinstance(element, str):
            current.append(str(element))
    chunks.append("".join(current))

    pages = {}
    for i, chunk in enumerate([c for c in chunks if c.strip()] or [""], 1):
        pages[i] = " ".join(chunk.split())
    return pages


def _extract_pdfplumber(data: bytes, max_pages: int) -> tuple[dict[int, str], int]:
    pages, total, _ = _pdfplumber_staged(data, max_pages)
    return pages, total


def _pdfplumber_staged(data: bytes, max_pages: int, *, probe_pages: int = 0,
                       promising=None) -> tuple[dict[int, str], int, bool]:
    """解析 PDF，可以中途停。返回 (页字典, 总页数, 是否提前停了)。

    先翻前 `probe_pages` 页，交给 `promising` 判断值不值得往下翻；
    判断为不值得就到此为止 —— 一份两百页的文件，只花了十几页的钱。

    页对象解析完就 close()：pdfplumber 默认把每页的字符、线条全缓存在
    内存里，一份两百页的综合文件能吃掉几百兆。用完即弃的是文件，
    页缓存也一样。
    """
    import pdfplumber

    pages: dict[int, str] = {}
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        total = len(pdf.pages)
        limit = min(max_pages, total) if max_pages else total

        def read(upto: int) -> None:
            for i in range(len(pages), upto):
                page = pdf.pages[i]
                pages[i + 1] = page.extract_text() or ""
                page.close()

        if probe_pages and promising is not None and limit > probe_pages:
            read(probe_pages)
            if not promising(pages):
                return pages, total, True

        read(limit)
    return pages, total, False


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

    所以现在的策略是：pdfplumber 出结果，**它看着不对劲时**再用 pypdf
    复核。谁的字符多用谁，并在两者差异显著时留下记录 —— 解析器选择
    本身也是出处。「不对劲」的判据见 _THIN_CHARS_PER_PAGE。
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

    return _cross_check(pages, total, used, note, data, max_pages,
                        secondary, switch_ratio)


# pdfplumber 每页少于这么多字，才值得再跑一遍 pypdf 复核。
#
# 复核不是白来的：它把整份文件再解析一遍。而复核要防的是 pdfplumber
# **失手**（崩掉或吐不出东西），失手的样子就是字数塌下来 ——
# 1417 那单 pdfplumber 正常发挥是 687 字/页，pypdf 的残缺版是 270。
# pdfplumber 已经吐出了一页近千字的正文，再跑一遍 pypdf 只是多花时间：
# 三份实测样本里，复核从来没有翻转过结果。
_THIN_CHARS_PER_PAGE = 300


def _cross_check(pages, total, used, note, data, max_pages,
                 secondary, switch_ratio):
    """pdfplumber 的结果看着不对劲时，用 pypdf 复核一遍。"""
    parsed = len(pages) or 1
    if used and _chars(pages) / parsed >= _THIN_CHARS_PER_PAGE:
        return pages, total, used, note

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
             limiter: "RateLimiter | None" = None, on_retry=None,
             max_pages: int = 0, min_text_chars: int = 500) -> PdfDoc:
    """链接进，带页码的文本出。这是本模块唯一需要调用的函数。

    .htm 和 .pdf 都收 —— 披露易两种格式都在发，短公告发 .htm。
    """
    data, from_cache = fetch_bytes(url, cache_dir, session=session,
                                   user_agent=user_agent, limiter=limiter,
                                   on_retry=on_retry)
    return parse_doc(url, data, from_cache=from_cache, max_pages=max_pages,
                     min_text_chars=min_text_chars)


def parse_doc(url: str, data: bytes, *, from_cache: bool = False,
              max_pages: int = 0, min_text_chars: int = 500,
              probe_pages: int = 0, promising=None) -> PdfDoc:
    """字节 → 带页码的文本。**纯 CPU，不碰网络。**

    和 fetch_bytes 分开是有原因的，实测（12 份真实公告，只算解析）：

        1 路并发解析：12.13 秒
        4 路并发解析：20.56 秒   ← 慢了 70%

    解析是 CPU 型工作，GIL 决定了它没法真并行 —— 多开线程只是把同样的
    活切碎轮流做，总时间不减反增，还把 Tk 主线程一起拖下水（界面卡顿从
    74 毫秒涨到 328 毫秒）。所以下载并发、解析串行：各取所长。

    `probe_pages` + `promising` 打开分段解析：先翻前几页问一句
    「这文件值得翻完吗」，不值得就到此为止。实跑那 26 份新公告里
    有 5/6 的结论是「不像要约公告」—— 而每得出一次这个结论，
    都要先把一份一两百页的文件从头解析到尾，八十多秒。
    """
    if is_html_link(url):
        pages = extract_html_pages(data)
        return PdfDoc(url=url, pages=pages, page_count=len(pages),
                      has_text_layer=_chars(pages) >= min_text_chars,
                      from_cache=from_cache, extractor="html",
                      extractor_note="披露易的 .htm 版公告，正文与 PDF 版一致",
                      pages_parsed=len(pages))

    stopped = False
    if probe_pages and promising is not None:
        try:
            pages, page_count, stopped = _pdfplumber_staged(
                data, max_pages, probe_pages=probe_pages, promising=promising)
            extractor, note = "pdfplumber", ""
            if not stopped:
                pages, page_count, extractor, note = _cross_check(
                    pages, page_count, extractor, note, data, max_pages,
                    _extract_pypdf, _SWITCH_RATIO)
        except Exception as exc:                 # 分段读失败 → 退回整份读
            log.warning("分段解析失败，改为整份解析：%s", exc)
            stopped = False
            pages, page_count, extractor, note = extract_pages(data, max_pages)
    else:
        pages, page_count, extractor, note = extract_pages(data, max_pages)

    # 提前收工的不判扫描件 —— 只翻了十几页就说「这份没文本层」是冤枉它。
    has_text = _chars(pages) >= min_text_chars or stopped

    if not has_text:
        log.warning("这份 PDF 没有文本层（疑似扫描件），需 OCR 并强制人工复核：%s", url)

    if stopped:
        note = (f"前 {len(pages)} 页看不出要约迹象，未再往下解析"
                f"（全文共 {page_count} 页）" + (f"；{note}" if note else ""))
    elif max_pages and page_count > len(pages):
        # 撞上页数上限。同样必须留痕 —— 「缺价值比较」如果是因为它在
        # 第 120 页而我们只看到第 80 页，那和「公告里根本没有」是两回事。
        note = (f"只解析了前 {len(pages)} 页（全文共 {page_count} 页，"
                f"受 config.yaml 里 pdf.max_pages 限制）"
                + (f"；{note}" if note else ""))

    return PdfDoc(url=url, pages=pages, page_count=page_count,
                  has_text_layer=has_text, from_cache=from_cache,
                  extractor=extractor, extractor_note=note,
                  pages_parsed=len(pages), stopped_early=stopped)
