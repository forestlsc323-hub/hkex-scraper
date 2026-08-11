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
    """假 PDF：直接给页文本，不联网。"""

    def __init__(self, pages, has_text=True):
        self.pages, self.has_text_layer = pages, has_text


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

    RUN.bat 曾用 pythonw.exe 启动，它没有控制台：界面若在启动阶段崩掉，
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
    """RUN.bat 不能用 pythonw.exe —— 那样启动失败就是静默的。"""
    bat = (runner.Path(__file__).parent.parent / "RUN.bat").read_bytes().decode("utf-8")
    # 只看真正会执行的行 —— 注释里提到 pythonw 是在说明为什么不用它
    live = [ln for ln in bat.splitlines()
            if ln.strip() and not ln.strip().upper().startswith("REM")]
    assert not any("pythonw" in ln for ln in live), \
        "pythonw 没有控制台，启动失败时用户什么都看不到"
    assert any("python.exe app.py" in ln for ln in live)


@pytest.mark.parametrize("name", ["RUN.bat", "一键运行.bat", "一键更新.bat"])
def test_every_bat_uses_crlf_line_endings(name):
    """LF 换行会让 Windows cmd 解析崩掉，双击后闪一下就关（真踩过）。"""
    raw = (runner.Path(__file__).parent.parent / name).read_bytes()
    assert b"\r\n" in raw
    assert raw.count(b"\n") == raw.count(b"\r\n"), f"{name} 里有裸 LF 换行"


def test_updater_never_overwrites_what_the_user_produced():
    """更新脚本必须跳过 data\\ 和 logs\\ —— 覆盖了就是把跑出来的结果删了。

    .venv 也要跳过，否则每次更新都得重装依赖，用户会以为程序坏了。
    """
    bat = (runner.Path(__file__).parent.parent / "一键更新.bat"
           ).read_bytes().decode("utf-8")
    copy_line = next(ln for ln in bat.splitlines() if "robocopy" in ln)
    for protected in ("data", "logs", ".venv"):
        assert protected in copy_line.split("/XD")[1], f"更新会覆盖 {protected}"


def test_updater_points_at_the_branch_we_actually_push_to():
    """分支名写错的话，用户点了更新会一直拿到旧代码，而且毫无提示。"""
    root = runner.Path(__file__).parent.parent
    bat = (root / "一键更新.bat").read_bytes().decode("utf-8")
    branch = next(ln for ln in bat.splitlines() if ln.startswith('set "BRANCH='))
    branch = branch.split("=", 1)[1].rstrip('"')
    # 解压出来的顶层目录名 = 仓库名 + "-" + 分支名里的 / 换成 -
    folder = next(ln for ln in bat.splitlines() if ln.startswith('set "FOLDER='))
    folder = folder.split("=", 1)[1].rstrip('"')
    assert folder == "hkex-scraper-" + branch.replace("/", "-")


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


# ---------------------------------------------------------------- 解析串行

def test_parsing_never_runs_two_at_a_time(_isolate, monkeypatch):
    """解析必须串行。实测 12 份真实公告只算解析：

        1 路 12.13 秒 / 2 路 13.59 秒 / 4 路 20.56 秒

    GIL 让 CPU 型工作没法真并行，多开线程总时间反而涨 70%，
    还把 Tk 主线程一起拖住 —— 那就是「未响应」的来源。
    下载并发（I/O，等网络时放开 GIL），解析串行。
    """
    import time as _t

    from hkexdb import pdf_source, screening as S

    monkeypatch.setattr(runner, "_speed_settings",
                        lambda: (4000, 4, "keyword", ["要約"]))

    overlap = {"max": 0, "now": 0}
    guard = threading.Lock()
    real_parse = pdf_source.parse_doc

    def watched_parse(url, data, **kw):
        with guard:
            overlap["now"] += 1
            overlap["max"] = max(overlap["max"], overlap["now"])
        _t.sleep(0.05)
        try:
            return real_parse(url, data, **kw)
        finally:
            with guard:
                overlap["now"] -= 1

    monkeypatch.setattr(pdf_source, "parse_doc", watched_parse)

    page = ("「要約價」 指 每股要約股份0.519港元 "
            "價值比較每股要約價為每股0.519港元，較："
            "(i) 股份於最後交易日在聯交所所報收市價每股1.870港元折讓約72.25%。"
            "要約人於要約項下須支付的最高現金代價約為5,440萬港元。")
    pdf = b"%PDF-1.4 " + page.encode("utf-8")

    def fetch_only(url):
        _t.sleep(0.02)                       # 假装在下载
        return runner._Fetched(url, pdf, False)

    monkeypatch.setattr(pdf_source, "extract_pages",
                        lambda data, max_pages=0: ({1: page}, 1, "fake", ""))

    rows = [{"row_id": f"r{i}", "date": "2026-06-15", "code": f"{i:05d}",
             "name": f"公司{i}", "title": "作出強制性無條件現金要約",
             "pdf_url": f"/x/{i}.pdf", "verdict": S.Verdict(bucket=S.RETAINED)}
            for i in range(8)]

    runner._extract_deals(rows, lambda *_: None, lambda *_: None, None,
                          open_pdf=fetch_only)
    assert overlap["max"] == 1, \
        f"同时有 {overlap['max']} 份在解析 —— 解析必须串行"


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
