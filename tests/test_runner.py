"""流程执行器的测试。

界面（app.py）在这个环境里跑不了（没有 tkinter），所以逻辑全部下沉到
runner.py，界面只剩转发。这些测试覆盖的就是「界面点下去之后真正发生的事」。
"""

from __future__ import annotations

import datetime as dt
import threading

import pytest

from hkexdb import runner


def fake_records(n: int, day: str = "2026-06-01") -> list[dict]:
    """混进真实标题，好让筛查层有东西可判。"""
    titles = [
        "聯合公告 (1) 完成出售及購買 (2) 作出強制性無條件現金要約 及 (3) 恢復股份買賣",
        "寄發綜合文件",
        "每月最新資料",
        "董事會會議日期",
        "建議以協議安排方式將公司私有化",
    ]
    return [{
        "NEWS_ID": f"n{i}",
        "DATE_TIME": f"{day} 08:{i % 60:02d}",
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


def test_launcher_uses_crlf_line_endings():
    """LF 换行会让 Windows cmd 解析崩掉，双击后闪一下就关（真踩过）。"""
    raw = (runner.Path(__file__).parent.parent / "RUN.bat").read_bytes()
    assert b"\r\n" in raw
    assert raw.count(b"\n") == raw.count(b"\r\n"), "存在裸 LF 换行"
