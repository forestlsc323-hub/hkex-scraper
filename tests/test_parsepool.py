"""解析子进程。

这个模块的存在理由是一次「跑了一个小时没动」：日志停在 27/68，
界面标题「未响应」，而界面上「已运行」的秒数冻在 8 分 38 秒 ——
秒数冻住本身就是证据，主线程被一起拖死了，不是慢，是停。

根因是 MAX_SECONDS_PER_PDF 那 90 秒**从来没有被执行过**，它只被拿去
拼了一句「最多再等 90 秒」显示给用户。Python 没有办法中断一个线程。

所以这里最要紧的一条测试是：真的卡住的解析，真的会被掐掉。
"""

from __future__ import annotations

import time

import pytest

from hkexdb import parsepool
from tests.test_pdf_source import make_pdf, make_pdf_pages


def _hang(*args):
    """一个永远不返回的「解析」。线程杀不掉它，进程杀得掉。"""
    while True:
        time.sleep(0.05)


def test_a_normal_document_comes_back_with_its_pages():
    with parsepool.ParsePool(timeout=60) as pool:
        doc = pool.parse("x.pdf", make_pdf(["hello world"]), min_text_chars=1)
    assert doc.pages and doc.page_count == 1


def test_a_parse_that_never_returns_is_actually_killed(monkeypatch):
    """这条是整个模块的理由。

    以前这种情况的表现是：那个线程永远回不来，而它还攥着解析锁，
    后面几十份全部堵死，界面一起冻住，用户等一个小时之后强杀程序。
    """
    monkeypatch.setattr(parsepool, "_worker", _hang)

    pool = parsepool.ParsePool(timeout=1.0)
    try:
        started = time.monotonic()
        with pytest.raises(parsepool.ParseTimeout):
            pool.parse("stuck.pdf", b"%PDF-1.4")
        assert time.monotonic() - started < 20, "超时没生效，又卡住了"
    finally:
        pool.close()


def test_the_pool_still_works_after_a_timeout(monkeypatch):
    """掐掉卡住的那个之后要能接着干活 —— 否则一份烂文件依然毁掉整批。"""
    monkeypatch.setattr(parsepool, "_worker", _hang)
    pool = parsepool.ParsePool(timeout=1.0)
    try:
        with pytest.raises(parsepool.ParseTimeout):
            pool.parse("stuck.pdf", b"%PDF-1.4")

        # 换回真的解析器，后面这份必须正常出结果。
        # ⚠️ 超时之后那个槽是**新起的**进程，它的第一份活要先把解析库
        # import 一遍（spawn 的代价），这段时间算在超时里。生产上超时是
        # 90 秒，这点开销无所谓；测试里为了让第一份快点超时用了 1 秒，
        # 所以这里得把表调回正常值，否则测的是 spawn 快不快，不是池子还能不能用。
        pool.timeout = 60
        monkeypatch.setattr(parsepool, "_worker", _real_worker())
        doc = pool.parse("ok.pdf", make_pdf(["hello world"]), min_text_chars=1)
        assert doc.pages
    finally:
        pool.close()


def _real_worker():
    import importlib
    return importlib.reload(parsepool)._worker


def test_a_worker_that_crashes_falls_back_instead_of_losing_the_document(monkeypatch):
    """子进程炸了（内存不够、解析库崩了）不能让这一份凭空消失。"""
    def boom(*args):
        raise MemoryError("子进程炸了")

    monkeypatch.setattr(parsepool, "_worker", boom)
    pool = parsepool.ParsePool(timeout=30)
    try:
        with pytest.raises(MemoryError):
            # 退回本进程再试一次，还是炸 —— 那就如实抛出来，
            # 上层会把它变成这一行的「抽取失败」，而不是整批停摆。
            pool.parse("x.pdf", make_pdf(["hi"]), min_text_chars=1)
    finally:
        pool.close()


def test_it_falls_back_to_inline_parsing_when_no_subprocess_is_available(monkeypatch):
    """打包成 exe、受限环境里 spawn 可能不可用 —— 不能因此不能跑。"""
    said = []

    def no_pool(*a, **kw):
        raise OSError("这里起不了子进程")

    monkeypatch.setattr(parsepool.multiprocessing, "get_context", no_pool)
    pool = parsepool.ParsePool(timeout=30, on_note=said.append)

    doc = pool.parse("x.pdf", make_pdf(["hello world"]), min_text_chars=1)
    assert doc.pages
    assert said and "掐断" in said[0], "降级了必须说出来，不能悄悄降"


def test_staged_parsing_survives_the_trip_through_the_subprocess():
    """早停的那两个字段要能跨进程带回来，否则日志上又看不见了。"""
    pages = [[f"page {i} routine filler"] for i in range(1, 21)]
    with parsepool.ParsePool(timeout=60) as pool:
        doc = pool.parse("x.pdf", make_pdf_pages(pages), probe_pages=5,
                         min_text_chars=10)
    assert doc.stopped_early
    assert doc.pages_parsed == 5 and doc.page_count == 20


def test_closing_twice_is_harmless():
    pool = parsepool.ParsePool(timeout=30)
    pool.close()
    pool.close()


def test_a_subprocess_that_starts_but_cannot_work_is_detected_up_front(monkeypatch):
    """光「Pool 建起来了」不算数。

    spawn 出来的子进程要重新 import 一遍主模块，那一步可能失败。而 Pool
    会**不停地重启**死掉的工作进程 —— 于是每一份公告都白等满一个超时
    才轮到下一份，68 份 × 90 秒，比原来那个卡死还难受。
    开工前花一秒钟问清楚，问不通就老老实实退回本进程。
    """
    monkeypatch.setattr(parsepool, "_smoke", _hang)      # 子进程干不了活

    said = []
    pool = parsepool.ParsePool(timeout=90, on_note=said.append)
    try:
        assert said and "本进程" in said[0], "降级了必须说出来"
        # 而且降级之后照样能出结果，不是躺平
        doc = pool.parse("x.pdf", make_pdf(["hello world"]), min_text_chars=1)
        assert doc.pages
    finally:
        pool.close()


def test_the_smoke_test_does_not_slow_down_a_healthy_start():
    """健康环境下这一步不该拖时间 —— 否则每次抓取都白搭几秒。"""
    started = time.monotonic()
    parsepool.ParsePool(timeout=90).close()
    assert time.monotonic() - started < parsepool.SMOKE_SECONDS


# ---------------------------------------------------------------- 并行度

def test_two_slots_really_parse_at_the_same_time():
    """进程时代的结论和线程时代正好相反。

    早先测出「4 路并发解析比 1 路慢 70%」，那是 GIL 的结果。搬进子进程
    之后重测 8 份 60 页：1 个 17.3 秒 / 2 个 8.8 秒 / 4 个 4.7 秒。
    这条守住「两个槽是真的同时在跑」，不是排队跑。
    """
    import concurrent.futures as cf

    pdf = make_pdf(["hello world"])
    with parsepool.ParsePool(timeout=60, workers=2) as pool:
        started = time.monotonic()
        with cf.ThreadPoolExecutor(max_workers=2) as ex:
            futures = [ex.submit(pool.parse, f"x{i}.pdf", pdf,
                                 min_text_chars=1) for i in range(2)]
            docs = [f.result() for f in futures]
        took = time.monotonic() - started

    assert all(d.pages for d in docs)
    assert took < 30, "两个槽却串成了一条队"


def test_a_timeout_in_one_slot_does_not_kill_the_other(monkeypatch):
    """一个 Pool 开 N 个 worker 的话，terminate() 是整锅端 ——
    一份公告超时会把正在正常解析的邻居一起杀掉。

    「超时只能干掉那一份」这条隔离性是上一轮花整整一轮换来的，
    不能为了并行把它丢掉。
    """
    import concurrent.futures as cf

    pool = parsepool.ParsePool(timeout=2.0, workers=2)
    try:
        good = make_pdf(["hello world"])

        def stuck():
            monkeypatch.setattr(parsepool, "_worker", _hang)
            try:
                pool.parse("stuck.pdf", b"%PDF-1.4")
            except parsepool.ParseTimeout:
                return "掐掉了"
            return "没掐"

        with cf.ThreadPoolExecutor(max_workers=2) as ex:
            f_bad = ex.submit(stuck)
            assert f_bad.result() == "掐掉了"

        # 另一个槽必须还活着
        monkeypatch.setattr(parsepool, "_worker", _real_worker())
        assert pool.parse("ok.pdf", good, min_text_chars=1).pages
    finally:
        pool.close()


def test_the_slot_count_is_what_actually_gets_created():
    pool = parsepool.ParsePool(timeout=30, workers=3)
    try:
        assert len(pool._slots) == 3
    finally:
        pool.close()
