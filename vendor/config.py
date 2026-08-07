# -*- coding: utf-8 -*-
"""
hkex_client.py 依赖的配置模块。
按 hkex_client.py 中实际引用的字段名反推而来。
"""

# ---------- 接口地址 ----------
# 检索接口（披露易 Title Search 的后端 servlet）。
# 该地址由检索页前端 JS 推断，HKEX 偶尔会改；若返回 404，
# 请打开 https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=zh
# 用浏览器 F12 -> Network 面板看实际请求 URL 再回来改。
HKEX_SEARCH_URL = "https://www1.hkexnews.hk/search/titleSearchServlet.do"

# 公告文件域名前缀，与 FILE_LINK 拼成完整 PDF 地址
# 例：https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0721/xxx.pdf
HKEX_BASE_URL = "https://www1.hkexnews.hk"


# ---------- 请求参数 ----------
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=zh",
    "Connection": "keep-alive",
}

# 单次请求超时（秒）。用 (连接超时, 读取超时) 元组比单个数字更稳。
REQUEST_TIMEOUT = (10, 60)


# ---------- 节流与翻页 ----------
# 每次请求之间的最小间隔（秒）。并发模式下 _RateLimiter 复用此值，
# 因此调小它等于全局提速；不建议低于 0.5，容易被封 IP。
SLEEP_BETWEEN_REQUESTS = 1.0

# 翻页步长：每翻一页 rowRange 增加这么多。
# 披露易接口没有 offset，只能不断增大"返回上限"重新请求，
# 步长太小会导致重复流量暴涨，建议 500~1000。
ROW_RANGE_STEP = 500


# ---------- 业务限制 ----------
# 全市场按日检索允许回溯的最大天数（本文件未直接使用，
# 由调用方 run_update.py 读取；search_by_ticker 明确绕过该限制）。
MAX_LOOKBACK_DAYS = 90
