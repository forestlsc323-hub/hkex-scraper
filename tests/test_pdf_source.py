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


class FakeResp:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        pass


class FakeSession:
    """记录被请求了几次 —— 用来验证缓存真的挡住了重复请求。"""

    def __init__(self, content: bytes):
        self.content = content
        self.calls: list[str] = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        return FakeResp(self.content)


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
    assert "46" in note or "40" in note or "%" in note      # 记录了分歧


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
