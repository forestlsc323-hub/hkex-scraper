"""T0 Screening：抓 PDF 之前的标题层筛选。

实现《港股要约 T0 Screening 作业手册》。规则在 screening_rules.yaml，
本模块只负责执行，不在代码里写死任何词表。

铁律二：分类层的错误是静默污染，所以这一层的输出必须是
**软删除 + 判定依据**，而不是一个干净的清单。被灰掉的行要留着，
留着才能跑矛盾行自检。
"""

from __future__ import annotations

import random
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import resolve_file

# 判定桶
EXCLUDED = "excluded"        # 灰：后续/程序公告
RETAINED = "retained"        # 留：命中 T0 特征
SPECIAL = "special"          # 非三种要约的品种，剔除但可分辨
MANUAL = "manual"            # 两层都没命中、但题材相关，逐条人工看
SUPERSEDED = "superseded"    # 被取代的旧标题行（坑⑫）
IRRELEVANT = "irrelevant"    # 标题与要约/收购毫无关系，不属于本课题

# 多编号联合公告的编号形式："(1)" "（1）" "(i)" 等
_NUMBERED = re.compile(r"[(（]\s*(?:\d{1,2}|[ivxIVX]{1,4})\s*[)）]")


class RuleError(RuntimeError):
    """规则文件自身有问题 —— 宁可不跑，也不能带着错词表跑。"""


@dataclass
class Rules:
    raw: dict
    version: str
    never_exclude: list[str]
    t0_corpus: list[str]
    neutral_nouns: list[str]
    exclude_terms: list[str]
    dual_track: list[dict]
    retain_terms: list[str]
    retain_patterns: list[tuple[re.Pattern, str]]
    special_species: list[dict]
    domain_terms: list[str]
    manual_flags: list[dict]
    superseded_marker: str
    replacement_marker: str
    contradiction_terms: list[str]
    audit_sample_size: int
    audit_seed: int

    def validate(self) -> None:
        """启动自检（坑①②⑤）。规则有问题就抛异常，不带病运行。

        两道关：
        1) 裸词护栏 —— 排除词不得**等于**护栏词。是「等于」不是「包含」：
           「要約結果」含「要約」但合法，裸词「強制」才是地雷（坑②）。
        2) 语料反测 —— 排除词不得命中任何一条已核实的真实 T0 标题。
           这是「词表按真实数据迭代」的机器化版本，比人肉护栏可靠得多。
        3) 相关性闸门反测 —— 每一条真实 T0 标题都必须命中至少一个
           题材词，否则闸门自己会变成漏检源。
        """
        for t0 in self.t0_corpus:
            if self.domain_terms and not any(t in t0 for t in self.domain_terms):
                raise RuleError(
                    "题材词表覆盖不到已核实的真实 T0 标题，闸门会把它判成"
                    f"「与要约无关」：\n  {t0[:80]}…")

        for term in self.exclude_terms:
            if term in self.never_exclude:
                raise RuleError(
                    f"排除词「{term}」是护栏裸词，用作子串会团灭 T0（坑①②）。")
            if term in self.neutral_nouns:
                raise RuleError(
                    f"排除词「{term}」是中性名词，必须配动词才有阶段信息（坑⑤）。")
            for t0 in self.t0_corpus:
                if term in t0:
                    raise RuleError(
                        f"排除词「{term}」命中已核实的真实 T0 标题，会造成漏检：\n"
                        f"  {t0[:80]}…")


def load_rules(path: str | Path = "screening_rules.yaml") -> Rules:
    resolved = resolve_file(path)
    if not resolved.exists():
        raise FileNotFoundError(
            f"找不到规则文件 {resolved}。请在仓库根目录下运行。")
    raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    markers = raw.get("title_correction_markers", {})
    rules = Rules(
        raw=raw,
        version=raw["version"],
        never_exclude=raw.get("never_exclude_exact", []),
        t0_corpus=raw.get("t0_title_corpus", []),
        neutral_nouns=raw.get("neutral_nouns_never_alone", []),
        exclude_terms=raw.get("exclude_terms", []),
        dual_track=raw.get("dual_track_terms", []),
        retain_terms=raw.get("retain_terms", []),
        retain_patterns=[(re.compile(p["pattern"]), p["label"])
                         for p in raw.get("retain_patterns", [])],
        special_species=raw.get("special_species", []),
        domain_terms=raw.get("offer_domain_terms", []),
        manual_flags=raw.get("manual_review_flags", []),
        superseded_marker=markers.get("superseded", ""),
        replacement_marker=markers.get("replacement", ""),
        contradiction_terms=raw.get("contradiction_check_terms", []),
        audit_sample_size=int(raw.get("random_audit_sample_size", 15)),
        audit_seed=int(raw.get("random_audit_seed", 0)),
    )
    rules.validate()
    return rules


@dataclass
class Verdict:
    """一条公告的判定结果。依据必须写清楚 —— 铁律三。"""

    bucket: str
    reasons: list[str] = field(default_factory=list)
    matched_exclude: list[str] = field(default_factory=list)
    matched_retain: list[str] = field(default_factory=list)
    species: str = ""
    manual_flags: list[str] = field(default_factory=list)
    is_bundled: bool = False       # 是否多编号联合公告

    @property
    def is_grey(self) -> bool:
        """软删除 —— 灰掉但不删除，还要参与矛盾行自检。"""
        return self.bucket in (EXCLUDED, SPECIAL, SUPERSEDED, IRRELEVANT)


def is_bundled_announcement(title: str) -> bool:
    """多编号联合公告？坑④⑧ 都要靠它区分双轨词的命运。

    坑⑧：长标题要读完每一个编号项，最后一项往往才是身份证。
    """
    return len(_NUMBERED.findall(title)) >= 2


def classify_title(title: str, rules: Rules) -> Verdict:
    """标题层判定。顺序即手册的第一步→第二步→第三步。

    排除优先于保留（手册第三步）：后续公告标题几乎都会把要约全称
    复述一遍，先跑保留词会大量误留。
    """
    title = (title or "").strip()
    v = Verdict(bucket=MANUAL)
    v.is_bundled = is_bundled_announcement(title)

    # 坑⑫：被取代的旧标题行直接灰掉，不参与后续判定
    if rules.superseded_marker and rules.superseded_marker in title:
        v.bucket = SUPERSEDED
        v.reasons.append(f"标题勘误：命中「{rules.superseded_marker}」，取修改后版本（坑⑫）")
        return v

    # 坑②：这些词不自动判，只打人工标记
    for flag in rules.manual_flags:
        if flag["term"] in title:
            v.manual_flags.append(f"{flag['term']}：{flag['reason']}")

    # 坑⑬：非三种要约的品种单独成桶，剔除但可分辨
    for species in rules.special_species:
        hits = [t for t in species["terms"] if t in title]
        if hits:
            v.bucket = SPECIAL
            v.species = species["name"]
            v.reasons.append(f"非三种要约品种：{species['name']}（命中 {hits}）（坑⑬）")
            return v

    # 坑④：双轨词 —— 独立成篇 vs 联合公告打包项，命运相反
    for dual in rules.dual_track:
        terms = [dual["term"], *dual.get("aliases", [])]
        hit = next((t for t in terms if t in title), None)
        if not hit:
            continue
        outcome = dual["bundled"] if v.is_bundled else dual["standalone"]
        where = "作为联合公告打包项" if v.is_bundled else "独立成篇"
        if outcome == "exclude":
            v.bucket = EXCLUDED
            v.matched_exclude.append(hit)
            v.reasons.append(f"双轨词「{hit}」{where} → 排除（坑④）")
            return v
        if outcome == "retain":
            v.bucket = RETAINED
            v.matched_retain.append(hit)
            v.reasons.append(f"双轨词「{hit}」{where} → T0 强特征（坑④）")
            return v
        v.reasons.append(f"双轨词「{hit}」{where} → 中性，继续判（坑④）")

    # 第一步：排除层
    v.matched_exclude = [t for t in rules.exclude_terms if t in title]
    if v.matched_exclude:
        v.bucket = EXCLUDED
        v.reasons.append(f"排除层命中 {v.matched_exclude}")
        return v

    # 第二步：保留层
    v.matched_retain = [t for t in rules.retain_terms if t in title]
    for pattern, label in rules.retain_patterns:
        if pattern.search(title):
            v.matched_retain.append(label)
    if v.matched_retain:
        v.bucket = RETAINED
        v.reasons.append(f"保留层命中 {v.matched_retain}")
        return v

    # 第三步：两层都没命中 → 看题材相关性再分流
    #
    # 手册的输入是「已经筛过一遍的要约相关表」，所以第三步直接转人工。
    # 我们的输入是全市场公告，得先把「盈利警告」「翌日披露報表」这类
    # 与要约毫无关系的分出去，否则人工桶会被 96% 的噪音淹掉。
    hits = [t for t in rules.domain_terms if t in title]
    if rules.domain_terms and not hits:
        v.bucket = IRRELEVANT
        v.reasons.append("标题不含任何要约/收购题材词，判为与本课题无关")
        return v

    v.reasons.append(
        f"排除层与保留层均未命中，但含题材词 {hits}，转人工复核（手册第三步）"
        if hits else "排除层与保留层均未命中，转人工复核（手册第三步）")
    return v


# ---------------------------------------------------------------- 坑⑫

def resolve_title_corrections(records: list[dict], rules: Rules) -> list[dict]:
    """处理标题勘误对：取修改后版本，灰掉被取代版本。

    披露易的「(修改後標題)」与「(取消－標題已被取代及更換)」成对出现。
    这对行当初撑爆了「标题行数=241」的校验 —— 计数时要认得它们。
    """
    for rec in records:
        title = rec.get("title", "")
        rec["title_correction"] = ""
        if rules.superseded_marker and rules.superseded_marker in title:
            rec["title_correction"] = "superseded"
        elif rules.replacement_marker and rules.replacement_marker in title:
            rec["title_correction"] = "replacement"
    return records


# ---------------------------------------------------------------- 坑⑨

def _normalize(title: str) -> str:
    return re.sub(r"\s+", "", title or "")


@dataclass
class MirrorPair:
    """同一份联合公告在双方代码下各归档一次 —— 合并记一单。"""

    title: str
    codes: list[str]
    names: list[str]
    row_ids: list[str]


def find_mirror_pairs(records: list[dict]) -> list[MirrorPair]:
    """找镜像归档（坑⑨正向）：同日同标题、不同股票代码。

    要约方本身也是上市公司时会出现（嘀嗒/同程、巨腾/蓝思、
    东曜/药明系、圣牧/现代牧业）。合并记一单，主体取受要约方。

    ⚠️ 「哪个是受要约方」本模块不自动判 —— 需要看标题里
    「收購 X 全部已發行股份」的 X 是谁，而公司简称与标题里的
    全称未必字面一致。这里只把镜像对挑出来交给人工，
    自动猜错会把整单记到错误的公司名下（坑⑨）。
    """
    buckets: dict[tuple[str, str], list[dict]] = {}
    for rec in records:
        key = (rec.get("date", ""), _normalize(rec.get("title", "")))
        buckets.setdefault(key, []).append(rec)

    pairs = []
    for (_, title), group in buckets.items():
        codes = sorted({r.get("code", "") for r in group if r.get("code")})
        if len(codes) >= 2:
            pairs.append(MirrorPair(
                title=title,
                codes=codes,
                names=sorted({r.get("name", "") for r in group}),
                row_ids=[r.get("row_id", "") for r in group],
            ))
    return pairs


def deal_key(offeror: str, target_code: str) -> str:
    """计数口径：按「要约人 × 标的」（坑⑨反向）。

    同一家公司被不同要约人先后发要约（绿科×2、金川×2、天鸽 MGO+PO）
    要分开记，**绝不去重**。所以 key 必须带要约人。
    """
    return f"{(offeror or '?').strip()}|{(target_code or '?').strip()}"


# ---------------------------------------------------------------- 四层防漏网

@dataclass
class QualityReport:
    counts: dict[str, int]
    contradictions: list[dict]
    random_sample: list[dict]
    reconciliation: dict[str, int]
    reconciled: bool
    notes: list[str] = field(default_factory=list)


def contradiction_check(records: list[dict], rules: Rules) -> list[dict]:
    """第 1 层：矛盾行自检。

    在被灰的行里，筛标题含「恢復買賣／復牌／委任IFA／訂立」的，
    逐条看是不是误杀。这是最强的一层防误杀。
    """
    out = []
    for rec in records:
        verdict: Verdict = rec["verdict"]
        if not verdict.is_grey:
            continue
        hits = [t for t in rules.contradiction_terms if t in rec.get("title", "")]
        if hits:
            out.append({**rec, "contradiction_terms": hits})
    return out


def reconcile_counts(records: list[dict]) -> tuple[dict[str, int], bool]:
    """底部数量校验：每条记录都有归属，一条都不许丢。

    手册原文要求「记录数＝代号数＝简称数＝标题行数＝判定桶合计」，
    那是针对手工表的：代号靠合并单元格向下继承，少一个代号就说明
    继承断了、行错位了，必须立刻停。

    披露易接口没有合并单元格，也就没有继承可断；但它确实会返回
    **本来就没有股票代号**的行（交易所自身公告、部分债务证券发行人）。
    真跑一次全市场，这类行让原来的等式恒不成立，于是「数量校验：不平」
    每次都亮 —— 一个永远在响的警报等于没有警报。

    所以拆成两级：
      硬校验（决定 ok）—— 判定桶合计＝记录数，且每条都有标题。
        这两项一旦不等，就是流程真的丢了行或分类漏判，必须停。
      软缺口（只记数，见 screen() 的 notes）—— 缺代号／缺简称。
        照样逐条数出来摆在报告里，但不阻断流程。
    """
    buckets = Counter(r["verdict"].bucket for r in records)
    total = len(records)
    counts = {
        "记录数": total,
        "有代号数": sum(1 for r in records if str(r.get("code", "")).strip()),
        "有简称数": sum(1 for r in records if str(r.get("name", "")).strip()),
        "有标题数": sum(1 for r in records if str(r.get("title", "")).strip()),
        "判定桶合计": sum(buckets.values()),
    }
    ok = counts["判定桶合计"] == total and counts["有标题数"] == total
    return {**counts, **{f"桶:{k}": v for k, v in sorted(buckets.items())}}, ok


def random_audit(records: list[dict], rules: Rules) -> list[dict]:
    """第 4 层：从灰堆里随机抽 N 条复核。种子固定，抽查结果可复现。

    分层抽：irrelevant 桶动辄几千条，混在一起抽会把「被排除词灰掉的」
    那几十条挤没 —— 而那几十条才是最可能误杀的。所以两层各抽 N 条。
    """
    rng = random.Random(rules.audit_seed)
    out = []
    for stratum in (
        [r for r in records if r["verdict"].is_grey
         and r["verdict"].bucket != IRRELEVANT],
        [r for r in records if r["verdict"].bucket == IRRELEVANT],
    ):
        if stratum:
            out += rng.sample(stratum, min(rules.audit_sample_size, len(stratum)))
    return out


def screen(records: list[dict], rules: Rules) -> QualityReport:
    """跑完整个筛选并出质控报告。records 会被就地加上 verdict 字段。"""
    resolve_title_corrections(records, rules)
    for rec in records:
        rec["verdict"] = classify_title(rec.get("title", ""), rules)

    counts, ok = reconcile_counts(records)
    report = QualityReport(
        counts=dict(Counter(r["verdict"].bucket for r in records)),
        contradictions=contradiction_check(records, rules),
        random_sample=random_audit(records, rules),
        reconciliation=counts,
        reconciled=ok,
    )

    mirrors = find_mirror_pairs(records)
    if mirrors:
        report.notes.append(
            f"发现 {len(mirrors)} 组镜像归档（同日同标题、不同代码），"
            f"需人工指定受要约方后合并记一单（坑⑨）")

    # 软缺口：不阻断，但必须摆出来 —— 缺代号的行没法归到某一单上
    missing_code = [r for r in records if not str(r.get("code", "")).strip()]
    if missing_code:
        sample = "；".join(r.get("title", "")[:24] for r in missing_code[:3])
        report.notes.append(
            f"有 {len(missing_code)} 条记录没有股票代号（披露易本身就没给），"
            f"无法归到具体一单上，已留在表内待人工看。例如：{sample}")

    if not ok:
        report.notes.append("⚠️ 数量校验不平：有记录没进任何判定桶或没有标题，"
                            "先查勘误行与分类漏判（坑⑫）再往下走")
    return report
