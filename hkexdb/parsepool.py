"""在**另一个进程**里解析 PDF，这样卡死的那一份能被真正掐掉。

写这个模块是因为一次「跑了一个小时没动」。日志停在 27/68，界面标题
挂着「未响应」，而界面上那个「已运行」的秒数冻在 8 分 38 秒 ——
它冻住这件事本身就是证据：界面主线程一起被拖死了，不是慢，是停。

原因是我自己埋的，两处：

  一、`MAX_SECONDS_PER_PDF = 90` 从来没有被执行过。它只被拿去拼了
      一句「最多再等 90 秒」显示给用户看。Python **没有办法中断一个
      线程** —— pdfplumber 一旦在某份文件上陷进去，那个线程就永远
      回不来，而它手里还攥着解析锁，于是后面 41 份全部堵死，永远。
      我跟用户说过「最多再等 90 秒」，那句话是假的。

  二、我把下载预算从 30 秒放宽到 180 秒（为了救那份被误杀的大文件）。
      09880 優必選原先在 30 秒、7,104 KB 处被掐断，那一单丢了但整批
      还能跑完；放宽之后它被完整下下来，然后把解析器喂死了。
      我修好了一个症状，把它换成了一个更坏的。

线程杀不掉，进程杀得掉。所以解析挪进子进程：

  · 界面进程里不再有 CPU 密集的活，主线程不会再被拖住 ——
    「未响应」从根上没有了。
  · 超时是**真的**超时：到点 terminate，那一行标成「解析超时」，
    剩下的接着跑。这才对得起那句「最多再等 90 秒」。

并行度：**进程时代和线程时代的结论正好相反。**

早先测出「4 路并发解析比 1 路慢 70%」，那是线程 + GIL 的结论 ——
CPU 型工作在一个解释器里没法真并行，多开线程只是把同样的活切碎
轮流做。解析搬进子进程之后没有 GIL 了，重测（8 份 60 页）：

    1 个进程  17.3 秒      2 个进程   8.8 秒
    3 个进程   6.6 秒      4 个进程   4.7 秒   ← 3.7 倍

所以现在开多个进程。但**每个进程各自独立**（一个进程一个 Pool），
不是一个 Pool 开多个 worker —— 后者在超时时只能整锅端掉，
会把正在正常解析的邻居一起杀了。这条隔离性是上一轮花了整整一轮
才换来的，不能为了并行把它丢掉。
"""

from __future__ import annotations

import logging
import multiprocessing
import queue
import threading

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 90.0
SMOKE_SECONDS = 30.0       # 开工前那次「你能干活吗」最多等多久


def _smoke() -> str:
    """子进程里最小的一件活：能 import 到解析层就算能干。"""
    from . import pdf_source              # noqa: F401
    return "ok"


class ParseTimeout(Exception):
    """这份公告解析超时，已经被掐掉。"""


class _WorkerDied(Exception):
    """子进程自己炸了（不是超时）。上层会退回本进程再试一次。"""


def _worker(url: str, data: bytes, probe_pages: int, max_pages: int,
            min_text_chars: int) -> dict:
    """子进程里干的活。**必须是模块级函数**，否则 spawn 传不过去。

    返回普通 dict 而不是 PdfDoc：跨进程只传最朴素的数据，
    免得哪天 dataclass 加个字段就在 pickle 那里炸掉。
    """
    from . import extractor, pdf_source

    doc = pdf_source.parse_doc(
        url, data, probe_pages=probe_pages, max_pages=max_pages,
        min_text_chars=min_text_chars,
        promising=extractor.looks_like_offer if probe_pages else None)
    return {
        "pages": doc.pages,
        "page_count": doc.page_count,
        "has_text_layer": doc.has_text_layer,
        "extractor": doc.extractor,
        "extractor_note": doc.extractor_note,
        "pages_parsed": doc.pages_parsed,
        "stopped_early": doc.stopped_early,
    }


class _Slot:
    """一个独立的解析子进程。**一个槽一个进程**，互不牵连。

    为什么不用「一个 Pool 开 N 个 worker」：Pool.terminate() 是整锅端，
    一份公告解析超时就会把正在正常干活的邻居一起杀掉。而「超时只能
    干掉那一份」这条隔离性，是上一轮花了整整一轮才换来的。
    """

    def __init__(self, timeout: float, note):
        self.timeout = timeout
        self._note = note
        self.lock = threading.Lock()
        self.pool = None
        self._start(smoke=True)

    def _start(self, smoke: bool = False) -> None:
        try:
            self.pool = multiprocessing.get_context("spawn").Pool(processes=1)
        except Exception as exc:                        # noqa: BLE001
            self.pool = None
            self._note(f"解析子进程起不来（{type(exc).__name__}），"
                       f"改在本进程解析：卡死的公告将无法掐断")
            return
        if smoke and not self._works():
            self.close()
            self._note("解析子进程起来了但干不了活，改在本进程解析："
                       "卡死的公告将无法掐断")

    def _works(self) -> bool:
        """开工前先让子进程干一件最小的活，确认它真的能干。

        光「Pool 建起来了」不算数：spawn 出来的子进程要重新 import 一遍
        主模块，那一步可能失败（打包环境、奇怪的启动方式）。而 Pool 会
        **不停地重启**死掉的工作进程，于是每一份公告都要白等满一个超时
        才轮到下一份 —— 68 份 × 90 秒，比原来那个卡死还难受。

        宁可开工前花一秒钟问清楚。
        """
        try:
            return self.pool.apply_async(_smoke, ()).get(SMOKE_SECONDS) == "ok"
        except Exception:                               # noqa: BLE001
            return False

    def run(self, args) -> dict:
        """在这个槽里解析一份。**并行度的真实上界就在这一层。**

        排队等槽不算「正在解析」—— 早先的测试在 ParsePool.parse 外面数
        并发，数到的是排队的线程，于是 2 个槽量出 4 路并行。
        把执行放进槽里，数的地方和真相就对上了。
        """
        if self.pool is None:
            return _worker(*args)
        try:
            return self.pool.apply_async(_worker, args).get(self.timeout)
        except multiprocessing.TimeoutError:
            # 只掐这一个槽 —— 别的槽正在正常解析，不能连坐
            self.restart()
            raise ParseTimeout(
                f"解析超过 {self.timeout:.0f} 秒，已掐断") from None
        except Exception as exc:                        # noqa: BLE001
            self.restart()
            raise _WorkerDied(f"{type(exc).__name__}: {exc}") from exc

    def restart(self) -> None:
        """掐掉卡住的那个，换一个新的。

        terminate() 是真的发信号杀进程 —— 这正是线程做不到、
        而我们绕这一大圈想要的东西。
        """
        self.close()
        self._start()

    def close(self) -> None:
        pool, self.pool = self.pool, None
        if pool is not None:
            try:
                pool.terminate()
                pool.join()
            except Exception:                           # noqa: BLE001
                pass


class ParsePool:
    """若干个独立的解析子进程，每个都带一把真的秒表。

    起不来就退回本进程解析（打包成 exe、受限环境里 spawn 可能不可用）。
    退回意味着重新暴露在「卡死无法掐断」的风险里，所以这件事要说出来，
    不能悄悄降级。
    """

    def __init__(self, timeout: float = TIMEOUT_SECONDS, on_note=None,
                 workers: int = 1):
        self.timeout = timeout
        self._on_note = on_note
        self._slots = [_Slot(timeout, self._note) for _ in range(max(1, workers))]
        self._free: queue.Queue = queue.Queue()
        for slot in self._slots:
            self._free.put(slot)

    def _note(self, text: str) -> None:
        log.warning(text)
        if self._on_note:
            self._on_note(text)

    def parse(self, url: str, data: bytes, *, probe_pages: int = 0,
              max_pages: int = 0, min_text_chars: int = 500):
        """解析一份。超时抛 ParseTimeout，**并且那个进程真的死了**。"""
        from .pdf_source import PdfDoc

        args = (url, data, probe_pages, max_pages, min_text_chars)
        slot = self._free.get()          # 没有空槽就在这儿排队
        try:
            payload = slot.run(args)
        except ParseTimeout:
            raise
        except _WorkerDied as died:
            # 子进程自己炸了（内存不够、解析库崩了）。这一份退回本进程
            # 再试一次 —— 一份烂文件不该让后面几十份全部走无池子路径。
            self._note(f"解析子进程出错（{died.args[0]}），这一份改在本进程解析")
            payload = _worker(*args)
        finally:
            self._free.put(slot)         # 出了什么事都要把槽还回去

        return PdfDoc(url=url, from_cache=False, **payload)

    def close(self) -> None:
        for slot in self._slots:
            slot.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
