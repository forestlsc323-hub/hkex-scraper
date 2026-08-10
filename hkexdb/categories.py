"""披露易的公告分类码勘察。

这是你 asso 那份客户端里唯一还没被用起来的东西：

    def search_by_category(self, d1, d2, t2code):
        \"\"\"按收购相关类别代码检索（双保险，验证用）\"\"\"
        j = self._query_once(cur, end, ..., search_type="1",
                             t1="10000", t2=t2code)

他把架子搭好了，但 `t2code` 要填什么，代码里没有 —— 我们的 config.yaml
里那行 `category_t2codes: []` 一直空着，注释写的是「待 F12 确认」。

为什么值得拿到：关键词模式靠「收購」两个字捞，一个月带回 113 条，
其中绝大多数是上市规则第 14 章的普通交易公告。而披露易自己就按
「收購及合併」分好了类 —— 用它的分类码，服务端直接给你要约公告，
噪音在源头就没了。

这个模块只做**勘察**：把检索页里的分类树读出来，把名字里带
收購/合併/要約 的挑出来给你看。读到什么写什么，读不到就说读不到 ——
绝不猜一个码填进去，猜错的后果是静默漏掉整类公告。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

SEARCH_PAGE = "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=zh"

# 我们要找的分类：名字里带这些字的
WANTED = re.compile(r"收購|合併|要約|收购|合并|要约|"
                    r"takeover|merger|offer", re.I)


@dataclass
class Category:
    level: str          # t1code / t2Gcode / t2code
    code: str
    label: str

    def __str__(self) -> str:
        return f"{self.level}={self.code}　{self.label}"


def _from_select(html: str) -> list[Category]:
    """从 <select> 的 <option> 里读。检索页把分类做成下拉框时走这条。"""
    out: list[Category] = []
    for m in re.finditer(
            r'<select[^>]*\b(?:id|name)\s*=\s*["\']([^"\']*(?:t1code|t2Gcode|'
            r't2code)[^"\']*)["\'][^>]*>(.*?)</select>', html,
            re.I | re.S):
        level, body = m.group(1), m.group(2)
        for opt in re.finditer(
                r'<option[^>]*\bvalue\s*=\s*["\']([^"\']*)["\'][^>]*>(.*?)</option>',
                body, re.I | re.S):
            label = re.sub(r"<[^>]+>", "", opt.group(2))
            label = " ".join(label.split())
            if label:
                out.append(Category(level, opt.group(1).strip(), label))
    return out


def _from_json_blobs(html: str) -> list[Category]:
    """从内嵌的 JSON 里读。披露易近年把分类树放进 <script> 里的对象数组，
    典型形如 {"code":"10000","name":"公司公告"} 或 {"c":"...","n":"..."}。
    """
    out: list[Category] = []
    for m in re.finditer(r'\[\s*\{.{0,20000}?\}\s*\]', html, re.S):
        try:
            data = json.loads(m.group(0))
        except ValueError:
            continue
        if not isinstance(data, list):
            continue
        for item in data:
            if not isinstance(item, dict):
                continue
            code = next((str(item[k]) for k in ("code", "c", "value", "id")
                         if k in item), "")
            label = next((str(item[k]) for k in ("name", "n", "text", "label",
                                                 "desc")
                          if k in item), "")
            if code and label:
                out.append(Category("json", code, label))
    return out


def parse_categories(html: str) -> list[Category]:
    """把检索页里能认出来的分类全读出来，去重后返回。"""
    seen, out = set(), []
    for cat in _from_select(html) + _from_json_blobs(html):
        key = (cat.level, cat.code, cat.label)
        if key not in seen:
            seen.add(key)
            out.append(cat)
    return out


def takeover_categories(cats: list[Category]) -> list[Category]:
    """挑出名字里带收購/合併/要約的。"""
    return [c for c in cats if WANTED.search(c.label)]


def probe(fetch_html) -> tuple[list[Category], list[Category], str]:
    """跑一次勘察。`fetch_html()` 由调用方提供（要带会话 cookie）。

    返回 (全部分类, 疑似收购相关, 给人看的报告)。
    """
    try:
        html = fetch_html(SEARCH_PAGE)
    except Exception as exc:
        return [], [], (f"打不开检索页：{type(exc).__name__}: {exc}\n"
                        f"这一步必须能连上披露易才有意义。")

    cats = parse_categories(html)
    hits = takeover_categories(cats)

    lines = [f"检索页读到 {len(cats)} 个分类项。", ""]
    if not cats:
        lines += [
            "一个都没读出来 —— 说明分类树不是写在页面 HTML 里的，",
            "多半是页面加载后再用 JS 单独请求的。",
            "",
            "手工拿到它的办法（三分钟）：",
            "  1. 浏览器打开 https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=zh",
            "  2. 按 F12 打开开发者工具，切到 Network 面板",
            "  3. 在页面上把「標題類別」选成「收購及合併」相关的那一项",
            "  4. 看 Network 里新出现的请求，URL 参数里的 t1code / t2code 就是",
            "  5. 把那两个数字发给 Claude，或直接填进 config.yaml 的 listing.category",
        ]
        return cats, hits, "\n".join(lines)

    if hits:
        lines += ["疑似收购相关的分类："]
        lines += [f"  {c}" for c in hits]
        lines += ["", "把上面对应的码填进 config.yaml：",
                  "  listing:",
                  "    mode: \"category\"",
                  "    category_t1code: \"10000\"        # 一级码",
                  "    category_t2codes: [\"...\"]       # 上面挑出来的二级码",
                  "",
                  "填完先点「自检」对一遍，确认分类模式没漏掉要约公告再用。"]
    else:
        lines += ["读到了分类，但没有一个名字里带收購/合併/要約。",
                  "全部分类如下，你自己认一下哪个是：", ""]
        lines += [f"  {c}" for c in cats[:120]]
        if len(cats) > 120:
            lines.append(f"  …（还有 {len(cats) - 120} 个，见报告文件）")
    return cats, hits, "\n".join(lines)
