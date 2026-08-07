# -*- coding: utf-8 -*-
"""hkex_client.py 需要的配置。它 `import config`，这就是那个 config。

这个文件是**为了让你那份客户端原样跑起来**而补的垫片，
hkex_client.py 本身一个字节都没改。
"""

HKEX_BASE_URL = "https://www1.hkexnews.hk"
HKEX_SEARCH_URL = HKEX_BASE_URL + "/search/titleSearchServlet.do"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": HKEX_BASE_URL + "/search/titlesearch.xhtml?lang=zh",
}

REQUEST_TIMEOUT = 60

# 翻页步长。单日全市场约 600~800 条，取 1000 则多数日子一轮取完。
ROW_RANGE_STEP = 1000

# 两次请求之间的间隔（秒）
SLEEP_BETWEEN_REQUESTS = 1.5
