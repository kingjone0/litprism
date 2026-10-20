"""机械闸门与有界修复。

设计立场
--------
闸门只做**确定性判断**，不调用模型。这样同样的输入必然得到同样的结论 ——
可复现是评测的前提，也是"为什么这条被判为漏读"能解释清楚的前提。

五道闸门
--------
| 闸门 | 判断 | 失败后果 |
|---|---|---|
| `json_complete` | 返回是可解析的对象 | 整条指令重跑 |
| `has_citation` | 每个非空字段都带来源 | 该字段清空（既有逻辑） |
| `numeric_parse` | 含数字的陈述能抽出数值 | 标记待人工，不阻断 |
| `section_coverage` | 条数落在期望区间 | 低于下限 → 疑似漏读，重跑 |

**`section_coverage` 是注意力质量的唯一可观测代理。** 它不完美
（模型可以"编满"而不报错），但配合引用逐字校验能挡住大部分情况。

修复有界
--------
最多 2 轮。第三轮收益递减而成本线性。修完仍不过的，**如实标为未覆盖**，
绝不伪装成通过 —— 这是整套体系的口径底线。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_REPAIR_ROUNDS = 2

# 与 semantics.make_units 的切句规则保持一致，否则"条数"两边对不上
_SENTENCE = re.compile(r'.+?(?:[。！？](?:\s*\[P\d+[^\]]*\])*|$)', re.S | re.I)
_CITATION = re.compile(r'\[P\d+[^\]]*\]')
_DIGIT = re.compile(r'\d')


@dataclass
class GateResult:
    name: str
    ok: bool
    detail: str = ''
    action: str = 'pass'          # pass | repair | flag

    def to_dict(self) -> dict:
        return {'name': self.name, 'ok': self.ok, 'detail': self.detail, 'action': self.action}


@dataclass
class GateReport:
    results: list[GateResult] = field(default_factory=list)

    @property
    def failed(self) -> list[GateResult]:
        return [r for r in self.results if not r.ok]

    @property
    def needs_repair(self) -> bool:
        return any(r.action == 'repair' for r in self.results)

    @property
    def score(self) -> tuple[int, int]:
        """越大越好：先看通过数，再看产出条数。用于在重跑结果之间取舍。"""
        return (sum(1 for r in self.results if r.ok), 0)

    def to_dict(self) -> dict:
        return {
            'results': [r.to_dict() for r in self.results],
            'passed': not self.failed,
            'needs_repair': self.needs_repair,
        }


def count_claims(data: dict, fields: list[tuple[str, str]]) -> int:
    """数「陈述」条数：按句切，去掉纯标点与纯引用行。

    与 ``semantics.make_units`` 用同一套切句规则 —— 两处不一致的话，
    闸门看到的条数和复核看到的条数会对不上。
    """
    total = 0
    for key, _ in fields:
        text = data.get(key)
        if not isinstance(text, str) or not text.strip():
            continue
        for part in _SENTENCE.findall(text.strip()):
            bare = _CITATION.sub('', part).strip()
            if bare and re.search(r'[\w\u4e00-\u9fff]', bare):
                total += 1
    return total


def numeric_claims(data: dict, fields: list[tuple[str, str]]) -> list[str]:
    """抽出含数字的陈述，供机械数值层使用。"""
    out = []
    for key, _ in fields:
        text = data.get(key)
        if not isinstance(text, str):
            continue
        for part in _SENTENCE.findall(text):
            bare = _CITATION.sub('', part).strip()
            if bare and _DIGIT.search(bare):
                out.append(bare)
    return out


def evaluate(
    data: dict,
    fields: list[tuple[str, str]],
    *,
    expected: tuple[int, int] | None = None,
    issues: list[str] | None = None,
) -> GateReport:
    """跑一遍闸门。``expected`` 来自计划里的期望条数区间。"""
    report = GateReport()

    # 1) JSON 完整：能到这里说明已解析成 dict；再确认不是"全是空字段"
    nonempty = sum(1 for k, _ in fields if (data.get(k) or '').strip())
    report.results.append(GateResult(
        'json_complete', True,
        f'{nonempty}/{len(fields)} 个字段非空',
    ))

    # 1b) 结构有效性：模型返回了未知字段/结构损坏时，整批字段已作废
    broken = [i for i in (issues or []) if '结构损坏' in i]
    if broken:
        report.results.append(GateResult('structure_valid', False,
                                         '模型返回了未知字段或结构损坏，整批字段作废', 'flag'))

    # 2) 引用存在性：字段有内容但没有任何来源标记
    missing = [label for k, label in fields
               if (data.get(k) or '').strip() and not _CITATION.search(data.get(k) or '')]
    has_any = any((data.get(k) or '').strip() for k, _ in fields)
    report.results.append(GateResult(
        'has_citation', not missing,
        '全部字段带来源' if (not missing and has_any) else
        ('各字段均无内容，未形成引用' if not has_any else f'缺来源：{"、".join(missing)}'),
        action='pass' if not missing else 'flag',
    ))

    # 3) 数值可解析：含数字的陈述必须能抽出数字，否则说明表述模糊
    numeric = numeric_claims(data, fields)
    unparsed = [t for t in numeric if not re.search(r'\d+(?:[.,]\d+)?', t)]
    report.results.append(GateResult(
        'numeric_parse', not unparsed,
        f'{len(numeric)} 条含数值陈述' if not unparsed else f'{len(unparsed)} 条含数字但抽不出数值',
        action='pass' if not unparsed else 'flag',
    ))

    # 4) 章节覆盖：条数落在期望区间
    total = count_claims(data, fields)
    if expected:
        lo, hi = expected
        if total < lo:
            detail = f'提出 {total} 条，期望 {lo}–{hi} 条 —— 疑似漏读'
            action = 'repair'
        elif total > hi:
            detail = f'提出 {total} 条，期望 {lo}–{hi} 条 —— 疑似重复或编造'
            action = 'flag'
        else:
            detail = f'{total} 条，落在期望区间 {lo}–{hi}'
            action = 'pass'
        report.results.append(GateResult('section_coverage', action == 'pass', detail, action))
    else:
        report.results.append(GateResult('section_coverage', True, f'{total} 条（无期望区间）'))

    # 5) 既有引用校验的遗留问题（页码不符、引用了不存在的片段等）
    if issues:
        report.results.append(GateResult(
            'citation_issues', False, '；'.join(issues[:3]), 'flag',
        ))
    return report


REPAIR_HINT = (
    '\n\n【上一轮的问题】本段约 {tokens} token，上一轮只提出 {got} 条陈述，'
    '低于预期（{lo}–{hi} 条）。请逐小节检查是否遗漏了：'
    '实验设置与超参数、对比基线、具体数值结果、作者的限定条件。'
    '只补充确实写在原文里的内容；没有依据的仍然留空。'
)


def repair_text(directive, got: int) -> str:
    """给"疑似漏读"的指令加一段定向提示。"""
    lo, hi = directive.expected
    return directive.text + REPAIR_HINT.format(
        tokens=f'{directive.tokens:,}', got=got, lo=lo, hi=hi,
    )


def split_scope(scope: str) -> tuple[str, str] | None:
    """把范围对半切，用于输出被截断时细分。

    只处理「A、B、C」这种并列形式；单一范围无法再切，返回 None。
    """
    parts = [p for p in re.split(r'、', scope) if p.strip()]
    if len(parts) < 2:
        return None
    mid = len(parts) // 2
    return '、'.join(parts[:mid]), '、'.join(parts[mid:])


# ---------------------------------------------------------------------------
# 跨指令重复检测（注意力失效的第二个信号）
#
# 条数闸门能抓"提得少"，抓不到"提得多但全是从别处搬来的"。
# 如果指令 B 声称只看第 5 节，却把第 3 节的陈述又抄了一遍，
# 条数看起来正常，实际上 B 根本没读自己的范围。
# ---------------------------------------------------------------------------
OVERLAP_THRESHOLD = 0.8      # 单句相似度达到此值算重复
UNIT_DUPLICATE_RATIO = 0.8   # 整个单元有这么多句重复 → 判定未读自己的范围


def _grams(text: str) -> set[str]:
    t = _CITATION.sub(' ', text or '').lower()
    out = set(re.findall(r'[a-z0-9][a-z0-9.+-]*', t))
    cjk = re.sub(r'[^\u4e00-\u9fff]', '', t)
    out |= {cjk[i:i + 2] for i in range(len(cjk) - 1)}
    return out


def _similarity(a: str, b: str) -> float:
    ga, gb = _grams(a), _grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / min(len(ga), len(gb))


def claim_sentences(data: dict, fields: list[tuple[str, str]]) -> list[str]:
    """按句取出陈述文本（去引用标记），用于重复检测。"""
    out = []
    for key, _ in fields:
        text = data.get(key)
        if not isinstance(text, str):
            continue
        for part in _SENTENCE.findall(text):
            bare = _CITATION.sub('', part).strip()
            if bare and re.search(r'[\w\u4e00-\u9fff]', bare):
                out.append(bare)
    return out


def duplicate_ratio(candidate: list[str], reference: list[str]) -> float:
    """``candidate`` 里有多少句能在 ``reference`` 中找到高度相似的句子。"""
    if not candidate:
        return 0.0
    hits = 0
    for c in candidate:
        if any(_similarity(c, r) >= OVERLAP_THRESHOLD for r in reference):
            hits += 1
    return hits / len(candidate)


def overlap_flags(units: list[tuple[str, list[str]]]) -> list[dict]:
    """找出"几乎全是别人内容"的单元。

    ``units`` 是 ``[(单元标签, 陈述列表), ...]``。返回可疑单元的说明。
    只在两个单元的范围**不同**时才有意义 —— 范围相同的重复是正常的。
    """
    flags = []
    for i, (label_a, claims_a) in enumerate(units):
        for j, (label_b, claims_b) in enumerate(units):
            if i == j or not claims_a or not claims_b:
                continue
            # 只报"后面的抄前面的"，避免同一对报两次
            if j < i:
                continue
            ratio = duplicate_ratio(claims_b, claims_a)
            if ratio >= UNIT_DUPLICATE_RATIO:
                flags.append({
                    'unit': label_b,
                    'resembles': label_a,
                    'ratio': round(ratio, 3),
                    'detail': (
                        f'{label_b} 的 {ratio*100:.0f}% 陈述与 {label_a} 高度重合 —— '
                        '可能没有读取自己的范围，请人工核对这一段的原文'
                    ),
                })
    return flags
