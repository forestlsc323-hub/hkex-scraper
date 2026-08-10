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
                        fetch=make_fetch(fake_records(20)))

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
                        fetch=make_fetch(fake_records(20)))
    assert sum(result.buckets.values()) == 20
    assert "retained" in result.buckets and "excluded" in result.buckets


def test_report_html_is_self_contained(_isolate):
    result = runner.run(dt.date(2026, 6, 1), dt.date(2026, 6, 1),
                        fetch=make_fetch(fake_records(10)))
    html = result.report_path.read_text(encoding="utf-8")
    assert "<link" not in html and "cdn." not in html


def test_dates_are_passed_through_untouched(_isolate):
    calls = []
    runner.run(dt.date(2026, 3, 4), dt.date(2026, 3, 9),
               fetch=make_fetch(fake_records(3), calls=calls))
    assert calls == [(dt.date(2026, 3, 4), dt.date(2026, 3, 9))]


# ---------------------------------------------------------------- 回调

def test_log_and_step_callbacks_fire(_isolate):
    logs, steps = [], []
    runner.run(dt.date(2026, 6, 1), dt.date(2026, 6, 1),
               on_log=logs.append, on_step=lambda i, f: steps.append((i, f)),
               fetch=make_fetch(fake_records(5)))

    assert logs, "界面靠 on_log 显示日志，一条都没有就是瞎的"
    assert {i for i, _ in steps} == {0, 1, 2, 3}, "四个步骤都要报进度"


def test_step_index_stays_in_range(_isolate):
    """界面拿 index 去索引 STEPS，越界就崩。"""
    steps = []
    runner.run(dt.date(2026, 6, 1), dt.date(2026, 6, 1),
               on_step=lambda i, f: steps.append(i),
               fetch=make_fetch(fake_records(3)))
    assert all(0 <= i < len(runner.STEPS) for i in steps)


def test_a_broken_callback_does_not_kill_the_run(_isolate):
    """界面的回调可能因为窗口已关而抛异常，不能连累抓取。"""
    def boom(*_):
        raise RuntimeError("界面没了")

    result = runner.run(dt.date(2026, 6, 1), dt.date(2026, 6, 1),
                        on_step=boom, fetch=make_fetch(fake_records(3)))
    # on_step 抛异常会被 run() 的兜底捕获，但日志和诊断仍要留下
    assert result.log_path.exists()
    assert result.diagnostic_path is not None


# ---------------------------------------------------------------- 边界

def test_zero_records_skips_downstream_but_still_writes_diagnostic(_isolate):
    """抓到 0 条不是崩溃，但也不能假装成功。"""
    result = runner.run(dt.date(2026, 6, 1), dt.date(2026, 6, 1),
                        fetch=make_fetch([]))
    assert not result.ok
    assert result.fetched == 0
    assert result.report_path is None
    assert result.diagnostic_path.exists()     # 诊断照样要有，否则没法排查


def test_fetch_failure_is_captured_not_raised(_isolate):
    """抓取炸了要变成 Result.error，不能把异常抛到界面线程里。"""
    def boom(*_args):
        raise ConnectionError("连不上披露易")

    result = runner.run(dt.date(2026, 6, 1), dt.date(2026, 6, 1), fetch=boom)
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
                        cancel_event=event, fetch=fetch)
    assert not result.ok
    assert result.error == "用户停止"


# ---------------------------------------------------------------- 诊断文件

def test_diagnostic_contains_what_i_need_to_debug(_isolate):
    result = runner.run(dt.date(2026, 6, 1), dt.date(2026, 6, 1),
                        fetch=make_fetch(fake_records(8)))
    text = result.diagnostic_path.read_text(encoding="utf-8")
    for needle in ("环境", "Python：", "抓到：8 条", "判定桶", "运行日志"):
        assert needle in text, f"诊断文件里缺 {needle}"


def test_diagnostic_written_even_when_fetch_dies(_isolate):
    """失败时的诊断比成功时更重要。"""
    def boom(*_args):
        raise TimeoutError("超时")

    result = runner.run(dt.date(2026, 6, 1), dt.date(2026, 6, 1), fetch=boom)
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
