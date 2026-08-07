# hkex-scraper —— 香港收购要约先例交易数据库

从香港交易所披露易（HKEXnews）抓取收购要约公告，结构化成审计级的
先例交易数据库（precedent transactions）。

**当前进度：阶段一（数据源勘察与列表抓取）。分类、抽取、校验尚未开始。**

---

## 三条设计铁律

代码里的每个设计都要能追溯到这三条。改代码前先想它是否违反了铁律。

1. **模型只做定位与摘录，绝不做计算。** 所有百分比、乘积、加总、汇率换算
   由 Python 复算。溢价率不是「用行情重算真值」，而是**用公告自己的数字
   交叉验证公告自己的百分比**。
2. **分类层单独验收后才能往下走。** 分类错误是静默污染。
3. **每个字段必须带 `source_quote` + `page`。** 无出处一律 `null` +
   `confidence=low`。

---

## 上手（Python 新手请逐条照做）

### 第 0 步：装环境

在项目目录下打开终端，依次执行：

```bash
python3 -m venv .venv
```

这一句建了一个「虚拟环境」——把本项目要用的第三方库装在项目自己的文件夹里，
不污染你电脑上的系统 Python。只需做一次。

```bash
.venv/bin/pip install -r requirements.txt
```

装依赖。也只需做一次。

> Windows 用户：上面两句里的 `.venv/bin/` 换成 `.venv\Scripts\`。

### 第 1 步：填上你的邮箱

打开 `config.yaml`，把这一行里的 `YOUR_EMAIL_HERE` 换成你的真实邮箱：

```yaml
  user_agent: "hkex-precedent-db/0.1 (research; contact: YOUR_EMAIL_HERE)"
```

这是礼貌爬取的基本要求，见 `docs/01-数据源勘察.md`。

### 第 2 步：勘察数据源

```bash
.venv/bin/python run_probe.py
```

只发几个请求，很轻。跑完打开 **`data/probe/PROBE_REPORT.md`**。

这份报告回答你提的问题 1 和问题 2，**每一条结论都来自真实响应，不含推测**。
重点看四节：

| 报告章节 | 你要确认什么 |
| --- | --- |
| 1. robots.txt | 有没有 `Crawl-delay`？有的话回 `config.yaml` 把间隔调上去 |
| 2. 检索页 | 结果是服务端渲染还是 JS 异步取的 |
| 3. 检索接口 | **哪个接口地址 + 哪套日期参数名真的能用** |
| 4. 分类代码 | 有没有收购/要约相关的 Headline Category |

如果第 3 节的结论和 `hkexdb/listing.py` 顶部的 `SERVLET` /
`DATE_PARAM_NAMES` 不一致，**改那两行**，然后再往下走。

如果第 3 节说「没有任何组合返回数据」，按报告里的提示在浏览器 F12 抓一次
真实请求，把 URL 发我，我照实际情况改。

### 第 3 步：抓列表

先在 `config.yaml` 里把日期范围改成你要的：

```yaml
date_range:
  from: "2026-01-01"
  to: "2026-08-07"
```

然后：

```bash
.venv/bin/python run_listing.py
```

产出 **`data/raw/listing_raw.csv`**。

**中途断了直接重跑同一条命令**，已经抓完的时间块会自动跳过。

---

## 阶段一验收标准

> `data/raw/listing_raw.csv` 覆盖配置的完整日期范围，字段与接口返回**逐字一致**（未经任何清洗），
> 且断网重跑后产出的 CSV 与一次跑完的结果**逐字节相同**。

自查清单：

- [ ] `PROBE_REPORT.md` 第 3 节给出了明确的接口结论（不是「没有任何组合返回数据」）
- [ ] `robots.txt` 里没有禁止抓取检索路径；有 `Crawl-delay` 的话已同步到 config
- [ ] CSV 行数 > 0，且随手抽 3 行，`FILE_LINK` 拼上域名后能在浏览器打开
- [ ] 日期范围的**首月和末月**都有数据（验证切块没漏头尾）
- [ ] 把 `listing_raw.csv` 改名备份，再跑一次 `run_listing.py`，两份文件内容相同
- [ ] 中英两版公告都在（`_lang` 列同时有 `ZH` 和 `EN`）

**还有一项要人工做**（对应 `docs/01-数据源勘察.md` 第 2.2 节）：
等 probe 确认了分类代码，在 `config.yaml` 加一条按类别检索的 profile，
再跑一次，然后比对「按类别抓到的」和「按关键词抓到的」的差集。
差集里的东西决定了最终抓取口径能不能只用一种。

---

## 目录结构

```
config.yaml            所有可调参数。改行为改这里，不要改 .py
run_probe.py           第一步：勘察数据源
run_listing.py         第二步：抓列表

hkexdb/
  config.py            读 config.yaml
  logsetup.py          日志：屏幕看进度，文件留全量
  http_client.py       礼貌爬取层（限速/重试/缓存）—— 所有外网请求都走这里
  probe.py             勘察逻辑
  listing.py           列表抓取逻辑
  storage.py           raw 层存储（原样落盘 + 断点续跑 + 幂等）

  validators.py        V3~V8 校验器（铁律一在这里落地：所有算术由 Python 做）

docs/01-数据源勘察.md   问题 1、2 的回答，含置信度标注
docs/02-两单实测-字段陷阱清单.md
                       1417 MGO 与 3336 VGO 实测出的字段陷阱
tests/fixtures/        两单真实公告的人工基准（含 source_quote + page）
tests/                 离线测试，不联网
prototype/             早期原型，不可用于正式库，见其 README
data/                  运行产物（不进 git）
logs/                  运行日志（不进 git）
```

分层与工程要求里的 `scraper / classifier / parser / assembler / extractor /
validator / storage / review` 对应关系：阶段一只实现了 **scraper**（`probe.py` +
`listing.py`）和 **storage**（`storage.py`）。其余五层留到后续阶段，
现在不建空壳目录，避免看起来像已经做了。

---

## 关键设计说明

### 缓存 = 幂等

`http_client.py` 把每个响应连同**首次抓取时间**一起存盘。重跑时从缓存读，
时间戳原样带出，所以产出的 CSV 逐字节一致。工程要求里的「同输入同输出」
由此满足，测试 `test_rebuild_csv_is_byte_identical_across_runs` 守着这条。

### 断点续跑 + 幂等如何共存

- 抓到的行**追加**写进 `listing_rows.jsonl`
- 完成的（查询 × 时间块）记进 `checkpoint.json`，重跑时跳过
- CSV **每次从 jsonl 重新生成**，按 `_row_uid` 去重、按固定顺序排序

所以一个时间块抓到一半崩了，重跑会把前几页重复追加，但生成 CSV 时会去掉。
测试 `test_interrupted_run_resumes_and_stays_idempotent` 验证了
「崩溃后重跑」和「一次跑完」两份 CSV 逐字节相同。

### raw 层不清洗

`NEWS_ID` 前后的空格、没见过的新字段、中英双版重复——一律原样保留。
raw 是所有下游的地基，在这里「顺手清理」，后面发现清错了就回不去了。
测试 `test_raw_layer_keeps_all_fields_and_adds_provenance` 守着这条。

出处信息统一以 `_` 开头（`_query_name` / `_page_no` / `_fetched_at` /
`_source_url` 等），和公告本身的字段区分开。

---

## 跑测试

```bash
.venv/bin/python -m pytest tests -q
```

36 个测试，全部离线（网络层被替换成假的），几秒跑完。分两类：

- `test_listing.py`（17）—— 阶段一抓取：切块、分页、断点续跑、幂等
- `test_validators.py`（19）—— 两单真实公告的回归基准 + 附录 D 五个案例

其中最关键的一条是 `test_equality_check_would_false_flag_the_vgo`：
3336 那单把均价印作「約 X.XX」，若 V4 用等式检验，11 项里有 8 项会被误报为
错误，真错误就会淹没在假警报里。详见 `docs/02`。

---

## ⚠️ 进入阶段二前需要你补的材料

这三样在你的指导里被引用，但没有附上。**不影响阶段一，阶段二开始前需要：**

1. **附录 A（判定规则）** —— 你指定它是「唯一真相源」，分类层必须严格实现它。
   没有它我无法开始阶段二。
2. **V1–V11 校验器的定义** —— 附录 D 引用了 V3、V4、V5、V6、V8、V9、V10、V11，
   但只给了「检测点」，没给判定条件和阈值。推测它们在附录 A 里。
3. **附录 C 的第 1–5 项** —— 你贴的是第 6–10 项（框线摘要、三列表格、
   中英双语、图片型 PDF、异体字繁简）。
4. **31 单人工核对样本库** —— 铁律三要求跑通后输出逐字段准确率报告，
   这是唯一的测试集。

另外确认一件事：你之前说的 **VPO 已确认为 VGO（自愿全面要约）**，
`prototype/hkex_offers/config.py` 里已按 VGO 实现。正式版的类型判定会以
附录 A 为准，届时会重写。
