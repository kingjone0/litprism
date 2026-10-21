"""人工补录机器漏提的陈述（N05 / 原 E-1）。

为什么需要它
------------
审核界面只有「确认 / 修正 / 拒绝 / 撤销」四个动作，全部作用于**机器已经提出**的陈述。
所以无论人审多少遍，都产生不了"机器漏了什么"的信号 —— 已采集漏提占比一直是 n/a。

补录把这个信号补上：人从原文指出一条机器没提的陈述，系统记账。

口径
----
    已采集漏提占比 = 人工补录数 / (机器陈述数 + 人工补录数)

这个比例只在**有人真的去补**时才有意义，所以未采集时一律返回 ``None``（界面显示 n/a），
绝不用 0 冒充。同时必须同时给出**绝对数** —— 只看比例会诱导"少补 = 指标好看"。

为什么必须绑原文引用
--------------------
没有引用的补录无法与"凭印象补一条"区分，分母就不可信。
所以 ``quote`` 必须能在 ``chunk_id`` 的原文里逐字找到，找不到直接拒绝。
"""

from __future__ import annotations

import re
import uuid

from .semantics import payload_hash

# 与解读六字段保持一致（与 extract._FIELDS 同源；此处单独列一份，
# 避免 web 层反向依赖 extract 的私有常量）。
FIELDS: list[tuple[str, str]] = [
    ('research_question', '研究问题'),
    ('methodology', '方法论'),
    ('experiment_design', '实验设计'),
    ('key_conclusions', '关键结论'),
    ('innovations', '创新点'),
    ('limitations', '局限与未来工作'),
]
FIELD_KEYS = {k for k, _ in FIELDS}
FIELD_LABELS = dict(FIELDS)

MAX_TEXT = 2000
MAX_QUOTE = 2000
MAX_NOTE = 5000

_WS = re.compile(r'\s+')


class ClaimRejected(ValueError):
    """补录被拒绝。``reason`` 直接展示给用户。"""

    def __init__(self, reason: str, *, status: int = 400):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def normalize_text(text: str) -> str:
    """折叠空白后比较：PDF 抽取常把一句话断成多行，逐字比较会误拒。"""
    return _WS.sub(' ', text or '').strip()


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def validate(kb, paper_id: str, req: dict) -> dict:
    """校验并规范化一条补录。不写库。"""
    field = (req.get('field') or '').strip()
    if field not in FIELD_KEYS:
        raise ClaimRejected('请选择要补入的字段。')

    text = (req.get('text') or '').strip()
    if not text:
        raise ClaimRejected('请填写这条陈述的内容。')
    if len(text) > MAX_TEXT:
        raise ClaimRejected(f'陈述过长（上限 {MAX_TEXT} 字），请拆成多条。')

    quote = (req.get('quote') or '').strip()
    if not quote:
        raise ClaimRejected('请从原文中选出一段作为依据；没有依据的补录不计入已采集漏提占比。')
    if len(quote) > MAX_QUOTE:
        raise ClaimRejected(f'引用过长（上限 {MAX_QUOTE} 字）。')

    chunk_id = (req.get('chunk_id') or '').strip()
    if not chunk_id:
        raise ClaimRejected('请指定这段引用来自哪个原文块。')
    row = kb._con.execute(
        'SELECT id, paper_id, page, text, material_id FROM chunks WHERE id=?', (chunk_id,)
    ).fetchone()
    if row is None:
        raise ClaimRejected('原文块不存在，可能已被重新导入。', status=409)
    if row['paper_id'] != paper_id:
        raise ClaimRejected('该原文块不属于这篇论文。')
    analysis = kb.get_analysis(paper_id)
    # 只在**分析确实绑定了材料版本**时才比对。v12 之前的分析 material_id 为空，
    # 而 chunks 已被迁移回填 —— 拿 None 去比会让所有历史论文的补录一律失败。
    analysis_material = (analysis or {}).get('material_id')
    if analysis_material and analysis_material != row['material_id']:
        raise ClaimRejected('原文块与当前分析的材料版本不一致，请选择该分析对应的原文。', status=409)

    if normalize_text(quote) not in normalize_text(row['text']):
        raise ClaimRejected(
            '引用与原文不一致：这段文字在该原文块里找不到。请直接选中原文句子，不要改写。'
        )

    note = (req.get('note') or '').strip()
    if len(note) > MAX_NOTE:
        raise ClaimRejected(f'说明过长（上限 {MAX_NOTE} 字）。')

    return {
        'field': field,
        'text': text,
        'quote': quote,
        'chunk_id': chunk_id,
        'page': row['page'],
        'note': note,
    }


def add(kb, paper_id: str, analysis_hash: str, req: dict) -> dict:
    """写入一条补录，返回记录。"""
    clean = validate(kb, paper_id, req)

    if req.get('analysis_hash') and req['analysis_hash'] != analysis_hash:
        raise ClaimRejected('解读版本已变化，请重新读取陈述与原文后再补录。', status=409)

    # 同一版本内，同字段同文本只允许一条 —— 否则重复点击会污染分母
    for existing in list_claims(kb, paper_id, analysis_hash):
        if existing['field'] == clean['field'] and normalize_text(existing['text']) == normalize_text(clean['text']):
            raise ClaimRejected('这条陈述已经补录过了。', status=409)

    claim_id = 'mc_' + uuid.uuid4().hex[:12]
    with kb.tx() as con:
        con.execute(
            'INSERT INTO manual_claims (id, paper_id, analysis_hash, field, text,'
            ' chunk_id, page, quote, note, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (claim_id, paper_id, analysis_hash, clean['field'], clean['text'],
             clean['chunk_id'], clean['page'], clean['quote'], clean['note'], _now()),
        )
    return next(c for c in list_claims(kb, paper_id, analysis_hash) if c['id'] == claim_id)


def list_claims(kb, paper_id: str, analysis_hash: str) -> list[dict]:
    rows = kb._con.execute(
        'SELECT * FROM manual_claims WHERE paper_id=? AND analysis_hash=? ORDER BY rowid',
        (paper_id, analysis_hash),
    ).fetchall()
    return [
        {
            'id': r['id'], 'field': r['field'], 'field_label': FIELD_LABELS.get(r['field'], r['field']),
            'text': r['text'], 'chunk_id': r['chunk_id'], 'page': r['page'],
            'quote': r['quote'], 'note': r['note'] or '', 'created_at': r['created_at'],
            'origin': 'manual',
        }
        for r in rows
    ]


def delete(kb, paper_id: str, analysis_hash: str, claim_id: str) -> bool:
    """删除一条补录。写错了必须能撤回，否则它会永久留在已采集漏提占比分母里。"""
    with kb.tx() as con:
        cur = con.execute(
            'DELETE FROM manual_claims WHERE id=? AND paper_id=? AND analysis_hash=?',
            (claim_id, paper_id, analysis_hash),
        )
    return bool(cur.rowcount)


def recall(machine: int, manual: int) -> dict:
    """已采集漏提占比统计。

    只在**两边都有数**时才给比例：

    * 没有补录 → 还没人去补，不等于已采集漏提占比 0%；
    * 机器没有陈述 → 没有可比的分母，也不能算成 100%（那不是"全漏了"，
      而是这份解读根本没产出可核对的陈述）。

    两种情形都返回 ``ratio=None``，界面显示 n/a，并说明缺的是哪一边。
    同时始终给出绝对数 —— 只看比例会诱导"少补 = 指标好看"。
    """
    total = machine + manual
    if manual and machine:
        ratio = manual / total
        note = f'人工补录 {manual} 条，机器提出 {machine} 条'
    elif manual:
        ratio = None
        note = f'已补录 {manual} 条，但这份解读没有机器陈述可作分母，已采集漏提占比不可计算'
    else:
        ratio = None
        note = '尚未有人工补录，已采集漏提占比不可用（不等于 0）'
    return {'machine': machine, 'manual': manual, 'total': total, 'ratio': ratio, 'note': note}


def report_for(kb, paper_id: str, payload: dict) -> dict:
    """把某次分析的补录与已采集漏提占比整理成接口返回结构。"""
    digest = payload_hash(payload)
    claims = list_claims(kb, paper_id, digest)
    machine = len((payload.get('_semantic_review') or {}).get('items') or [])
    return {
        'analysis_hash': digest,
        'items': claims,
        'recall': recall(machine, len(claims)),
    }
