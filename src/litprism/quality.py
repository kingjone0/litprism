"""解读质量聚合（N06）。

单一计算源
----------
命令行工具 ``tools/eval_extraction.py`` 与界面质量面板**必须用同一套口径**，
否则同一份数据会给出两个数字，用户无从判断该信哪个。所以聚合逻辑放在这里，
两边都调它。

指标定义
--------
| 指标 | 定义 | 说明 |
|---|---|---|
| 复核覆盖 | 已审 / 全部陈述 | 人到底看了多少 |
| 逐字精度 | 确认且未改动 / 已审 | 机器陈述被原样接受的比例 |
| 修正率 | 确认但改过正文 / 已审 | 方向对但表述需纠正 |
| 拒绝率 | 拒绝 / 已审 | 机器说错了 |
| 可用率 | (逐字 + 修正) / 已审 | 人工处理后能用的比例 |
| 已采集漏提占比 | 人工补录 / (机器 + 人工) | 机器漏了多少；两边都有数才算 |

口径纪律
--------
1. 分母为 0 时一律 ``None``（界面显示 n/a），**不显示 0%**；
2. 已采集漏提占比在机器没有陈述时也不计算 —— 那不是"全漏了"，是没有分母；
3. 分字段报告，不合并成单一总分（总分掩盖"局限"字段 20% 这类问题）；
4. 人工确认只表示"接受这份快照"，不等于内容客观真实。
"""

from __future__ import annotations

from .manual_claims import FIELD_LABELS, FIELDS, normalize_text

_EMPTY = {
    'total': 0, 'reviewed': 0, 'verbatim': 0, 'corrected': 0,
    'rejected': 0, 'machine_supported': 0, 'manual': 0,
}


def _ratio(num: int, den: int) -> float | None:
    """分母为 0 时返回 None —— "不知道"不能写成 0。"""
    return (num / den) if den else None


def aggregate(report: dict, reviews: list[dict], manual: list[dict] | None = None) -> dict[str, dict]:
    """按字段聚合。返回 ``{field: counters}``。

    ``reviews`` 是审核记录（含撤销）；``manual`` 是人工补录。
    """
    items = {i.get('id'): i for i in (report or {}).get('items') or []}

    # 同一陈述可能有多条记录（确认→撤销→再确认），以**最后一条**为准
    last: dict[str, dict] = {}
    for rec in reviews or []:
        cid = rec.get('claim_id')
        if cid:
            last[cid] = rec

    per_field: dict[str, dict] = {}

    def slot(field: str) -> dict:
        return per_field.setdefault(field or '正文', dict(_EMPTY))

    for item in items.values():
        f = slot(item.get('field'))
        f['total'] += 1
        if item.get('status') == 'machine_supported':
            f['machine_supported'] += 1

    for cid, rec in last.items():
        item = items.get(cid)
        if item is None:
            continue  # 陈述来自旧版本，已不在当前报告里
        action = rec.get('action')
        if action == 'revoke':
            continue  # 撤销是回退动作，不计入审核样本
        f = slot(item.get('field'))
        f['reviewed'] += 1
        if action == 'reject':
            f['rejected'] += 1
        elif action == 'confirm':
            reviewed = normalize_text(rec.get('reviewed_text'))
            if reviewed and reviewed != normalize_text(item.get('text')):
                f['corrected'] += 1
            else:
                f['verbatim'] += 1

    for claim in manual or []:
        slot(claim.get('field'))['manual'] += 1

    return per_field


def _decorate(field: str, c: dict) -> dict:
    reviewed = c['reviewed']
    total = c['total']
    machine = total
    manual = c['manual']
    return {
        'field': field,
        'label': FIELD_LABELS.get(field, field),
        **c,
        # 比例：分母为 0 一律 None
        'precision': _ratio(c['verbatim'], reviewed),
        'usable': _ratio(c['verbatim'] + c['corrected'], reviewed),
        'reject_rate': _ratio(c['rejected'], reviewed),
        'coverage': _ratio(reviewed, total),
        'recall': _ratio(manual, machine + manual) if (manual and machine) else None,
    }


def summarize(report: dict, reviews: list[dict], manual: list[dict] | None = None) -> dict:
    """给接口/界面用的完整摘要：分字段 + 合计 + 口径说明。"""
    per_field = aggregate(report, reviews, manual)
    fields = [_decorate(f, c) for f, c in per_field.items()]
    # 陈述多的字段排前面，和评测脚本的排序一致
    fields.sort(key=lambda f: (-f['total'], f['label']))

    totals = dict(_EMPTY)
    for c in per_field.values():
        for k in totals:
            totals[k] += c[k]
    total_row = _decorate('__all__', totals)
    total_row['label'] = '合计'

    reviewed = totals['reviewed']
    manual_count = totals['manual']
    machine = totals['total']

    if reviewed or manual_count:
        if reviewed and manual_count and machine:
            note = f'已人工审核 {reviewed} 条，人工补录 {manual_count} 条'
        elif reviewed:
            note = f'已人工审核 {reviewed} 条；尚无人工补录，已采集漏提占比不可用（不等于 0）'
        elif manual_count and not machine:
            note = f'已补录 {manual_count} 条，但这份解读没有机器陈述可作分母'
        else:
            note = f'已补录 {manual_count} 条；机器陈述尚未人工审核'
    else:
        note = '尚无人工审核与补录：指标不可用，不能按 0 解读'

    return {
        'fields': fields,
        'totals': total_row,
        'has_data': bool(reviewed or manual_count),
        'note': note,
        'definitions': {
            'precision': '确认且未改动 / 已审',
            'usable': '(逐字 + 修正) / 已审',
            'coverage': '已审 / 全部陈述',
            'recall': '已采集漏提占比 = 人工补录 / (机器陈述 + 人工补录)，不是真实召回率',
        },
    }


def empty_summary() -> dict:
    """没有任何解读时的空摘要。"""
    return summarize({}, [], [])
