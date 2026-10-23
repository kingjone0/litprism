"""Immutable pre-review candidates, separate from the published analysis.

Stored in the existing analysis-version archive so backups already preserve
them. The ac_ namespace is excluded from normal historical-analysis lists.
"""
from __future__ import annotations

import json

from .evidence import citations_for, stored_excerpts
from .materials import MaterialChanged
from .semantics import payload_hash


def save(kb, pid, material_id, candidates, excerpts, *, model, run_id=None, parent_hash=None):
    if not candidates:
        return None
    from .knowledge import now
    with kb.tx() as con:
        paper = kb.get_paper(pid) or {}
        if not material_id or paper.get('current_material_id') != material_id:
            raise MaterialChanged('保存候选前原文材料已变化。')
        citations = citations_for(excerpts)
        stored_excerpts(kb, citations)
        parent = (kb.get_analysis(pid) or {}).get('payload') or {}
        if parent_hash is not None and payload_hash(parent) != parent_hash:
            raise MaterialChanged('提取期间分析版本已变化，未发布本轮候选。')
        payload = {'_stage': 'candidate', '_model': model, '_analysis_run_id': run_id,
                   '_parent_analysis_hash': payload_hash(parent), '_candidates': candidates,
                   '_citations': citations, '_semantic_review': {
                       'status': 'not_run', 'passed': False, 'items': [], 'differences': [],
                       'supported': 0, 'pending': len(candidates),
                       'note': '提取候选已保存，机器复核尚未完成；可以直接核对原文并人工审核。'}}
        identity = 'ac_'+payload_hash([pid, material_id, payload])
        con.execute('INSERT OR IGNORE INTO analysis_versions VALUES (?,?,?,?,?,?)',
                    (identity, pid, material_id, json.dumps(payload, ensure_ascii=False), model, now()))
    return identity


def pending(kb, pid, analysis=None):
    """Only the latest current, unfinalized branch can be actively reviewed."""
    row = kb._con.execute("SELECT id FROM analysis_versions WHERE paper_id=? AND id LIKE 'ac_%' ORDER BY rowid DESC LIMIT 1", (pid,)).fetchone()
    if not row:
        return None
    candidate = kb.get_analysis(pid, version_id=row['id'])
    if not candidate or not candidate.get('material_current'):
        return None
    payload = candidate['payload']
    current = (analysis if analysis is not None else kb.get_analysis(pid)) or {}
    published = current.get('payload') or {}
    if published.get('_candidate_snapshot_id') == row['id']:
        return None
    if payload.get('_stage') != 'candidate' or payload.get('_parent_analysis_hash') != payload_hash(published):
        return None
    return candidate


def by_run(kb, pid, run_id):
    row = kb._con.execute("SELECT id FROM analysis_versions WHERE paper_id=? AND id LIKE 'ac_%' "
                          "AND json_extract(payload, '$._analysis_run_id')=? ORDER BY rowid DESC LIMIT 1",
                          (pid, run_id)).fetchone()
    return kb.get_analysis(pid, version_id=row['id']) if row else None
