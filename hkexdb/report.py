"""把筛查结果生成一张能直接看的网页。

产出是**单个自包含 HTML 文件**：没有外链、没有 CDN、没有依赖，
双击就能在浏览器里打开，断网也能用。数据以 JSON 嵌在页面里。

为什么要这个：`screened.csv` 有一两万行，Excel 打开能看，
但「哪些被灰了、为什么灰、矛盾行有哪些」需要来回筛列。
这张页面把判定桶做成可点的筛选器，判定依据直接展开，
一屏之内就能过完人工复核桶和矛盾行 —— 那是铁律二要求的动作。
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path

BUCKET_META = {
    "retained": ("留存", "命中 T0 特征，进入抽取", "#1f7a4d"),
    "manual": ("人工复核", "两层均未命中，必须逐条看", "#a86400"),
    "excluded": ("已灰", "后续／程序公告（软删除，行还在）", "#6b7280"),
    "special": ("特殊品种", "非三种要约，剔除但可分辨", "#7c3aed"),
    "superseded": ("被取代", "标题勘误的旧版本", "#9ca3af"),
    "irrelevant": ("题材无关", "标题无任何要约/收购字眼", "#9ca3af"),
}

# 卡片顺序：留存（出数的那些）第一，人工复核第二，题材无关垫底
BUCKET_ORDER = ["retained", "manual", "special", "excluded", "superseded",
                "irrelevant"]

_CSS = """
:root{--bg:#ffffff;--fg:#1a1a1a;--muted:#6b7280;--line:#e5e7eb;--card:#f9fafb;--accent:#1d4ed8}
@media (prefers-color-scheme:dark){:root{--bg:#14161a;--fg:#e8e8e8;--muted:#9ca3af;--line:#2b2f36;--card:#1c1f25;--accent:#7aa2f7}}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:var(--bg);color:var(--fg);
     font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--muted);font-size:13px;margin-bottom:20px}
.cards{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:18px}
.card{border:1px solid var(--line);border-radius:8px;padding:10px 14px;cursor:pointer;
      background:var(--card);min-width:120px;transition:.15s}
.card:hover{border-color:var(--accent)}
.card.on{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
.card .n{font-size:22px;font-weight:600}
.card .l{font-size:12px;color:var(--muted)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.bar{display:flex;gap:10px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
input[type=search]{flex:1;min-width:240px;padding:8px 12px;border:1px solid var(--line);
     border-radius:6px;background:var(--bg);color:var(--fg);font-size:14px}
.hint{color:var(--muted);font-size:12px}
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:13px}
th{position:sticky;top:0;background:var(--card);text-align:left;padding:9px 10px;
   border-bottom:1px solid var(--line);white-space:nowrap;cursor:pointer;user-select:none}
th:hover{color:var(--accent)}
td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tr.row:hover{background:var(--card)}
tr.row{cursor:pointer}
.tag{font-size:11px;padding:2px 7px;border-radius:99px;color:#fff;white-space:nowrap}
.title{max-width:640px}
.why{display:none;background:var(--card);color:var(--muted);font-size:12px}
.why.open{display:table-row}
.why td{padding:10px 14px}
.why b{color:var(--fg)}
a{color:var(--accent)}
.empty{padding:40px;text-align:center;color:var(--muted)}
.warn{border-left:3px solid #d97706;background:var(--card);padding:12px 16px;
      border-radius:6px;margin-bottom:18px}
code{background:var(--card);padding:1px 5px;border-radius:4px;font-size:12px}
"""

_JS = """
const rows = DATA.rows;
let bucket = null, sortKey = 'date', sortDir = -1;

const esc = s => String(s??'').replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function render(){
  const q = document.getElementById('q').value.trim().toLowerCase();
  let view = rows.filter(r =>
    (!bucket || r.bucket === bucket) &&
    (!q || (r.code+' '+r.name+' '+r.title+' '+r.reasons).toLowerCase().includes(q)));

  view.sort((a,b) => {
    const x = String(a[sortKey]??''), y = String(b[sortKey]??'');
    return x < y ? -sortDir : x > y ? sortDir : 0;
  });

  document.getElementById('count').textContent =
    view.length + ' / ' + rows.length + ' 条';

  const tb = document.getElementById('tb');
  if (!view.length){ tb.innerHTML =
    '<tr><td colspan="6" class="empty">没有匹配的记录</td></tr>'; return; }

  tb.innerHTML = view.map((r,i) => {
    const m = DATA.meta[r.bucket] || ['?','','#888'];
    const pdf = r.pdf ? `<a href="${esc(r.pdf)}" target="_blank" rel="noopener">PDF</a>` : '';
    return `<tr class="row" onclick="toggle(${i})">
      <td>${esc(r.date)}</td>
      <td><code>${esc(r.code)}</code></td>
      <td>${esc(r.name)}</td>
      <td><span class="tag" style="background:${m[2]}">${esc(m[0])}</span></td>
      <td class="title">${esc(r.title)}</td>
      <td>${pdf}</td></tr>
      <tr class="why" id="w${i}"><td colspan="6">
        <b>判定依据：</b>${esc(r.reasons) || '（无）'}<br>
        ${r.matched_exclude ? '<b>排除层命中：</b>'+esc(r.matched_exclude)+'<br>' : ''}
        ${r.matched_retain ? '<b>保留层命中：</b>'+esc(r.matched_retain)+'<br>' : ''}
        ${r.species ? '<b>品种：</b>'+esc(r.species)+'<br>' : ''}
        ${r.flags ? '<b>人工标记：</b>'+esc(r.flags)+'<br>' : ''}
        <b>NEWS_ID：</b><code>${esc(r.uid)}</code>
      </td></tr>`;
  }).join('');
}

function toggle(i){ document.getElementById('w'+i).classList.toggle('open'); }

function pick(b){
  bucket = (bucket === b) ? null : b;
  document.querySelectorAll('.card').forEach(c =>
    c.classList.toggle('on', c.dataset.b === bucket));
  render();
}

function sortBy(k){
  sortDir = (sortKey === k) ? -sortDir : 1;
  sortKey = k;
  render();
}

document.getElementById('q').addEventListener('input', render);
render();
"""


def _safe_json(obj) -> str:
    """把数据序列化成可以安全嵌进 <script> 的 JSON。

    直接 json.dumps 出来的字符串里若含 `</script>`，浏览器会在那里
    提前闭合脚本标签，后面的内容当成 HTML 解析 —— 公告标题是外部数据，
    足以借此注入。转义 `<` 与 `&` 即可根除：JSON 里 \u003c 与 `<` 等价，
    解析出来的数据一字不差。
    """
    return (json.dumps(obj, ensure_ascii=False)
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026"))


def _row_from_csv(rec: dict) -> dict:
    return {
        "date": rec.get("date", ""),
        "code": rec.get("code", ""),
        "name": rec.get("name", ""),
        "bucket": rec.get("bucket", ""),
        "title": rec.get("title", ""),
        "pdf": rec.get("pdf_url", ""),
        "reasons": rec.get("reasons", ""),
        "matched_exclude": rec.get("matched_exclude", ""),
        "matched_retain": rec.get("matched_retain", ""),
        "species": rec.get("species", ""),
        "flags": rec.get("manual_flags", ""),
        "uid": rec.get("row_id", ""),
    }


def _deals_table(deals) -> str:
    """要约结果表 —— 这是你真正要的东西，放在页面最上面。"""
    if not deals:
        return ('<div class="warn">这一批里没有抽到要约公告。'
                '可能是该时段确实没有，也可能是筛查词表漏了新措辞 —— '
                '看下面「人工复核」桶里有没有像要约的标题。</div>')

    head = ("<tr><th>日期</th><th>代码</th><th>受要约方</th><th>要约方</th>"
            "<th>要约方FA</th><th>类型</th><th>对价</th>"
            "<th>要约价<br>HKD</th><th>溢价率</th><th>口径</th>"
            "<th>交易规模<br>HKD</th><th>上市地位</th>"
            "<th>复算校验</th><th>原文</th></tr>")
    ncols = head.count("<th>")
    body = []
    for d in deals:
        pct = d.premium_pct
        color = ("#1f7a4d" if pct and not pct.startswith("-")
                 else "#b91c1c" if pct else "var(--muted)")
        size = ""
        if d.deal_size:
            # 只加千分位，小数位原样保留 —— 取整会丢掉角分
            whole, _, frac = d.deal_size.partition(".")
            try:
                size = f"{int(whole):,}" + (f".{frac}" if frac else "")
            except ValueError:
                size = d.deal_size
        flag = "" if d.confidence == "high" else \
            f'<span class="tag" style="background:#a86400">{d.confidence}</span>'
        pdf = (f'<a href="{html.escape(d.pdf_url)}" target="_blank" '
               f'rel="noopener">PDF</a>' if d.pdf_url else "")
        small = 'style="font-size:12px"'
        body.append(
            f"<tr><td>{html.escape(d.date)}</td>"
            f"<td><code>{html.escape(d.code)}</code></td>"
            f"<td>{html.escape(d.target_full or d.name)}</td>"
            f"<td>{html.escape(d.offeror)}</td>"
            f"<td {small}>{html.escape(d.offeror_fa)}</td>"
            f"<td><b>{html.escape(d.offer_type)}</b> {flag}</td>"
            f"<td {small}>{html.escape(d.consideration)}</td>"
            f"<td>{html.escape(d.offer_price)}</td>"
            f'<td style="color:{color};font-weight:600">'
            f"{html.escape(pct)}{'%' if pct else ''}</td>"
            f"<td {small}>{html.escape(d.premium_basis)}</td>"
            f"<td>{size}</td>"
            f"<td {small}>{html.escape(d.listing_intent)}</td>"
            f"<td {small}>{html.escape(d.checks)}</td>"
            f"<td>{pdf}</td></tr>")
        ladder = "　".join(f"{k} {v}%" for k, v in d.premium_ladder.items())
        if ladder:
            body.append(f'<tr><td colspan="{ncols}" style="color:var(--muted);'
                        f'font-size:12px">溢价梯子　{html.escape(ladder)}</td></tr>')
        if d.notes:
            body.append(f'<tr><td colspan="{ncols}" style="color:#a86400;'
                        f'font-size:12px">{html.escape(d.notes)}</td></tr>')

    return (f'<h2 style="margin:24px 0 8px">要约明细（{len(deals)} 单）</h2>'
            f'<div class="wrap"><table><thead>{head}</thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def build_html(records: list[dict], *, rules_version: str = "",
               source: str = "", notes: list[str] | None = None,
               deals=None) -> str:
    """生成自包含 HTML。records 是 screened.csv 读出来的行。"""
    rows = [_row_from_csv(r) for r in records]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["bucket"]] = counts.get(row["bucket"], 0) + 1

    cards = []
    for bucket in BUCKET_ORDER:
        if bucket not in counts:
            continue
        label, desc, color = BUCKET_META[bucket]
        cards.append(
            f'<div class="card" data-b="{bucket}" onclick="pick(\'{bucket}\')" '
            f'title="{html.escape(desc)}">'
            f'<div class="n">{counts[bucket]}</div>'
            f'<div class="l"><span class="dot" style="background:{color}"></span>'
            f'{label}</div></div>')

    warn = ""
    if notes:
        warn = ('<div class="warn">' +
                "<br>".join(html.escape(n) for n in notes) + "</div>")

    payload = _safe_json(
        {"rows": rows, "meta": {k: list(v) for k, v in BUCKET_META.items()}})

    return f"""<!doctype html>
<html lang="zh-HK"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>披露易公告筛查结果</title><style>{_CSS}</style></head><body>
<h1>披露易公告筛查结果</h1>
<div class="sub">
  共 {len(rows)} 条　·　规则版本 <code>{html.escape(rules_version or "?")}</code>
  　·　数据源 <code>{html.escape(source or "?")}</code>
  　·　生成于 {datetime.now().strftime("%Y-%m-%d %H:%M")}
</div>
{warn}
{_deals_table(deals or [])}
<h2 style="margin:28px 0 8px">全部公告（筛查结果）</h2>
<div class="cards">{''.join(cards)}</div>
<div class="bar">
  <input id="q" type="search" placeholder="搜索代码 / 名称 / 标题 / 判定依据…">
  <span class="hint" id="count"></span>
  <span class="hint">点上方卡片筛选桶　·　点表头排序　·　点行展开判定依据</span>
</div>
<div class="wrap"><table>
<thead><tr>
  <th onclick="sortBy('date')">日期</th>
  <th onclick="sortBy('code')">代码</th>
  <th onclick="sortBy('name')">名称</th>
  <th onclick="sortBy('bucket')">判定</th>
  <th onclick="sortBy('title')">标题</th>
  <th>原文</th>
</tr></thead>
<tbody id="tb"></tbody></table></div>
<script>const DATA={payload};{_JS}</script>
</body></html>"""


def write_report(records: list[dict], out_path: Path, **kwargs) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(build_html(records, **kwargs), encoding="utf-8")
    return out_path
