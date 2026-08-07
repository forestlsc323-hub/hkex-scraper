"""阶段一勘察：用真实响应回答"这个站到底怎么检索"。

为什么要有这个脚本，而不是直接把参数写死在代码里：

  写代码的人（包括我）对 hkexnews 的参数名、分类代码只有"大概印象"。
  印象写进代码就成了看不见的假设，一旦站点改版或印象本来就错，
  表现是"抓到 0 条"或者更糟——"抓到一些但不全"，后者你不会发现。

  所以：先跑这个脚本，让站点自己回答，再据此配置 config.yaml。

产物全部原样落盘到 data/probe/，报告写在 data/probe/PROBE_REPORT.md。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup

from .http_client import PoliteSession

log = logging.getLogger(__name__)

BASE = "https://www1.hkexnews.hk"
SEARCH_PAGE = f"{BASE}/search/titlesearch.xhtml"
ROBOTS = f"{BASE}/robots.txt"

# 检索结果接口的候选地址。哪个能用由脚本实测决定。
SERVLET_CANDIDATES = [
    f"{BASE}/search/titleSearchServlet.do",
    f"{BASE}/search/titlesearchservlet.do",
]

# 日期参数名的候选组合。同上，实测决定。
DATE_PARAM_VARIANTS = [
    ("fromDate", "toDate"),
    ("from", "to"),
]

# 分类（Headline Category）清单的候选地址。
# 站点通常把下拉框选项做成静态 JSON 给前端用；能找到就能直接按类别过滤。
CATEGORY_URL_CANDIDATES = [
    f"{BASE}/ncms/script/eds/hkex_web_tier12_search_type_zh_HK.json",
    f"{BASE}/ncms/script/eds/hkex_web_tier12_search_type_en_US.json",
    f"{BASE}/ncms/script/eds/hkex_web_tier12_search_type_zh_CN.json",
]

# 判断"这一页的结果是不是服务端直接渲染好的"
_RESULT_MARKERS = ["headline", "release-time", "doc-link", "search-result"]


@dataclass
class Section:
    title: str
    lines: list[str] = field(default_factory=list)

    def add(self, line: str = "") -> None:
        self.lines.append(line)


_CAUSE_HINTS = (
    ("Tunnel connection failed", "连不上：代理拒绝 CONNECT（出网被拦）"),
    ("Name or service not known", "连不上：域名解析失败"),
    ("Connection refused", "连不上：连接被拒"),
    ("timed out", "连不上：超时"),
    ("Max retries exceeded", "连不上：重试用尽"),
)


def _brief(exc: BaseException) -> str:
    """把又臭又长的异常压成一句话。完整堆栈在日志里，报告要能读。

    根因通常埋在 __cause__ 链的底部 —— http_client 把网络异常包成
    RuntimeError 再抛，只看最外层那句会得到「请求失败：<url>」，
    等于什么都没说。所以要顺着链往下找。
    """
    seen: list[str] = []
    node: BaseException | None = exc
    while node is not None and len(seen) < 6:
        seen.append(str(node))
        node = node.__cause__ or node.__context__

    joined = " | ".join(seen)
    for needle, short in _CAUSE_HINTS:
        if needle in joined:
            return short
    return seen[0].split("params=")[0].strip()[:160]


def _save(probe_dir: Path, name: str, content: str) -> Path:
    probe_dir.mkdir(parents=True, exist_ok=True)
    path = probe_dir / name
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------- robots

def check_robots(session: PoliteSession, probe_dir: Path) -> Section:
    """合规第一步：先看对方允许什么。"""
    sec = Section("1. robots.txt（合规基线）")
    try:
        resp = session.get(ROBOTS)
    except Exception as exc:
        sec.add(f"- 抓取失败：{_brief(exc)}")
        sec.add("- **无法确认 robots 规则，请手工在浏览器打开 " + ROBOTS + " 确认后再继续。**")
        return sec

    path = _save(probe_dir, "robots.txt", resp.text)
    sec.add(f"- 已保存：`{path}`")
    sec.add("")
    sec.add("```")
    sec.add(resp.text.strip()[:2000])
    sec.add("```")

    disallows = re.findall(r"(?im)^\s*Disallow:\s*(\S+)", resp.text)
    hits = [d for d in disallows if d != "/" and "/search" in d]
    sec.add("")
    if any(d.strip() == "/" for d in disallows):
        sec.add("- ⚠️ 出现 `Disallow: /`，**先停下**，人工确认适用范围再决定是否继续。")
    elif hits:
        sec.add(f"- ⚠️ 与检索路径相关的 Disallow：{hits} —— 人工确认后再继续。")
    else:
        sec.add("- 未见针对 `/search` 的 Disallow。")

    crawl_delay = re.findall(r"(?im)^\s*Crawl-delay:\s*(\S+)", resp.text)
    if crawl_delay:
        sec.add(f"- ⚠️ 站点声明 Crawl-delay = {crawl_delay}，"
                "请把 config.yaml 的 `min_interval_seconds` 调到不低于该值。")
    else:
        sec.add("- 未声明 Crawl-delay；本项目默认 1.5 秒/次的保守间隔。")
    return sec


# ---------------------------------------------------------------- 检索页

def check_search_page(session: PoliteSession, probe_dir: Path) -> tuple[Section, str]:
    """判断检索页是静态 HTML 还是 JS 动态渲染，并挖出下拉框里的分类选项。"""
    sec = Section("2. 检索页：静态 HTML 还是 JS 动态渲染？")
    try:
        resp = session.get(SEARCH_PAGE, params={"lang": "zh"})
    except Exception as exc:
        sec.add(f"- 抓取失败：{_brief(exc)}")
        return sec, ""

    html = resp.text
    path = _save(probe_dir, "titlesearch.html", html)
    sec.add(f"- 已保存：`{path}`（{len(html):,} 字符）")

    soup = BeautifulSoup(html, "html.parser")

    # 服务端渲染的话，首屏 HTML 里应该已经能看到公告标题/PDF 链接
    pdf_links = [a.get("href") for a in soup.find_all("a", href=True)
                 if str(a.get("href")).lower().endswith(".pdf")]
    marker_hits = [m for m in _RESULT_MARKERS if m in html.lower()]

    sec.add(f"- 首屏 HTML 中的 .pdf 链接数：**{len(pdf_links)}**")
    sec.add(f"- 命中的结果区标记：{marker_hits or '无'}")
    if pdf_links:
        sec.add("- → 首屏已含结果，可考虑直接解析 HTML。")
    else:
        sec.add("- → 首屏**不含**公告结果，说明结果由 JS 异步取回，"
                "应当直接调用后端接口（见第 3 节），不要去解析这个 HTML。")

    # 下拉框选项就是官方的分类清单，含 t1code / t2code 的真实取值
    sec.add("")
    sec.add("**页面内的下拉框（分类代码的第一手来源）：**")
    selects = soup.find_all("select")
    if not selects:
        sec.add("- 页面里没有 <select>，说明下拉框也是 JS 动态填充的，见第 4 节。")
    dump = {}
    for sel in selects:
        sel_id = sel.get("id") or sel.get("name") or "(无 id)"
        options = [{"value": o.get("value"), "text": o.get_text(strip=True)}
                   for o in sel.find_all("option")]
        dump[sel_id] = options
        sec.add(f"- `{sel_id}`：{len(options)} 个选项")
        for opt in options[:8]:
            sec.add(f"    - `{opt['value']}` → {opt['text']}")
        if len(options) > 8:
            sec.add(f"    - …… 其余 {len(options) - 8} 项见 selects.json")
    if dump:
        _save(probe_dir, "selects.json",
              json.dumps(dump, ensure_ascii=False, indent=2))
        sec.add(f"- 完整选项已保存：`{probe_dir / 'selects.json'}`")

    return sec, html


# ---------------------------------------------------------------- 接口

def check_servlet(session: PoliteSession, probe_dir: Path,
                  date_from: str, date_to: str) -> Section:
    """把候选接口地址 × 候选参数名逐个试一遍，报告哪个组合真的返回数据。"""
    sec = Section("3. 检索接口：哪个地址 / 哪套参数名真的能用？")
    sec.add(f"- 试探用的日期范围：{date_from} ~ {date_to}")
    sec.add("")

    working = []
    reached_server = False
    for servlet in SERVLET_CANDIDATES:
        for from_key, to_key in DATE_PARAM_VARIANTS:
            params = {
                "sortDir": "0",
                "sortByOptions": "DateTime",
                "category": "0",
                "market": "SEHK",
                "stockId": "-1",
                "documentType": "-1",
                from_key: date_from,
                to_key: date_to,
                "title": "",
                "searchType": "1",
                "t1code": "-1",
                "t2Gcode": "-1",
                "t2code": "-1",
                "rowRange": "10",
                "lang": "ZH",
                "pageNo": "1",
            }
            label = f"{servlet.rsplit('/', 1)[-1]} + {from_key}/{to_key}"
            try:
                resp = session.get(servlet, params=params, headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": SEARCH_PAGE + "?lang=zh",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                })
            except Exception as exc:
                sec.add(f"- ❌ `{label}` → {_brief(exc)}")
                continue

            reached_server = True
            try:
                payload = json.loads(resp.text)
            except json.JSONDecodeError:
                sec.add(f"- ❌ `{label}` → HTTP {resp.status}，但返回的不是 JSON"
                        f"（前 120 字：{resp.text[:120]!r}）")
                continue

            rows = payload.get("result")
            if isinstance(rows, str):
                rows = json.loads(rows) if rows.strip() else []
            rows = rows or []

            sec.add(f"- ✅ `{label}` → HTTP {resp.status}，JSON 顶层键："
                    f"{sorted(payload.keys())}，本页 {len(rows)} 条")
            if rows:
                fname = f"servlet_{from_key}.json"
                _save(probe_dir, fname,
                      json.dumps(payload, ensure_ascii=False, indent=2))
                sec.add(f"    - 原始响应已保存：`{probe_dir / fname}`")
                sec.add(f"    - 单条记录的字段：`{sorted(rows[0].keys())}`")
                sec.add("    - 第一条样例：")
                for k, v in list(rows[0].items())[:12]:
                    sec.add(f"        - `{k}` = {str(v)[:110]!r}")
                working.append((servlet, from_key, to_key))
            else:
                sec.add("    - ⚠️ 返回 0 条，可能是参数名对了但取值不对，"
                        "也可能是这套参数名不成立。")

    sec.add("")
    if working:
        servlet, from_key, to_key = working[0]
        sec.add(f"**结论：使用 `{servlet}`，日期参数名为 `{from_key}` / `{to_key}`。**")
        sec.add(f"→ 把这两个名字填进 `hkexdb/listing.py` 顶部的 `DATE_PARAM_NAMES`。")
    elif reached_server:
        sec.add("**结论：请求到达了服务器，但没有任何组合返回数据。**")
        sec.add("→ 说明接口地址或参数名与当前站点结构不符。"
                "请在浏览器打开检索页，按 F12 → Network → 手工搜一次，"
                "把那条 XHR 请求的完整 URL 复制给我，我照实际情况改。")
    else:
        sec.add("**结论：全部请求在连接阶段就失败了，本节什么也没测出来。**")
        sec.add("→ 这**不能**说明接口地址或参数名有问题 —— 先解决网络再重跑。")
    return sec


# ---------------------------------------------------------------- 分类

def check_categories(session: PoliteSession, probe_dir: Path, html: str) -> Section:
    """回答：能不能按"收购守则/要约"这类 Headline Category 直接过滤？"""
    sec = Section("4. Headline Category：能否按类别过滤？")
    sec.add("这是本阶段最有价值的一问——能按类别过滤的话，"
            "抓取面会从「全市场所有公告」缩到「收购相关公告」。")
    sec.add("")

    found_any = False
    for url in CATEGORY_URL_CANDIDATES:
        try:
            resp = session.get(url)
        except Exception as exc:
            sec.add(f"- ❌ `{url}` → {_brief(exc)}")
            continue
        try:
            payload = json.loads(resp.text)
        except json.JSONDecodeError:
            sec.add(f"- ❌ `{url}` → HTTP {resp.status}，非 JSON")
            continue

        found_any = True
        name = url.rsplit("/", 1)[-1]
        _save(probe_dir, name, json.dumps(payload, ensure_ascii=False, indent=2))
        sec.add(f"- ✅ `{url}` → 已保存 `{probe_dir / name}`")

        # 在整份 JSON 里找收购/要约相关的条目，把它们的代码挖出来
        hits = _find_takeover_entries(payload)
        if hits:
            sec.add("    - 命中的收购/要约相关分类：")
            for trail, value in hits[:25]:
                sec.add(f"        - {trail} → `{value}`")
        else:
            sec.add("    - ⚠️ 这份 JSON 里没找到收购/要约相关字样。")

    if not found_any:
        sec.add("- 候选地址全部拿不到分类清单。")
        sec.add("")
        sec.add("**改用手工确认（三分钟）：**")
        sec.add("1. 浏览器打开 " + SEARCH_PAGE + "?lang=zh")
        sec.add("2. F12 → Network → 勾一个「标题类别」，看请求里 "
                "`t1code` / `t2Gcode` / `t2code` 变成了什么")
        sec.add("3. 把这几个值告诉我，或直接填进 config.yaml 的 queries")

    sec.add("")
    sec.add("**注意（不要跳过）：** 即使找到了分类代码，也**先不要**只靠类别过滤。")
    sec.add("先按类别抓一遍、再按标题关键词抓一遍，比对两边的差集——")
    sec.add("如果类别过滤漏掉了标题关键词能抓到的公告，说明发行人存在归类不一致，")
    sec.add("那就必须两种口径并用。这一步做完才能定下最终抓取口径。")
    return sec


_TAKEOVER_WORDS = re.compile(
    r"收購|收购|要約|要约|合併|合并|takeover|merger|privatis|privatiz|"
    r"share\s*buy-?back|購回|购回",
    re.IGNORECASE,
)


def _find_takeover_entries(node, trail: str = "") -> list[tuple[str, str]]:
    """递归遍历分类 JSON，找出文本里含收购/要约字样的节点及其代码。"""
    hits: list[tuple[str, str]] = []

    if isinstance(node, dict):
        text_bits = [str(v) for k, v in node.items()
                     if isinstance(v, str) and _TAKEOVER_WORDS.search(v)]
        if text_bits:
            code = node.get("code") or node.get("value") or node.get("id") or "?"
            hits.append((f"{trail} :: {' / '.join(text_bits)[:80]}", str(code)))
        for key, value in node.items():
            hits.extend(_find_takeover_entries(value, f"{trail}.{key}"))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            hits.extend(_find_takeover_entries(item, f"{trail}[{i}]"))

    return hits


# ---------------------------------------------------------------- 汇总

def run(session: PoliteSession, probe_dir: Path,
        date_from: str, date_to: str) -> Path:
    sections = [check_robots(session, probe_dir)]

    page_sec, html = check_search_page(session, probe_dir)
    sections.append(page_sec)
    sections.append(check_servlet(session, probe_dir, date_from, date_to))
    sections.append(check_categories(session, probe_dir, html))

    # 一个请求都没到达服务器 —— 这次勘察什么也没测出来。
    # 必须说清楚，否则「没有任何组合返回数据」会被误读成「接口不可用」，
    # 让人白跑去 F12 抓包。
    offline = session.stats["network"] == 0

    lines = [
        "# HKEXnews 数据源勘察报告",
        "",
    ]
    if offline:
        lines += [
            "> # ⛔ 本次勘察无效：一个请求都没有发出去",
            ">",
            f"> {session.stats['retries']} 次重试全部在连接阶段就失败，"
            "**没有任何一个请求到达披露易的服务器**。",
            ">",
            "> 所以下面每一节的「失败」都只说明本机连不上，"
            "**不能说明接口地址、参数名或分类代码有任何问题**。",
            ">",
            "> 先解决网络（本机代理 / 公司网络白名单 / 云端沙箱出网限制），",
            "> 再重跑 `python run_probe.py`。缓存里没有半成品，重跑是干净的。",
            "",
        ]
    lines += [
        "由 `run_probe.py` 自动生成。每一条结论都来自真实响应，不含推测。",
        "原始 HTML / JSON 与本报告同目录，可随时复核。",
        "",
    ]
    for sec in sections:
        lines.append(f"## {sec.title}")
        lines.append("")
        lines.extend(sec.lines)
        lines.append("")

    report = _save(probe_dir, "PROBE_REPORT.md", "\n".join(lines))
    log.info("勘察报告已写出：%s", report)
    return report
