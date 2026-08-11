"""更新器。

原来的 一键更新.bat 被卡巴斯基当成木马删了（PDM:Trojan.Win32.Generic.nblk），
所以更新改由程序自己做。这里守住三条：不碰你的数据、包不对就不写、
写坏的包不能把程序改残。
"""

import io
import zipfile

import pytest

from hkexdb import updater


def make_zip(files: dict[str, bytes], top="hkex-scraper-main") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for rel, blob in files.items():
            zf.writestr(f"{top}/{rel}", blob)
    return buf.getvalue()


def test_a_normal_update_writes_only_the_files_that_changed(tmp_path):
    """一次更新通常只动三五个文件，没变的不该重写。"""
    (tmp_path / "app.py").write_bytes(b"old")
    (tmp_path / "config.yaml").write_bytes(b"same")

    result = updater.apply_zip(
        make_zip({"app.py": b"new", "config.yaml": b"same"}), tmp_path)

    assert result.ok
    assert result.written == ["app.py"]
    assert (tmp_path / "app.py").read_bytes() == b"new"


def test_your_own_data_is_never_touched(tmp_path):
    """data\\ logs\\ .venv\\ 里的东西是你跑出来的，更新绝不能碰。

    这条比更新本身重要 —— 抓了半天的存档被一次更新洗掉，
    比更新失败严重得多。
    """
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "answer_key.csv").write_bytes("我手工核的答案".encode("utf-8"))
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "run.log").write_bytes("日志".encode("utf-8"))

    updater.apply_zip(make_zip({
        "app.py": b"new",
        "data/answer_key.csv": "仓库里那份空模板".encode("utf-8"),
        "logs/run.log": b"",
    }), tmp_path)

    assert (tmp_path / "data" / "answer_key.csv").read_bytes() == "我手工核的答案".encode("utf-8")
    assert (tmp_path / "logs" / "run.log").read_bytes() == "日志".encode("utf-8")


def test_local_extras_are_left_alone(tmp_path):
    """只增不删：本地多出来的文件一律不动。"""
    (tmp_path / "app.py").write_bytes(b"old")
    (tmp_path / "我自己的笔记.txt").write_bytes("别删我".encode("utf-8"))

    updater.apply_zip(make_zip({"app.py": b"new"}), tmp_path)

    assert (tmp_path / "我自己的笔记.txt").exists()


def test_a_package_without_app_py_is_refused_before_anything_is_written(tmp_path):
    """代理或校园网返回一个登录页、GitHub 改了打包方式 —— 都会得到一个
    结构不对的 zip。这种包写下去就是把程序改残，宁可一个字节都不写。
    """
    (tmp_path / "app.py").write_bytes(b"old")

    result = updater.apply_zip(make_zip({"README.md": "登录页".encode("utf-8")}), tmp_path)

    assert not result.ok and "app.py" in result.error
    assert (tmp_path / "app.py").read_bytes() == b"old"


def test_garbage_instead_of_a_zip_is_a_sentence_not_a_crash(tmp_path):
    result = updater.apply_zip(b"<html>404 Not Found</html>", tmp_path)
    assert not result.ok and result.error


def test_a_zip_cannot_write_outside_the_folder(tmp_path):
    """zip slip：包里写 ../../ 就能改到程序目录外面去。"""
    result = updater.apply_zip(
        make_zip({"app.py": b"new", "../../evil.txt": b"x"}), tmp_path)

    assert result.ok
    assert not (tmp_path.parent.parent / "evil.txt").exists()


def test_a_network_failure_is_reported_not_raised(tmp_path):
    import requests

    def boom(url):
        raise requests.ConnectionError("名称解析失败")

    result = updater.update(tmp_path, fetch=boom)
    assert not result.ok and "GitHub" in result.error


def test_nested_new_files_get_their_folder_created(tmp_path):
    (tmp_path / "app.py").write_bytes(b"x")
    updater.apply_zip(
        make_zip({"app.py": b"x", "hkexdb/brand_new.py": b"code"}), tmp_path)
    assert (tmp_path / "hkexdb" / "brand_new.py").read_bytes() == b"code"


def test_no_tmp_files_are_left_behind(tmp_path):
    """写一半的 .tmp 留在目录里，下次更新会把它当成正经文件。"""
    (tmp_path / "app.py").write_bytes(b"old")
    updater.apply_zip(make_zip({"app.py": b"new"}), tmp_path)
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("branch", ["main", updater.BRANCH])
def test_the_download_url_points_at_the_right_branch(branch):
    assert branch in updater.zip_url(branch=branch)
    assert updater.REPO in updater.zip_url(branch=branch)


# ---------------------------------------------------------------- 偶发重置

def test_a_connection_reset_is_retried_not_surfaced(tmp_path):
    """用户点「检查更新」撞上 ConnectionResetError(10054，远程主机强迫
    关闭了一个现有的连接) —— 典型的偶发重置。

    这个项目里所有别的网络调用都带退避重试，唯独更新器当初是光杆一发
    get，于是一次抖动就变成一句红色报错，而用户能做的只有再点一次。
    那正是重试该干的活。
    """
    import requests

    class Flaky:
        def __init__(self):
            self.calls = 0

        def get(self, url, **kw):
            self.calls += 1
            if self.calls < 3:
                raise requests.ConnectionError("远程主机强迫关闭了一个现有的连接")
            return Resp(make_zip({"app.py": b"new"}))

    class Resp:
        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            pass

    sess = Flaky()
    blob = updater.download("u", session=sess, sleeper=lambda _: None)

    assert sess.calls == 3
    assert blob


def test_it_gives_up_after_the_configured_attempts(tmp_path):
    """一直连不上就说人话，别把 WinError 10054 原样甩到用户脸上。"""
    import requests

    class Dead:
        def get(self, url, **kw):
            raise requests.ConnectionError("10054")

    result = updater.update(
        tmp_path,
        fetch=lambda url: updater.download(url, session=Dead(),
                                           sleeper=lambda _: None))

    assert not result.ok
    assert "重试" in result.error and "热点" in result.error


def test_a_404_is_not_retried(tmp_path):
    """分支名写错重试一百次也还是 404 —— 白等四轮退避没有意义。"""
    import requests

    class NotFound:
        def __init__(self):
            self.calls = 0

        def get(self, url, **kw):
            self.calls += 1
            return self

        def raise_for_status(self):
            raise requests.HTTPError("404 Not Found")

    sess = NotFound()
    with pytest.raises(requests.HTTPError):
        updater.download("u", session=sess, sleeper=lambda _: None)
    assert sess.calls == 1


def test_each_retry_is_announced(tmp_path):
    """重试要说出来 —— 界面上什么都不动，用户会以为程序死了。"""
    import requests

    class Flaky:
        def __init__(self):
            self.calls = 0

        def get(self, url, **kw):
            self.calls += 1
            raise requests.ConnectionError("重置")

    said = []
    try:
        updater.download("u", session=Flaky(), sleeper=lambda _: None,
                         on_retry=lambda a, t, e: said.append(a))
    except requests.ConnectionError:
        pass
    assert said == [1, 2, 3]        # 最后一次失败不报「稍后重试」
