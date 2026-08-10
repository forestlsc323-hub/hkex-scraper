"""分类码勘察的测试。

这是 asso 那份客户端里唯一没被用起来的东西 —— search_by_category
把架子搭好了，缺的是 t2code 的值。这一层负责去把它读出来。
"""

from __future__ import annotations

from hkexdb import categories as C
from hkexdb import runner


SELECT_PAGE = """
<html><body>
<select id="t1code">
  <option value="-2">全部</option>
  <option value="10000">公司公告</option>
  <option value="20000">上市公司資料</option>
</select>
<select name="t2code">
  <option value="-2">全部</option>
  <option value="10500">收購及合併 - 要約文件</option>
  <option value="10600">須予公佈的交易</option>
  <option value="10700">業績</option>
</select>
</body></html>
"""

JSON_PAGE = """
<html><body><script>
var tree = [{"code":"10500","name":"收購及合併"},{"code":"10700","name":"業績"}];
</script></body></html>
"""


def test_reads_categories_out_of_select_boxes():
    cats = C.parse_categories(SELECT_PAGE)
    codes = {c.code for c in cats}
    assert "10000" in codes and "10500" in codes


def test_picks_out_the_takeover_ones():
    """名字里带收購/合併/要約的才是我们要的。"""
    hits = C.takeover_categories(C.parse_categories(SELECT_PAGE))
    labels = [h.label for h in hits]
    assert any("收購及合併" in x for x in labels)
    assert not any("業績" in x for x in labels)


def test_reads_categories_out_of_embedded_json():
    """披露易近年把分类树放进 <script> 里，不是下拉框。"""
    hits = C.takeover_categories(C.parse_categories(JSON_PAGE))
    assert [h.code for h in hits] == ["10500"]


def test_the_same_entry_is_not_reported_twice():
    """同一项被两个读法各读到一次，只能出现一次。

    注意「-2 全部」在 t1code 和 t2code 两个下拉里都有，那是两项不是
    重复 —— 去重要连层级一起看，否则会把真分类误删。
    """
    once = C.parse_categories(SELECT_PAGE)
    twice = C.parse_categories(SELECT_PAGE + SELECT_PAGE)
    assert len(once) == len(twice)

    keys = [(c.level, c.code, c.label) for c in twice]
    assert len(keys) == len(set(keys))
    assert sum(1 for c in twice if c.code == "-2") == 2   # 两个下拉各一个


def test_a_page_with_no_categories_gives_manual_instructions():
    """读不到就说读不到，并给出 F12 手工拿码的步骤 ——
    绝不猜一个码填进去，猜错是静默漏掉整类公告。"""
    _cats, hits, report = C.probe(lambda url: "<html><body>空</body></html>")
    assert not hits
    assert "F12" in report and "Network" in report


def test_an_unreachable_page_says_so_plainly():
    def boom(url):
        raise ConnectionError("连不上")

    cats, hits, report = C.probe(boom)
    assert cats == [] and hits == []
    assert "打不开检索页" in report and "连不上" in report


def test_the_report_tells_you_what_to_put_in_config():
    _c, _h, report = C.probe(lambda url: SELECT_PAGE)
    assert "config.yaml" in report
    assert "category_t2codes" in report
    assert "自检" in report, "换模式前必须先对一遍，不然可能静默漏掉"


def test_probe_writes_a_file_with_every_category(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    path = runner.probe_categories(fetch_html=lambda url: SELECT_PAGE)
    text = runner.Path(path).read_text(encoding="utf-8")
    assert "10500" in text and "業績" in text, "全部分类都要留档，好让人自己认"


# ---------------------------------------------------------------- 原件副本

def test_cache_info_counts_nothing_when_there_is_no_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    assert runner.cache_info() == (0, 0)


def test_cache_info_and_clear_report_real_numbers(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    cache = tmp_path / runner.CACHE_DIR
    cache.mkdir(parents=True)
    for i in range(3):
        (cache / f"{i}.pdf").write_bytes(b"x" * 1000)

    assert runner.cache_info() == (3, 3000)
    assert runner.clear_cache() == (3, 3000)
    assert runner.cache_info() == (0, 0)


def test_human_size_is_readable():
    assert runner.human_size(0) == "0 B"
    assert runner.human_size(1500).endswith("KB")
    assert runner.human_size(90 * 1024 * 1024).startswith("90")
    assert runner.human_size(90 * 1024 * 1024).endswith("MB")
