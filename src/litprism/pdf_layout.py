"""Conservative ruled-table reading. Raw PDF text remains available separately.

Only complete numeric rows with unique, geometrically recovered headers become
citable records. Ambiguous tables keep a locator, never guessed column names.
"""
from __future__ import annotations

import json
import os
import re

# The sidecar stdout is a handshake pipe, not a channel for library advertisements.
os.environ.setdefault('PYMUPDF_SUGGEST_LAYOUT_ANALYZER', '0')

_NUMBER = re.compile(r'^[+−-]?\d+(?:[.,]\d+)*(?:[eE][+−-]?\d+)?(?:%|[a-zA-Z*†‡]+)?$')
_CAPTION = re.compile(r'^(?:Table|表)\s*[A-Za-z0-9]+(?:\.\d+)*\s*[:：.]', re.I)


def _regions(page):
    groups = []
    for drawing in page.get_drawings():
        rect = drawing['rect']
        if rect.width < 100 or rect.height > 1:
            continue
        group = next((g for g in groups if abs(g[0][0]-rect.x0) < 4
                      and abs(g[0][2]-rect.x1) < 4), None)
        if group is None:
            groups.append([tuple(rect)])
        else:
            group.append(tuple(rect))
    regions = []
    for group in groups:
        lines = sorted(set(group), key=lambda r: r[1])
        for top, mid, bottom in zip(lines, lines[1:], lines[2:], strict=False):
            if not (5 < mid[1]-top[1] < 60 and 10 < bottom[1]-mid[1] < 700):
                continue
            box = (min(top[0], mid[0], bottom[0]), top[1],
                   max(top[2], mid[2], bottom[2]), bottom[1])
            if any(box[0] < r[0][2]-1 and box[2] > r[0][0]+1
                   and box[1] < r[0][3]-1 and box[3] > r[0][1]+1 for r in regions):
                continue
            words = page.get_text('words', clip=(box[0], mid[1], box[2], box[3]))
            numeric_rows = {round(w[1]/3) for w in words if _NUMBER.fullmatch(w[4])}
            if len(numeric_rows) >= 2:
                regions.append((box, mid[1]))
    return sorted(regions, key=lambda r: (r[0][1], r[0][0]))


def _numeric_records(page, box, divider):
    tables = page.find_tables(clip=box, strategy='text').tables
    if len(tables) != 1:
        return None
    table = tables[0]
    headers, body = [], []
    for row, values in zip(table.rows, table.extract(), strict=True):
        values = [' '.join((v or '').split()) for v in values]
        if not any(values):
            continue
        (headers if (row.bbox[1]+row.bbox[3])/2 < divider else body).append(values)
    if not headers or len(body) < 2 or table.col_count < 2:
        return None
    # The text strategy can put a vertical cut *inside* a word. Such a grid
    # yields plausible but truncated headers and must never become evidence.
    cuts = sorted({cell[0] for row in table.rows for cell in row.cells if cell})[1:]
    for word in page.get_text('words', clip=box):
        if any(word[0]+.5 < cut < word[2]-.5 for cut in cuts):
            return None
    names = [' '.join(row[i] for row in headers if row[i]).strip() for i in range(table.col_count)]
    names[0] = names[0] or '行'
    if not all(names) or len(set(names)) != len(names):
        return None
    if any(not any(c.isalpha() for c in name) for name in names):
        return None
    if any(not row[0] or not row[0][0].isalpha()
           or not all(_NUMBER.fullmatch(v) for v in row[1:]) for row in body):
        return None
    return names, body


def reading_layout(page):
    """Return (reading text, table regions). Does not modify page/PDF bytes."""
    regions = _regions(page)
    if not regions:
        return None, []
    lines = []
    for block in page.get_text('dict').get('blocks', []):
        for line in block.get('lines', []):
            lines.append((line['bbox'], ''.join(s['text'] for s in line['spans'])))
    captions = [(box, text) for box, text in lines if _CAPTION.match(text.strip())]
    records, replacements = [], []
    for index, (box, divider) in enumerate(regions, 1):
        near = sorted(captions, key=lambda c: min(abs(c[0][1]-box[3]), abs(c[0][3]-box[1])))
        caption = near[0][1] if near and min(abs(near[0][0][1]-box[3]), abs(near[0][0][3]-box[1])) < 120 else ''
        locator = f'p.{page.number+1} 表格区域{index}'
        if caption:
            locator += ' 邻近表题：'+caption
        try:
            result = _numeric_records(page, box, divider)
        except Exception:
            result = None
        record = {'bbox': list(box), 'locator': locator, 'status': 'structured' if result else 'needs_review'}
        if result:
            names, rows = result
            record.update(headers=names, rows=rows)
            # Repeat every header in every row so chunk/batch boundaries cannot detach them.
            text = '\n'.join('【表格行】'+locator+' '+json.dumps(dict(zip(names, row, strict=True)), ensure_ascii=False)
                             for row in rows)
        else:
            text = '【表格待核对】'+locator+'；行列或表头未可靠恢复，该区域数值未纳入可引用片段，请核对原 PDF。'
        replacements.append((box, text))
        records.append(record)
    ordered, inserted = [], set()
    for box, text in lines:
        x, y = (box[0]+box[2])/2, (box[1]+box[3])/2
        region = next((i for i, (r, _) in enumerate(replacements)
                       if r[0] <= x <= r[2] and r[1] <= y <= r[3]), None)
        if region is None:
            ordered.append(text)
        elif region not in inserted:
            ordered.append(replacements[region][1])
            inserted.add(region)
    return '\n'.join(ordered), records
