"""程序自己更新自己：从 GitHub 拉最新版覆盖本文件夹。

原来这件事是 `一键更新.bat` 干的 —— cmd 调 PowerShell，PowerShell
从 GitHub 下 zip，解压，robocopy 覆盖本地文件。功能上没毛病，但那串
动作和下载器木马的行为特征一模一样，卡巴斯基把它判成
`PDM:Trojan.Win32.Generic.nblk` 直接删掉了（用户真遇上了）。

杀软没冤枉它。「一个脚本从互联网取内容并就地覆盖可执行文件」本来
就该被拦 —— 换成任何一款杀软都可能拦。所以别再跟启发式引擎较劲：
改成程序自己更新自己，走它本来就在用的 requests，界面上点一下就行。

三条硬规矩：

  只增不删  ——  只写 zip 里有的文件。本地多出来的一律不动，
                所以 data\\ logs\\ .venv\\ 里的东西不可能被更新弄丢。
  先验后写  ——  zip 里必须有 app.py，否则判定这包不对，一个字节都不写。
  原子替换  ——  每个文件先写 .tmp 再 os.replace，中途断电不会留半截文件。
"""

from __future__ import annotations

import io
import os
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import requests

REPO = "forestlsc323-hub/hkex-scraper"
BRANCH = "claude/hkex-disclosure-data-extraction-unftw7"

# 这些目录属于你，不属于代码仓库，永远不碰。
KEEP = {"data", "logs", ".venv", ".git", "__pycache__", ".pytest_cache"}

# 更新完必须还在的东西。zip 里少了它就说明下错了包（比如代理返回了
# 一个登录页），这时候宁可什么都不做。
SENTINEL = "app.py"


def zip_url(repo: str = REPO, branch: str = BRANCH) -> str:
    return f"https://codeload.github.com/{repo}/zip/refs/heads/{branch}"


@dataclass
class UpdateResult:
    ok: bool
    written: list[str] = field(default_factory=list)
    error: str = ""


# 重试几次、每次退避多久。和 pdf_source 那边同一套路数。
ATTEMPTS = 4
BACKOFF = 1.5

# 连接被对方重置、超时、被代理掐断 —— 这些重试一次通常就回来了。
# HTTP 4xx 不在其列：分支名写错重试一百次也还是 404。
_TRANSIENT = (requests.ConnectionError, requests.Timeout,
              requests.exceptions.ChunkedEncodingError)


def download(url: str, *, session=None, timeout: int = 120,
             attempts: int = ATTEMPTS, sleeper=time.sleep, on_retry=None) -> bytes:
    """取 zip。**偶发的连接重置要自己扛下来，别丢给用户。**

    用户点「检查更新」撞上
    `ConnectionResetError(10054, '远程主机强迫关闭了一个现有的连接')` ——
    典型的偶发重置（防火墙、代理、GitHub 那头随手掐一条连接都会这样）。
    这个项目里所有别的网络调用都带退避重试，唯独更新器是光杆一发 get，
    于是一次抖动就变成一句红色报错，而用户能做的只有再点一次。
    那正是重试该干的活。
    """
    sess = session or requests.Session()
    last = None
    for attempt in range(attempts):
        try:
            resp = sess.get(url, timeout=timeout,
                            headers={"User-Agent": "hkex-precedent-db-updater"})
            resp.raise_for_status()
            return resp.content
        except _TRANSIENT as exc:
            last = exc
            if attempt < attempts - 1:
                if on_retry:
                    on_retry(attempt + 1, attempts, exc)
                sleeper(BACKOFF * (2 ** attempt))
    raise last


def _entries(zf: zipfile.ZipFile) -> dict[str, str]:
    """zip 里的 {仓库内相对路径: zip 内名字}，已剔除不该覆盖的目录。

    GitHub 的 zip 最外面裹了一层 `仓库名-分支名/`，要先剥掉。
    """
    out: dict[str, str] = {}
    for name in zf.namelist():
        if name.endswith("/"):
            continue
        parts = name.split("/")
        if len(parts) < 2:
            continue
        rel_parts = parts[1:]
        if any(p in KEEP for p in rel_parts):
            continue
        # zip 里出现 .. 或绝对路径就是恶意包，直接跳过（zip slip）
        if any(p in ("..", "") for p in rel_parts):
            continue
        out["/".join(rel_parts)] = name
    return out


def apply_zip(data: bytes, root: Path) -> UpdateResult:
    """把 zip 里的文件写进 root。返回真正改动了哪些文件。

    只写内容确实变了的 —— 一次更新通常只动三五个文件，逐个报出来，
    比「已更新」三个字有用得多。
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        return UpdateResult(False, error=f"下载回来的不是 zip（{exc}）")

    with zf:
        files = _entries(zf)
        if SENTINEL not in files:
            return UpdateResult(
                False, error=f"包里没有 {SENTINEL}，不像是本项目的代码，已放弃")

        written = []
        for rel, name in sorted(files.items()):
            blob = zf.read(name)
            target = root / rel
            if target.exists() and target.read_bytes() == blob:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_bytes(blob)
            os.replace(tmp, target)
            written.append(rel)

    return UpdateResult(True, written=written)


def update(root: Path, *, session=None, repo: str = REPO,
           branch: str = BRANCH, fetch=None, on_retry=None) -> UpdateResult:
    """下载并覆盖。网络出错变成一句人话，不往上抛。"""
    getter = fetch or (lambda url: download(url, session=session,
                                            on_retry=on_retry))
    try:
        blob = getter(zip_url(repo, branch))
    except _TRANSIENT as exc:
        # 重试了 ATTEMPTS 次还是不行，那多半不是抖动。给一句能照着做的话，
        # 而不是把 WinError 10054 原样甩到用户脸上。
        return UpdateResult(False, error=(
            f"连不上 GitHub（重试 {ATTEMPTS} 次都失败）：{exc}\n\n"
            "多半是网络或代理挡住了 codeload.github.com。\n"
            "换个网络（比如手机热点）再点一次通常就好。"))
    except requests.RequestException as exc:
        return UpdateResult(False, error=f"连不上 GitHub：{exc}")
    except OSError as exc:
        return UpdateResult(False, error=f"下载失败：{exc}")
    return apply_zip(blob, root)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    print("正在从 GitHub 取最新版…")
    result = update(root)
    if not result.ok:
        print(f"[X] {result.error}")
        return 1
    if not result.written:
        print("[OK] 已经是最新版，没有文件需要更新。")
    else:
        print(f"[OK] 更新了 {len(result.written)} 个文件：")
        for rel in result.written[:20]:
            print(f"      {rel}")
        if len(result.written) > 20:
            print(f"      …另有 {len(result.written) - 20} 个")
        print("\n    关掉程序窗口重新打开，新版才生效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
