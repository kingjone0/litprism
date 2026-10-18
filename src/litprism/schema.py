"""数据库 Schema 定义与版本化迁移。

**单一事实来源**：所有建表语句集中在这里，`db.py` 只负责调用。

迁移机制
--------
用 SQLite 内建的 ``PRAGMA user_version`` 记录当前版本。每次启动时：

1. 读 ``user_version``；
2. 按版本顺序应用所有「大于当前版本」的迁移；
3. 每应用一个迁移，立刻把 ``user_version`` 推到该版本。

这样既是**幂等**的（重复启动不会重复执行），也是**可增量升级**的
（老库只跑缺的那几个迁移）。

**关于 ALTER TABLE**：SQLite 不支持 ``ADD COLUMN IF NOT EXISTS``，
所以新增列一律走 ``_column_exists()`` 检查后再加，保证迁移可重复执行。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# v1 —— 基础表（已有实现，保持不变）
# ---------------------------------------------------------------------------
SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS papers (
    id              TEXT PRIMARY KEY,
    doi             TEXT,
    arxiv_id        TEXT,
    title           TEXT NOT NULL,
    authors         TEXT DEFAULT '[]',
    year            INTEGER,
    venue           TEXT,
    abstract        TEXT,
    url             TEXT,
    source          TEXT DEFAULT 'local_pdf',
    origin          TEXT,
    verify_status   TEXT DEFAULT 'unverified',
    verify_level    TEXT DEFAULT 'unknown',
    verify_confidence REAL DEFAULT 0.0,
    verify_evidence TEXT DEFAULT '{}',
    verified_at     TEXT,
    tags            TEXT DEFAULT '[]',
    note            TEXT DEFAULT '',
    full_text       TEXT,
    fingerprint     TEXT,
    created_at      TEXT,
    updated_at      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_doi  ON papers(doi) WHERE doi IS NOT NULL AND doi <> '';
CREATE INDEX IF NOT EXISTS idx_papers_fp          ON papers(fingerprint);
CREATE INDEX IF NOT EXISTS idx_papers_verify      ON papers(verify_status);

CREATE TABLE IF NOT EXISTS chunks (
    rowid     INTEGER PRIMARY KEY AUTOINCREMENT,
    id        TEXT UNIQUE NOT NULL,
    paper_id  TEXT NOT NULL,
    page      INTEGER,
    section   TEXT,
    text      TEXT NOT NULL,
    FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chunks_paper ON chunks(paper_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    seg_text,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS analyses (
    paper_id          TEXT PRIMARY KEY,
    research_question TEXT,
    methodology       TEXT,
    experiment_design TEXT,
    key_conclusions   TEXT,
    innovations       TEXT,
    limitations       TEXT,
    field             TEXT,
    keywords          TEXT DEFAULT '[]',
    payload           TEXT DEFAULT '{}',
    model             TEXT,
    created_at        TEXT,
    FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS concepts (
    id         TEXT PRIMARY KEY,
    paper_id   TEXT,
    term       TEXT NOT NULL,
    definition TEXT,
    category   TEXT,
    created_at TEXT,
    FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_concepts_term ON concepts(term);
CREATE INDEX IF NOT EXISTS idx_concepts_paper ON concepts(paper_id);

CREATE TABLE IF NOT EXISTS relations (
    id          TEXT PRIMARY KEY,
    paper_a     TEXT NOT NULL,
    paper_b     TEXT NOT NULL,
    rel_type    TEXT,
    score       REAL DEFAULT 0.0,
    evidence    TEXT DEFAULT '',
    created_at  TEXT,
    UNIQUE(paper_a, paper_b, rel_type)
);
CREATE INDEX IF NOT EXISTS idx_rel_a ON relations(paper_a);
CREATE INDEX IF NOT EXISTS idx_rel_b ON relations(paper_b);

CREATE TABLE IF NOT EXISTS verify_log (
    id         TEXT PRIMARY KEY,
    query      TEXT,
    doi        TEXT,
    title      TEXT,
    result     TEXT,
    level      TEXT,
    confidence REAL,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_vlog_title ON verify_log(title);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# ---------------------------------------------------------------------------
# v2 —— 会话 / 压缩 / 记忆 / 白名单 / 交付物 / 提供商 / MCP / OCR
# ---------------------------------------------------------------------------
SCHEMA_V2 = """
-- ===================== 知识库分组 =====================
CREATE TABLE IF NOT EXISTS collections (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    parent_id   TEXT,
    sort_order  INTEGER DEFAULT 0,
    created_at  TEXT,
    deleted_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_collections_parent ON collections(parent_id);

-- ===================== 会话与消息 =====================
-- append-only 事件日志。压缩只追加事件，永不原地改写。
CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    scope       TEXT,                     -- JSON：绑定的分组/论文 id，NULL = 全库
    model       TEXT,                     -- 该会话使用的模型，覆盖默认配置
    pinned      INTEGER DEFAULT 0,
    -- 上下文管理：上次压缩锚点（用于「在新增量上动手」的增量计算）
    compact_anchor_seq INTEGER DEFAULT 0,
    created_at  TEXT,
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_conv_updated ON conversations(updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    seq             INTEGER NOT NULL,          -- 名义化事件序号，非负安全整数
    ref             TEXT NOT NULL,             -- 稳定可打印引用，固定宽度零填充：m00123
    role            TEXT NOT NULL,             -- user | assistant | system
    content         TEXT,
    -- LitPrism 特有：结构化附件
    citations       TEXT,                      -- JSON：[{chunk_id, paper_id, page, section, tag}]
    citation_check  TEXT,                      -- JSON：引用回溯校验结果
    -- 压缩相关
    kind            TEXT DEFAULT 'normal',     -- normal | compact-checkpoint
    shadowed_refs   TEXT,                      -- JSON：本消息遮蔽了哪些 ref（仅 checkpoint 有）
    block_id        TEXT,                      -- 关联的压缩块
    meta            TEXT,                      -- JSON：模型、耗时、token 等
    created_at      TEXT,
    UNIQUE (conversation_id, seq),
    UNIQUE (conversation_id, ref),
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_msg_conv_seq ON messages(conversation_id, seq);
CREATE INDEX IF NOT EXISTS idx_msg_kind     ON messages(conversation_id, kind);

-- ===================== 压缩 =====================
CREATE TABLE IF NOT EXISTS compaction_blocks (
    id                   TEXT PRIMARY KEY,
    conversation_id      TEXT NOT NULL,
    tier                 INTEGER DEFAULT 1,    -- 本项目只用 1
    topic                TEXT,
    summary              TEXT NOT NULL,
    direct_refs          TEXT NOT NULL,        -- JSON：直接覆盖的消息 ref
    effective_refs       TEXT NOT NULL,        -- JSON：传递性覆盖的全部原始 ref（血统）
    created_at           TEXT,
    survived_count       INTEGER DEFAULT 0,    -- 存活轮次（老化用）
    active               INTEGER DEFAULT 1,    -- 是否渲染进上下文
    shadowed_token_count INTEGER NOT NULL DEFAULT 0,
    summary_token_count  INTEGER NOT NULL DEFAULT 0,
    -- LitPrism 特有：被折叠区间涉及的引用句柄。
    -- 正文可折叠，句柄永不折叠 —— 可追溯性的最小载体。
    citation_handles     TEXT,                 -- JSON：[{chunk_id, paper_id, page}]
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_block_conv   ON compaction_blocks(conversation_id, active);
CREATE INDEX IF NOT EXISTS idx_block_refs   ON compaction_blocks(conversation_id, created_at);

-- 压缩事务：先立据再干活
CREATE TABLE IF NOT EXISTS compaction_tx (
    id              TEXT PRIMARY KEY,          -- compactionId
    conversation_id TEXT NOT NULL,
    source_command  TEXT NOT NULL,             -- nudge | pressure | manual | overflow
    owner_turn      TEXT,
    status          TEXT NOT NULL,             -- started | summarized | committed | failed | busy
    start_seq       INTEGER NOT NULL,
    end_seq         INTEGER NOT NULL,
    failure_code    TEXT,                      -- 闭集，见 invariants.FAILURE_CODES
    failure_detail  TEXT,
    shadowed_refs   TEXT,                      -- JSON
    created_at      TEXT,
    updated_at      TEXT,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tx_conv   ON compaction_tx(conversation_id, status);
CREATE INDEX IF NOT EXISTS idx_tx_status ON compaction_tx(status);

-- 压缩账本：回答「这次压缩回本了吗」
CREATE TABLE IF NOT EXISTS compaction_ledger (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    block_id        TEXT,
    trigger         TEXT,
    shadowed_tokens INTEGER NOT NULL DEFAULT 0,
    summary_tokens  INTEGER NOT NULL DEFAULT 0,
    repriced_tokens INTEGER NOT NULL DEFAULT 0,   -- 本次折叠造成的重付（简化版 compRepay）
    created_at      TEXT,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_ledger_conv ON compaction_ledger(conversation_id, created_at DESC);

-- ===================== 记忆 =====================
CREATE TABLE IF NOT EXISTS memories (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,   -- research_interest | term_preference | style_preference | misc
    key          TEXT,            -- 可选键，如术语原名
    value        TEXT NOT NULL,
    source       TEXT NOT NULL,   -- explicit（用户明说）| derived（系统推断，二期）
    confidence   REAL,
    pinned       INTEGER DEFAULT 0,   -- 是否常驻注入
    priority     INTEGER DEFAULT 100, -- 常驻层超限时的裁减优先级（小者先保）
    created_at   TEXT,
    updated_at   TEXT,
    last_used_at TEXT,
    deleted_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_mem_kind   ON memories(kind) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_mem_pinned ON memories(pinned) WHERE deleted_at IS NULL;

-- 不可变轮次视图：pin 到该轮，供 turn memory bar 展示与审计
CREATE TABLE IF NOT EXISTS turn_views (
    id               TEXT PRIMARY KEY,
    conversation_id  TEXT NOT NULL,
    turn_seq         INTEGER NOT NULL,
    read_refs        TEXT,           -- JSON：本轮注入/检索到的记忆与片段
    written_refs     TEXT,           -- JSON：本轮写入的记忆
    resident_tokens  INTEGER DEFAULT 0,
    retrieved_tokens INTEGER DEFAULT 0,
    frozen           INTEGER DEFAULT 1,   -- 不可变标记（保留扩展余地）
    created_at       TEXT,
    UNIQUE (conversation_id, turn_seq),
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_turn_conv ON turn_views(conversation_id, turn_seq);

-- ===================== 白名单（人工确认放行）=====================
CREATE TABLE IF NOT EXISTS whitelist (
    id                TEXT PRIMARY KEY,
    paper_id          TEXT,
    doi               TEXT,
    title             TEXT NOT NULL,
    level_at_confirm  TEXT,          -- 确认时的自动核验判定，保留原始结论
    verify_evidence   TEXT,          -- 确认时的核验证据快照（JSON）
    reason            TEXT NOT NULL, -- 用户填写的确认依据，必填
    confirmed_at      TEXT,
    confirmed_by      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_wl_doi ON whitelist(doi) WHERE doi IS NOT NULL AND doi <> '';
CREATE INDEX IF NOT EXISTS idx_wl_title ON whitelist(title);

-- ===================== 交付物 =====================
CREATE TABLE IF NOT EXISTS deliverables (
    id           TEXT PRIMARY KEY,
    type         TEXT NOT NULL,      -- review | comparison | notes | peer_review
    title        TEXT NOT NULL,
    skill_id     TEXT,               -- 生成它的 Skill（内核能力则为 null）
    source_scope TEXT,               -- JSON：来源论文/分组
    content      TEXT,               -- Markdown 正文
    citations    TEXT,               -- JSON：引用清单
    status       TEXT DEFAULT 'draft', -- draft | final | exported
    model        TEXT,
    created_at   TEXT,
    updated_at   TEXT,
    deleted_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_deliv_type ON deliverables(type) WHERE deleted_at IS NULL;

-- ===================== 模型提供商 =====================
CREATE TABLE IF NOT EXISTS providers (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    base_url    TEXT NOT NULL,
    api_key_ref TEXT,                -- 加密存储的引用键，不存明文
    chat_model  TEXT,
    embed_model TEXT,
    multimodal  INTEGER DEFAULT 0,   -- 是否支持图像输入（决定能否承担扫描件转写）
    is_custom   INTEGER DEFAULT 0,
    status      TEXT,                -- ok | unconfigured | failed
    status_note TEXT,
    checked_at  TEXT,
    sort_order  INTEGER DEFAULT 0,
    created_at  TEXT,
    updated_at  TEXT
);

-- ===================== MCP =====================
CREATE TABLE IF NOT EXISTS mcp_servers (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    transport  TEXT DEFAULT 'stdio', -- stdio | sse
    command    TEXT,
    args       TEXT,                 -- JSON
    env        TEXT,                 -- JSON
    readonly   INTEGER DEFAULT 1,
    enabled    INTEGER DEFAULT 0,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS mcp_audit (
    id          TEXT PRIMARY KEY,
    server_id   TEXT,
    tool        TEXT,
    args_digest TEXT,                -- 参数摘要，不存明文
    ok          INTEGER,
    error       TEXT,
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_mcp_audit ON mcp_audit(created_at DESC);

-- ===================== 扫描件转写 =====================
CREATE TABLE IF NOT EXISTS ocr_jobs (
    id          TEXT PRIMARY KEY,
    paper_id    TEXT,
    page        INTEGER,
    provider_id TEXT,
    model       TEXT,
    status      TEXT,                -- pending | ok | failed
    tokens      INTEGER,
    cost_usd    REAL,
    error       TEXT,
    created_at  TEXT,
    FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_ocr_paper ON ocr_jobs(paper_id, page);
"""

# papers 表 v2 新增列：(列名, 完整列定义)
PAPERS_V2_COLUMNS: list[tuple[str, str]] = [
    ("collection_id", "collection_id TEXT"),
    ("display_title", "display_title TEXT"),   # 用户重命名后的显示名，不改 title
    ("sort_order", "sort_order INTEGER DEFAULT 0"),
    ("deleted_at", "deleted_at TEXT"),         # 回收站（软删除）
]

# ---------------------------------------------------------------------------
# v3 —— 模型提供商与模型目录
# ---------------------------------------------------------------------------
SCHEMA_V3 = """
-- 提供商的模型目录：用户从「获取可用模型」拉取后勾选保留的条目。
-- 与 providers.chat_model 的关系：chat_model 是当前选中的那一个，
-- 本表是"可选项清单"。
CREATE TABLE IF NOT EXISTS provider_models (
    id           TEXT PRIMARY KEY,
    provider_id  TEXT NOT NULL,
    model_id     TEXT NOT NULL,
    display_name TEXT,
    enabled      INTEGER DEFAULT 1,   -- 是否暴露给 chat 的模型选择器
    sort_order   INTEGER DEFAULT 0,
    created_at   TEXT,
    UNIQUE (provider_id, model_id),
    FOREIGN KEY (provider_id) REFERENCES providers(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_pmodels_provider ON provider_models(provider_id, sort_order);
"""

PROVIDERS_V3_COLUMNS: list[tuple[str, str]] = [
    ("protocol", "protocol TEXT DEFAULT 'openai'"),   # openai | anthropic
]


def _migrate_v3(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_V3)
    if _table_exists(conn, "providers"):
        for col, ddl in PROVIDERS_V3_COLUMNS:
            if not _column_exists(conn, "providers", col):
                conn.execute(f"ALTER TABLE providers ADD COLUMN {ddl}")


# ---------------------------------------------------------------------------
# 迁移框架
# ---------------------------------------------------------------------------
@dataclass
class Migration:
    version: int
    description: str
    apply: Callable[[sqlite3.Connection], None]


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _migrate_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_V1)


def _migrate_v2(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_V2)
    # ALTER TABLE 不支持 IF NOT EXISTS，逐个检查后再加
    if _table_exists(conn, "papers"):
        for col, ddl in PAPERS_V2_COLUMNS:
            if not _column_exists(conn, "papers", col):
                conn.execute(f"ALTER TABLE papers ADD COLUMN {ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_papers_collection ON papers(collection_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_papers_deleted    ON papers(deleted_at)")


def _migrate_v4(conn: sqlite3.Connection) -> None:
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS evidence_sources (
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, url TEXT,
          content TEXT NOT NULL, content_hash TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
          reviewed_at TEXT, review_note TEXT
        );
        CREATE TABLE IF NOT EXISTS evidence_chunks (
          id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES evidence_sources(id),
          ordinal INTEGER NOT NULL, text TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_evidence_chunks_source ON evidence_chunks(source_id);
        CREATE TABLE IF NOT EXISTS evidence_reviews (
          id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES evidence_sources(id),
          content_hash TEXT NOT NULL, action TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL
        );
    ''')


def _migrate_v5(conn: sqlite3.Connection) -> None:
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS semantic_runs (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS semantic_reviews (
          id TEXT PRIMARY KEY, paper_id TEXT REFERENCES papers(id), run_id TEXT REFERENCES semantic_runs(id),
          analysis_hash TEXT NOT NULL, claim_id TEXT NOT NULL, action TEXT NOT NULL,
          note TEXT NOT NULL, reviewed_text TEXT, knowledge_id TEXT, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_semantic_review_version ON semantic_reviews(paper_id,run_id,analysis_hash,claim_id);
    ''')


MIGRATIONS: list[Migration] = [
    Migration(1, "基础表：papers / chunks / chunks_fts / analyses / concepts / relations / verify_log / meta", _migrate_v1),
    Migration(2, "会话 / 压缩 / 记忆 / 白名单 / 交付物 / 提供商 / MCP / OCR", _migrate_v2),
    Migration(3, "模型目录 provider_models + providers.protocol", _migrate_v3),
    Migration(4, '知识与网页候选、原文快照及人工审核审计', _migrate_v4),
    Migration(5, '按解读版本保存陈述人工审核与知识入库记录', _migrate_v5),
]

SCHEMA_VERSION: int = max(m.version for m in MIGRATIONS)


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def apply_migrations(conn: sqlite3.Connection, *, verbose: bool = False) -> list[str]:
    """把数据库升到最新版本。返回本次实际应用的迁移描述列表。

    **幂等**：已应用过的迁移不会重复执行。

    **事务说明**：``executescript()`` 会先隐式提交挂起事务，再以 autocommit 逐句执行，
    因此这里**不手动包 BEGIN/COMMIT**（包了会报 "cannot commit - no transaction is active"）。
    代价是单个迁移不是原子的 —— 但所有迁移都写成幂等的
    （``IF NOT EXISTS`` + 建列前检查），失败后可安全重试，不会留下半成品。
    """
    applied: list[str] = []
    prev_isolation = conn.isolation_level
    ver = current_version(conn)
    try:
        conn.isolation_level = None          # autocommit
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")

        for m in sorted(MIGRATIONS, key=lambda x: x.version):
            if m.version <= ver:
                continue
            try:
                m.apply(conn)
                conn.execute(f"PRAGMA user_version = {int(m.version)}")
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(
                    f"迁移 v{m.version}（{m.description}）失败：{type(e).__name__}: {e}。"
                    "数据库可能处于中间状态；迁移是幂等的，修复后可直接重试。"
                ) from e
            applied.append(f"v{m.version}: {m.description}")
            if verbose:
                print(f"  [migrate] v{m.version} {m.description}")
    finally:
        conn.isolation_level = prev_isolation

    if not applied and verbose:
        print(f"  [migrate] 已是最新版本 v{ver}")
    return applied


def init_schema(conn: sqlite3.Connection, *, verbose: bool = False) -> list[str]:
    """初始化或升级数据库。全新库会一次建全。"""
    return apply_migrations(conn, verbose=verbose)


def schema_report(conn: sqlite3.Connection) -> dict:
    """当前 schema 状态，供 `litprism stats` 与排查用。"""
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            " ORDER BY name"
        ).fetchall()
    ]
    return {
        "user_version": current_version(conn),
        "schema_version": SCHEMA_VERSION,
        "up_to_date": current_version(conn) >= SCHEMA_VERSION,
        "tables": tables,
        "table_count": len(tables),
        "migrations": [f"v{m.version} {m.description}" for m in sorted(MIGRATIONS, key=lambda x: x.version)],
    }
