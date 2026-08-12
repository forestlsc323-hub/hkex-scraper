"""流程执行器的测试。

界面（app.py）在这个环境里跑不了（没有 tkinter），所以逻辑全部下沉到
runner.py，界面只剩转发。这些测试覆盖的就是「界面点下去之后真正发生的事」。
"""

from __future__ import annotations

import datetime as dt
import json
import threading

import pytest

from hkexdb import runner


def fake_records(n: int, day: str = "2026-06-01") -> list[dict]:
    """混进真实标题，好让筛查层有东西可判。

    ⚠️ DATE_TIME 一律用披露易**真实的** DD/MM/YYYY 格式。
    夹具当初写成 ISO，结果日期解析的 bug 一个测试都没碰到 ——
    2594 条公告被日期筛子全部丢掉，而 387 个测试全绿。
    夹具的格式必须和真实接口一致，否则测的是另一个系统。
    """
    d = dt.date.fromisoformat(day)
    stamp = f"{d.day:02d}/{d.month:02d}/{d.year}"
    titles = [
        "聯合公告 (1) 完成出售及購買 (2) 作出強制性無條件現金要約 及 (3) 恢復股份買賣",
        "寄發綜合文件",
        "每月最新資料",
        "董事會會議日期",
        "建議以協議安排方式將公司私有化",
    ]
    return [{
        "NEWS_ID": f"n{i}",
        "DATE_TIME": f"{stamp} 08:{i % 60:02d}",
        "STOCK_CODE": f"{i % 9 + 1:05d}",
        "STOCK_NAME": f"公司{i}",
        "TITLE": titles[i % len(titles)],
        "FILE_LINK": f"/x/{i}.pdf",
    } for i in range(n)]


class FakeDoc:
    """假 PDF：直接给页文本，不联网。

    ⚠️ 属性必须和真的 PdfDoc 对齐。夹具比真接口少一个字段，测出来的
    就是另一个系统 —— 日期格式那次（ISO vs DD/MM/YYYY）和 stream=True
    那次都是这么栽的，这已经是第三回了。
    """

    def __init__(self, pages, has_text=True, page_count=None):
        self.pages, self.has_text_layer = pages, has_text
        self.page_count = page_count if page_count is not None else len(pages)
        self.pages_parsed = len(pages)
        self.stopped_early = False
        self.extractor = "fake"
        self.extractor_note = ""
        self.from_cache = False


def fake_open_pdf(url):
    """留存桶里的公告都会走到这里 —— 给一份带完整要约字段的假公告。"""
    return FakeDoc({1: "「要約價」 指 每股要約股份0.519港元",
                    2: "價值比較每股要約價為每股0.519港元，較："
                       "(i) 股份於最後交易日在聯交所所報收市價每股1.870港元折讓約72.25%；"
                       "(ii) 股份於緊接最後交易日（包括該日）前三十(30)個連續交易日"
                       "在聯交所所報平均收市價每股約1.168港元折讓約55.57%。"
                       "最高與最低股價股份在聯交所所報最高收市價為每股1.980港元，"
                       "及股份在聯交所所報最低收市價為每股0.200港元。"
                       "要約人於要約項下須支付的最高現金代價約為5,440萬港元。"})


def _fake_worker(*_args):
    """假解析。**必须是模块级函数** —— 局部 lambda 没法 pickle 给子进程。"""
    page = ("「要約價」 指 每股要約股份0.519港元 "
            "價值比較每股要約價為每股0.519港元，較："
            "(i) 股份於最後交易日在聯交所所報收市價每股1.870港元折讓約72.25%。"
            "要約人於要約項下須支付的最高現金代價約為5,440萬港元。")
    return {"pages": {1: page}, "page_count": 1, "has_text_layer": True,
            "extractor": "fake", "extractor_note": "", "pages_parsed": 1,
            "stopped_early": False}


def make_fetch(records, *, calls=None):
    def fetch(d1, d2, log, on_step, cancel_event):
        if calls is not None:
            calls.append((d1, d2))
        log(f"（假抓取）{d1} ~ {d2}")
        on_step(0, 1.0)
        return records
    return fetch


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """别把测试产物写进仓库。"""
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    (tmp_path / "screening_rules.yaml").write_bytes(
        (runner.Path(__file__).parent.parent / "screening_rules.yaml").read_bytes())
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ---------------------------------------------------------------- 正常路径

def test_full_run_produces_every_artifact(_isolate):
    result = runner.run(dt.date(2026, 6, 1), dt.date(2026, 6, 7),
                        open_pdf=fake_open_pdf, fetch=make_fetch(fake_records(20)))

    assert result.ok
    assert result.fetched == 20
    assert result.screened == 20
    assert result.report_path.exists()
    assert result.diagnostic_path.exists()
    assert result.log_path.exists()
    assert not result.error


def test_buckets_are_reported_back_to_the_ui(_isolate):
    """界面顶部要显示桶分布，所以 Result 必须带回来。"""
    result = runner.run(dt.date(2026, 6, 1), dt.date(2026, 6, 1),
                        open_pdf=fake_open_pdf, fetch=make_fetch(fake_records(20)))
    assert sum(result.buckets.values()) == 20
    assert "retained" in result.buckets and "excluded" in result.buckets


def test_report_html_is_self_contained(_isolate):
    result = runner.run(dt.date(2026, 6, 1), dt.date(2026, 6, 1),
                        open_pdf=fake_open_pdf, fetch=make_fetch(fake_records(10)))
    html = result.report_path.read_text(encoding="utf-8")
    assert "<link" not in html and "cdn." not in html


def test_dates_are_passed_through_untouched(_isolate):
    calls = []
    runner.run(dt.date(2026, 3, 4), dt.date(2026, 3, 9),
               open_pdf=fake_open_pdf, fetch=make_fetch(fake_records(3), calls=calls))
    assert calls == [(dt.date(2026, 3, 4), dt.date(2026, 3, 9))]


# ---------------------------------------------------------------- 回调

def test_log_and_step_callbacks_fire(_isolate):
    logs, steps = [], []
    runner.run(dt.date(2026, 6, 1), dt.date(2026, 6, 1),
               on_log=logs.append, on_step=lambda i, f: steps.append((i, f)),
               open_pdf=fake_open_pdf, fetch=make_fetch(fake_records(5)))

    assert logs, "界面靠 on_log 显示日志，一条都没有就是瞎的"
    assert {i for i, _ in steps} == set(range(len(runner.STEPS))), "每个步骤都要报进度"


def test_step_index_stays_in_range(_isolate):
    """界面拿 index 去索引 STEPS，越界就崩。"""
    steps = []
    runner.run(dt.date(2026, 6, 1), dt.date(2026, 6, 1),
               on_step=lambda i, f: steps.append(i),
               open_pdf=fake_open_pdf, fetch=make_fetch(fake_records(3)))
    assert all(0 <= i < len(runner.STEPS) for i in steps)


def test_a_broken_callback_does_not_kill_the_run(_isolate):
    """界面的回调可能因为窗口已关而抛异常，不能连累抓取。"""
    def boom(*_):
        raise RuntimeError("界面没了")

    result = runner.run(dt.date(2026, 6, 1), dt.date(2026, 6, 1),
                        on_step=boom, open_pdf=fake_open_pdf, fetch=make_fetch(fake_records(3)))
    # on_step 抛异常会被 run() 的兜底捕获，但日志和诊断仍要留下
    assert result.log_path.exists()
    assert result.diagnostic_path is not None


# ---------------------------------------------------------------- 边界

def test_zero_records_skips_downstream_but_still_writes_diagnostic(_isolate):
    """抓到 0 条不是崩溃，但也不能假装成功。"""
    result = runner.run(dt.date(2026, 6, 1), dt.date(2026, 6, 1),
                        open_pdf=fake_open_pdf, fetch=make_fetch([]))
    assert not result.ok
    assert result.fetched == 0
    assert result.report_path is None
    assert result.diagnostic_path.exists()     # 诊断照样要有，否则没法排查


def test_fetch_failure_is_captured_not_raised(_isolate):
    """抓取炸了要变成 Result.error，不能把异常抛到界面线程里。"""
    def boom(*_args):
        raise ConnectionError("连不上披露易")

    result = runner.run(dt.date(2026, 6, 1), dt.date(2026, 6, 1), fetch=boom, open_pdf=fake_open_pdf)
    assert not result.ok
    assert "ConnectionError" in result.error
    assert "连不上披露易" in result.error
    assert result.log_path.exists()
    assert "Traceback" in result.log_path.read_text(encoding="utf-8")


def test_cancel_stops_the_run(_isolate):
    """「停止」按钮：置位后要停下，并且不算成功。"""
    event = threading.Event()
    event.set()

    def fetch(d1, d2, log, on_step, cancel_event):
        if cancel_event.is_set():
            raise runner.Cancelled()
        return fake_records(5)

    result = runner.run(dt.date(2026, 6, 1), dt.date(2026, 6, 1),
                        cancel_event=event, fetch=fetch, open_pdf=fake_open_pdf)
    assert not result.ok
    assert result.error == "用户停止"


# ---------------------------------------------------------------- 诊断文件

def test_diagnostic_contains_what_i_need_to_debug(_isolate):
    result = runner.run(dt.date(2026, 6, 1), dt.date(2026, 6, 1),
                        open_pdf=fake_open_pdf, fetch=make_fetch(fake_records(8)))
    text = result.diagnostic_path.read_text(encoding="utf-8")
    for needle in ("环境", "Python：", "抓到：8 条", "判定桶", "运行日志"):
        assert needle in text, f"诊断文件里缺 {needle}"


def test_diagnostic_written_even_when_fetch_dies(_isolate):
    """失败时的诊断比成功时更重要。"""
    def boom(*_args):
        raise TimeoutError("超时")

    result = runner.run(dt.date(2026, 6, 1), dt.date(2026, 6, 1), fetch=boom, open_pdf=fake_open_pdf)
    assert result.diagnostic_path.exists()
    assert "TimeoutError" in result.diagnostic_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 界面契约

def test_app_module_imports_without_tkinter():
    """app.py 在没有 tkinter 的机器上也要能 import，然后退回命令行。"""
    import importlib
    import app
    importlib.reload(app)
    assert callable(app.main)
    assert callable(app.run_console)


def test_app_reads_default_dates_from_config():
    import app
    d1, d2 = app.default_dates()
    assert dt.date.fromisoformat(d1) <= dt.date.fromisoformat(d2)


# ---------------------------------------------------------------- 启动兜底

def test_crash_guard_writes_a_file_and_never_dies_silently(tmp_path, monkeypatch):
    """双击后「什么都没发生」是最难排查的失败 —— 必须留下痕迹。

    一键运行.bat 曾用 pythonw.exe 启动，它没有控制台：界面若在启动阶段崩掉，
    用户看不到任何东西，我也拿不到线索。现在改用 python.exe 保留控制台，
    并加这层兜底：写文件 + 打控制台 + 尽量弹窗。
    """
    import app
    monkeypatch.setattr(app, "ROOT", tmp_path)
    monkeypatch.setattr(app, "main", lambda: (_ for _ in ()).throw(
        RuntimeError("启动就炸")))

    rc = app._crash_guard()

    assert rc == 1
    crash = tmp_path / "app_crash.txt"
    assert crash.exists(), "崩溃了却没留下任何文件"
    text = crash.read_text(encoding="utf-8")
    assert "RuntimeError" in text and "启动就炸" in text
    assert "发给 Claude" in text


def test_crash_guard_passes_through_success(tmp_path, monkeypatch):
    import app
    monkeypatch.setattr(app, "main", lambda: 0)
    assert app._crash_guard() == 0


def test_launcher_keeps_a_console_for_errors():
    """一键运行.bat 不能用 pythonw.exe —— 那样启动失败就是静默的。"""
    bat = (runner.Path(__file__).parent.parent / "一键运行.bat").read_bytes().decode("utf-8")
    # 只看真正会执行的行 —— 注释里提到 pythonw 是在说明为什么不用它
    live = [ln for ln in bat.splitlines()
            if ln.strip() and not ln.strip().upper().startswith("REM")]
    assert not any("pythonw" in ln for ln in live), \
        "pythonw 没有控制台，启动失败时用户什么都看不到"
    assert any("python.exe app.py" in ln for ln in live)


@pytest.mark.parametrize("name", ["一键运行.bat", "一键更新.bat"])
def test_every_bat_uses_crlf_line_endings(name):
    """LF 换行会让 Windows cmd 解析崩掉，双击后闪一下就关（真踩过）。"""
    raw = (runner.Path(__file__).parent.parent / name).read_bytes()
    assert b"\r\n" in raw
    assert raw.count(b"\n") == raw.count(b"\r\n"), f"{name} 里有裸 LF 换行"


def test_the_update_bat_does_not_download_anything_itself():
    """卡巴斯基把原来那个 .bat 判成 PDM:Trojan.Win32.Generic.nblk 删了。

    它没冤枉：cmd 调 PowerShell 从互联网下载内容再就地覆盖可执行文件，
    正是下载器木马的行为特征。这条守着别再写回去 —— 下载和覆盖都归
    hkexdb\\updater.py，.bat 只负责把它叫起来。

    更新本身的安全性（不碰 data\\、包不对就不写）由 test_updater.py 守。
    """
    text = (runner.Path(__file__).parent.parent / "一键更新.bat"
            ).read_bytes().decode("utf-8")
    # 只看会真正执行的行 —— REM 注释里写着这段历史，那是要留的
    code = "\n".join(ln for ln in text.lower().splitlines()
                     if not ln.strip().startswith("rem"))
    for banned in ("powershell", "invoke-webrequest", "curl", "robocopy",
                   "expand-archive", "codeload"):
        assert banned not in code, f".bat 里又出现了 {banned}，杀软会再删一次"
    assert "hkexdb.updater" in code


# ---------------------------------------------------------------- 速度旋钮

def test_speed_settings_come_from_config_yaml(tmp_path, monkeypatch):
    """步长和并发段数写在 config.yaml，不改 vendor 客户端一个字。"""
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    (tmp_path / "config.yaml").write_text(
        'listing:\n  row_range_step: 4000\n  max_workers: 4\n'
        '  mode: "full"\n  title_keywords: ["要約"]\n', encoding="utf-8")
    assert runner._speed_settings() == (4000, 4, "full", ["要約"])


def test_speed_settings_fall_back_when_config_is_unreadable(tmp_path, monkeypatch):
    """配置坏了要能跑，不能因为一行 YAML 打不开就整个程序起不来。"""
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    step, workers, mode, keywords = runner._speed_settings()
    assert step >= 1000 and workers >= 1
    assert mode in ("keyword", "full") and keywords


def test_row_range_step_is_big_enough_for_a_real_day():
    """实测单日最多 1928 条（2026-06-01）。步长小于它就要多跑一轮，
    而多跑一轮意味着把当天所有记录再拉一遍 —— 平方级浪费。"""
    import yaml
    cfg = yaml.safe_load(
        (runner.Path(__file__).parent.parent / "config.yaml").read_text(
            encoding="utf-8"))
    assert cfg["listing"]["row_range_step"] >= 2000


def test_row_range_step_stays_under_the_server_cap():
    """超过服务端 10000 条上限会被截断，而且翻页失效 —— 静默丢数据。"""
    import yaml
    cfg = yaml.safe_load(
        (runner.Path(__file__).parent.parent / "config.yaml").read_text(
            encoding="utf-8"))
    rounds = cfg["listing"]["max_rounds_per_day"]
    assert cfg["listing"]["row_range_step"] * rounds <= 10000


def test_fetch_pushes_the_knobs_into_the_vendor_client(tmp_path, monkeypatch):
    """vendor/hkex_client.py 一字未改，所以旋钮只能从它的 config 模块覆盖进去。"""
    import sys
    import types

    monkeypatch.setattr(runner, "ROOT", tmp_path)
    (tmp_path / "config.yaml").write_text(
        'listing:\n  row_range_step: 4000\n  max_workers: 4\n  mode: "full"\n', encoding="utf-8")

    fake_config = types.ModuleType("config")
    fake_config.ROW_RANGE_STEP = 500
    seen = {}

    class FakeClient:
        def __init__(self, *a, **kw):
            self.session = types.SimpleNamespace(cookies=[])

        def search(self, d1, d2, *, progress_cb, cancel_event, max_workers):
            seen["workers"] = max_workers
            seen["step"] = fake_config.ROW_RANGE_STEP
            return [{"NEWS_ID": "n1"}]

    fake_client_mod = types.ModuleType("hkex_client")
    fake_client_mod.HKEXClient = FakeClient
    monkeypatch.setitem(sys.modules, "config", fake_config)
    monkeypatch.setitem(sys.modules, "hkex_client", fake_client_mod)

    out = runner._fetch(dt.date(2026, 6, 1), dt.date(2026, 6, 7),
                        lambda *_: None, lambda *_: None, None)
    assert out and seen["step"] == 4000
    assert seen["workers"] == 4


def test_segments_never_exceed_the_number_of_days(tmp_path, monkeypatch):
    """抓 2 天却切 4 段，会白建两个会话（每个会话都要访问一次检索页）。"""
    import sys
    import types

    monkeypatch.setattr(runner, "ROOT", tmp_path)
    (tmp_path / "config.yaml").write_text(
        'listing:\n  row_range_step: 4000\n  max_workers: 4\n  mode: "full"\n', encoding="utf-8")

    fake_config = types.ModuleType("config")
    fake_config.ROW_RANGE_STEP = 500
    seen = {}

    class FakeClient:
        def __init__(self, *a, **kw):
            self.session = types.SimpleNamespace(cookies=[])

        def search(self, d1, d2, *, progress_cb, cancel_event, max_workers):
            seen["workers"] = max_workers
            return []

    mod = types.ModuleType("hkex_client")
    mod.HKEXClient = FakeClient
    monkeypatch.setitem(sys.modules, "config", fake_config)
    monkeypatch.setitem(sys.modules, "hkex_client", mod)

    runner._fetch(dt.date(2026, 6, 1), dt.date(2026, 6, 2),
                  lambda *_: None, lambda *_: None, None)
    assert seen["workers"] == 2


def test_vendor_cancel_is_translated_so_the_ui_says_stopped(tmp_path, monkeypatch):
    """客户端有自己的 CancelledError。不翻译的话，点「停止」会显示成崩溃。"""
    import sys
    import types

    monkeypatch.setattr(runner, "ROOT", tmp_path)

    class VendorCancelled(Exception):
        pass
    VendorCancelled.__name__ = "CancelledError"

    fake_config = types.ModuleType("config")
    fake_config.ROW_RANGE_STEP = 500

    class FakeClient:
        def __init__(self, *a, **kw):
            self.session = types.SimpleNamespace(cookies=[])

        def search(self, *a, **kw):
            raise VendorCancelled("任务已被用户停止")

    mod = types.ModuleType("hkex_client")
    mod.HKEXClient = FakeClient
    monkeypatch.setitem(sys.modules, "config", fake_config)
    monkeypatch.setitem(sys.modules, "hkex_client", mod)

    with pytest.raises(runner.Cancelled):
        runner._fetch(dt.date(2026, 6, 1), dt.date(2026, 6, 2),
                      lambda *_: None, lambda *_: None, None)


# ---------------------------------------------------------------- 关键词模式

def _fake_vendor(monkeypatch, tmp_path, *, by_keyword):
    """造一个假披露易：按 title 参数返回不同的结果。"""
    import sys
    import types

    cfg = types.ModuleType("config")
    cfg.ROW_RANGE_STEP = 4000
    cfg.SLEEP_BETWEEN_REQUESTS = 0
    cfg.REQUEST_TIMEOUT = (1, 1)
    cfg.HKEX_SEARCH_URL = "https://x/titleSearchServlet.do"
    calls = []

    class Resp:
        def __init__(self, payload):
            self._p = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._p

    class Session:
        cookies: list = []

        def get(self, url, params=None, timeout=None):
            calls.append(params)
            recs = by_keyword.get(params["title"], [])
            return Resp({"result": json.dumps(recs) if recs else "null"})

    class FakeClient:
        def __init__(self, *a, **kw):
            self.session = Session()

        @staticmethod
        def _clean(rec):
            return dict(rec)

        def search(self, *a, **kw):
            raise AssertionError("关键词模式不该退回全量")

    mod = types.ModuleType("hkex_client")
    mod.HKEXClient = FakeClient
    monkeypatch.setitem(sys.modules, "config", cfg)
    monkeypatch.setitem(sys.modules, "hkex_client", mod)
    return calls


def test_fast_mode_keywords_cover_every_verified_t0_title():
    """关键词模式是漏检风险最高的一处改动。

    screening_rules.yaml 里那六条标题是人工核实过的真实 T0 ——
    任何一条不含任何关键词，这条路就会静默漏掉那一类公告。
    """
    import yaml
    root = runner.Path(__file__).parent.parent
    rules = yaml.safe_load((root / "screening_rules.yaml").read_text(encoding="utf-8"))
    cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    keywords = cfg["listing"]["title_keywords"]

    for title in rules["t0_title_corpus"]:
        assert any(k in title for k in keywords), \
            f"已核实的真实 T0 一个关键词都没命中，关键词模式会漏掉它：\n  {title[:80]}"


def test_keyword_mode_unions_results_and_dedupes(_isolate, monkeypatch):
    """一条公告可能同时含「要約」和「收購」，不能记两遍。"""
    (_isolate / "config.yaml").write_text(
        'listing:\n  mode: "keyword"\n  title_keywords: ["要約", "收購"]\n',
        encoding="utf-8")
    both = {"NEWS_ID": "a", "DATE_TIME": "2026-06-01 08:00", "TITLE": "收購要約"}
    only = {"NEWS_ID": "b", "DATE_TIME": "2026-06-02 08:00", "TITLE": "收購事項"}
    _fake_vendor(monkeypatch, _isolate,
                 by_keyword={"要約": [both], "收購": [both, only]})

    out = runner._fetch(dt.date(2026, 6, 1), dt.date(2026, 6, 30),
                        lambda *_: None, lambda *_: None, None)
    assert [r["NEWS_ID"] for r in out] == ["b", "a"]      # 按时间倒序，不重复


def test_keyword_mode_queries_by_month_not_by_day(_isolate, monkeypatch):
    """筛过之后一个月才几十条，按天切纯属浪费请求 ——
    这是照抄 asso 那份文件 search_by_category 的做法。"""
    (_isolate / "config.yaml").write_text(
        'listing:\n  mode: "keyword"\n  title_keywords: ["要約"]\n',
        encoding="utf-8")
    rec = {"NEWS_ID": "a", "DATE_TIME": "2026-01-05 08:00", "TITLE": "要約"}
    calls = _fake_vendor(monkeypatch, _isolate, by_keyword={"要約": [rec]})

    runner._fetch(dt.date(2026, 1, 1), dt.date(2026, 3, 31),
                  lambda *_: None, lambda *_: None, None)
    assert len(calls) == 3, "三个月一个关键词就该是 3 次请求"
    assert [c["fromDate"] for c in calls] == ["20260101", "20260201", "20260301"]
    assert [c["toDate"] for c in calls] == ["20260131", "20260228", "20260331"]


def test_keyword_mode_actually_sends_the_keyword(_isolate, monkeypatch):
    """title 传空就是全量 —— 传错了会静默变成把全市场拉回来。"""
    (_isolate / "config.yaml").write_text(
        'listing:\n  mode: "keyword"\n  title_keywords: ["私有化"]\n',
        encoding="utf-8")
    rec = {"NEWS_ID": "a", "DATE_TIME": "2026-06-01 08:00", "TITLE": "私有化"}
    calls = _fake_vendor(monkeypatch, _isolate, by_keyword={"私有化": [rec]})

    runner._fetch(dt.date(2026, 6, 1), dt.date(2026, 6, 30),
                  lambda *_: None, lambda *_: None, None)
    assert calls[0]["title"] == "私有化"
    assert calls[0]["lang"] == "zh" and calls[0]["searchType"] == "0"


def test_a_dead_keyword_falls_back_to_the_full_scan(_isolate, monkeypatch):
    """某个关键词一条都没返回 = 服务端语义和预期不符。

    宁可慢，不可漏 —— 这时候必须退回全量，而不是交出一份少了一半的表。
    """
    import sys
    import types

    (_isolate / "config.yaml").write_text(
        'listing:\n  mode: "keyword"\n  title_keywords: ["要約", "收購"]\n',
        encoding="utf-8")

    cfg = types.ModuleType("config")
    cfg.ROW_RANGE_STEP = 4000
    cfg.SLEEP_BETWEEN_REQUESTS = 0
    cfg.REQUEST_TIMEOUT = (1, 1)
    cfg.HKEX_SEARCH_URL = "https://x/s.do"
    fell_back = []

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"result": "null"}          # 每个关键词都返回 0 条

    class FakeClient:
        def __init__(self, *a, **kw):
            self.session = types.SimpleNamespace(
                cookies=[], get=lambda *a, **k: Resp())

        @staticmethod
        def _clean(rec):
            return dict(rec)

        def search(self, *a, **kw):
            fell_back.append(True)
            return [{"NEWS_ID": "x"}]

    mod = types.ModuleType("hkex_client")
    mod.HKEXClient = FakeClient
    monkeypatch.setitem(sys.modules, "config", cfg)
    monkeypatch.setitem(sys.modules, "hkex_client", mod)

    logs = []
    out = runner._fetch(dt.date(2026, 6, 1), dt.date(2026, 6, 30),
                        logs.append, lambda *_: None, None)
    assert fell_back, "关键词全空却没退回全量 —— 会交出一份漏掉一半的表"
    assert out == [{"NEWS_ID": "x"}]
    assert any("退回全量" in ln for ln in logs), "退回了却不告诉用户"


def test_full_mode_is_still_reachable(_isolate, monkeypatch):
    """config 写 full 就老老实实全量抓，不许偷偷走快的那条。"""
    import sys
    import types

    (_isolate / "config.yaml").write_text(
        'listing:\n  mode: "full"\n', encoding="utf-8")
    cfg = types.ModuleType("config")
    cfg.ROW_RANGE_STEP = 500
    used = []

    class FakeClient:
        def __init__(self, *a, **kw):
            self.session = types.SimpleNamespace(cookies=[])

        def search(self, *a, **kw):
            used.append("full")
            return []

    mod = types.ModuleType("hkex_client")
    mod.HKEXClient = FakeClient
    monkeypatch.setitem(sys.modules, "config", cfg)
    monkeypatch.setitem(sys.modules, "hkex_client", mod)

    runner._fetch(dt.date(2026, 6, 1), dt.date(2026, 6, 7),
                  lambda *_: None, lambda *_: None, None)
    assert used == ["full"]


def test_self_check_flags_a_missed_offer_announcement(_isolate):
    """自检的全部意义：把关键词模式漏掉的、而且筛查层会留下来的，列出来。"""
    offer = {"NEWS_ID": "miss", "DATE_TIME": "2026-06-01 08:00",
             "STOCK_CODE": "01417",
             "TITLE": "聯合公告 - 可能強制性無條件現金要約及恢復買賣"}
    noise = {"NEWS_ID": "n1", "DATE_TIME": "2026-06-01 09:00",
             "STOCK_CODE": "00001", "TITLE": "翌日披露報表"}

    logs = []
    runner.self_check(dt.date(2026, 6, 1), dt.date(2026, 6, 7),
                      on_log=logs.append,
                      fetch_keyword=lambda *a: [noise],
                      fetch_full=lambda *a: [noise, offer])
    text = "\n".join(logs)
    assert "关键词漏掉 1 条" in text
    assert "01417" in text and "改成 full" in text


def test_self_check_says_all_clear_when_only_noise_is_missed(_isolate):
    logs = []
    runner.self_check(dt.date(2026, 6, 1), dt.date(2026, 6, 7),
                      on_log=logs.append,
                      fetch_keyword=lambda *a: [],
                      fetch_full=lambda *a: [
                          {"NEWS_ID": "n1", "DATE_TIME": "2026-06-01 09:00",
                           "STOCK_CODE": "00001", "TITLE": "翌日披露報表"}])
    text = "\n".join(logs)
    assert "可以放心用" in text


def test_self_check_writes_a_file_you_can_send_me(_isolate):
    path = runner.self_check(dt.date(2026, 6, 1), dt.date(2026, 6, 7),
                             fetch_keyword=lambda *a: [],
                             fetch_full=lambda *a: [])
    assert runner.Path(path).exists()


# ---------------------------------------------------------------- 抽取步并发

def test_concurrent_extraction_keeps_the_output_order_stable(_isolate, monkeypatch):
    """并发拿回来的顺序是乱的，但表必须每次长一样 —— 否则同输入不同输出，
    审计链就断了。按提交顺序收结果，不按完成顺序。"""
    monkeypatch.setattr(runner, "_speed_settings",
                        lambda: (4000, 4, "keyword", ["要約"]))
    from hkexdb import screening as S
    rows = [{"row_id": f"r{i}", "date": "2026-06-15", "code": f"{i:05d}",
             "name": f"公司{i}", "title": "作出強制性無條件現金要約",
             "pdf_url": f"/listedco/listconews/sehk/2026/0615/{i}.pdf",
             "verdict": S.Verdict(bucket=S.RETAINED)} for i in range(8)]

    import random
    import time

    def jittery_open(url):
        time.sleep(random.random() * 0.05)      # 故意让完成顺序乱掉
        return fake_open_pdf(url)

    first = runner._extract_deals(rows, lambda *_: None, lambda *_: None,
                                  None, open_pdf=jittery_open)
    second = runner._extract_deals(rows, lambda *_: None, lambda *_: None,
                                   None, open_pdf=jittery_open)
    assert [d.code for d in first] == [f"{i:05d}" for i in range(8)]
    assert [d.code for d in first] == [d.code for d in second]


def test_one_bad_pdf_does_not_kill_the_other_hundred(_isolate, monkeypatch):
    """年初至今有一百多份 PDF，其中一份挂掉不能让整批白跑。"""
    monkeypatch.setattr(runner, "_speed_settings",
                        lambda: (4000, 4, "keyword", ["要約"]))
    from hkexdb import screening as S
    rows = [{"row_id": f"r{i}", "date": "2026-06-15", "code": f"{i:05d}",
             "name": f"公司{i}", "title": "作出強制性無條件現金要約",
             "pdf_url": f"/x/{i}.pdf", "verdict": S.Verdict(bucket=S.RETAINED)}
            for i in range(5)]

    def flaky(url):
        if "/2.pdf" in url:
            raise ValueError("这个链接返回的不是 PDF")
        return fake_open_pdf(url)

    deals = runner._extract_deals(rows, lambda *_: None, lambda *_: None,
                                  None, open_pdf=flaky)
    assert len(deals) == 5
    bad = deals[2]
    assert "抽取失败" in bad.notes and bad.confidence == "low"
    assert sum(1 for d in deals if d.verdict == "offer") == 4


def test_cancelling_mid_extraction_stops_the_batch(_isolate, monkeypatch):
    monkeypatch.setattr(runner, "_speed_settings",
                        lambda: (4000, 2, "keyword", ["要約"]))
    from hkexdb import screening as S
    event = threading.Event()
    event.set()
    rows = [{"row_id": "r0", "date": "2026-06-15", "code": "00001",
             "name": "甲", "title": "要約", "pdf_url": "/x/0.pdf",
             "verdict": S.Verdict(bucket=S.RETAINED)}]
    with pytest.raises(runner.Cancelled):
        runner._extract_deals(rows, lambda *_: None, lambda *_: None,
                              event, open_pdf=fake_open_pdf)


# ---------------------------------------------------------------- 镜像归档

def _mirror_pair():
    """实跑撞上的两组之一：巨騰國際 / 藍思科技 同日同标题各归档一次。"""
    title = ("聯合公告 (1)買賣協議 (2)中信里昂證券代表藍思科技股份有限公司提出"
             "自願性有條件全面現金要約 (4)復牌")
    target = runner.Deal(code="03336", name="巨騰國際", date="2026-05-18",
                         title=title, offeror="藍思科技股份有限公司",
                         verdict="offer", premium_pct="-15.45")
    offeror = runner.Deal(code="06613", name="藍思科技", date="2026-05-18",
                          title=title, offeror="藍思科技股份有限公司",
                          verdict="offer", premium_pct="-15.45")
    return target, offeror


def test_the_offeror_side_of_a_mirror_filing_is_marked_not_counted_twice():
    """两边抽出来的数字一模一样，直接进表就是把同一单记了两遍 ——
    做中位数时这一单的权重凭空翻倍。"""
    target, offeror = _mirror_pair()
    assert runner.mark_mirror_filings([target, offeror]) == 1
    assert offeror.verdict == "mirror"
    assert target.verdict == "offer", "受要约方那一行必须留着"
    assert "坑⑨" in offeror.verdict_reason


def test_the_mirror_row_stays_in_the_table():
    """铁律二：软删除。标出来，不删掉。"""
    target, offeror = _mirror_pair()
    runner.mark_mirror_filings([target, offeror])
    row = runner._deal_row(offeror)
    assert row[0] == "镜像重复"
    assert "藍思科技" in row[1]


def test_two_different_deals_on_the_same_day_are_not_merged():
    """同日两单不同交易，标题不同 —— 绝不能当成镜像。"""
    a = runner.Deal(code="01417", name="浦江中國", date="2026-06-15",
                    title="甲公告", offeror="YOMI.SUN", verdict="offer")
    b = runner.Deal(code="03336", name="巨騰國際", date="2026-06-15",
                    title="乙公告", offeror="藍思科技股份有限公司",
                    verdict="offer")
    assert runner.mark_mirror_filings([a, b]) == 0
    assert a.verdict == b.verdict == "offer"


def test_same_company_two_offers_are_never_merged():
    """同一家公司被不同要约人先后发要约要分开记（绿科×2、金川 MGO+PO）。"""
    a = runner.Deal(code="02362", name="金川國際", date="2026-03-02",
                    title="甲", offeror="ALTERNATIVE LIQUIDITY", verdict="offer")
    b = runner.Deal(code="02362", name="金川國際", date="2026-05-27",
                    title="乙", offeror="別的要约人", verdict="offer")
    assert runner.mark_mirror_filings([a, b]) == 0


def test_an_unidentifiable_mirror_pair_is_left_for_a_human():
    """认不出谁是要约方就两行都留着 —— 手册说这一步不许自动猜受要约方。"""
    title = "聯合公告 全面現金要約"
    a = runner.Deal(code="01111", name="甲公司", date="2026-05-18",
                    title=title, offeror="毫不相干的離岸公司", verdict="offer")
    b = runner.Deal(code="02222", name="乙公司", date="2026-05-18",
                    title=title, offeror="毫不相干的離岸公司", verdict="offer")
    assert runner.mark_mirror_filings([a, b]) == 0
    assert a.verdict == b.verdict == "offer"


def test_parsing_runs_in_parallel_but_never_more_than_configured(
        _isolate, monkeypatch):
    """解析并行度由槽位数说了算 —— 多一个都不行。

    早先这条测试断言的是**相反**的事（「解析必须串行」），依据是实测
    「4 路并发解析比 1 路慢 70%」。那个结论只在线程里成立：CPU 型工作
    受 GIL 限制没法真并行。解析搬进子进程之后没有 GIL 了，重测 8 份
    60 页：1 个进程 17.3 秒 / 2 个 8.8 秒 / 3 个 6.6 秒 / 4 个 4.7 秒。

    同一个结论，换了执行模型就正好反过来 —— 所以断言也跟着反过来，
    但**上界**必须守住：开多少槽就最多几份在解析，不能失控。
    """
    import time as _t

    from hkexdb import parsepool, pdf_source, screening as S

    monkeypatch.setattr(runner, "_speed_settings",
                        lambda: (4000, 4, "keyword", ["要約"]))
    monkeypatch.setattr(runner, "_parse_workers", lambda: 2)

    overlap = {"max": 0, "now": 0}
    guard = threading.Lock()
    real_run = parsepool._Slot.run

    def watched_parse(self, args):
        with guard:
            overlap["now"] += 1
            overlap["max"] = max(overlap["max"], overlap["now"])
        _t.sleep(0.05)
        try:
            return real_run(self, args)
        finally:
            with guard:
                overlap["now"] -= 1

    monkeypatch.setattr(parsepool._Slot, "run", watched_parse)

    page = ("「要約價」 指 每股要約股份0.519港元 "
            "價值比較每股要約價為每股0.519港元，較："
            "(i) 股份於最後交易日在聯交所所報收市價每股1.870港元折讓約72.25%。"
            "要約人於要約項下須支付的最高現金代價約為5,440萬港元。")
    pdf = b"%PDF-1.4 " + page.encode("utf-8")

    def fetch_only(url):
        _t.sleep(0.02)                       # 假装在下载
        return runner._Fetched(url, pdf, False)

    monkeypatch.setattr(parsepool, "_worker", _fake_worker)

    rows = [{"row_id": f"r{i}", "date": "2026-06-15", "code": f"{i:05d}",
             "name": f"公司{i}", "title": "作出強制性無條件現金要約",
             "pdf_url": f"/x/{i}.pdf", "verdict": S.Verdict(bucket=S.RETAINED)}
            for i in range(8)]

    deals = runner._extract_deals(rows, lambda *_: None, lambda *_: None, None,
                                  open_pdf=fetch_only)

    assert len(deals) == 8
    assert overlap["max"] <= 2, \
        f"同时有 {overlap['max']} 份在解析，配的是 2 个槽 —— 并行度失控了"


def test_the_activity_line_says_how_long_each_one_has_waited(_isolate, monkeypatch):
    """并发跑批时最慢的那份必然排最后。消不掉，但要让它可预期：
    还剩几份、分别是谁、各等了多久、最多还要等多久。"""
    from hkexdb import screening as S

    monkeypatch.setattr(runner, "_speed_settings",
                        lambda: (4000, 2, "keyword", ["要約"]))
    notes = []
    rows = [{"row_id": "r0", "date": "2026-06-15", "code": "00318",
             "name": "黃河實業", "title": "作出強制性無條件現金要約",
             "pdf_url": "/x/0.pdf", "verdict": S.Verdict(bucket=S.RETAINED)}]

    runner._extract_deals(rows, lambda *_: None, lambda *_: None, None,
                          open_pdf=fake_open_pdf, on_activity=notes.append)

    text = " ".join(notes)
    assert "还剩" in text and "00318" in text
    assert "等了" in text, "不显示每一份等了多久"
    assert str(runner.MAX_SECONDS_PER_PDF) in text, "不给等待的上界"


# ---------------------------------------------------------------- 同标的封顶

def test_only_the_earliest_few_announcements_per_target_are_opened(_isolate):
    """实跑 71 份里 09638 法拉帝一家占 19 份，全是同一单 PO 的后续公告 ——
    27% 的时间花在下不需要的东西上，而答案表里它只有一单。

    一单交易只有一个 T0，T0 一定是最早那份，所以按日期取最早的几份。
    """
    rows = [{"row_id": f"r{i}", "date": f"2026-0{1 + i // 9}-{1 + i % 9:02d}",
             "code": "09638"} for i in range(19)]
    keep, skipped = runner._cap_per_target(rows)

    assert len(keep) == runner.MAX_PER_TARGET
    assert skipped == {"09638": 19 - runner.MAX_PER_TARGET}
    assert [r["date"] for r in keep] == sorted(r["date"] for r in keep)
    assert keep[0]["date"] == "2026-01-01", "T0 是最早那份，必须留住"


def test_two_different_deals_on_one_company_both_survive_the_cap():
    """金川國際在答案表里有 MGO 和 PO 各一单，相隔近三个月 ——
    封顶取的是最早几份，不能把第二单挤掉。"""
    rows = [{"row_id": "a", "date": "2026-03-02", "code": "02362"},
            {"row_id": "b", "date": "2026-03-05", "code": "02362"},
            {"row_id": "c", "date": "2026-03-26", "code": "02362"},
            {"row_id": "d", "date": "2026-05-27", "code": "02362"}]
    keep, skipped = runner._cap_per_target(rows)
    assert len(keep) == 4 and not skipped


def test_the_cap_is_configurable(_isolate):
    (_isolate / "config.yaml").write_text(
        "listing:\n  max_per_target: 2\n", encoding="utf-8")
    rows = [{"row_id": f"r{i}", "date": f"2026-01-{1 + i:02d}", "code": "09638"}
            for i in range(6)]
    keep, skipped = runner._cap_per_target(rows)
    assert len(keep) == 2 and skipped == {"09638": 4}


def test_different_targets_are_not_capped_against_each_other():
    rows = [{"row_id": f"r{i}", "date": "2026-01-01", "code": f"{i:05d}"}
            for i in range(20)]
    keep, skipped = runner._cap_per_target(rows)
    assert len(keep) == 20 and not skipped


# ---------------------------------------------------------------- 清理临时文件

def test_the_archive_is_never_on_the_delete_list():
    """存档是这个工具的本体 —— 公告列表、抽出来的字段、出处引文都在里面。

    把它列进「可删」名单，就等于给用户一个把几小时抓取成果一键清零的按钮。
    """
    listed = {rel for rel, _, _ in runner.DISPOSABLE}
    assert not any(rel.startswith("data/store") for rel in listed)


def test_clearing_disposable_files_leaves_the_archive_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    (tmp_path / "data" / "store").mkdir(parents=True)
    (tmp_path / "data" / "store" / "listing.csv").write_text("宝贝", encoding="utf-8")
    (tmp_path / "data" / "cache" / "pdf").mkdir(parents=True)
    (tmp_path / "data" / "cache" / "pdf" / "a.pdf").write_bytes(b"x" * 100)

    rows = runner.disposable_report()
    gone, freed = runner.clear_disposable([r[0] for r in rows])

    assert gone == 1 and freed == 100
    assert (tmp_path / "data" / "store" / "listing.csv").exists()


def test_a_path_not_on_the_list_is_refused(tmp_path, monkeypatch):
    """名单外的路径一律忽略 —— 免得哪天一个笔误把 data/store 端了。"""
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    (tmp_path / "data" / "store").mkdir(parents=True)
    (tmp_path / "data" / "store" / "deals.csv").write_text("x", encoding="utf-8")

    assert runner.clear_disposable(["data/store", "data", "."]) == (0, 0)
    assert (tmp_path / "data" / "store" / "deals.csv").exists()


def test_each_file_is_only_counted_once(tmp_path, monkeypatch):
    """data/cache/pdf 套在 data/cache 里面，两条都在名单上 ——
    不去重的话报出来的占用会翻倍，用户以为多出一倍垃圾。"""
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    (tmp_path / "data" / "cache" / "pdf").mkdir(parents=True)
    (tmp_path / "data" / "cache" / "pdf" / "a.pdf").write_bytes(b"x" * 500)

    assert sum(size for _, _, _, size, _ in runner.disposable_report()) == 500


def test_a_mirror_pair_is_caught_even_when_the_titles_differ():
    """同日同标题这个键太脆。

    实跑年初至今，表里明明有两组镜像（東曜/藥明合聯、巨騰/藍思），
    只标出来一组 —— 双方各自归档时标题可能差一个字（代号后缀、
    括号里的英文名），日期也可能差一天。

    「要约价、交易规模、要约方三样完全相同」是比标题强得多的证据：
    两行讲的就是同一单。
    """
    target = runner.Deal(code="01875", name="東曜藥業－Ｂ", date="2026-06-02",
                         title="聯合公告 自願性有條件全面現金要約",
                         offeror="藥明合聯生物技術有限公司* (WuXi XDC Cayman Inc.)",
                         offer_price="4.00", deal_size="2790300000",
                         verdict="offer")
    offeror = runner.Deal(code="02268", name="藥明合聯", date="2026-06-03",
                          title="聯合公告 自願性有條件全面現金要約 及 恢復買賣",
                          offeror="藥明合聯生物技術有限公司* (WuXi XDC Cayman Inc.)",
                          offer_price="4.00", deal_size="2790300000",
                          verdict="offer")

    assert runner.mark_mirror_filings([target, offeror]) == 1
    assert offeror.verdict == "mirror"
    assert target.verdict == "offer", "受要约方那一行必须留着"


def test_identical_numbers_alone_do_not_merge_two_unrelated_companies():
    """数字撞车但两家都不是要约方 —— 谁也不能标，交人工。

    手册说这一步不许自动猜受要约方。
    """
    a = runner.Deal(code="01417", name="浦江中國", date="2026-06-15", title="甲",
                    offeror="第三方控股有限公司", offer_price="1.00",
                    deal_size="100000000", verdict="offer")
    b = runner.Deal(code="03336", name="巨騰國際", date="2026-06-15", title="乙",
                    offeror="第三方控股有限公司", offer_price="1.00",
                    deal_size="100000000", verdict="offer")
    assert runner.mark_mirror_filings([a, b]) == 0


def test_a_mirror_row_is_only_marked_once():
    """两个键都能命中同一行时，不能重复计数 —— 日志上的数字会对不上。"""
    target, offeror = _mirror_pair()
    target.offer_price = offeror.offer_price = "2.20"
    target.deal_size = offeror.deal_size = "1905849908.60"
    assert runner.mark_mirror_filings([target, offeror]) == 1


def test_a_document_that_hangs_the_parser_does_not_hang_the_whole_batch(
        _isolate, monkeypatch):
    """一份卡死的公告只能毁掉它自己那一行。

    实跑撞上过：日志停在 27/68，界面标题「未响应」，界面上「已运行」
    的秒数冻在 8 分 38 秒 —— 秒数冻住本身就是证据，主线程被一起拖死了。
    用户等了一个小时才强杀程序。

    根因是那 90 秒从来没被执行过（只拿去拼了一句话给用户看），
    而 Python 没有办法中断一个线程。
    """
    from hkexdb import parsepool, screening as S

    monkeypatch.setattr(runner, "_speed_settings",
                        lambda: (4000, 2, "keyword", ["要約"]))

    page = ("「要約價」 指 每股要約股份0.519港元 "
            "價值比較每股要約價為每股0.519港元，較："
            "(i) 股份於最後交易日在聯交所所報收市價每股1.870港元折讓約72.25%。"
            "要約人於要約項下須支付的最高現金代價約為5,440萬港元。")
    good = {"pages": {1: page}, "page_count": 1, "has_text_layer": True,
            "extractor": "fake", "extractor_note": "", "pages_parsed": 1,
            "stopped_early": False}

    def parse(self, url, data, **kw):
        if "/stuck" in url:
            raise parsepool.ParseTimeout("解析超过 90 秒，已掐断")
        from hkexdb.pdf_source import PdfDoc
        return PdfDoc(url=url, from_cache=False, **good)

    monkeypatch.setattr(parsepool.ParsePool, "parse", parse)

    rows = [{"row_id": f"r{i}", "date": "2026-06-15", "code": f"{i:05d}",
             "name": f"公司{i}", "title": "作出強制性無條件現金要約",
             "pdf_url": "/stuck.pdf" if i == 2 else f"/x/{i}.pdf",
             "verdict": S.Verdict(bucket=S.RETAINED)} for i in range(6)]

    deals = runner._extract_deals(
        rows, lambda *_: None, lambda *_: None, None,
        open_pdf=lambda url: runner._Fetched(url, b"%PDF-1.4", False))

    assert len(deals) == 6, "卡住一份就少一批 —— 那正是要修的东西"
    assert sum(1 for d in deals if d.verdict == "offer") == 5
    stuck = deals[2]
    assert "解析超时" in stuck.verdict_reason
    assert "手工打开原文" in stuck.verdict_reason, "得告诉人下一步该干嘛"


def test_the_ninety_seconds_we_promise_is_the_one_we_enforce(_isolate):
    """界面上写「最多再等 90 秒」，就必须真的最多 90 秒。

    这句话曾经是假的：那个常数只被拿去拼字符串，没有任何地方执行它。
    对用户说了一个做不到的保证，比不说更坏。
    """
    import inspect

    from hkexdb import parsepool

    src = inspect.getsource(runner._extract_deals)
    assert "ParsePool" in src, "解析必须交给能被掐断的子进程"
    assert runner._parse_timeout() == runner.MAX_SECONDS_PER_PDF
    # 子进程真的会用上这个数字
    assert "timeout" in inspect.signature(parsepool.ParsePool).parameters


def test_two_deals_years_apart_on_one_company_both_get_opened():
    """跨年份抓取时这是**必需**的，不是优化。

    原来按股票代码在整个日期范围里数上限，抓一年看不出问题；一旦抓
    2024~2026，一家公司 2024 年那单的四份公告会把名额全占掉，
    2026 年那单一份都打不开 —— 而日志上只显示「跳过 N 份」，
    看不出丢掉的是一整单交易。
    """
    rows = ([{"row_id": f"a{i}", "code": "00195", "date": f"2024-03-{i + 1:02d}"}
             for i in range(6)]
            + [{"row_id": f"b{i}", "code": "00195", "date": f"2026-05-{i + 1:02d}"}
               for i in range(6)])

    keep, skipped = runner._cap_per_target(rows)

    years = {r["date"][:4] for r in keep}
    assert years == {"2024", "2026"}, "有一整单交易被名额挤掉了"
    assert len([r for r in keep if r["date"].startswith("2024")]) == 4
    assert len([r for r in keep if r["date"].startswith("2026")]) == 4
    assert skipped == {"00195": 4}


def test_the_same_deals_two_offers_a_quarter_apart_are_separated():
    """02362 金川：2026-03 一单 MGO，2026-05 一单 PO，隔 86 天。

    用户自己的手册写着「同一家公司被不同要约人先后发要约要分开记」。
    """
    rows = ([{"row_id": f"m{i}", "code": "02362", "date": f"2026-03-{i + 2:02d}"}
             for i in range(5)]
            + [{"row_id": f"p{i}", "code": "02362", "date": f"2026-05-{i + 27:02d}"}
               for i in range(2)])

    keep, _ = runner._cap_per_target(rows)

    assert len([r for r in keep if r["date"].startswith("2026-05")]) == 2, \
        "后面那单 PO 被前面那单的名额挤掉了"


def test_follow_ups_inside_one_deal_are_still_capped():
    """别为了修跨年份把原来的收益丢了 —— 09638 那 19 份仍然只开最早 4 份。"""
    rows = [{"row_id": f"r{i}", "code": "09638",
             "date": f"2026-03-{i % 28 + 1:02d}"} for i in range(19)]
    keep, skipped = runner._cap_per_target(rows)
    assert len(keep) == 4 and skipped == {"09638": 15}


def test_the_gap_that_starts_a_new_deal_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    (tmp_path / "config.yaml").write_text(
        "listing:\n  max_per_target: 2\n  new_deal_gap_days: 5\n", encoding="utf-8")

    rows = [{"row_id": "a", "code": "00001", "date": "2026-01-01"},
            {"row_id": "b", "code": "00001", "date": "2026-01-02"},
            {"row_id": "c", "code": "00001", "date": "2026-01-03"},
            {"row_id": "d", "code": "00001", "date": "2026-02-20"}]

    keep, _ = runner._cap_per_target(rows)
    assert [r["row_id"] for r in keep] == ["a", "b", "d"]


def test_rows_without_a_usable_date_are_not_dropped():
    """没有日期的照样要进抽取 —— 静默丢行是这一层最不该犯的错。"""
    rows = [{"row_id": "x", "code": "00001", "date": ""},
            {"row_id": "y", "code": "00001", "date": "不是日期"}]
    keep, _ = runner._cap_per_target(rows)
    assert len(keep) == 2


def test_a_chunk_that_hits_the_row_limit_is_split_and_re_queried(
        monkeypatch, tmp_path, _isolate):
    """一个 (月, 关键词) 只发一个请求、rowRange=500、不翻页。

    某个月超过 500 条，多出来的一声不响就没了 —— 而**日志上一切正常**：
    那一段照样显示「返回 500 条」，没人看得出后面还有。抓一年碰不上，
    抓 2024~2026 迟早撞上。用户要的是任意年份都能爬，所以这是必修的。
    """
    import sys
    import types

    cap = 4
    cfg = types.ModuleType("config")
    cfg.ROW_RANGE_STEP = cap
    cfg.SLEEP_BETWEEN_REQUESTS = 0
    cfg.REQUEST_TIMEOUT = (1, 1)
    cfg.HKEX_SEARCH_URL = "https://x/titleSearchServlet.do"

    # 6 月 1~10 号每天两条。整月一次问只能拿回 4 条（顶到上限）。
    everything = [
        {"NEWS_ID": f"n{d}{i}", "DATE_TIME": f"{d:02d}/06/2026 08:00",
         "STOCK_CODE": "00001", "STOCK_NAME": "公司", "TITLE": "作出要約",
         "FILE_LINK": f"/x/{d}{i}.pdf"}
        for d in range(1, 11) for i in range(2)]

    def within(a, b):
        lo, hi = int(a), int(b)
        out = []
        for rec in everything:
            d, m, y = rec["DATE_TIME"].split()[0].split("/")
            if lo <= int(f"{y}{m}{d}") <= hi:
                out.append(rec)
        return out

    calls = []

    class Resp:
        def __init__(self, p):
            self._p = p

        def raise_for_status(self):
            return None

        def json(self):
            return self._p

    class Session:
        cookies: list = []

        def get(self, url, params=None, timeout=None):
            calls.append((params["fromDate"], params["toDate"],
                          int(params["rowRange"])))
            all_hit = within(params["fromDate"], params["toDate"])
            room = int(params["rowRange"])
            hit = all_hit[:room]
            # ⚠️ 真接口就是这么答的：hasNextRow 说明还有没取完的。
            # 夹具不还原这个字段，测出来的就是另一个系统。
            return Resp({"result": json.dumps(hit) if hit else "null",
                         "hasNextRow": len(all_hit) > room,
                         "loadedRecord": len(hit)})

    class FakeClient:
        def __init__(self, *a, **kw):
            self.session = Session()

        @staticmethod
        def _clean(rec):
            return dict(rec)

    mod = types.ModuleType("hkex_client")
    mod.HKEXClient = FakeClient
    monkeypatch.setitem(sys.modules, "config", cfg)
    monkeypatch.setitem(sys.modules, "hkex_client", mod)
    monkeypatch.setattr(runner, "_speed_settings",
                        lambda: (cap, 1, "keyword", ["要約"]))

    got = runner._fetch(dt.date(2026, 6, 1), dt.date(2026, 6, 30),
                        lambda *_: None, lambda *_: None, None)

    assert len(got) == 20, f"没翻完页，只拿回 {len(got)} 条"
    assert len(calls) > 1, "接口说还有下一页，却没有再问一次"
    assert max(c[2] for c in calls) > cap, "翻页靠把 rowRange 调大，没调"


def test_a_single_day_over_the_server_hard_cap_is_reported_not_swallowed(
        monkeypatch, _isolate):
    """劈到单日还是取不完，那就是服务端硬顶（10000 条）—— 劈不动了。

    这时唯一能做对的事是**说出来**。悄悄返回一份不完整的数据，
    正是铁律二说的静默污染。
    """
    import sys
    import types

    cfg = types.ModuleType("config")
    cfg.ROW_RANGE_STEP = 5000
    cfg.SLEEP_BETWEEN_REQUESTS = 0
    cfg.REQUEST_TIMEOUT = (1, 1)
    cfg.HKEX_SEARCH_URL = "https://x/titleSearchServlet.do"

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            # 永远说「还有下一页」—— 模拟撞上硬顶。
            # 得真返回点东西，否则关键词模式会以为服务端语义不对而退回全量。
            rec = [{"NEWS_ID": "n1", "DATE_TIME": "01/06/2026 08:00",
                    "STOCK_CODE": "00001", "STOCK_NAME": "公司",
                    "TITLE": "作出要約", "FILE_LINK": "/x/1.pdf"}]
            return {"result": json.dumps(rec), "hasNextRow": True,
                    "loadedRecord": 10000}

    class Session:
        cookies: list = []

        def get(self, url, params=None, timeout=None):
            return Resp()

    class FakeClient:
        def __init__(self, *a, **kw):
            self.session = Session()

        @staticmethod
        def _clean(rec):
            return dict(rec)

    mod = types.ModuleType("hkex_client")
    mod.HKEXClient = FakeClient
    monkeypatch.setitem(sys.modules, "config", cfg)
    monkeypatch.setitem(sys.modules, "hkex_client", mod)
    monkeypatch.setattr(runner, "_speed_settings",
                        lambda: (5000, 1, "keyword", ["要約"]))

    said = []
    runner._fetch(dt.date(2026, 6, 1), dt.date(2026, 6, 2),
                  said.append, lambda *_: None, None)

    assert any("单日就超过服务端硬顶" in s for s in said), \
        "取不全却不吭声 —— 这正是最该避免的那种失败"


# ---------------------------------------------------------------- 原文摘录

def test_the_excerpt_keeps_the_sentences_that_matter_and_drops_the_rest():
    """一份公告两三万字，贴过来没人看得完。真正有用的就是
    「較最後交易日收市價每股 X 港元折讓約 Y%」那几句。

    这个功能存在的理由：我手上真实公告正文只有 1,805 字，而一次实跑
    要读一百多万字 —— 54 条正则全是从那点样本反推的，覆盖不到的
    措辞就抽不出来。溢价率那 9 个空白全是这么来的。
    """
    pages = {
        1: "封面。本公司董事會欣然宣佈。" + "无关内容。" * 200,
        2: "無關的一頁。" * 300,
        3: "價值比較每股要約價0.519港元，較最後交易日收市價每股1.870港元"
           "折讓約72.25%。",
    }
    text = runner.wording_excerpt(pages)

    assert "折讓約72.25%" in text and "第 3 页" in text
    assert "無關的一頁" not in text, "把没用的整页也抄进来了"
    assert len(text) < 2000, "摘录应该短到能直接贴，不是搬运全文"


def test_overlapping_windows_are_merged_not_repeated():
    """同一段里挤着好几个锚点，不能把这段抄好几遍。"""
    pages = {1: "價值比較 要約價 溢價 折讓 最後交易日 都挤在这一句里。"}
    text = runner.wording_excerpt(pages)
    assert text.count("都挤在这一句里") == 1


def test_a_document_with_no_anchor_says_so_instead_of_dumping_everything():
    text = runner.wording_excerpt({1: "董事會會議召開日期。" * 50})
    assert "一个锚点都没出现" in text


def test_the_excerpt_is_capped_so_one_document_cannot_flood_the_file():
    pages = {i: f"第{i}段 價值比較 溢價 折讓" for i in range(1, 40)}
    text = runner.wording_excerpt(pages, limit=5)
    assert text.count("—— 第") <= 5
    assert "已截断" in text


def test_export_writes_a_file_even_when_a_download_fails(_isolate, monkeypatch):
    """取不回来的那一行要如实写进文件，不能让整个导出失败。"""
    from hkexdb import pdf_source

    def boom(url, cache, **kw):
        raise ConnectionError("连不上")

    monkeypatch.setattr(pdf_source, "fetch_bytes", boom)
    path = runner.export_wording(
        [{"股票代码": "00195", "受要约方": "綠科", "公告日期": "2026-05-29",
          "PDF链接": "https://x/1.pdf"}])

    text = path.read_text(encoding="utf-8")
    assert "00195" in text and "取不回来" in text


def test_a_row_without_a_link_is_skipped_not_crashed(_isolate):
    path = runner.export_wording([{"股票代码": "00001", "PDF链接": ""}])
    assert "没有 PDF 链接" in path.read_text(encoding="utf-8")


# ------------------------------------------------ 同一单交易的两份文件

def test_the_composite_document_does_not_double_count_the_deal():
    """一单要约通常至少两份文件写着完整字段：先出联合公告，几周后出
    综合文件。两份的要约价、溢价、规模一模一样。

    实跑 2025 全年撞上一批 —— 02442 怡俊集團 24 页的公告和 80 页的
    综合文件在表里各占一行，用户一眼就看出来了。直接进表就是把同一单
    记了两遍，做中位数时这一单的权重凭空翻倍。
    """
    early = runner.Deal(code="02442", name="怡俊集團控股", date="2025-12-02",
                        offer_price="0.7517", deal_size="76673400",
                        premium_pct="-77.80", verdict="offer", news_id="a")
    late = runner.Deal(code="02442", name="怡俊集團控股", date="2025-12-30",
                       offer_price="0.7517", deal_size="76673400",
                       premium_pct="-77.80", verdict="offer", news_id="b")

    assert runner.mark_duplicate_filings([late, early]) == 1
    assert early.verdict == "offer", "T0 那份要留着"
    assert late.verdict == "duplicate"
    assert "同一单" in late.verdict_reason


def test_the_duplicate_row_stays_in_the_table():
    """铁律二：软删除。综合文件里有公告没有的东西（时间表、独立意见），
    人可能正想看它。"""
    a = runner.Deal(code="02442", date="2025-12-02", offer_price="0.75",
                    deal_size="1", premium_pct="-1", verdict="offer")
    b = runner.Deal(code="02442", date="2025-12-30", offer_price="0.75",
                    deal_size="1", premium_pct="-1", verdict="offer")
    runner.mark_duplicate_filings([a, b])
    assert runner._deal_row(b)[0] == "同单重复"


def test_two_genuinely_different_deals_are_not_merged():
    """同一家公司先后两单要约，价钱不同 —— 绝不能合并。"""
    a = runner.Deal(code="02362", date="2026-03-02", offer_price="0.01",
                    deal_size="7000000", premium_pct="-98", verdict="offer")
    b = runner.Deal(code="02362", date="2026-05-27", offer_price="0.02",
                    deal_size="39000000", premium_pct="-51", verdict="offer")
    assert runner.mark_duplicate_filings([a, b]) == 0


def test_rows_that_are_not_offers_are_left_alone():
    """非要约那些行本来就不进统计，别去动它们。"""
    a = runner.Deal(code="00195", date="2025-01-14", verdict="not_offer")
    b = runner.Deal(code="00195", date="2025-01-20", verdict="not_offer")
    assert runner.mark_duplicate_filings([a, b]) == 0


def test_rows_without_a_code_are_never_merged():
    """披露易没给代码的那两条，谁也不知道是不是同一单。"""
    a = runner.Deal(code="", date="2025-01-14", offer_price="1",
                    deal_size="1", premium_pct="1", verdict="offer")
    b = runner.Deal(code="", date="2025-01-20", offer_price="1",
                    deal_size="1", premium_pct="1", verdict="offer")
    assert runner.mark_duplicate_filings([a, b]) == 0


def test_two_filings_of_one_deal_fill_in_each_others_blanks():
    """同一单的两份文件信息互补，不该在表里留一个本来有答案的空格。

    实跑 2025：00372 保德那单，公告那份抽到溢价 -2.23%（正是答案），
    综合文件那份溢价是空的。原来的去重键里带着溢价率，一空一有就配不上
    —— 既没合并，打分时还可能配到空的那一行去，白丢一分。
    """
    early = runner.Deal(code="00372", date="2025-04-16", offer_price="0.175",
                        deal_size="37544924.20", premium_pct="",
                        offeror="", verdict="offer", news_id="a")
    late = runner.Deal(code="00372", date="2025-04-24", offer_price="0.175",
                       deal_size="37544924.20", premium_pct="-2.23",
                       offeror="MARCHING GREAT LIMITED", verdict="offer",
                       news_id="b")

    assert runner.mark_duplicate_filings([early, late]) == 1
    assert early.premium_pct == "-2.23", "另一份有答案，却留了个空格"
    assert early.offeror == "MARCHING GREAT LIMITED"
    assert "另一份文件" in early.notes, "补了字段必须说出来"


def test_a_field_the_keeper_already_has_is_never_overwritten():
    """留下的那份自己有值就以它为准 —— T0 才是这单的准星。"""
    early = runner.Deal(code="00372", date="2025-04-16", offer_price="0.175",
                        deal_size="1", premium_pct="-2.23", verdict="offer")
    late = runner.Deal(code="00372", date="2025-04-24", offer_price="0.175",
                       deal_size="1", premium_pct="-9.99", verdict="offer")
    runner.mark_duplicate_filings([early, late])
    assert early.premium_pct == "-2.23"


def test_the_premium_ladder_comes_along_too():
    early = runner.Deal(code="00372", date="2025-04-16", offer_price="0.175",
                        deal_size="1", verdict="offer")
    late = runner.Deal(code="00372", date="2025-04-24", offer_price="0.175",
                       deal_size="1", verdict="offer",
                       premium_ladder={"最后交易日收市价": "-2.23"})
    runner.mark_duplicate_filings([early, late])
    assert early.premium_ladder == {"最后交易日收市价": "-2.23"}


def test_rows_without_an_offer_price_are_never_merged():
    """两行都没抽到要约价时，「同代码同规模」不足以断定是同一单。"""
    a = runner.Deal(code="00195", date="2025-01-14", offer_price="",
                    deal_size="", verdict="offer")
    b = runner.Deal(code="00195", date="2025-06-20", offer_price="",
                    deal_size="", verdict="offer")
    assert runner.mark_duplicate_filings([a, b]) == 0


# ------------------------------------------- 主值溢价率的符号：算术说了算还是标出来

class _Pick:
    """⚠️ 字段要和 selectors.PremiumPick 一致 —— 少一个就测不出真问题。"""

    def __init__(self, label, benchmark, stated_pct, direction):
        self.label, self.benchmark = label, benchmark
        self.stated_pct, self.stated_direction = stated_pct, direction


class _Ex:
    def __init__(self, offer_price):
        self.offer_price = offer_price


def _deal_with(premium: str):
    d = runner.Deal(date="2025-01-02", code="00001", name="某某")
    d.premium_pct, d.premium_basis = premium, "最后交易日前30日均价"
    d.confidence, d.notes = "high", ""
    return d


def test_a_typo_in_the_wording_is_corrected_by_the_announcements_own_numbers():
    """要约价 1.20 高于基准 1.00，公告却写「折让 20%」——
    百分比复算得上，说明错的只是那两个字，符号按算术改。"""
    deal = _deal_with("-20.00")
    runner._reconcile_premium_direction(
        deal, _Pick("最后交易日前30日均价", "1.00", "20.00", "discount"),
        _Ex("1.20"))
    assert deal.premium_pct == "20.00"
    assert "纠正" in deal.notes
    assert deal.confidence == "high"       # 数字自洽，没有降级的理由


def test_a_mismatched_benchmark_is_flagged_instead_of_flipped():
    """基准价配错行时不许改数 —— 改了就是把一个错的数改成另一个错的数。"""
    deal = _deal_with("-20.00")
    runner._reconcile_premium_direction(
        deal, _Pick("最后交易日前30日均价", "1.00", "20.00", "premium"),
        _Ex("0.11"))
    assert deal.premium_pct == "-20.00"    # 一个字符都没动
    assert "方向存疑" in deal.premium_basis
    assert deal.confidence == "low"
    assert "人工核" in deal.notes


def test_an_agreeing_pick_is_left_completely_alone():
    deal = _deal_with("-20.00")
    runner._reconcile_premium_direction(
        deal, _Pick("最后交易日前30日均价", "1.00", "20.00", "discount"),
        _Ex("0.80"))
    assert (deal.premium_pct, deal.premium_basis, deal.notes,
            deal.confidence) == ("-20.00", "最后交易日前30日均价", "", "high")


def test_nothing_to_recheck_leaves_the_row_untouched():
    deal = _deal_with("-20.00")
    runner._reconcile_premium_direction(
        deal, _Pick("最后交易日前30日均价", "", "20.00", "discount"), _Ex(""))
    assert deal.premium_pct == "-20.00" and deal.notes == ""


def test_the_correction_note_carries_both_numbers():
    """铁律三：结论要能追溯。只说「改了符号」没法复核。"""
    deal = _deal_with("-20.00")
    runner._reconcile_premium_direction(
        deal, _Pick("最后交易日前30日均价", "1.00", "20.00", "discount"),
        _Ex("1.20"))
    assert "1.20" in deal.notes and "1.00" in deal.notes
