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


def _caption_regions(page, regions, captions):
    """Bind a caption to its own ruled frame, never the next table's header.

    Captions are often below tables. A previous caption can be nearer to the
    next header than that table's own caption. Match from caption to frame
    first, then expand through interior rules up to the caption boundary.
    """
    def same_column(a, b):
        return a[0]-12 <= b[0] <= a[2] and min(a[2], b[2])-max(a[0], b[0]) > 20
    def gap(a, b):
        return min(abs(b[1]-a[3]), abs(b[3]-a[1]))
    owners = {}
    for ci, (caption_box, _) in enumerate(captions):
        near = sorted((gap(box, caption_box), ri) for ri, (box, _) in enumerate(regions)
                      if same_column(box, caption_box))
        if near and near[0][0] < 120 and (len(near) == 1 or near[1][0]-near[0][0] > 3):
            owners[ci] = near[0][1]
    rules = [tuple(d['rect']) for d in page.get_drawings()
             if d['rect'].width >= 100 and d['rect'].height <= 1]
    bound, unresolved = [], []
    for ri, (box, divider) in enumerate(regions):
        assigned = [ci for ci, owner in owners.items() if owner == ri]
        if len(assigned) != 1:
            nearby = any(same_column(box, cb) and gap(box, cb) < 120 for cb, _ in captions)
            unresolved.append((box, divider, '', 'ambiguous' if nearby else 'missing'))
            continue
        ci = assigned[0]
        cb, caption = captions[ci]
        peers = [b for j, (b, _) in enumerate(captions) if j != ci and same_column(box, b)]
        if cb[1] >= box[3]:
            low = max((b[3] for b in peers if b[3] < box[1]), default=0)
            high = cb[1]
        elif cb[3] <= box[1]:
            low = cb[3]
            high = min((b[1] for b in peers if b[1] > box[3]), default=page.rect.height)
        else:
            unresolved.append((box, divider, '', 'ambiguous'))
            continue
        lines = sorted({r[1] for r in rules if abs(r[0]-box[0]) < 4 and abs(r[2]-box[2]) < 4
                        and low < r[1] < high})
        if len(lines) >= 3 and 5 < lines[1]-lines[0] < 60 and lines[-1]-lines[0] < 700:
            box, divider = (box[0], lines[0], box[2], lines[-1]), lines[1]
        bound.append((box, divider, caption, 'bound'))
    # An interior separator may have produced a second partial region; the
    # full frame already owns it and must replace all its numeric raw text.
    for region in unresolved:
        box = region[0]
        if not any(b[0] <= box[0]+1 and b[1] <= box[1]+1 and b[2] >= box[2]-1 and b[3] >= box[3]-1
                   for b, *_ in bound):
            bound.append(region)
    return sorted(bound, key=lambda r: (r[0][1], r[0][0]))


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
    regions = _caption_regions(page, regions, captions)
    records, replacements = [], []
    for index, (box, divider, caption, caption_status) in enumerate(regions, 1):
        locator = f'p.{page.number+1} 表格区域{index}'
        if caption:
            locator += ' 已绑定表题：'+caption
        try:
            result = _numeric_records(page, box, divider) if caption_status != 'ambiguous' else None
        except Exception:
            result = None
        record = {'bbox': list(box), 'locator': locator, 'caption': caption, 'caption_status': caption_status,
                  'status': 'structured' if result else 'needs_review'}
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
