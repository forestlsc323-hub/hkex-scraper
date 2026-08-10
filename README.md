# hkex-scraper —— 香港收购要约先例交易数据库

从香港交易所披露易（HKEXnews）抓取收购要约公告，结构化成审计级的
先例交易数据库（precedent transactions）。

**当前进度：抓取 → 筛查 → 抽取 → 明细界面已打通。**

---

## Windows 上怎么用（不用装 git，不用敲命令）

文件夹里有三个 `.bat`，双击就行：

| 双击这个 | 干什么 |
|---|---|
| **一键更新.bat** | 从 GitHub 拉最新版覆盖本文件夹。`data\` 和 `logs\` 不动 |
| **一键运行.bat** / **RUN.bat** | 打开程序 |

更新完直接跑，不用重装 Python，也不用重建虚拟环境。

跑完程序目录下会多一个 **SEND_TO_CLAUDE.txt** —— 这次运行的环境、
结果、完整日志都压在里面。出问题时把这个文件发给 Claude 就够了。

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

## 目录结构

```
一键更新.bat      双击：从 GitHub 拉最新版（data/ logs/ .venv/ 不动）
一键运行.bat      双击：打开程序
RUN.bat           同上，一键运行.bat 只是它的中文名

app.py            界面：两个标签页（要约明细 / 抓取）。刻意做得很薄，
                  只画窗口、转发消息 —— 逻辑越少越不会出错
config.yaml       所有可调参数。改行为改这里，不要改 .py
screening_rules.yaml
                  分类层的唯一真相源。改词表改这里，然后重跑，
                  **不要手改产出的判定结果**

hkexdb/
  runner.py       整条流水线：抓取 → 筛查 → 抽取 → 生成网页 → 打包诊断
  store.py        跨次运行的存档：抓过的别再抓，抽过的别再抽
  screening.py    T0 标题筛选（十三个坑逐条实现）
  extractor.py    从公告正文摘字段。只定位摘录，一个数都不算
  valuation.py    估值组：股数×要约价、P/B、泄露涨幅。算术全在这里
  selectors.py    主值溢价率怎么选
  validators.py   用公告自己的数字交叉验证公告自己的百分比
  scoring.py      拿结果对人工答案表，出字段级准确率
  dealsview.py    要约明细表的取数、筛选、排序、明细
  report.py       自包含 HTML 报告
  pdf_source.py   链接进、带页码的文本出（PDF 与 .htm 都收）
  categories.py   披露易公告分类码勘察
  config.py       找配置、读配置

vendor/
  hkex_client.py  实战客户端，**一字未改**。所有旋钮从 config.yaml
                  覆盖进去，不动它的代码
  config.py       它依赖的配置模块

data/             运行产物（不进 git，答案表除外）
  store/          存档：listing.csv / coverage.json / deals.csv / evidence.json
  answer_key.csv  人工核查过的标准答案 —— 准确率的唯一标尺，进 git
tests/            回归测试。每个测试的名字就是它守的那个坑
docs/             调查记录（历史留档，命令行入口已删除）
```

## 跑测试

```bash
.venv/bin/python -m pytest tests -q
```

测试的名字就是它守的那个坑，例如：

- `test_page_footer_does_not_glue_two_items_together` —— 页脚「- 11 -」夹在
  两条比较项中间，粘连后锚点会判错，产出一个看着正常的错数字
- `test_hkex_dates_are_really_parsed_not_string_swapped` —— 披露易给的是
  DD/MM/YYYY，当成 ISO 比大小会静默丢掉全部数据
- `test_a_new_extractor_version_forces_a_re_extract` —— 改了抽取逻辑却
  复用旧结果，是最难发现的一类污染

## ⚠️ 进入阶段二前需要你补的材料

这三样在你的指导里被引用，但没有附上。**不影响阶段一，阶段二开始前需要：**

1. **附录 A（判定规则）** —— 你指定它是「唯一真相源」，分类层必须严格实现它。
   没有它我无法开始阶段二。
2. **V1–V11 校验器的定义** —— 附录 D 引用了 V3、V4、V5、V6、V8、V9、V10、V11，
   但只给了「检测点」，没给判定条件和阈值。推测它们在附录 A 里。
3. **附录 C 的第 1–5 项** —— 你贴的是第 6–10 项（框线摘要、三列表格、
   中英双语、图片型 PDF、异体字繁简）。
4. **人工核对样本库** —— 你已经给了 25 单（存在 `data/answer_key.csv`），
   界面上点「对答案（准确率）」就能出字段级报告。补得越多，标尺越准。

另外，**VPO 已确认为 VGO（自愿全面要约）**，`hkexdb/extractor.py` 里
按 VGO 实现。附录 A 到手后类型判定会以它为准。
