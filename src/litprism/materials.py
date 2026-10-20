"""Immutable, ordered material snapshots. IDs of historical chunks never change."""
from __future__ import annotations

import hashlib
import json
import sqlite3


class MaterialChanged(ValueError):
    """The input snapshot changed while generating an analysis."""


def content_hash(chunks):
    """Include boundaries and physical locations, not ephemeral chunk IDs."""
    body = [[c.get('page'), c.get('section'), c['text'].strip()] for c in chunks]
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def migrate(conn):
    """v12 is atomic; a failed backfill rolls back columns and data together."""
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    conn.execute('BEGIN')
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS materials (
            id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            source TEXT, origin TEXT, content_hash TEXT NOT NULL, imported_at TEXT NOT NULL,
            level TEXT NOT NULL, status TEXT NOT NULL,
            UNIQUE(paper_id, content_hash))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS analysis_versions (
            id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            material_id TEXT REFERENCES materials(id), payload TEXT NOT NULL, model TEXT,
            created_at TEXT, UNIQUE(paper_id, id))''')
        for table, column in [('papers', 'current_material_id'), ('chunks', 'material_id'),
                              ('analyses', 'material_id')]:
            if column not in {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} TEXT REFERENCES materials(id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_chunks_material ON chunks(material_id)')
        for paper in conn.execute('SELECT * FROM papers WHERE current_material_id IS NULL').fetchall():
            p = dict(paper)
            chunks = [dict(r) for r in conn.execute(
                'SELECT * FROM chunks WHERE paper_id=? ORDER BY rowid', (p['id'],))]
            if not chunks:
                continue
            mid = 'mat_legacy_' + hashlib.sha256(p['id'].encode()).hexdigest()[:20]
            conn.execute('INSERT OR IGNORE INTO materials VALUES (?,?,?,?,?,?,?,?)',
                         (mid, p['id'], p['source'], p['origin'], content_hash(chunks),
                          p['created_at'] or '', p['material_level'] or 'unknown', 'current'))
            conn.execute('UPDATE chunks SET material_id=? WHERE paper_id=? AND material_id IS NULL', (mid, p['id']))
            conn.execute('UPDATE papers SET current_material_id=? WHERE id=?', (mid, p['id']))
            # Bind using citation ownership. Ambiguous legacy provenance stays unbound.
            analysis = conn.execute('SELECT payload FROM analyses WHERE paper_id=?', (p['id'],)).fetchone()
            if analysis:
                payload = json.loads(analysis['payload'] or '{}')
                ids = {c['id'] for c in chunks}
                refs = payload.get('_citations') or []
                if refs and all(c.get('chunk_id') in ids for c in refs):
                    conn.execute('UPDATE analyses SET material_id=? WHERE paper_id=?', (mid, p['id']))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.row_factory = previous_factory
