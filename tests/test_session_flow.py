"""端到端会话测试：打一个真的 HTTP 服务器，它没有 cookie 就拒绝服务。

这些测试跑的是**真实的 requests、真实的 TCP、真实的 Set-Cookie / Cookie 往返**，
只是对端换成了本地的假披露易。它们能验证假 session 验证不了的那一环：
建会话 → 服务端下发 cookie → 后续请求带上 cookie。
"""

from __future__ import annotations

import datetime as dt

import pytest

from hkexdb import listing
from hkexdb.config import Config
from hkexdb.http_client import PoliteSession
from hkexdb.storage import RawStore

from fake_hkex_server import SESSION_COOKIE, FakeHKEXServer
from test_listing import _make_config


@pytest.fixture
def cfg(tmp_path) -> Config:
    base = _make_config(tmp_path)
    return Config(**{**base.__dict__,
                     "date_from": dt.date(2026, 1, 5),
                     "date_to": dt.date(2026, 1, 6),
                     "row_range_step": 1000,
                     "cache_enabled": True})


def _point_at(server: FakeHKEXServer, monkeypatch) -> None:
    """把模块级的地址常量指向本地假服务器。"""
    monkeypatch.setattr(listing, "BASE", server.base_url)
    monkeypatch.setattr(listing, "SEARCH_PAGE",
                        f"{server.base_url}/search/titlesearch.xhtml")
    monkeypatch.setattr(listing, "SERVLET",
                        f"{server.base_url}/search/titleSearchServlet.do")


def _session(cfg: Config) -> PoliteSession:
    return PoliteSession(user_agent="test", cache_dir=cfg.cache_dir,
                         min_interval_seconds=0, timeout_seconds=10,
                         max_retries=0, backoff_base_seconds=0,
                         cache_enabled=cfg.cache_enabled)


# ---------------------------------------------------------------- 会话

def test_warm_up_receives_a_cookie(cfg, monkeypatch):
    with FakeHKEXServer({"2026-01-05": 3}) as server:
        _point_at(server, monkeypatch)
        session = _session(cfg)
        listing.warm_up_session(session)
        assert SESSION_COOKIE in {c.name for c in session.session.cookies}


def test_servlet_is_rejected_without_warm_up(cfg, monkeypatch):
    """不建会话直接打 servlet —— 假服务器返回 403，和真站点一样。

    这条测试的意义是证明假服务器**真的在检查 cookie**，
    下一条测试的通过才有说服力。
    """
    with FakeHKEXServer({"2026-01-05": 3}) as server:
        _point_at(server, monkeypatch)
        session = _session(cfg)
        with pytest.raises(Exception):
            listing.fetch_day(session, RawStore(cfg.raw_dir), cfg,
                              dt.date(2026, 1, 5))


def test_full_flow_with_warm_up_succeeds(cfg, monkeypatch):
    """建会话之后，整条链路跑通：cookie → 查询 → 去重 → 落盘。"""
    with FakeHKEXServer({"2026-01-05": 7, "2026-01-06": 4}) as server:
        _point_at(server, monkeypatch)
        session = _session(cfg)
        listing.warm_up_session(session)
        store = RawStore(cfg.raw_dir)

        n1 = listing.fetch_day(session, store, cfg, dt.date(2026, 1, 5))
        n2 = listing.fetch_day(session, store, cfg, dt.date(2026, 1, 6))

        assert (n1, n2) == (7, 4)
        _, rows = store.rebuild_csv()
        assert rows == 11


# ---------------------------------------------------------------- 缓存坑

def test_warm_up_always_hits_the_network(cfg, monkeypatch):
    """**这条守着一个真实踩过的 bug。**

    warm-up 若走缓存，第二次运行时检索页从本地副本返回 ——
    一个 HTTP 请求都没发，cookie 自然没种上，随后 servlet 请求全是裸的。
    第一次跑得好好的，重跑却静默失效。cookie 是会话状态，不可缓存。
    """
    with FakeHKEXServer({"2026-01-05": 2}) as server:
        _point_at(server, monkeypatch)

        listing.warm_up_session(_session(cfg))
        first = server.hits["/search/titlesearch.xhtml"]

        # 换一个全新 session（模拟第二次运行），但用同一个缓存目录
        second_session = _session(cfg)
        listing.warm_up_session(second_session)

        assert server.hits["/search/titlesearch.xhtml"] == first + 1, \
            "第二次 warm-up 被缓存吃掉了 —— cookie 不会种上"
        assert SESSION_COOKIE in {c.name for c in second_session.session.cookies}


def test_rerun_still_works_end_to_end(cfg, monkeypatch):
    """完整重跑：新进程、旧缓存，仍然要能抓到数据。"""
    with FakeHKEXServer({"2026-01-05": 5}) as server:
        _point_at(server, monkeypatch)
        day = dt.date(2026, 1, 5)

        s1 = _session(cfg)
        listing.warm_up_session(s1)
        assert listing.fetch_day(s1, RawStore(cfg.raw_dir), cfg, day) == 5

        # 第二次：换 session、换 store（checkpoint 还在，故走跳过分支）
        s2 = _session(cfg)
        listing.warm_up_session(s2)
        store2 = RawStore(cfg.raw_dir)
        assert store2.is_done(day.isoformat())
        _, rows = store2.rebuild_csv()
        assert rows == 5


# ---------------------------------------------------------------- 翻页

def test_row_range_grows_against_a_real_server(cfg, monkeypatch):
    """真服务器上验证翻页：2500 条、步长 1000 → 三轮。"""
    with FakeHKEXServer({"2026-01-05": 2500}) as server:
        _point_at(server, monkeypatch)
        session = _session(cfg)
        listing.warm_up_session(session)

        n = listing.fetch_day(session, RawStore(cfg.raw_dir), cfg,
                              dt.date(2026, 1, 5))
        assert n == 2500          # 重叠部分已按 NEWS_ID 去重
        assert server.hits["/search/titleSearchServlet.do"] == 3


def test_one_round_when_a_day_fits_in_the_step(cfg, monkeypatch):
    """步长 1000 覆盖典型单日量（600~800 条）→ 一轮取完，无冗余流量。"""
    with FakeHKEXServer({"2026-01-05": 700}) as server:
        _point_at(server, monkeypatch)
        session = _session(cfg)
        listing.warm_up_session(session)
        listing.fetch_day(session, RawStore(cfg.raw_dir), cfg, dt.date(2026, 1, 5))
        assert server.hits["/search/titleSearchServlet.do"] == 1


# ---------------------------------------------------------------- 清洗

def test_html_entities_are_cleaned_on_the_wire(cfg, monkeypatch):
    """假服务器返回的标题带 &amp; 和 tooltip，落盘时应有原样与清洗两份。"""
    import json

    with FakeHKEXServer({"2026-01-05": 1}) as server:
        _point_at(server, monkeypatch)
        session = _session(cfg)
        listing.warm_up_session(session)
        store = RawStore(cfg.raw_dir)
        listing.fetch_day(session, store, cfg, dt.date(2026, 1, 5))

        rec = json.loads(store.rows_path.read_text(encoding="utf-8").strip())
        assert "&amp;" in rec["TITLE"] and "<div" in rec["TITLE"]   # 原样
        assert rec["TITLE_CLEAN"] == "公告0 & 附件"                  # 清洗后


# ---------------------------------------------------------------- PDF

def test_pdf_download_reuses_the_warmed_session(cfg, monkeypatch):
    """PDF 也要 cookie —— 必须复用检索用的那个 session。

    实战客户端的 download_pdf 用的就是 self.session。
    换一个裸 session 会被 403 顶回来。
    """
    from hkexdb import pdf_source

    with FakeHKEXServer({"2026-01-05": 1}) as server:
        _point_at(server, monkeypatch)
        session = _session(cfg)
        listing.warm_up_session(session)

        url = f"{server.base_url}/listedco/listconews/sehk/2026/0105/x.pdf"
        data, _ = pdf_source.fetch_bytes(url, cfg.cache_dir / "pdf",
                                         session=session.session)
        assert data.startswith(b"%PDF")


def test_pdf_download_fails_with_a_bare_session(cfg, monkeypatch):
    """反证：裸 session 拿不到，说明上一条测的是真东西。"""
    import requests

    from hkexdb import pdf_source

    with FakeHKEXServer({"2026-01-05": 1}) as server:
        _point_at(server, monkeypatch)
        url = f"{server.base_url}/listedco/listconews/sehk/2026/0105/x.pdf"
        with pytest.raises(requests.HTTPError):
            pdf_source.fetch_bytes(url, cfg.cache_dir / "pdf",
                                   session=requests.Session())
