"""「链接进、文本出」这条路径的离线测试。

不联网：requests 会话被替换成假的，返回临场生成的 PDF 字节。
验证的是拿到字节之后的行为 —— 解析、页码、缓存、异常处理。
"""

from __future__ import annotations

import io

import pytest

from hkexdb import pdf_source


def make_pdf(lines: list[str]) -> bytes:
    """造一份带真实文本层的单页 PDF（Helvetica 只认 Latin，故测试用英文）。"""
    ops = "BT /F1 9 Tf 40 780 Td 11 TL\n"
    for line in lines:
        safe = line.replace("(", "").replace(")", "").replace("\\", "")
        ops += f"({safe}) Tj T*\n"
    ops += "ET"
    stream = ops.encode()

    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
              % (len(objs) + 1, xref))
    return out.getvalue()


def make_pdf_pages(pages: list[list[str]]) -> bytes:
    """造一份多页 PDF。分段解析的测试要能数清楚翻了几页。"""
    font_obj = 3 + 2 * len(pages)
    objs: list[bytes] = []
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(b"<< /Type /Pages /Kids [%s] /Count %d >>"
                % (kids.encode(), len(pages)))
    for i, lines in enumerate(pages):
        ops = "BT /F1 9 Tf 40 780 Td 11 TL\n"
        for line in lines:
            safe = line.replace("(", "").replace(")", "").replace("\\", "")
            ops += f"({safe}) Tj T*\n"
        ops += "ET"
        stream = ops.encode()
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                    b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                    % (font_obj, 4 + 2 * i))
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream
                    + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
              % (len(objs) + 1, xref))
    return out.getvalue()


# --------------------------------------------------- 分段解析（先看几页再说）

def _twenty_pages(marker_page: int | None) -> bytes:
    pages = [[f"page {i} routine filler text"] for i in range(1, 21)]
    if marker_page:
        pages[marker_page - 1].append("OFFER PRICE HKD 2.20 per share")
    return make_pdf_pages(pages)


def _has_offer(pages):
    return "OFFER" in "".join(pages.values())


def test_a_document_with_no_offer_sign_in_the_first_pages_stops_there():
    """一两百页的文件，只为了确认它不值得解析而整份解析 —— 那 80 秒
    是实跑里最大的一笔浪费（26 份新公告，前 6 份有 5 份是这个结局）。
    """
    pages, total, stopped = pdf_source._pdfplumber_staged(
        _twenty_pages(None), 0, probe_pages=5, promising=_has_offer)

    assert stopped
    assert total == 20            # 总页数照样如实报出来
    assert len(pages) == 5        # 只翻了 5 页


def test_a_document_that_looks_like_an_offer_gets_parsed_to_the_end():
    pages, total, stopped = pdf_source._pdfplumber_staged(
        _twenty_pages(2), 0, probe_pages=5, promising=_has_offer)

    assert not stopped
    assert len(pages) == total == 20


def test_stopping_early_is_written_into_the_document_not_hidden():
    """铁律二：提前收工是判定的一部分，必须能被看见。"""
    doc = pdf_source.parse_doc("x.pdf", _twenty_pages(None),
                               probe_pages=5, promising=_has_offer,
                               min_text_chars=10)

    assert doc.stopped_early
    assert doc.pages_parsed == 5 and doc.page_count == 20
    assert "20 页" in doc.extractor_note and "5 页" in doc.extractor_note


def test_an_early_stop_is_never_mistaken_for_a_scanned_document():
    """只翻了 5 页就说「这份没有文本层、要 OCR」是冤枉它 ——
    那会把一份正常公告推进人工复核队列。"""
    doc = pdf_source.parse_doc("x.pdf", _twenty_pages(None),
                               probe_pages=5, promising=_has_offer,
                               min_text_chars=100000)
    assert doc.has_text_layer


def test_probe_pages_zero_means_parse_everything():
    doc = pdf_source.parse_doc("x.pdf", _twenty_pages(None), probe_pages=0,
                               promising=_has_offer, min_text_chars=10)
    assert not doc.stopped_early and doc.pages_parsed == 20


def test_a_document_shorter_than_the_probe_is_just_parsed_whole():
    """8 页的文件设 12 页侦察 —— 没有「提前」可言，不该标成早停。"""
    doc = pdf_source.parse_doc(
        "x.pdf", make_pdf_pages([[f"page {i}"] for i in range(1, 9)]),
        probe_pages=12, promising=_has_offer, min_text_chars=10)
    assert not doc.stopped_early and doc.pages_parsed == 8


def test_a_broken_pdf_falls_back_to_whole_document_parsing(monkeypatch):
    """分段读挂了不能连累这一份 —— 退回原来的整份解析。"""
    def boom(*a, **kw):
        raise RuntimeError("分段读崩了")

    monkeypatch.setattr(pdf_source, "_pdfplumber_staged", boom)
    doc = pdf_source.parse_doc("x.pdf", make_pdf(["hello world"]),
                               probe_pages=5, promising=_has_offer,
                               min_text_chars=1)
    assert doc.pages and not doc.stopped_early


class FakeResp:
    """假响应。

    ⚠️ 必须和 requests 的真实接口一致 —— 包括 stream=True 时的
    iter_content 和 close。夹具和真接口对不上，测的就是另一个系统
    （日期格式那次已经吃过一回亏了）。
    """

    def __init__(self, content: bytes, *, chunks: int = 1, on_chunk=None):
        self.content = content
        self._chunks = chunks
        self._on_chunk = on_chunk
        self.closed = False

    def raise_for_status(self):
        pass

    def iter_content(self, size=None):
        step = max(1, len(self.content) // self._chunks) if self.content else 1
        for i in range(0, len(self.content) or 1, step):
            if self._on_chunk:
                self._on_chunk()
            yield self.content[i:i + step]

    def close(self):
        self.closed = True


class FakeSession:
    """记录被请求了几次 —— 用来验证缓存真的挡住了重复请求。"""

    def __init__(self, content: bytes, *, chunks: int = 1, on_chunk=None):
        self.content = content
        self.calls: list[str] = []
        self._chunks = chunks
        self._on_chunk = on_chunk
        self.last: FakeResp | None = None

    def get(self, url, headers=None, timeout=None, stream=False):
        self.calls.append(url)
        self.last = FakeResp(self.content, chunks=self._chunks,
                             on_chunk=self._on_chunk)
        return self.last


VALUE_SECTION = [
    "The Offer Price of HK$2.20 per Offer Share represents a discount of",
    "approximately 15.45% to the average closing price of approximately",
    "HK$2.60 per Share over the 30 trading days up to the Undisturbed Date.",
] * 6


def test_url_in_text_out(tmp_path):
    """核心路径：给一个链接，直接拿到带页码的文本。"""
    session = FakeSession(make_pdf(VALUE_SECTION))
    doc = pdf_source.open_pdf("https://example.test/a.pdf", tmp_path,
                              session=session)

    assert doc.page_count == 1
    assert doc.has_text_layer
    assert not doc.from_cache
    assert "15.45%" in doc.text
    assert set(doc.pages) == {1}          # 页码从 1 起


def test_page_numbers_are_recoverable_for_source_quotes(tmp_path):
    """铁律三要的 page 出处，靠 find()/page_of() 取。"""
    session = FakeSession(make_pdf(VALUE_SECTION))
    doc = pdf_source.open_pdf("https://example.test/b.pdf", tmp_path,
                              session=session)

    hits = doc.find("15.45%")
    assert hits and all(page == 1 for page, _ in hits)
    assert doc.page_of("15.45%") == 1
    assert doc.page_of("这句话不存在") is None    # 找不到就是 None，不猜


def test_second_call_does_not_hit_the_network(tmp_path):
    """同一个链接不重复请求 —— 礼貌爬取，也让重跑幂等。"""
    session = FakeSession(make_pdf(VALUE_SECTION))
    url = "https://example.test/c.pdf"

    first = pdf_source.open_pdf(url, tmp_path, session=session)
    second = pdf_source.open_pdf(url, tmp_path, session=session)

    assert len(session.calls) == 1
    assert first.from_cache is False and second.from_cache is True
    assert first.text == second.text          # 同输入同输出


def test_different_urls_are_cached_separately(tmp_path):
    session = FakeSession(make_pdf(VALUE_SECTION))
    pdf_source.open_pdf("https://example.test/d.pdf", tmp_path, session=session)
    pdf_source.open_pdf("https://example.test/e.pdf", tmp_path, session=session)
    assert len(session.calls) == 2


def test_non_pdf_response_is_rejected_loudly(tmp_path):
    """链接失效时 HKEXnews 会回 HTML 错误页 —— 必须报错，不能当成空公告。

    静默返回空文本会让这单在库里变成「所有字段为空」，
    而不是「这单抓取失败」，两者的后续处理完全不同。
    """
    session = FakeSession(b"<html><body>Page not found</body></html>")
    with pytest.raises(ValueError, match="不是 PDF"):
        pdf_source.open_pdf("https://example.test/gone.pdf", tmp_path,
                            session=session)


def test_scanned_pdf_is_flagged_not_silently_empty(tmp_path):
    """扫描件没有文本层 —— 要标出来，交给 OCR 和人工（附录 C 第 9 项）。"""
    session = FakeSession(make_pdf(["x"]))     # 文本极少，模拟扫描件
    doc = pdf_source.open_pdf("https://example.test/scan.pdf", tmp_path,
                              session=session)
    assert doc.has_text_layer is False
    assert doc.page_count == 1                 # 页数仍然读得出来


# ---------------------------------------------------------------- 解析器选择

def _fake_extractor(pages: dict[int, str]):
    def extract(data, max_pages):
        return pages, len(pages)
    return extract


def _boom(data, max_pages):
    raise RuntimeError("解析器崩了")


RICH = {i: "完整文本 " * 200 for i in range(1, 11)}          # ~2000 字/页
POOR = {i: "残缺 " * 60 for i in range(1, 11)}               # ~40% 的量


def test_pdfplumber_wins_when_pypdf_silently_drops_text():
    """这条测试守着本项目踩过的一个真实大坑。

    pypdf 在 1417 上只取到 39.3%、在 00195 上只取到 46.0% 的文本，
    丢掉的部分包含全部关键锚点（價值比較/溢價/折讓/要約價/財務資源/最後交易日）。
    但它丢得不彻底 —— 剩下七八千字，远超任何「文本太少就回退」的阈值，
    所以旧逻辑（pypdf 优先 + 500 字阈值）会静默产出空抽取而日志一切正常。
    """
    pages, total, used, note = pdf_source.extract_pages(
        b"x", primary=_fake_extractor(RICH), secondary=_fake_extractor(POOR))

    assert used == "pdfplumber"
    assert pages == RICH


def test_a_healthy_pdfplumber_result_is_not_re_parsed_by_pypdf():
    """复核不是白来的 —— 它把整份文件再解析一遍。

    复核要防的是 pdfplumber **失手**，而失手的样子是字数塌下来。
    pdfplumber 已经吐出一页两千字的正文时再跑一遍 pypdf 纯属白花时间：
    三份实测样本里，复核从来没有翻转过结果。
    """
    called = []

    def spy(data, max_pages):
        called.append(1)
        return POOR, 10

    _, _, used, _ = pdf_source.extract_pages(
        b"x", primary=_fake_extractor(RICH), secondary=spy)

    assert used == "pdfplumber"
    assert not called, "pdfplumber 结果健康时不该再跑一遍 pypdf"


def test_a_thin_pdfplumber_result_still_gets_cross_checked():
    """反过来：字数塌了就必须复核，并把分歧记下来。"""
    thin = {i: "残 " * 20 for i in range(1, 11)}            # 40 字/页
    _, _, used, note = pdf_source.extract_pages(
        b"x", primary=_fake_extractor(thin), secondary=_fake_extractor(RICH))

    assert used == "pypdf"
    assert "人工抽查" in note


def test_small_advantage_does_not_flip_the_extractor():
    """3336 那单 pypdf 只多 1.6% —— 噪声级差异不得翻转解析器。

    翻转会让结果随解析库版本漂移，破坏「同输入同输出」。
    """
    slightly_more = {i: "完整文本 " * 203 for i in range(1, 11)}   # +1.5%
    _, _, used, _ = pdf_source.extract_pages(
        b"x", primary=_fake_extractor(RICH),
        secondary=_fake_extractor(slightly_more))
    assert used == "pdfplumber"


def test_pypdf_takes_over_when_pdfplumber_clearly_fails():
    """反过来：pdfplumber 明显失手时要能切过去，并留下警告。"""
    _, _, used, note = pdf_source.extract_pages(
        b"x", primary=_fake_extractor(POOR), secondary=_fake_extractor(RICH))
    assert used == "pypdf"
    assert "人工抽查" in note


def test_pypdf_takes_over_when_pdfplumber_raises():
    pages, _, used, _ = pdf_source.extract_pages(
        b"x", primary=_boom, secondary=_fake_extractor(RICH))
    assert used == "pypdf"
    assert pages == RICH


def test_extractor_choice_is_recorded_as_provenance(tmp_path):
    """铁律三：用了哪个解析器也是出处的一部分，必须留痕。"""
    session = FakeSession(make_pdf(VALUE_SECTION))
    doc = pdf_source.open_pdf("https://example.test/prov.pdf", tmp_path,
                              session=session)
    assert doc.extractor in {"pdfplumber", "pypdf"}


def test_cache_survives_a_new_session_object(tmp_path):
    """副本存在磁盘上，换个进程重跑也还在 —— 原始文件永久保留。"""
    url = "https://example.test/f.pdf"
    pdf_source.open_pdf(url, tmp_path, session=FakeSession(make_pdf(VALUE_SECTION)))

    fresh = FakeSession(b"network should be skipped this time")
    doc = pdf_source.open_pdf(url, tmp_path, session=fresh)
    assert fresh.calls == []
    assert doc.from_cache and "15.45%" in doc.text


# ---------------------------------------------------------------- 下载预算

def test_a_trickling_server_is_cut_off_by_the_wall_clock(tmp_path):
    """requests 的 timeout=(10, 25) 不是总时长。

    10 = 建立连接最多等多久；25 = **两次收到数据之间**最多等多久。
    服务端每 24 秒吐一个字节，读超时永远不触发，这次请求可以拖到
    天荒地老 —— 「85/86 之后一直等」就是这么来的。
    真正的上界只能自己拿秒表卡。
    """
    clock = {"t": 0.0}

    def tick():
        clock["t"] += 5.0            # 每收一块就过去 5 秒

    session = FakeSession(make_pdf(VALUE_SECTION), chunks=20, on_chunk=tick)
    monkey = lambda: clock["t"]      # noqa: E731

    with pytest.raises(pdf_source.DownloadTooSlow) as err:
        pdf_source._read_within(session.get("u", stream=True), budget=12.0,
                                clock=monkey)
    assert "12 秒" in str(err.value)


def test_the_connection_is_closed_when_we_give_up(tmp_path):
    """掐断就要真掐断，不能留着连接白占对方的资源。"""
    clock = {"t": 0.0}
    session = FakeSession(make_pdf(VALUE_SECTION), chunks=20,
                          on_chunk=lambda: clock.__setitem__("t", clock["t"] + 5))
    resp = session.get("u", stream=True)
    with pytest.raises(pdf_source.DownloadTooSlow):
        pdf_source._read_within(resp, budget=8.0, clock=lambda: clock["t"])
    assert resp.closed, "放弃了却没关连接"


def test_a_normal_download_is_not_affected(tmp_path):
    """正常速度的下载一切照旧，预算只是兜底。"""
    session = FakeSession(make_pdf(VALUE_SECTION), chunks=8)
    doc = pdf_source.open_pdf("https://x/ok.pdf", tmp_path, session=session)
    assert doc.pages and doc.has_text_layer


def test_the_budget_is_a_named_constant_not_a_magic_number():
    assert pdf_source.BUDGET_SECONDS > 0
    assert isinstance(pdf_source.BUDGET_SECONDS, float)
