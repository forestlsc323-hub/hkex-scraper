# hkex-scraper

抓取香港交易所披露易（HKEXnews）的收购要约公告，从 PDF 中提取**要约类型**（MGO / VGO / PO）和**溢价率 / 折价率**。

## 安装

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 使用

抓今年年初到今天的全部 MGO / VGO / PO 公告：

```bash
.venv/bin/python -m hkex_offers.cli --out offers.csv
```

常用参数：

```bash
--from 2026-01-01 --to 2026-08-07   # 日期范围（默认今年 1 月 1 日至今天）
--lang ZH                            # ZH 或 EN，默认 ZH
--list-only                          # 只出公告清单，不下载 PDF（先看命中量）
--limit 20                           # 只处理前 20 份，调试用
--keywords 強制性 自願 部分要約       # 覆盖默认标题关键词
--max-pages 60                       # 每份 PDF 最多解析页数，0 = 全部
```

建议第一次先跑 `--list-only`，确认命中的公告数量和标题是否合理，再跑完整流程。

## 输出字段

| 字段 | 说明 |
| --- | --- |
| `stock_code` / `stock_name` | 股票代码 / 名称 |
| `date` | 公告发布时间 |
| `offer_type` | `MGO` 强制性全面要约 / `VGO` 自愿全面要约 / `PO` 部分要约 |
| `premium_pct` | 溢价率（%），折价时为空 |
| `discount_pct` | 折价率（%），溢价时为空 |
| `benchmark` | 比较基准：`last_trading_day_close`、`avg_5_days`、`nav` 等 |
| `pdf_url` | 公告 PDF 链接 |
| `status` | `ok` / `no_match` / `no_text_layer` / `download_error: ...` |
| `context` | 命中处的原文片段，用于人工核对 |

## 工作流程

1. `search.py` — 调用 HKEXnews 的 `titleSearchServlet.do`，按日期范围 + 标题关键词分页拉取公告列表。
2. `config.py` — 用标题正则筛掉债券购回等非收购要约，并把剩下的分成 MGO / VGO / PO。
3. `pdf.py` — 下载 PDF（本地缓存到 `.cache/pdf`），先用 pypdf 取文本，文本太少时回退 pdfplumber。
4. `extract.py` — 中英文正则匹配溢价 / 折价百分比，按**所在小句**判断比较基准，优先取「相对最后交易日收市价」那一条。
5. `cli.py` — 串起来并写出 CSV。

## 已知限制

- **扫描件 PDF** 没有文本层，会标成 `no_text_layer`，需要 OCR 才能处理。
- **措辞未覆盖** 的公告标成 `no_match`。这类需要看原文补正则——把 PDF 链接发我，我加规则。
- 同一宗收购通常有多份公告（3.5 公告、综合文件等），会各出一行，需要按 `stock_code` 去重。
- 搜索接口的参数名是按当前站点结构写的，若 HKEXnews 改版需要相应调整 `search.py`。

## 测试

```bash
.venv/bin/python -m pytest tests -q
```
