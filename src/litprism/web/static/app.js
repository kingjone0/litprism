/* ==========================================================================
   文献棱镜 LitPrism · 界面逻辑
   无构建步骤：模板字符串 + 原生 DOM。所有用户内容经 esc() 转义。
   ========================================================================== */

const S = {
  status: null,
  papers: [],
  conversations: [],
  skills: [],
  deliverables: [],
  whitelist: [],
  memories: [],
  activeView: 'library',
  activeConv: null,
  activePaper: null,
  tabs: [],
  theme: localStorage.getItem('litprism-theme') || 'light',
  inspector: localStorage.getItem('litprism-evidence-panel') === '1',
  sidebar: localStorage.getItem('litprism-sidebar') !== '0',
  inspectorTab: 'sources',
  messages: [],
  sources: [],
  sending: false,
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// 本地 SVG 图标保持跨平台一致，不依赖字体或远程资源。
function icon(name) {
  const paths = {
    sidebar: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>',
    panel: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/>',
    search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    close: '<path d="m6 6 12 12M6 18 18 6"/>',
    theme: '<path d="M20 13a8 8 0 0 1-9-9 8 8 0 1 0 9 9Z"/>',
    prism: '<path d="m12 3 9 17H3L12 3Z"/><path d="m12 3 3 17M3 20l10-10"/>',
    library: '<path d="M4 4h4v16H4zM10 4h4v16h-4zM17 4l4 15-4 1-4-15"/>',
    shield: '<path d="m12 3 8 3v6c0 4-4 7-8 9-4-2-8-5-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>',
    document: '<path d="M14 3H5v18h14V8l-5-5Z"/><path d="M14 3v5h5M8 12h8M8 16h6"/>',
    chat: '<path d="M21 11a8 8 0 0 1-8 8H6l-4 3 1-7a8 8 0 1 1 18-4Z"/>',
    skills: '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Z"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="m9 3-1 3-3 1-2 5 2 5 3 1 1 3h6l1-3 3-1 2-5-2-5-3-1-1-3H9Z"/>',
    send: '<path d="M12 19V5m-6 6 6-6 6 6"/>',
  };
  return `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.document}</svg>`;
}

/* ---------------------------------------------------------------- 工具 */
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}
function fmtNum(n) {
  if (n == null) return '—';
  return Number(n).toLocaleString('zh-CN');
}
function pct(v) { return `${Math.round((v || 0) * 100)}%`; }
function short(s, n = 60) {
  s = String(s ?? '');
  return s.length > n ? s.slice(0, n) + '…' : s;
}

/* 后台地址与 token 由桌面外壳在握手完成后用 eval 推给页面（也可来自 URL fragment）。
   两种来源都要认，且**必须等它到达**再发请求 —— 否则首屏会拿 401。 */
let resolveEnv;
const ENV_READY = new Promise(res => { resolveEnv = res; });

window.__setLitPrismEnv = function (cfg) {
  window.__LITPRISM__ = cfg || {};
  resolveEnv(window.__LITPRISM__);
};

(function resolveEnvFromFragment() {
  if (window.__LITPRISM__) return resolveEnv(window.__LITPRISM__);

  const m = /(?:^|[#&])t=([^&]*)/.exec(location.hash || '');
  if (m && m[1]) {
    window.__setLitPrismEnv({ base: '', token: decodeURIComponent(m[1]) });
    return;
  }

  /* 关键：桌面外壳里页面 origin 是 tauri://localhost，API 在后端端口上。
     此时**绝不能**用空配置放行 —— 相对路径会打到 Tauri 的资源处理器，
     请求根本到不了后端（表现是「界面空白、服务端零请求」，极难定位）。
     必须等外壳把 base/token 推过来。 */
  const inShell = location.protocol === 'tauri:' || /(^|\.)tauri\.localhost$/.test(location.hostname);
  if (inShell) {
    // 兜底：外壳彻底失败时也不能让界面永远卡住
    setTimeout(() => resolveEnv(window.__LITPRISM__ || {}), 120000);
    return;
  }

  // 浏览器直连 `run.py serve`：无外壳注入、无 token，立刻放行
  resolveEnv({});
})();

function apiEnv() { return window.__LITPRISM__ || {}; }

async function api(path, opts = {}) {
  await ENV_READY;
  const env = apiEnv();
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (env.token) headers['X-LitPrism-Token'] = env.token;
  const res = await fetch((env.base || '') + path, {
    ...opts,
    headers,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
  if (!res.ok) {
    throw new Error((data && (data.error || data.detail)) || `HTTP ${res.status}`);
  }
  return data;
}

function toast(msg, isErr = false) {
  const wrap = $('#toasts');
  const node = document.createElement('div');
  node.className = 'toast' + (isErr ? ' err' : '');
  node.textContent = msg;
  wrap.appendChild(node);
  setTimeout(() => node.remove(), 3200);
}

function badgeOf(level) {
  const map = {
    verified:        ['ok', '已核验'],
    manual_confirmed:['manual', '人工确认'],
    likely:          ['warn', '疑似匹配'],
    not_found:       ['danger', '未核到'],
    unchecked:       ['neutral', '无法判定'],
  };
  const [cls, text] = map[level] || ['neutral', '❔ 未知'];
  return `<span class="badge status-badge ${cls}">${text}</span>`;
}

/* ---------------------------------------------------------------- 主题 */
function applyTheme() {
  document.documentElement.dataset.theme = S.theme;
  $('#btnTheme').title = S.theme === 'dark' ? '切换为浅色' : '切换为深色';
}
function applyInspector() {
  $('#main').classList.toggle('inspector-hidden', !S.inspector);
  $('#inspector').inert = !S.inspector;
  $('#btnInspector').setAttribute('aria-expanded', String(S.inspector));
  $('#btnInspector').classList.toggle('selected', S.inspector);
}
function toggleInspector(show = !S.inspector) {
  S.inspector = show;
  localStorage.setItem('litprism-evidence-panel', show ? '1' : '0');
  applyInspector();
  if (show) refreshInspector();
}
function applySidebar() {
  $('#main').classList.toggle('sidebar-hidden', !S.sidebar);
  $('#sidebar').inert = !S.sidebar;
  $('#btnSidebar').setAttribute('aria-expanded', String(S.sidebar));
}
function toggleSidebar() {
  S.sidebar = !S.sidebar;
  localStorage.setItem('litprism-sidebar', S.sidebar ? '1' : '0');
  applySidebar();
}
function refreshInspector() {
  if (!S.inspector) return;
  const loader = { sources: loadInspectorSources, context: loadInspectorContext,
    graph: loadInspectorGraph, deliv: loadInspectorDeliv }[S.inspectorTab];
  if (loader) loader().catch(e => toast(e.message, true));
}

/* ==========================================================================
   视图切换
   ========================================================================== */
const VIEW_TITLES = {
  library: '知识库', paper: '论文', chat: '对话', compare: '对比',
  verify: '核验', deliverables: '交付物', skills: 'Skill', settings: '设置',
};

function openView(view, opts = {}) {
  S.activeView = view;
  $('#tbTitle').textContent = VIEW_TITLES[view] || view;
  $$('.pane').forEach(p => p.classList.toggle('active', p.dataset.pane === view));
  $('#workBody').classList.toggle('flush', view === 'chat');

  if (view === 'paper' && opts.paperId) S.activePaper = opts.paperId;
  if (view === 'chat' && opts.convId && opts.convId !== S.activeConv) {
    S.activeConv = opts.convId;
    S.messages = [];
    S.sources = [];
  }

  if (!S.tabs.includes(view)) S.tabs.push(view);
  renderTabs();
  $$('.side-item').forEach(b => b.classList.toggle('active',
    b.dataset.view === view || (view === 'paper' && b.dataset.paper === S.activePaper) ||
    (view === 'chat' && b.dataset.conv === S.activeConv)));

  const loader = {
    library: loadLibrary, paper: loadPaperDetail, chat: loadChat,
    deliverables: loadDeliverables, skills: loadSkills, verify: loadVerify,
  }[view];
  if (loader) loader().catch(e => toast(e.message, true));
  refreshInspector();
}

/* 侧边栏入口：设置走弹窗，其余切工作区视图 */
function enterView(view) {
  if (view === 'settings') { openSettings('general'); return; }
  openView(view);
}

function renderTabs() {
  const bar = $('#tabbar');
  if (!S.tabs.length) { bar.innerHTML = ''; return; }
  bar.innerHTML = S.tabs.map(v => `
    <button class="tab ${v === S.activeView ? 'active' : ''}" data-tab="${v}">
      ${esc(VIEW_TITLES[v] || v)}
      ${S.tabs.length > 1 ? `<span class="x" data-close="${v}">✕</span>` : ''}
    </button>`).join('');
  $$('.tab', bar).forEach(b => {
    b.onclick = e => {
      if (e.target.dataset.close) {
        S.tabs = S.tabs.filter(t => t !== e.target.dataset.close);
        if (S.activeView === e.target.dataset.close) openView(S.tabs[S.tabs.length - 1] || 'library');
        else renderTabs();
        return;
      }
      openView(b.dataset.tab);
    };
  });
}

/* ==========================================================================
   左侧栏
   ========================================================================== */
async function loadSidebar() {
  const [papers, convs, skills] = await Promise.all([
    api('/api/papers').catch(() => ({ items: [] })),
    api('/api/conversations').catch(() => ({ items: [] })),
    api('/api/skills').catch(() => ({ items: [], stats: {} })),
  ]);
  S.papers = papers.items || [];
  S.conversations = convs.items || [];
  S.skills = skills.items || [];

  $('#libCount').textContent = S.papers.length || '';
  $('#convCount').textContent = S.conversations.length || '';

  // ---- 对话 ----
  const convBox = $('#convList');
  convBox.innerHTML = S.conversations.length ? S.conversations.map(c => `
      <div class="side-item-wrap">
        <button class="side-item ${c.id === S.activeConv ? 'active' : ''}" data-conv="${esc(c.id)}">
          ${icon('chat')}
          <span class="label">${esc(short(c.title, 16))}</span>
          <span class="meta">${c.message_count || 0}</span>
        </button>
        <button class="conv-del" data-conv-del="${esc(c.id)}" title="删除会话">✕</button>
      </div>`).join('') : '<div class="side-empty">新建对话，开始一次有据可查的研究。</div>';
  $('#btnNewConv').onclick = newConversation;
  $$('[data-conv]', convBox).forEach(b => b.onclick = () => openView('chat', { convId: b.dataset.conv }));
  $$('[data-conv-del]', convBox).forEach(b => b.onclick = async e => {
    e.stopPropagation();
    const cid = b.dataset.convDel;
    const item = S.conversations.find(c => c.id === cid);
    if (!confirm(`删除会话「${item?.title || cid}」？\n全部消息与压缩记录会一并删除，不可恢复。`)) return;
    try {
      await api(`/api/conversations/${encodeURIComponent(cid)}`, { method: 'DELETE' });
      if (S.activeConv === cid) { S.activeConv = null; openView('library'); }
      toast('已删除');
      loadSidebar();
    } catch (err) { toast(err.message, true); }
  });

  // ---- 知识库 ----
  const lib = $('#libList');
  if (!S.papers.length) {
    lib.innerHTML = `<div class="side-empty">知识库为空。<br>用 <code class="mono">litprism add &lt;pdf&gt;</code> 导入。</div>`;
  } else {
    lib.innerHTML = S.papers.map(p => `
      <button class="side-item" data-paper="${esc(p.id)}">
        ${icon('document')}
        <span class="label" title="${esc(p.title)}">${esc(short(p.display_title || p.title, 20))}</span>
      </button>`).join('');
    $$('[data-paper]', lib).forEach(b => b.onclick = () => openView('paper', { paperId: b.dataset.paper }));
  }

  // 技能统一从底部进入管理页，不在主导航重复展开。
  $('#skillCount').textContent = S.skills.length || '';
  $$('.side-item[data-view]').forEach(b => b.onclick = () => enterView(b.dataset.view));
}

async function newConversation() {
  try {
    const r = await api('/api/conversations', { method: 'POST', body: { title: '新会话' } });
    await loadSidebar();
    openView('chat', { convId: r.id });
  } catch (e) { toast(e.message, true); }
}

/* ==========================================================================
   知识库
   ========================================================================== */
async function loadLibrary() {
  const q = ($('#libSearch').value || '').toLowerCase();
  const items = q
    ? S.papers.filter(p =>
        (p.title || '').toLowerCase().includes(q) ||
        (p.display_title || '').toLowerCase().includes(q) ||
        (p.authors || []).some(a => (a || '').toLowerCase().includes(q)))
    : S.papers;

  const st = S.status?.stats || {};
  $('#libSub').textContent =
    q ? `找到 ${items.length} 篇匹配文献` : '整理文献、阅读原文，让每一个结论有据可查。';
  const citable = S.papers.filter(p => ['verified', 'manual_confirmed'].includes(p.verify_level)).length;
  $('#libSummary').innerHTML = [
    [S.papers.length, '篇文献', 'library'], [citable, '篇核验可引用', 'shield'],
    [st.analyses ?? 0, '篇已解读', 'document'],
  ].map(([n, label, glyph]) => `<div class="summary-item">${icon(glyph)}<div><strong>${fmtNum(n)}</strong><span>${label}</span></div></div>`).join('');

  const box = $('#paperList');
  if (!items.length) {
    box.innerHTML = `<div class="empty">
      <div class="empty-mark">${icon('library')}</div>
      <div class="title">${q ? '没有匹配的论文' : '知识库还是空的'}</div>
      <div class="desc">${q ? '换个标题或作者关键词试试。' : '当前可通过命令 <code>litprism add &lt;论文.pdf&gt;</code> 导入文献。核验状态和原文来源会随文献保留。'}</div>
    </div>`;
    return;
  }

  box.innerHTML = items.map(p => `
    <button class="list-item" data-paper="${esc(p.id)}">
      <span class="paper-mark">${icon('document')}</span>
      <div class="list-main">
        <div class="list-title">${esc(p.display_title || p.title)}</div>
        <div class="list-sub">
          <span>${esc((p.authors || []).slice(0, 3).join('、') || '作者未知')}${(p.authors || []).length > 3 ? ' 等' : ''}</span>
          <span>${p.year || '年份未知'}</span>
          ${p.venue ? `<span>${esc(p.venue)}</span>` : ''}
          ${badgeOf(p.verify_level)}
        </div>
      </div>
      <span class="paper-open" aria-hidden="true">↗</span>
    </button>`).join('');

  $$('[data-paper]', box).forEach(b => b.onclick = () => openView('paper', { paperId: b.dataset.paper }));
}

/* ==========================================================================
   论文详情
   ========================================================================== */
async function loadPaperDetail() {
  const box = $('#paperDetail');
  if (!S.activePaper) {
    box.innerHTML = `<div class="empty"><div class="title">未选择论文</div></div>`;
    return;
  }
  box.innerHTML = `<div class="empty"><div class="skeleton" style="width:60%;height:18px;margin:0 auto 12px"></div>
    <div class="skeleton" style="width:40%;height:12px;margin:0 auto"></div></div>`;

  try {
    const d = await api(`/api/paper/${encodeURIComponent(S.activePaper)}`);
    const p = d.paper || d;
    const a = d.analysis || {};
    const concepts = d.concepts || [];

    box.innerHTML = `
      <div class="page-head">
        <div class="row">
          <div>
            <div class="page-title">${esc(p.display_title || p.title)}</div>
            <div class="page-sub">
              ${esc((p.authors || []).join('、') || '作者未知')} · ${p.year || '年份未知'}
              ${p.venue ? ' · ' + esc(p.venue) : ''}
            </div>
            <div class="row wrap" style="margin-top:10px">
              ${badgeOf(p.verify_level)}
              ${p.doi ? `<span class="mono muted">DOI: ${esc(p.doi)}</span>` : ''}
              <span class="mono muted">${d.chunks ?? 0} 个分块</span>
            </div>
          </div>
          <span style="flex:1"></span>
          <div class="row" style="gap:6px">
            <button class="btn sm" id="btnRename">重命名</button>
            <button class="btn sm danger" id="btnDelete">删除</button>
            <button class="btn sm primary" id="btnReanalyze">重新解读</button>
          </div>
        </div>
        <div class="segmented" style="margin-top:14px" id="paperTabs">
          <button class="active" data-ptab="analysis">解读</button>
          <button data-ptab="text">原文</button>
          <button data-ptab="concepts">术语 (${concepts.length})</button>
        </div>
      </div>

      <div data-ppane="analysis">
        ${a.research_question ? `
          <div class="card"><div class="card-body">
            ${analysisSection('研究问题', a.research_question)}
            ${analysisSection('方法', a.methodology)}
            ${analysisSection('实验设计', a.experiment_design)}
            ${analysisSection('主要结论', a.key_conclusions)}
            ${analysisSection('创新点', a.innovations)}
            ${analysisSection('局限', a.limitations)}
          </div></div>` : `
          <div class="card"><div class="card-body">
            <div class="empty"><div class="ico">🧩</div>
              <div class="title">尚未解读</div>
              <div class="desc">点「重新解读」生成结构化解读（研究问题 / 方法 / 实验 / 结论 / 创新点 / 局限）。</div>
            </div>
          </div></div>`}
      </div>

      <div data-ppane="text" style="display:none">
        <div class="card"><div class="card-body" id="paperTextBody">
          <div class="skeleton" style="width:60%"></div></div></div>
      </div>

      <div data-ppane="concepts" style="display:none">
        ${concepts.length ? `
          <div class="card"><div class="card-body">
            <table class="table">
              <thead><tr><th style="width:150px">术语</th><th>文中含义</th></tr></thead>
              <tbody>${concepts.map(c => `
                <tr><td><strong>${esc(c.term)}</strong></td><td>${esc(c.definition || '—')}</td></tr>`).join('')}
              </tbody>
            </table>
          </div></div>` : `<div class="card"><div class="card-body">
            <div class="empty"><div class="title">暂无术语</div></div></div></div>`}
      </div>
    `;

    // ---- 标签切换 ----
    $$('#paperTabs [data-ptab]').forEach(b => b.onclick = () => {
      $$('#paperTabs [data-ptab]').forEach(x => x.classList.toggle('active', x === b));
      $$('[data-ppane]').forEach(x => x.style.display = x.dataset.ppane === b.dataset.ptab ? '' : 'none');
      if (b.dataset.ptab === 'text') loadPaperText(S.activePaper);
    });

    // ---- 重命名 / 删除 ----
    $('#btnRename').onclick = () => {
      const cur = p.display_title || p.title || '';
      const name = prompt('新的显示名称（只影响界面显示，不改原始题录标题）', cur);
      if (name === null) return;
      api(`/api/paper/${encodeURIComponent(S.activePaper)}`, {
        method: 'PATCH', body: { display_title: name },
      }).then(() => {
        toast(name.trim() ? '已重命名' : '已恢复为原始标题');
        loadSidebar();
        loadPaperDetail();
      }).catch(e => toast(e.message, true));
    };
    $('#btnDelete').onclick = () => {
      if (!confirm('移入回收站？该论文将不再出现在检索与问答中。')) return;
      api(`/api/paper/${encodeURIComponent(S.activePaper)}`, { method: 'DELETE' })
        .then(() => { toast('已移入回收站'); S.activePaper = null; S.tabs = S.tabs.filter(t => t !== 'paper'); loadSidebar(); openView('library'); })
        .catch(e => toast(e.message, true));
    };

    $('#btnReanalyze').onclick = async () => {
      try {
        toast('正在解读…');
        await api(`/api/analyze/${encodeURIComponent(S.activePaper)}`, { method: 'POST' });
        toast('解读完成');
        loadPaperDetail();
      } catch (e) { toast(e.message, true); }
    };
  } catch (e) {
    box.innerHTML = `<div class="empty"><div class="title">加载失败</div><div class="desc">${esc(e.message)}</div></div>`;
  }
}

async function loadPaperText(pid) {
  const body = $('#paperTextBody');
  if (!body) return;
  try {
    const d = await api(`/api/paper/${encodeURIComponent(pid)}/text`);
    if (!d.pages?.length) {
      body.innerHTML = `<div class="empty"><div class="title">没有可显示的正文</div>
        <div class="desc">该文件可能没有可提取的文本层（扫描件）。扫描件转写功能一期暂缓。</div></div>`;
      return;
    }
    body.innerHTML = d.pages.map(pg => `
      <div style="margin-bottom:18px">
        <div style="font-size:11px;font-weight:650;letter-spacing:.05em;text-transform:uppercase;
                    color:var(--muted);margin-bottom:8px">
          ${pg.page === null ? '无页码' : `第 ${pg.page} 页`}
        </div>
        ${pg.chunks.map(c => `
          <div style="margin-bottom:10px">
            ${c.section ? `<div style="font-size:11.5px;color:var(--accent);font-weight:550;margin-bottom:3px">${esc(c.section)}</div>` : ''}
            <div style="font-size:13px;line-height:1.8;white-space:pre-wrap">${esc(c.text)}</div>
          </div>`).join('')}
      </div>`).join('');
  } catch (e) {
    body.innerHTML = `<div class="empty"><div class="desc">${esc(e.message)}</div></div>`;
  }
}

function analysisSection(label, text) {
  if (!text) return '';
  return `<div style="margin-bottom:16px">
    <div style="font-size:11px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;color:var(--muted);margin-bottom:5px">${label}</div>
    <div class="reading-text">${esc(text)}</div>
  </div>`;
}

/* ==========================================================================
   对话
   ========================================================================== */
async function loadChat() {
  const box = $('#chatDetail');
  const convId = S.activeConv;
  if (!S.activeConv) {
    box.innerHTML = `<div class="empty" style="padding-top:120px">
      <div class="ico">💬</div>
      <div class="title">没有选中的对话</div>
      <div class="desc">从左侧选择一个会话，或新建一个。</div>
    </div>`;
    return;
  }

  try {
    const d = await api(`/api/conversations/${encodeURIComponent(convId)}/messages`);
    if (S.activeView !== 'chat' || S.activeConv !== convId) return;
    const msgs = d.items || [];
    S.messages = msgs;
    S.sources = d.sources || [];
    $('#tbTitle').textContent = d.conversation.title || '对话';

    box.innerHTML = `
      <div class="chat-layout">
        <div class="page-head">
          <div class="row">
            <div>
              <div class="page-title">${esc(d.conversation.title)}</div>
              <div class="page-sub">基于本地文献 · 引用可回溯</div>
            </div>
          </div>
        </div>
        <div id="chatStream" role="log" aria-label="研究对话">
          ${msgs.length ? msgs.map(renderMessage).join('') : `
            <div class="empty chat-welcome"><div class="empty-mark">${icon('prism')}</div>
              <div class="eyebrow">READ · QUESTION · CONNECT</div>
              <h2 class="title">把问题交给文献</h2>
              <div class="desc">从一个研究问题开始，回答将附上原文来源。<br>证据不足时，会明确告诉你。</div>
              <div class="question-starters">
                <button class="starter" data-question="这些论文分别研究了什么问题？">梳理研究问题 <span>↗</span></button>
                <button class="starter" data-question="这些研究的方法有哪些局限？">寻找方法局限 <span>↗</span></button>
              </div>
            </div>`}
        </div>
        <div id="chatCompose">
          <div class="card"><div class="card-body">
            <div class="row" style="gap:8px;margin-bottom:9px">
              <span class="muted" style="font-size:11.5px">模型</span>
              <select class="select" id="chatModel"
                style="width:auto;min-width:210px;font-size:12px;padding:4px 9px"></select>
            </div>
            <div class="row" style="gap:8px;align-items:flex-end">
              <textarea class="textarea" id="chatInput" rows="2"
                aria-label="研究问题" placeholder="向文献提问，或继续追问…"></textarea>
              <button class="btn primary" id="btnSend" title="发送（Ctrl+Enter）" ${S.sending ? 'disabled' : ''}>${icon('send')}<span>发送</span></button>
            </div>
            <div style="margin-top:8px;font-size:11.5px;color:var(--muted)">
              <span>回答依据原文片段，证据不足时拒答。</span><kbd>Ctrl + Enter 发送</kbd>
            </div>
          </div></div>
        </div>
      </div>`;

    LitUI.bind(box);
    loadModels();

    const input = $('#chatInput');
    $$('.starter', box).forEach(b => b.onclick = () => { input.value = b.dataset.question; input.focus(); });
    const send = () => askQuestion();
    $('#btnSend').onclick = send;
    input.onkeydown = e => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); send(); }
    };
    input.focus();

    refreshInspector();
  } catch (e) {
    box.innerHTML = `<div class="empty"><div class="title">加载失败</div><div class="desc">${esc(e.message)}</div></div>`;
  }
}

async function askQuestion() {
  const input = $('#chatInput');
  const q = (input?.value || '').trim();
  if (!q || !S.activeConv || S.sending) return;
  S.sending = true;
  const convId = S.activeConv;
  const sendButton = $('#btnSend');

  const stream = $('#chatStream');
  const empty = stream.querySelector('.empty');
  if (empty) empty.remove();

  // 乐观插入用户消息
  stream.insertAdjacentHTML('beforeend', renderMessage({ role: 'user', content: q }));
  stream.insertAdjacentHTML('beforeend', `
    <div id="pending" style="margin-bottom:14px;display:flex;gap:10px">
      <div class="chat-avatar">AI</div>
      <div style="flex:1;min-width:0;max-width:88%">
        <div class="lui" style="padding:12px"><div class="skeleton" style="width:70%;margin-bottom:6px"></div>
        <div class="skeleton" style="width:45%"></div></div>
      </div>
    </div>`);
  input.value = '';
  $('#btnSend').disabled = true;
  stream.scrollIntoView({ block: 'end', behavior: 'smooth' });

  try {
    const r = await api(`/api/chat/${encodeURIComponent(convId)}`, {
      method: 'POST', body: { question: q },
    });
    if (S.activeConv !== convId || S.activeView !== 'chat' || !stream.isConnected) return;
    S.messages.push(r.user, r.assistant);
    S.sources = r.sources || [];
    $('#pending')?.remove();
    stream.insertAdjacentHTML('beforeend', renderMessage(r.assistant, true));
    if (r.insufficient_evidence) {
      stream.insertAdjacentHTML('beforeend', `<div class="lui-degraded">
        ⚠️ 本地知识库中没有找到足够的依据。可先在「知识库」里导入相关论文，
        或用 <code>litprism recommend</code> 从学术数据库检索并导入。</div>`);
    }
    if (r.auto_compact) {
      stream.insertAdjacentHTML('beforeend', `<div class="insp-note" style="margin:8px 0">
        已整理较早的对话，保留摘要以便继续研究。原始记录仍可追溯。</div>`);
    }
    LitUI.bind(stream);
    refreshInspector();
    loadSidebar();
  } catch (e) {
    if (!stream.isConnected) return;
    $('#pending')?.remove();
    stream.insertAdjacentHTML('beforeend', `
      <div class="lui-degraded">回答失败：${esc(e.message)}</div>`);
    toast(e.message, true);
  } finally {
    S.sending = false;
    sendButton.disabled = false;
    if ($('#btnSend')) $('#btnSend').disabled = false;
    if (stream.isConnected) $('#chatInput')?.focus();
  }
}

async function doCompact() {
  if (!S.activeConv) return;
  const btn = $('#btnCompact');
  const old = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '检查中…'; }
  try {
    // 先 dry-run：只报告会压哪一段，不写任何数据
    const dry = await api(`/api/compact/${encodeURIComponent(S.activeConv)}`, {
      method: 'POST', body: { dry_run: true },
    });
    if (!dry.ok) {
      toast(dry.reason || '没有可压缩的区间', true);
      return;
    }
    const okGo = confirm(
      `将把 ${dry.range} 折成摘要：\n` +
      `遮蔽 ${dry.shadowed_tokens} token，保留 ${dry.retained_tokens} token。\n\n` +
      `被折掉的消息不再进入上下文，但可随时按块还原。继续？`
    );
    if (!okGo) return;

    if (btn) btn.textContent = '压缩中…';
    const r = await api(`/api/compact/${encodeURIComponent(S.activeConv)}`, {
      method: 'POST', body: {},
    });
    if (!r.ok) {
      toast(r.reason || r.failure_code || '压缩失败', true);
      return;
    }
    toast(`已压缩，节省 ${r.ledger?.saved_per_turn ?? 0} token/轮`);
    await loadChat();
  } catch (e) {
    toast(e.message, true);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = old; }
  }
}

function renderMessage(m, fresh = false) {
  const isUser = m.role === 'user';
  const isCheckpoint = m.kind === 'compact-checkpoint';
  let check = m.citation_check || {};
  if (typeof check === 'string') { try { check = JSON.parse(check); } catch { check = {}; } }

  // 组件段优先；没有 segments 时退回纯文本
  let body;
  if (!isUser && Array.isArray(m.segments) && m.segments.length) {
    body = LitUI.renderSegments(m.segments);
  } else if (isCheckpoint) {
    body = `<div style="font-family:var(--mono);font-size:11.5px;color:var(--text-2);white-space:pre-wrap">${esc(short(m.content, 400))}</div>`;
  } else {
    body = LitUI.md(m.content || '');
  }

  const checkWarn = (!isUser && check && check.passed === false)
    ? `<div class="lui-degraded" style="margin-top:8px">
         ⚠️ 引用校验未通过${(check.issues || []).length ? '：' + esc(check.issues.join('；')) : ''}
       </div>` : '';

  return `
    <div class="chat-turn ${isUser ? 'from-user' : 'from-assistant'}">
      <div class="chat-avatar">${isUser ? '我' : icon('prism')}</div>
      <div class="chat-message">
        ${isCheckpoint ? `<div class="badge accent" style="margin-bottom:6px">压缩检查点</div>` : ''}
        <div class="chat-bubble ${isUser ? 'user' : ''}">${body}</div>
        ${checkWarn}
        <div style="margin-top:4px;font-size:10.5px;color:var(--muted);text-align:${isUser ? 'right' : 'left'}">
          ${isUser ? '你' : '文献棱镜'}${m.created_at ? ' · ' + esc(m.created_at.slice(0, 16).replace('T', ' ')) : ''}
        </div>
      </div>
    </div>`;
}

/* ==========================================================================
   交付物
   ========================================================================== */
async function loadDeliverables() {
  const d = await api('/api/deliverables');
  S.deliverables = d.items || [];
  const box = $('#delivList');

  if (!S.deliverables.length) {
    box.innerHTML = `<div class="empty">
      <div class="ico">📝</div>
      <div class="title">还没有交付物</div>
      <div class="desc">交付物由 Skill 的 <code>output.type: deliverable</code> 生成，<br>或由综述 / 对比等内核能力产出。</div>
    </div>`;
    return;
  }

  box.innerHTML = S.deliverables.map(x => `
    <div class="list-item">
      <div class="list-main">
        <div class="list-title">${esc(x.title)}</div>
        <div class="list-sub">
          <span class="badge ${x.status === 'draft' ? 'neutral' : x.status === 'final' ? 'ok' : 'accent'}">${esc(x.status_label)}</span>
          <span>${esc(x.type_label)}</span>
          <span>${x.word_count} 字</span>
          ${x.citation_count ? `<span>${x.citation_count} 条引用</span>` : ''}
          ${x.skill_id ? `<span class="mono">${esc(x.skill_id)}</span>` : ''}
        </div>
      </div>
      <div class="row" style="gap:6px;flex:none">
        <select class="select" data-dstat="${esc(x.id)}"
          style="font-size:11.5px;padding:4px 7px;width:auto">
          ${['draft', 'final', 'exported'].map(s =>
            `<option value="${s}"${s === x.status ? ' selected' : ''}>${({ draft: '草稿', final: '定稿', exported: '已导出' })[s]}</option>`).join('')}
        </select>
        <button class="btn sm" data-dexport="${esc(x.id)}">导出</button>
        <button class="btn sm" data-deliv="${esc(x.id)}">查看</button>
      </div>
    </div>`).join('');

  $$('[data-deliv]', box).forEach(b => b.onclick = () => showDeliverable(b.dataset.deliv));
  $$('[data-dstat]', box).forEach(s => s.onchange = async () => {
    try {
      await api(`/api/deliverables/${encodeURIComponent(s.dataset.dstat)}`, {
        method: 'PUT',
        body: { status: s.value },
      });
      toast('状态已更新');
      loadDeliverables();
    } catch (e) { toast(e.message, true); }
  });
  $$('[data-dexport]', box).forEach(b => b.onclick = async () => {
    const item = S.deliverables.find(x => x.id === b.dataset.dexport);
    const name = item?.title || 'deliverable';
    const dir = (S.status?.config?.export?.notes_dir || './notes').replace(/^\.\//, '');
    try {
      const p = await api(`/api/deliverables/${encodeURIComponent(b.dataset.dexport)}/export`, {
        method: 'POST',
        body: { path: `${dir}/deliverables/${name}.md` },
      });
      toast(`已导出到 ${p.path}`);
    } catch (e) { toast(e.message, true); }
  });
}

async function showDeliverable(id) {
  try {
    const d = await api(`/api/deliverables/${encodeURIComponent(id)}`);
    const w = window.open('', '_blank');
    const cites = (d.citations || []).map((c, i) =>
      `${i + 1}. ${c.paper_title || c.paper_id || ''} · p.${c.page ?? '?'}${c.tag ? ' · [' + c.tag + ']' : ''}`).join('\n');
    w.document.write(`<!DOCTYPE html><html><head><meta charset="utf-8">
      <title>${esc(d.title)}</title>
      <style>body{font-family:-apple-system,"Microsoft YaHei",sans-serif;max-width:760px;margin:40px auto;padding:0 24px;line-height:1.8;color:#23211e}
      h1{font-size:22px}pre{white-space:pre-wrap;font-family:inherit;font-size:14px}
      .meta{color:#86807a;font-size:12px;margin-bottom:24px;padding-bottom:12px;border-bottom:1px solid #e6e3de}
      .cites{margin-top:32px;padding-top:16px;border-top:1px solid #e6e3de;font-size:12.5px;color:#55504a;white-space:pre-wrap}</style>
      </head><body>
      <h1>${esc(d.title)}</h1>
      <div class="meta">${esc(d.type_label)} · ${esc(d.status_label)} · ${d.word_count} 字${d.citation_count ? ' · ' + d.citation_count + ' 条引用' : ''}</div>
      <pre>${esc(d.content)}</pre>
      ${cites ? `<div class="cites"><strong>引用来源</strong>\n${esc(cites)}</div>` : ''}
      </body></html>`);
  } catch (e) { toast(e.message, true); }
}

/* ==========================================================================
   Skill
   ========================================================================== */
async function loadSkills() {
  const d = await api('/api/skills');
  S.skills = d.items || [];
  $('#skillDir').textContent = d.dir || '—';
  $('#skillCount').textContent = S.skills.length || '';

  const q = ($('#skillSearch').value || '').toLowerCase();
  const items = q
    ? S.skills.filter(k => (k.name || '').toLowerCase().includes(q) || (k.id || '').toLowerCase().includes(q) || (k.path || '').toLowerCase().includes(q))
    : S.skills;

  const box = $('#skillCards');
  if (!items.length) {
    box.innerHTML = `<div class="card"><div class="card-body"><div class="empty">
      <div class="ico">◈</div>
      <div class="title">${q ? '没有匹配的 Skill' : '尚未添加 Skill'}</div>
      <div class="desc">
        ${q ? '换个关键词试试。' : 'Skill 是声明式的提示词扩展（不含可执行代码）。<br>在下方目录里新建一个子目录，放入 <code>skill.yaml</code> 与 <code>prompt.md</code> 即可。'}
      </div>
    </div></div></div>`;
    return;
  }

  box.innerHTML = items.map(k => {
    const v = k.validation || {};
    const issues = [...(v.errors || []), ...(v.warnings || [])];
    return `
      <div class="skill-card ${k.ok ? '' : 'failed'}">
        <div class="skill-avatar" style="background:${k.ok ? 'var(--accent)' : 'var(--danger)'}">
          ${esc((k.name || '?').slice(0, 1).toUpperCase())}
        </div>
        <div class="skill-main">
          <div class="skill-name">
            <span class="n">${esc(k.name || k.id)}</span>
            <span class="badge neutral">v${esc(k.version)}</span>
            <span class="badge ${k.ok ? 'ok' : 'danger'}">${esc(k.status)}</span>
            <span class="badge accent">本机</span>
          </div>
          ${k.description ? `<div class="skill-desc">${esc(k.description)}</div>` : ''}
          <div class="skill-path">📁 ${esc(k.path || '')}</div>
          ${issues.length ? `<div class="skill-issues">${issues.map(i =>
            `<div class="skill-issue ${i.startsWith('✗') ? 'err' : 'warn'}">${esc(i)}</div>`).join('')}</div>` : ''}
        </div>
      </div>`;
  }).join('');
}

function showNewSkill() {
  if ($('#newSkillCard')) return;
  const html = `
    <div class="card" id="newSkillCard" style="margin-bottom:12px;border-color:var(--accent)">
      <div class="card-body">
        <div style="font-size:13px;font-weight:600;margin-bottom:10px">新建 Skill</div>
        <div class="field"><label>标识（id）</label>
          <input class="input" id="nsId" placeholder="小写字母、数字、连字符，如 literature-review">
          <div class="hint">同时也是目录名，创建后不可改。</div></div>
        <div class="field"><label>显示名称</label>
          <input class="input" id="nsName" placeholder="如：文献综述草稿"></div>
        <div class="field"><label>用途说明</label>
          <input class="input" id="nsDesc" placeholder="一句话说明它做什么"></div>
        <div class="row" style="gap:8px">
          <span class="muted" style="font-size:11.5px">会生成 skill.yaml / prompt.md / README.md 三个声明式文件。</span>
          <span style="flex:1"></span>
          <button class="btn sm" id="nsCancel">取消</button>
          <button class="btn sm primary" id="nsCreate">创建</button>
        </div>
      </div>
    </div>`;
  const box = $('#skillCards');
  box.insertAdjacentHTML('afterbegin', html);
  $('#nsCancel').onclick = () => $('#newSkillCard').remove();
  $('#nsCreate').onclick = async () => {
    const id = $('#nsId').value.trim();
    if (!id) { toast('请填写标识', true); return; }
    try {
      const r = await api('/api/skills/scaffold', {
        method: 'POST',
        body: { id, name: $('#nsName').value.trim() || id, description: $('#nsDesc').value.trim() },
      });
      toast(`已创建：${r.path}`);
      await loadSkills();
      await loadSidebar();
    } catch (e) { toast(e.message, true); }
  };
  $('#nsId').focus();
}

/* ==========================================================================
   核验与白名单
   ========================================================================== */
async function loadVerify() {
  await Promise.all([loadWhitelist()]);
}

async function loadWhitelist() {
  const d = await api('/api/whitelist');
  S.whitelist = d.items || [];
  $('#wlCount').textContent = S.whitelist.length;
  const box = $('#wlList');
  if (!S.whitelist.length) {
    box.innerHTML = `<div class="empty" style="padding:28px">
      <div class="title">白名单为空</div>
      <div class="desc">核验未通过的文献可人工确认放行，但必须填写确认依据，且会保留确认当时的自动判定。</div>
    </div>`;
    return;
  }
  box.innerHTML = S.whitelist.map(e => `
    <div class="list-item">
      <div class="list-main">
        <div class="list-title">${esc(e.title)}</div>
        <div class="list-sub">
          <span>确认时判定：${esc(e.level_at_confirm)}</span>
          ${e.doi ? `<span class="mono">${esc(e.doi)}</span>` : ''}
        </div>
        <div style="margin-top:5px;font-size:11.5px;color:var(--muted)">依据：${esc(e.reason)}</div>
      </div>
      <button class="btn sm danger" data-wl="${esc(e.id)}">移除</button>
    </div>`).join('');
  $$('[data-wl]', box).forEach(b => b.onclick = async () => {
    try {
      await api(`/api/whitelist/${encodeURIComponent(b.dataset.wl)}`, { method: 'DELETE' });
      toast('已移除');
      loadWhitelist();
    } catch (e) { toast(e.message, true); }
  });
}

async function doVerify() {
  const q = $('#verifyInput').value.trim();
  if (!q) return;
  const box = $('#verifyResult');
  box.innerHTML = `<div class="card" style="margin-top:12px"><div class="card-body">
    <div class="skeleton" style="width:50%"></div></div></div>`;
  try {
    const r = await api('/api/verify', { method: 'POST', body: { title: q } });
    const citable = r.level === 'verified' || r.level === 'manual_confirmed';

    box.innerHTML = `<div class="card" style="margin-top:12px"><div class="card-body">
      <div class="row" style="margin-bottom:10px">
        ${badgeOf(r.level)}
        <span style="font-size:12.5px">${esc(r.message || '')}</span>
      </div>
      <div class="metric"><span class="k">判定</span><span class="v">${esc(r.level)}</span></div>
      <div class="metric"><span class="k">置信度</span><span class="v">${(r.confidence ?? 0).toFixed(4)}</span></div>
      <div class="metric"><span class="k">响应数据源</span><span class="v">${esc((r.sources_ok || []).join('、') || '无')}</span></div>
      ${r.manual ? `<div class="metric"><span class="k">确认时自动判定</span><span class="v">${esc(r.original_level || '—')}</span></div>` : ''}
      ${(r.matches || []).length ? `
        <div style="margin-top:12px;font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.05em">候选匹配</div>
        ${r.matches.map(m => `<div style="margin-top:6px;font-size:12px">
          <span class="mono muted">[${esc(m.source)}]</span> ${m.similarity.toFixed(3)} · ${esc(m.title)}
        </div>`).join('')}` : ''}
    </div></div>

    ${!citable ? `
    <div class="card" style="margin-top:12px;border-color:var(--warn)">
      <div class="card-head"><span class="card-title">人工确认放行</span></div>
      <div class="card-body">
        <div style="font-size:12px;color:var(--muted);line-height:1.65;margin-bottom:10px">
          如果你确认这篇文献真实存在，可以人工放行。<strong>确认依据必填</strong>，
          且会保留确认当时的自动判定 —— 将来复核时能分清哪些是机器判的、哪些是人拍的板。
        </div>
        <textarea class="textarea" id="wlReason" rows="2"
          placeholder="例如：在 NeurIPS 2024 会议论文集里见过这篇，且作者组有相关前期工作"></textarea>
        <div class="row" style="margin-top:9px;gap:8px">
          <span class="muted" style="font-size:11.5px">放行后徽章显示为「🔵人工确认」，与「✅已核验」区分。</span>
          <span style="flex:1"></span>
          <button class="btn sm" id="btnWlConfirm">确认放行</button>
        </div>
      </div>
    </div>` : ''}
    `;

    const btn = $('#btnWlConfirm');
    if (btn) btn.onclick = async () => {
      const reason = $('#wlReason').value.trim();
      if (reason.length < 4) { toast('请填写确认依据（至少 4 个字）', true); return; }
      try {
        await api('/api/whitelist', {
          method: 'POST',
          body: { title: q, level_at_confirm: r.level, reason },
        });
        toast('已加入白名单');
        await loadWhitelist();
        doVerify();
      } catch (e) { toast(e.message, true); }
    };
  } catch (e) {
    box.innerHTML = `<div class="card" style="margin-top:12px"><div class="card-body">
      <div class="badge danger">核验失败</div>
      <div style="margin-top:8px;font-size:12.5px">${esc(e.message)}</div></div></div>`;
  }
}

/* ==========================================================================
   设置弹窗
   ========================================================================== */
const SETTINGS_SECTIONS = [
  { key: 'general',   label: '通用设置',    ico: '◐' },
  { key: 'model',     label: '模型',        ico: '◈' },
  { key: 'sources',   label: '数据源',      ico: '⇄' },
  { key: 'verify',    label: '核验与白名单', ico: '✓' },
  { key: 'retrieval', label: '检索',        ico: '⌕' },
  { key: 'export',    label: '导出',        ico: '⇩' },
  { key: 'about',     label: '关于',        ico: 'ℹ' },
];

S.providers = [];
S.presets = [];
S.protocols = [];
S.models = [];
S.activeModel = { provider_id: null, model: null };
S.secretsInfo = {};

function openSettings(key = 'general') {
  $('#settingsModal').classList.add('open');
  renderSettingsNav();
  renderSettingsPane(key);
}

function closeSettings() {
  $('#settingsModal').classList.remove('open');
}

function renderSettingsNav() {
  $('#settingsNav').innerHTML = SETTINGS_SECTIONS.map((s, i) => `
    <button class="${i === 0 ? 'active' : ''}" data-set="${s.key}">
      <span style="opacity:.7;font-size:12px">${s.ico}</span> ${s.label}
    </button>`).join('');
  $$('#settingsNav [data-set]').forEach(b => b.onclick = () => {
    $$('#settingsNav [data-set]').forEach(x => x.classList.toggle('active', x === b));
    renderSettingsPane(b.dataset.set);
  });
}

async function renderSettingsPane(key) {
  const box = $('#settingsPanes');
  if (!S.status) {
    try { S.status = await api('/api/status'); } catch { S.status = {}; }
  }
  const c = S.status.config || {};

  if (key === 'model') {
    box.innerHTML = `<div id="pvArea">
      <div class="skeleton" style="width:40%;margin-bottom:12px"></div>
      <div class="skeleton" style="width:80%"></div></div>`;
    await loadProviders();
    return;
  }

  const EDITABLE = ['sources', 'verify', 'retrieval', 'export'];
  box.innerHTML = `
    <div class="card" id="set-pane-${key}"><div class="card-body">${settingsBody(key, c)}</div></div>
    ${EDITABLE.includes(key) ? `<div class="row" style="margin-top:14px;gap:8px">
      <span class="muted" style="font-size:11.5px">改动会写入配置文件；部分项需重启应用才完全生效。</span>
      <span style="flex:1"></span>
      <button class="btn sm primary" id="btnSaveCfg">保存</button>
    </div>` : ''}`;
  const saveBtn = $('#btnSaveCfg');
  if (saveBtn) saveBtn.onclick = () => saveSettingsPane(key);

  if (key === 'general') {
    const sel = $('#setTheme');
    if (sel) sel.onchange = () => {
      S.theme = sel.value;
      localStorage.setItem('litprism-theme', S.theme);
      applyTheme();
    };
  }
  if (key === 'verify') {
    const b = $('#setGoWhitelist');
    if (b) b.onclick = () => { closeSettings(); openView('verify'); };
  }
}

function row(name, desc, control) {
  return `<div class="setting-row">
    <div class="setting-label"><div class="name">${name}</div><div class="desc">${desc}</div></div>
    <div class="setting-control">${control}</div>
  </div>`;
}

function settingsBody(key, c) {
  const llm = S.status?.llm || {};
  switch (key) {
    case 'general':
      return row('数据目录', '知识库、缓存、导出笔记的存放位置。迁移时请先备份。',
          `<div class="mono" style="font-size:11.5px;word-break:break-all">${esc(c.app?.data_dir || '—')}</div>`) +
        row('主题', '浅色 / 深色。也可点标题栏右上角的 ◐ 切换。',
          `<select class="select" id="setTheme">
            <option value="light"${S.theme === 'light' ? ' selected' : ''}>浅色</option>
            <option value="dark"${S.theme === 'dark' ? ' selected' : ''}>深色</option>
          </select>`) +
        row('语言', '界面语言（一期仅简体中文）。', `<input class="input" value="简体中文" disabled>`) +
        row('面板布局', '右侧检视面板的显示状态会被记住。',
          `<button class="btn sm" id="btnResetLayout">重置布局</button>`);

    case 'sources': {
      const items = Object.entries(c.sources || {});
      if (!items.length) return '<div class="muted">无数据源配置</div>';
      return items.map(([k, v]) => {
        const label = { crossref: 'Crossref', openalex: 'OpenAlex', arxiv: 'arXiv',
                        semantic_scholar: 'Semantic Scholar', unpaywall: 'Unpaywall' }[k] || k;
        const on = !!v?.enabled;
        return row(
          `${label}`,
          k === 'semantic_scholar' || k === 'unpaywall'
            ? '需要 API Key 或为可选增强源。'
            : '免费开放，建议保持启用。',
          `<div style="display:flex;flex-direction:column;gap:6px">
             <label class="row" style="gap:6px;cursor:pointer">
               <input type="checkbox" data-cfg="sources.${k}.enabled" data-type="bool"${on ? ' checked' : ''}>
               <span style="font-size:12.5px">启用</span>
             </label>
             ${v && 'mailto' in v ? `<input class="input" data-cfg="sources.${k}.mailto"
               placeholder="联系邮箱（部分源要求）" value="${esc(v.mailto || '')}"
               style="font-size:11.5px;padding:4px 8px">` : ''}
           </div>`);
      }).join('');
    }

    case 'verify':
      return row('已核验阈值', '标题相似度达到该值，且有多源佐证时判定为「已核验」。',
          `<input class="input" type="number" step="0.01" min="0" max="1"
            data-cfg="verification.verified_threshold" data-type="number"
            value="${c.verification?.verified_threshold ?? 0.92}">`) +
        row('疑似阈值', '低于该值视为未匹配；介于两阈值之间为「疑似匹配」，需人工确认。',
          `<input class="input" type="number" step="0.01" min="0" max="1"
            data-cfg="verification.likely_threshold" data-type="number"
            value="${c.verification?.likely_threshold ?? 0.82}">`) +
        row('要求年份匹配', '开启后，年份差异较大的候选会被降级。',
          `<label class="row" style="gap:6px;cursor:pointer">
            <input type="checkbox" data-cfg="verification.require_year_match" data-type="bool"
              ${c.verification?.require_year_match ? 'checked' : ''}>
            <span style="font-size:12.5px">开启</span>
          </label>`) +
        row('人工确认白名单', '自动核验未通过但经人工确认放行的文献。徽章显示为「🔵人工确认」。',
          `<button class="btn sm" id="setGoWhitelist">查看（${S.whitelist.length} 条）</button>`) +
        row('判定等级', '五态：已核验 / 人工确认 / 疑似 / 未核到 / 无法判定。只有前两者可引用。',
          `<div class="row wrap" style="gap:4px">${badgeOf('verified')}${badgeOf('manual_confirmed')}${badgeOf('likely')}${badgeOf('not_found')}${badgeOf('unchecked')}</div>`);

    case 'retrieval':
      return row('分块大小', '每个检索单元的字数。块过大会稀释相似度，过小会切断上下文。',
          `<input class="input" type="number" min="200" max="4000"
            data-cfg="retrieval.chunk_size" data-type="number"
            value="${c.retrieval?.chunk_size ?? 900}">`) +
        row('分块重叠', '相邻块的重叠字数，避免关键句被切断。',
          `<input class="input" type="number" min="0" max="1000"
            data-cfg="retrieval.chunk_overlap" data-type="number"
            value="${c.retrieval?.chunk_overlap ?? 150}">`) +
        row('召回数', '每次检索返回的片段数。受上下文预算约束，实际会动态调整。',
          `<input class="input" type="number" min="1" max="50"
            data-cfg="retrieval.top_k" data-type="number"
            value="${c.retrieval?.top_k ?? 8}">`) +
        row('最低相关度', '低于该分数的片段会被丢弃。0 表示不过滤。',
          `<input class="input" type="number" step="0.01" min="0" max="1"
            data-cfg="retrieval.min_score" data-type="number"
            value="${c.retrieval?.min_score ?? 0}">`) +
        row('上下文预算', '压缩在「新增量」上动手，不在「窗口位置」上动手。',
          `<div class="mono" style="font-size:11.5px">window ${c.context?.window_tokens ?? '—'} · step ${c.context?.step_tokens ?? '—'}</div>`) +
        row('启用扫描件转写', '需要支持图像输入的多模态模型。一期暂缓，开启后不生效。',
          `<span class="badge neutral">暂缓</span>`);

    case 'export':
      return row('默认格式', '导出笔记与交付物的默认格式。',
          `<select class="select" data-cfg="export.formats.0">
            <option value="markdown"${(c.export?.formats || ['markdown'])[0] === 'markdown' ? ' selected' : ''}>Markdown</option>
            <option value="json"${(c.export?.formats || [])[0] === 'json' ? ' selected' : ''}>JSON</option>
          </select>`) +
        row('笔记目录', '导出文件的落盘位置（相对数据目录）。',
          `<input class="input" data-cfg="export.notes_dir"
            value="${esc(c.export?.notes_dir || './notes')}">`);

    case 'about':
      return row('版本', '应用与数据库 schema 版本。',
          `<div class="mono" style="font-size:12px">v1.0.0 · schema v3</div>`) +
        row('知识库', '当前库内数据量。',
          `<div class="mono" style="font-size:11.5px">
            ${S.status?.stats?.papers ?? 0} 篇论文 · ${fmtNum(S.status?.stats?.chunks)} 块
          </div>`) +
        row('当前模型', '对话使用的模型。可在「模型」页切换。',
          `<div class="mono" style="font-size:11.5px">${esc(llm.provider || '—')} / ${esc(llm.model || '—')}</div>`) +
        row('MCP 服务', '一期只做对外 Server。默认只读，随应用启动按配置决定是否拉起。',
          `<div class="row" style="gap:6px">
            <span class="badge ${c.mcp?.readonly ? 'ok' : 'warn'}">${c.mcp?.readonly ? '只读' : '读写'}</span>
            <span class="badge neutral">${c.mcp?.enabled ? '随应用启动' : '未启用'}</span>
          </div>`) +
        row('系统提示词', '学术规范与防幻觉约束为框架内置，不在此处暴露 —— 防止无意改坏可信度地基。',
          `<span class="badge neutral">不可更改</span>`);
  }
  return '';
}

/* 收集 pane 里所有 [data-cfg] 控件的值，拼成嵌套 patch */
function collectPatch(pane) {
  const patch = {};
  pane.querySelectorAll('[data-cfg]').forEach(el => {
    const path = el.dataset.cfg.split('.');
    let v;
    if (el.dataset.type === 'bool' || el.type === 'checkbox') v = el.checked;
    else if (el.dataset.type === 'number' || el.type === 'number') v = Number(el.value);
    else v = el.value;

    let node = patch;
    for (let i = 0; i < path.length - 1; i++) {
      const k = path[i];
      const isIdx = /^\d+$/.test(k);
      if (isIdx) {
        node[k] = node[k] || [];
      } else {
        node[k] = node[k] || {};
      }
      node = node[k];
    }
    node[path[path.length - 1]] = v;
  });
  return patch;
}

async function saveSettingsPane(key) {
  const pane = $(`#set-pane-${key}`);
  if (!pane) return;
  const patch = collectPatch(pane);
  try {
    const r = await api('/api/config', { method: 'PATCH', body: patch });
    S.status = { ...(S.status || {}), config: r.config };
    toast('已保存');
    (r.warnings || []).forEach(w => toast(w, true));
    if (key === 'sources') loadSidebar();
  } catch (e) {
    toast(e.message, true);
  }
}

/* ==========================================================================
   模型提供商
   ========================================================================== */
async function loadProviders() {
  const d = await api('/api/providers');
  S.providers = d.items || [];
  S.presets = d.presets || [];
  S.protocols = d.protocols || [];
  S.activeModel = d.active || { provider_id: null, model: null };
  S.secretsInfo = d.secrets || {};
  renderProviders();
}

function renderProviders() {
  const area = $('#pvArea');
  if (!area) return;

  const activePid = S.activeModel.provider_id;
  const cards = S.providers.map(p => {
    const dotCls = p.status === 'ok' ? 'ok' : p.status === 'failed' ? 'failed' : 'unconfigured';
    const enabled = (p.models || []).filter(m => m.enabled).length;
    const isActive = p.id === activePid;
    return `
      <div class="pv-card" data-pv="${esc(p.id)}">
        <div class="pv-head" data-pv-toggle="${esc(p.id)}">
          <span class="pv-name">${esc(p.name)}</span>
          <span class="pv-tag">${esc(p.is_custom ? '自定义' : '预设')}</span>
          <span class="pv-dot ${dotCls}" title="${esc(p.status_label)}"></span>
          ${isActive ? '<span class="badge accent">当前使用</span>' : ''}
          <span style="flex:1"></span>
          <span class="muted" style="font-size:11px">${enabled}/${(p.models || []).length} 模型</span>
          <div class="pv-actions">
            <button class="btn sm" data-pv-edit="${esc(p.id)}">编辑</button>
            <button class="btn sm danger" data-pv-del="${esc(p.id)}">删除</button>
          </div>
        </div>
        <div class="pv-body">${providerEditor(p)}</div>
      </div>`;
  }).join('');

  area.innerHTML = `
    <div class="row" style="margin-bottom:12px">
      <div>
        <div style="font-size:13px;font-weight:600">模型提供商</div>
        <div class="muted" style="font-size:11.5px;margin-top:2px">
          API Key 加密存储（${esc(S.secretsInfo.backend || '—')}），不写入配置文件。
        </div>
      </div>
      <span style="flex:1"></span>
      <button class="btn sm primary" id="btnAddProvider">＋ 添加提供商</button>
    </div>
    <div id="pvList">${cards || `<div class="empty" style="padding:30px">
      <div class="title">尚未配置模型提供商</div>
      <div class="desc">添加一个后，可从提供商拉取模型列表，并选择哪些暴露到对话。</div>
    </div>`}</div>
    ${S.secretsInfo.backend === 'weak'
      ? `<div class="pv-note" style="border-color:var(--warn);color:var(--warn)">${esc(S.secretsInfo.note)}</div>`
      : `<div class="pv-note">${esc(S.secretsInfo.note || '')}</div>`}
  `;

  $$('#pvList [data-pv-toggle]').forEach(h => h.onclick = e => {
    if (e.target.closest('[data-pv-edit],[data-pv-del]')) return;
    h.closest('.pv-card').classList.toggle('expanded');
  });
  $$('#pvList [data-pv-edit]').forEach(b => b.onclick = e => {
    e.stopPropagation();
    b.closest('.pv-card').classList.add('expanded');
  });
  $$('#pvList [data-pv-del]').forEach(b => b.onclick = async e => {
    e.stopPropagation();
    if (!confirm('删除该提供商？已保存的 API Key 会一并清除。')) return;
    try {
      await api(`/api/providers/${encodeURIComponent(b.dataset.pvDel)}`, { method: 'DELETE' });
      toast('已删除');
      await loadProviders();
      await loadModels();
    } catch (err) { toast(err.message, true); }
  });
  const addBtn = $('#btnAddProvider');
  if (addBtn) addBtn.onclick = showAddProvider;
  bindProviderEditors();
}

function providerEditor(p) {
  const modelRows = (p.models || []).map(m => `
    <div class="pm-row">
      <input class="input" data-pm-id placeholder="模型 ID，如 gpt-4o" value="${esc(m.model_id)}">
      <input class="input" data-pm-name placeholder="显示名称" value="${esc(m.display_name)}">
      <button class="icon-btn ${m.enabled ? 'on' : ''}" data-pm-toggle
        title="${m.enabled ? '已暴露给对话' : '未暴露给对话'}">${m.enabled ? '◉' : '○'}</button>
      <button class="icon-btn" data-pm-del title="删除">🗑</button>
    </div>`).join('');

  return `
    <div class="field" style="margin-top:10px">
      <label>API 密钥</label>
      <input class="input" type="password" data-pv-key
        placeholder="${p.has_key ? `已配置（${esc(p.api_key_mask)}），输入新值可替换` : '输入 API Key'}">
      <div class="hint">留空表示不修改。Key 加密后存入本地密钥库，不写入 config.yaml。</div>
    </div>
    <div class="field">
      <label>显示名称</label>
      <input class="input" data-pv-name value="${esc(p.name)}">
    </div>
    <div class="field">
      <label>API 地址</label>
      <input class="input" data-pv-url value="${esc(p.base_url)}">
    </div>
    <div class="field">
      <label>API 协议</label>
      <select class="select" data-pv-protocol>
        ${S.protocols.map(x => `<option value="${esc(x.key)}"${x.key === p.protocol ? ' selected' : ''}>${esc(x.label)}</option>`).join('')}
      </select>
    </div>

    <div class="pm-head">
      模型目录
      <span class="spacer"></span>
      <button data-pv-reset>恢复默认模型</button>
      <button data-pv-fetch>获取可用模型</button>
    </div>
    <div class="hint" style="margin:-4px 0 8px;font-size:11px">
      ◉ 表示该模型会出现在对话的模型选择器里，点击圆圈切换。
    </div>
    <div data-pv-models>${modelRows || '<div class="pm-empty">尚未配置模型。点「获取可用模型」从提供商拉取，或手动添加。</div>'}</div>
    <div class="row" style="margin-top:10px;gap:8px">
      <button class="btn sm" data-pv-addmodel>＋ 添加一行</button>
      <span style="flex:1"></span>
      <button class="btn sm" data-pv-test>测试连接</button>
      <button class="btn sm primary" data-pv-save>保存</button>
    </div>
    ${p.status_note ? `<div class="pv-note">${esc(p.status_note)}</div>` : ''}
  `;
}

function bindProviderEditors() {
  $$('#pvList .pv-card').forEach(card => {
    const pid = card.dataset.pv;
    const q = sel => card.querySelector(sel);

    const bindRows = () => {
      card.querySelectorAll('[data-pm-toggle]').forEach(b => b.onclick = () => {
        b.classList.toggle('on');
        const on = b.classList.contains('on');
        b.textContent = on ? '◉' : '○';
        b.title = on ? '已暴露给对话' : '未暴露给对话';
      });
      card.querySelectorAll('[data-pm-del]').forEach(b => b.onclick = () => {
        b.closest('[data-pm-row]')?.remove();
        b.closest('.pm-row')?.remove();
      });
    };
    bindRows();

    q('[data-pv-addmodel]')?.addEventListener('click', () => {
      const box = q('[data-pv-models]');
      box.querySelector('.pm-empty')?.remove();
      const div = document.createElement('div');
      div.className = 'pm-row';
      div.innerHTML = `
        <input class="input" data-pm-id placeholder="模型 ID">
        <input class="input" data-pm-name placeholder="显示名称">
        <button class="icon-btn on" data-pm-toggle title="已暴露给对话">◉</button>
        <button class="icon-btn" data-pm-del title="删除">🗑</button>`;
      box.appendChild(div);
      bindRows();
    });

    q('[data-pv-fetch]')?.addEventListener('click', async () => {
      const btn = q('[data-pv-fetch]');
      const old = btn.textContent;
      btn.disabled = true;
      btn.textContent = '拉取中…';
      try {
        const r = await api(`/api/providers/${encodeURIComponent(pid)}/fetch-models`, { method: 'POST' });
        showCandidates(card, pid, r.items || []);
      } catch (e) {
        toast(e.message, true);
      } finally {
        btn.disabled = false;
        btn.textContent = old;
      }
    });

    q('[data-pv-join]')?.addEventListener('click', async () => {
      const picked = [...card.querySelectorAll('[data-cand]:checked')]
        .map(cb => ({ model_id: cb.value, display_name: cb.dataset.name || cb.value }));
      if (!picked.length) { toast('请先勾选要加入的模型', true); return; }
      await joinModels(card, pid, picked);
    });
    q('[data-pv-joinall]')?.addEventListener('click', async () => {
      const picked = [...card.querySelectorAll('[data-cand]')]
        .map(cb => ({ model_id: cb.value, display_name: cb.dataset.name || cb.value }));
      if (!picked.length) { toast('没有可加入的模型', true); return; }
      await joinModels(card, pid, picked);
    });

    q('[data-pv-reset]')?.addEventListener('click', () => {
      const box = q('[data-pv-models]');
      box.innerHTML = '<div class="pm-empty">已清空，可重新添加或拉取</div>';
    });

    q('[data-pv-test]')?.addEventListener('click', async () => {
      try {
        const r = await api(`/api/providers/${encodeURIComponent(pid)}/test`, { method: 'POST' });
        toast(r.ok ? `连接正常：${r.message}` : `连接失败：${r.message}`, !r.ok);
        if (r.ok) loadProviders();
      } catch (e) { toast(e.message, true); }
    });

    q('[data-pv-save]')?.addEventListener('click', async () => {
      const models = [...card.querySelectorAll('.pm-row')].map(row => ({
        model_id: row.querySelector('[data-pm-id]').value.trim(),
        display_name: row.querySelector('[data-pm-name]').value.trim(),
        enabled: row.querySelector('[data-pm-toggle]').classList.contains('on'),
      })).filter(m => m.model_id);

      try {
        await api(`/api/providers/${encodeURIComponent(pid)}`, {
          method: 'PUT',
          body: {
            name: q('[data-pv-name]').value,
            base_url: q('[data-pv-url]').value,
            protocol: q('[data-pv-protocol]').value,
            api_key: q('[data-pv-key]').value || null,
          },
        });
        await api(`/api/providers/${encodeURIComponent(pid)}/models`, {
          method: 'PUT', body: { models },
        });
        toast('已保存');
        await loadProviders();
        await loadModels();
      } catch (e) { toast(e.message, true); }
    });
  });
}

/* 拉取到的可用模型 → 勾选加入目录（增量，不冲掉已调好的条目） */
function showCandidates(card, pid, items) {
  const existing = new Set(
    (S.providers.find(p => p.id === pid)?.models || []).map(m => m.model_id)
  );
  if (!items.length) {
    toast('该提供商未返回任何模型，可手动添加', true);
    return;
  }
  const dupes = items.filter(i => existing.has(i.model_id)).length;
  const fresh = items.filter(i => !existing.has(i.model_id));

  let box = card.querySelector('[data-pv-candidates]');
  if (!box) {
    box = document.createElement('div');
    box.setAttribute('data-pv-candidates', '');
    card.querySelector('.pv-body').appendChild(box);
  }
  box.style.display = '';
  box.innerHTML = `
    <div class="pm-head">
      可用模型（${items.length} 个${dupes ? `，${dupes} 个已在目录` : ''}）
      <span class="spacer"></span>
      <button data-pv-join>加入所选</button>
      ${fresh.length ? '<button data-pv-joinall>全部加入</button>' : ''}
      <button data-pv-closecand>收起</button>
    </div>
    <div style="max-height:190px;overflow-y:auto;border:1px solid var(--border);border-radius:6px">
      ${items.map(m => {
        const inDir = existing.has(m.model_id);
        return `<label style="display:flex;gap:8px;align-items:center;padding:6px 10px;
                       border-bottom:1px solid var(--border);font-size:12px;cursor:pointer">
          <input type="checkbox" data-cand="${esc(m.model_id)}" data-name="${esc(m.display_name)}"
                 ${inDir ? 'disabled' : 'checked'}>
          <span style="flex:1;min-width:0">
            ${esc(m.display_name || m.model_id)}
            ${m.model_id !== (m.display_name || '') ? `<span class="mono muted">· ${esc(m.model_id)}</span>` : ''}
          </span>
          ${inDir ? '<span class="badge ok">已在目录</span>' : ''}
        </label>`;
      }).join('')}
    </div>`;

  box.querySelector('[data-pv-closecand]').onclick = () => { box.style.display = 'none'; };
  box.querySelector('[data-pv-join]').onclick = async () => {
    const picked = [...box.querySelectorAll('[data-cand]:checked')]
      .map(cb => ({ model_id: cb.value, display_name: cb.dataset.name || cb.value }));
    if (!picked.length) { toast('请先勾选要加入的模型', true); return; }
    await joinModels(card, pid, picked);
  };
  const allBtn = box.querySelector('[data-pv-joinall]');
  if (allBtn) allBtn.onclick = async () => {
    const picked = [...box.querySelectorAll('[data-cand]')]
      .map(cb => ({ model_id: cb.value, display_name: cb.dataset.name || cb.value }));
    if (!picked.length) { toast('没有可加入的模型', true); return; }
    await joinModels(card, pid, picked);
  };
}

async function joinModels(card, pid, picked) {
  try {
    const r = await api(`/api/providers/${encodeURIComponent(pid)}/models`, {
      method: 'PUT', body: { mode: 'merge', models: picked },
    });
    toast(`已加入 ${r.added.length} 个模型${r.skipped.length ? `（${r.skipped.length} 个已存在，跳过）` : ''}`);
    card.querySelector('[data-pv-candidates]')?.remove();
    await loadProviders();
    await loadModels();
  } catch (e) { toast(e.message, true); }
}

function showAddProvider() {
  const opts = S.presets.map(p => `<option value="${esc(p.key)}">${esc(p.name)}</option>`).join('');

  const html = `
    <div class="card" id="addPvCard" style="margin-bottom:12px;border-color:var(--accent)">
      <div class="card-body">
        <div class="field">
          <label>预设</label>
          <select class="select" id="addPvPreset">
            <option value="">自定义</option>${opts}
          </select>
          <div class="hint">选预设会自动填好地址与协议。</div>
        </div>
        <div class="field"><label>显示名称</label>
          <input class="input" id="addPvName" placeholder="如：公司内网网关"></div>
        <div class="field"><label>API 地址</label>
          <input class="input" id="addPvUrl" placeholder="https://api.example.com/v1"></div>
        <div class="field"><label>API 协议</label>
          <select class="select" id="addPvProtocol">
            ${S.protocols.map(x => `<option value="${esc(x.key)}">${esc(x.label)}</option>`).join('')}
          </select></div>
        <div class="field"><label>API 密钥</label>
          <input class="input" type="password" id="addPvKey" placeholder="留空表示暂不配置（本地服务可留空）">
          <div class="hint">加密后存入本地密钥库，不写入配置文件。</div></div>
        <div class="row" style="gap:8px">
          <span style="flex:1"></span>
          <button class="btn sm" id="addPvCancel">取消</button>
          <button class="btn sm primary" id="addPvOk">添加</button>
        </div>
      </div>
    </div>`;

  const list = $('#pvList');
  if (!list) return;
  list.insertAdjacentHTML('afterbegin', html);

  const presetSel = $('#addPvPreset');
  presetSel.onchange = () => {
    const p = S.presets.find(x => x.key === presetSel.value);
    if (!p) return;
    $('#addPvName').value = p.name;
    $('#addPvUrl').value = p.base_url;
    $('#addPvProtocol').value = p.protocol;
  };
  $('#addPvCancel').onclick = () => $('#addPvCard').remove();
  $('#addPvOk').onclick = async () => {
    try {
      await api('/api/providers', {
        method: 'POST',
        body: {
          name: $('#addPvName').value,
          base_url: $('#addPvUrl').value,
          protocol: $('#addPvProtocol').value,
          api_key: $('#addPvKey').value,
          preset: presetSel.value || null,
        },
      });
      toast('已添加');
      await loadProviders();
      await loadModels();
    } catch (e) { toast(e.message, true); }
  };
}

/* ==========================================================================
   对话的模型选择器
   ========================================================================== */
async function loadModels() {
  try {
    const d = await api('/api/models');
    S.models = d.items || [];
    S.activeModel = d.active || S.activeModel;
  } catch { S.models = []; }
  renderModelSelector();
}

function renderModelSelector() {
  const sel = $('#chatModel');
  if (!sel) return;
  if (!S.models.length) {
    sel.innerHTML = `<option value="">未配置模型（使用默认）</option>`;
    return;
  }
  sel.innerHTML = S.models.map(m => {
    const isActive = m.provider_id === S.activeModel.provider_id && m.model_id === S.activeModel.model;
    return `<option value="${esc(m.provider_id)}::${esc(m.model_id)}"${isActive ? ' selected' : ''}>${esc(m.label)}</option>`;
  }).join('');
  sel.onchange = async () => {
    const [pid, mid] = sel.value.split('::');
    if (!pid || !mid) return;
    try {
      await api(`/api/providers/${encodeURIComponent(pid)}/active`, {
        method: 'POST', body: { model_id: mid },
      });
      S.activeModel = { provider_id: pid, model: mid };
      toast(`已切换到 ${mid}`);
    } catch (e) { toast(e.message, true); }
  };
}

/* ==========================================================================
   右侧检视面板
   ========================================================================== */
async function loadInspectorContext() {
  const box = $('#inspContext');
  if (!S.activeConv) {
    box.innerHTML = `<div class="empty" style="padding:28px 8px">
      <div class="ico">◫</div>
      <div class="title">未选中对话</div>
      <div class="desc">选择一个会话后，这里显示上下文占用、压缩块与账本。</div>
    </div>`;
    return;
  }
  try {
    const d = await api(`/api/context/${encodeURIComponent(S.activeConv)}`);
    const cp = d.context_pressure || {};
    const occ = d.two_kinds_of_occupancy || {};
    const bd = d.context_breakdown || {};
    const p = bd.percent || {};
    const barCls = cp.ratio >= 0.95 ? 'danger' : cp.ratio >= 0.75 ? 'warn' : '';

    box.innerHTML = `
      <div style="font-size:11px;font-weight:600;color:var(--muted);letter-spacing:.05em;text-transform:uppercase;margin-bottom:8px">
        两种占用
      </div>
      <div class="metric"><span class="k">会话记账</span><span class="v">${fmtNum(occ.total_tokens)}</span></div>
      <div class="metric"><span class="k">会话可见内容（估算）</span><span class="v">${fmtNum(occ.surface_tokens)}</span></div>
      <div style="font-size:11px;color:var(--muted);margin-top:6px;line-height:1.6">
        记账含已压缩原文；可见内容仅统计消息与摘要。系统指令、偏好和本轮检索片段另占空间，实际请求可能裁减历史。
      </div>

      <div style="margin-top:16px;font-size:11px;font-weight:600;color:var(--muted);letter-spacing:.05em;text-transform:uppercase">
        窗口压力
      </div>
      <div class="row" style="margin-top:6px">
        <span class="mono" style="font-size:15px">${pct(cp.ratio)}</span>
        <span class="muted" style="font-size:11px">/ 窗口 ${fmtNum(cp.window)}</span>
      </div>
      <div class="bar ${barCls}"><i style="width:${Math.min(100, (cp.ratio || 0) * 100)}%"></i></div>
      <div style="font-size:11px;color:var(--muted);margin-top:4px">来源：${esc(cp.source || '估算值')}</div>

      <div style="margin-top:16px;font-size:11px;font-weight:600;color:var(--muted);letter-spacing:.05em;text-transform:uppercase;margin-bottom:6px">
        构成分解
      </div>
      ${[['system', '系统提示词'], ['dialogue', '对话'], ['summary', '摘要'], ['citations', '引用片段']]
        .map(([k, label]) => `
        <div class="metric"><span class="k">${label}</span>
          <span class="v">${fmtNum(bd[k] || 0)} <span class="muted">${(p[k] || 0).toFixed(1)}%</span></span></div>`).join('')}

      ${(d.blocks || []).length ? `
        <div style="margin-top:16px;font-size:11px;font-weight:600;color:var(--muted);letter-spacing:.05em;text-transform:uppercase;margin-bottom:6px">
          压缩块
        </div>
        ${d.blocks.map(b => `
          <div style="padding:7px 0;border-bottom:1px dashed var(--border);font-size:11.5px">
            <div class="row"><span class="mono">${esc(b.range)}</span>
              <span class="spacer"></span>
              <span class="muted">${b.shadowed_tokens}→${b.summary_tokens}</span></div>
            <div class="muted" style="margin-top:2px">${esc(b.topic || '')} · 句柄 ${b.handles}</div>
          </div>`).join('')}` : ''}

      ${(d.ledger || []).length ? `
        <div style="margin-top:16px;font-size:11px;font-weight:600;color:var(--muted);letter-spacing:.05em;text-transform:uppercase;margin-bottom:6px">
          压缩账本
        </div>
        ${d.ledger.slice(0, 5).map(e => `
          <div class="metric"><span class="k">${esc(e.trigger)}</span>
            <span class="v">省 ${fmtNum(e.saved_per_turn)}/轮</span></div>`).join('')}` : ''}

      ${d.note ? `<div class="insp-note">${esc(d.note)}</div>` : ''}
    `;
  } catch (e) {
    box.innerHTML = `<div class="empty" style="padding:24px 8px"><div class="desc">${esc(e.message)}</div></div>`;
  }
}

async function loadInspectorSources() {
  const box = $('#inspSources');
  const view = S.activeView;
  const paperId = S.activePaper;
  if (view === 'chat') {
    box.innerHTML = S.sources.length
      ? `<p class="source-intro">最近一次回答检索到的原文片段。页码与内容均取自知识库；不表示每个片段都被回答引用。</p>${LitUI.renderSegments(S.sources)}`
      : '<div class="empty"><div class="title">本轮暂无原文来源</div><div class="desc">有检索依据的回答会在这里显示原文片段。历史对话和用户偏好不会成为引用来源。</div></div>';
    LitUI.bind(box);
    return;
  }
  if (view !== 'paper' || !paperId) {
    box.innerHTML = '<div class="empty"><div class="title">让结论回到原文</div><div class="desc">打开一篇文献查看原文，或在对话中提问，查看回答对应的证据片段。</div></div>';
    return;
  }
  box.innerHTML = '<div class="skeleton"></div>';
  const d = await api(`/api/paper/${encodeURIComponent(paperId)}/text`);
  if (S.activeView !== view || S.activePaper !== paperId) return;
  box.innerHTML = '<p class="source-intro">当前文献的原文摘录，按页码排列。</p>' + (d.pages || []).map(pg => `
    <details class="source-page"><summary>${pg.page == null ? '无页码' : '第 ' + esc(pg.page) + ' 页'}</summary>
      ${pg.chunks.map(c => `<div class="source-excerpt">${c.section ? `<strong>${esc(c.section)}</strong>` : ''}<p>${esc(c.text)}</p></div>`).join('')}
    </details>`).join('');
}

async function loadInspectorGraph() {
  const box = $('#inspGraph');
  box.innerHTML = `<div class="empty" style="padding:24px 8px"><div class="skeleton" style="width:70%"></div></div>`;
  try {
    const d = await api('/api/graph');
    const nodes = d.nodes || [];
    const edges = d.edges || d.relations || [];
    if (!nodes.length) {
      box.innerHTML = `<div class="empty" style="padding:28px 8px">
        <div class="ico">◎</div><div class="title">暂无关联数据</div>
        <div class="desc">导入两篇以上论文后自动计算关联。</div></div>`;
      return;
    }
    box.innerHTML = `
      <div class="metric"><span class="k">节点</span><span class="v">${nodes.length}</span></div>
      <div class="metric"><span class="k">边</span><span class="v">${edges.length}</span></div>
      <div style="margin-top:12px;font-size:11px;font-weight:600;color:var(--muted);letter-spacing:.05em;text-transform:uppercase;margin-bottom:6px">
        主题簇
      </div>
      ${nodes.slice(0, 12).map(n => `
        <div style="padding:6px 0;border-bottom:1px dashed var(--border);font-size:11.5px">
          ${esc(short(n.title || n.id, 34))}
        </div>`).join('')}
      <div class="insp-note">图谱渲染将在接入本地图库后可用；当前以列表形式呈现。</div>
    `;
  } catch (e) {
    box.innerHTML = `<div class="empty" style="padding:24px 8px"><div class="desc">${esc(e.message)}</div></div>`;
  }
}

async function loadInspectorDeliv() {
  const box = $('#inspDeliv');
  try {
    const d = await api('/api/deliverables');
    const items = d.items || [];
    if (!items.length) {
      box.innerHTML = `<div class="empty" style="padding:28px 8px">
        <div class="ico">📝</div><div class="title">暂无交付物</div></div>`;
      return;
    }
    box.innerHTML = items.slice(0, 10).map(x => `
      <button class="side-item" style="padding:8px 4px" data-inspdeliv="${esc(x.id)}">
        <span class="ico">📄</span>
        <span class="label">${esc(short(x.title, 22))}</span>
        <span class="meta">${esc(x.status_label)}</span>
      </button>`).join('');
    $$('[data-inspdeliv]', box).forEach(b => b.onclick = () => showDeliverable(b.dataset.inspdeliv));
  } catch (e) {
    box.innerHTML = `<div class="empty" style="padding:24px 8px"><div class="desc">${esc(e.message)}</div></div>`;
  }
}

/* ==========================================================================
   启动
   ========================================================================== */
function bindGlobal() {
  $$('[data-icon]').forEach(el => { el.innerHTML = icon(el.dataset.icon); });
  $('#btnSidebar').onclick = toggleSidebar;
  $('#btnNewConv').onclick = newConversation;
  $('#btnSearch').onclick = () => { openView('library'); $('#libSearch').focus(); };
  $('#btnCloseInspector').onclick = () => { toggleInspector(false); $('#btnInspector').focus(); };
  // 主题
  $('#btnTheme').onclick = () => {
    S.theme = S.theme === 'dark' ? 'light' : 'dark';
    localStorage.setItem('litprism-theme', S.theme);
    applyTheme();
    if (S.activeView === 'settings') loadSettings();
  };

  // 右侧面板开关
  $('#btnInspector').onclick = () => toggleInspector();

  // 侧边栏折叠
  $$('[data-toggle]').forEach(h => h.onclick = () => {
    h.classList.toggle('collapsed');
    const box = h.nextElementSibling;
    if (box) box.classList.toggle('hidden');
  });

  // 品牌 → 关于
  $('#brand').onclick = () => openSettings('about');

  // 视图入口
  $$('[data-view]').forEach(b => b.onclick = () => enterView(b.dataset.view));

  // 知识库搜索
  $('#libSearch').oninput = () => { if (S.activeView === 'library') loadLibrary(); };

  // Skill 搜索与刷新
  $('#skillSearch').oninput = () => { if (S.activeView === 'skills') loadSkills(); };
  $('#btnSkillRefresh').onclick = () => loadSkills().then(() => toast('已刷新'));
  const skillNewBtn = $('#btnSkillNew');
  if (skillNewBtn) skillNewBtn.onclick = showNewSkill;

  // 核验
  $('#btnVerify').onclick = doVerify;
  $('#verifyInput').onkeydown = e => { if (e.key === 'Enter') doVerify(); };

  // 导出全部
  $('#btnExportAll').onclick = () => toast('批量导出将在后续版本提供');

  // 右侧面板 tab
  $$('[data-insp]').forEach(t => t.onclick = () => {
    S.inspectorTab = t.dataset.insp;
    $$('[data-insp]').forEach(x => x.classList.toggle('active', x === t));
    $$('[data-insp-pane]').forEach(p => p.classList.toggle('active', p.dataset.inspPane === t.dataset.insp));
    refreshInspector();
  });

  // 设置弹窗
  $('#btnCloseSettings').onclick = closeSettings;
  $('#settingsModal').onclick = e => { if (e.target.id === 'settingsModal') closeSettings(); };
  $('#btnOpenConfig').onclick = () => {
    const p = S.status?.config?.app?.data_dir;
    toast(p ? `配置文件位于 ${p}/../config/config.yaml` : '配置文件位于项目 config/ 目录');
  };

  // 快捷键
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && $('#settingsModal').classList.contains('open')) {
      closeSettings();
      return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key === ',') {
      e.preventDefault();
      openSettings('general');
      return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'b') {
      e.preventDefault();
      if (e.altKey) toggleInspector(); else toggleSidebar();
      return;
    }
    if (e.key === 'Escape' && S.inspector) {
      toggleInspector(false);
      $('#btnInspector').focus();
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      openView('library');
      setTimeout(() => $('#libSearch')?.focus(), 60);
    }
  });
}

async function boot() {
  applyTheme();
  applyInspector();
  applySidebar();
  bindGlobal();

  try {
    S.status = await api('/api/status');
    $('#brandVersion').textContent = 'v' + (S.status.config?.app?.version || '1.0.0');
  } catch (e) {
    toast('无法连接后端：' + e.message, true);
  }

  await loadSidebar();
  openView('library');
}

boot();
