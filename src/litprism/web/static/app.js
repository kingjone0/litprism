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
  inspector: localStorage.getItem('litprism-inspector') !== '0',
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

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

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
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
    verified:        ['ok', '✅ 已核验'],
    manual_confirmed:['manual', '🔵 人工确认'],
    likely:          ['warn', '⚠️ 疑似'],
    not_found:       ['danger', '❌ 未核到'],
    unchecked:       ['neutral', '❔ 无法判定'],
  };
  const [cls, text] = map[level] || ['neutral', '❔ 未知'];
  return `<span class="badge ${cls}">${text}</span>`;
}

/* ---------------------------------------------------------------- 主题 */
function applyTheme() {
  document.documentElement.dataset.theme = S.theme;
  $('#btnTheme').title = S.theme === 'dark' ? '切换为浅色' : '切换为深色';
}
function applyInspector() {
  $('#main').classList.toggle('inspector-hidden', !S.inspector);
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
  $$('.pane').forEach(p => p.classList.toggle('active', p.dataset.pane === view));
  $('#workBody').classList.toggle('flush', view === 'chat');

  if (view === 'paper' && opts.paperId) S.activePaper = opts.paperId;
  if (view === 'chat' && opts.convId) S.activeConv = opts.convId;

  if (!S.tabs.includes(view)) S.tabs.push(view);
  renderTabs();
  $$('.side-item').forEach(b => b.classList.toggle('active', b.dataset.view === view));

  const loader = {
    library: loadLibrary, paper: loadPaperDetail, chat: loadChat,
    deliverables: loadDeliverables, skills: loadSkills, verify: loadVerify,
    settings: loadSettings,
  }[view];
  if (loader) loader().catch(e => toast(e.message, true));
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
  convBox.innerHTML = `<button class="side-new" id="btnNewConv">＋ 新建对话</button>` +
    S.conversations.map(c => `
      <button class="side-item ${c.id === S.activeConv ? 'active' : ''}" data-conv="${esc(c.id)}">
        <span class="ico">💬</span>
        <span class="label">${esc(short(c.title, 18))}</span>
        <span class="meta">${c.message_count || 0}</span>
      </button>`).join('');
  $('#btnNewConv').onclick = newConversation;
  $$('[data-conv]', convBox).forEach(b => b.onclick = () => openView('chat', { convId: b.dataset.conv }));

  // ---- 知识库 ----
  const lib = $('#libList');
  if (!S.papers.length) {
    lib.innerHTML = `<div class="side-empty">知识库为空。<br>用 <code class="mono">litprism add &lt;pdf&gt;</code> 导入。</div>`;
  } else {
    lib.innerHTML = S.papers.map(p => `
      <button class="side-item" data-paper="${esc(p.id)}">
        <span class="ico">📄</span>
        <span class="label" title="${esc(p.title)}">${esc(short(p.display_title || p.title, 20))}</span>
      </button>`).join('');
    $$('[data-paper]', lib).forEach(b => b.onclick = () => openView('paper', { paperId: b.dataset.paper }));
  }

  // ---- Skill ----
  const sl = $('#skillList');
  if (!S.skills.length) {
    sl.innerHTML = `<div class="side-empty">尚未添加 Skill</div>
      <button class="side-item" data-view="skills"><span class="ico">⚙</span><span class="label">管理 Skill</span></button>`;
  } else {
    sl.innerHTML = S.skills.map(k => `
      <button class="side-item" data-view="skills">
        <span class="ico">${k.ok ? '◈' : '✕'}</span>
        <span class="label">${esc(k.name)}</span>
        <span class="meta">v${esc(k.version)}</span>
      </button>`).join('');
  }
  $('#skillCount').textContent = S.skills.length || '';
  $$('.side-item[data-view]').forEach(b => b.onclick = () => openView(b.dataset.view));
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
    `${st.papers ?? items.length} 篇论文 · ${fmtNum(st.chunks)} 个分块 · 已解读 ${st.analyses ?? 0} 篇`;

  const box = $('#paperList');
  if (!items.length) {
    box.innerHTML = `<div class="empty">
      <div class="ico">📚</div>
      <div class="title">${q ? '没有匹配的论文' : '知识库还是空的'}</div>
      <div class="desc">${q ? '换个关键词试试。' : '用 <code>litprism add &lt;论文.pdf&gt;</code> 导入，或把 PDF 拖到窗口里。'}</div>
    </div>`;
    return;
  }

  box.innerHTML = items.map(p => `
    <button class="list-item" data-paper="${esc(p.id)}">
      <div class="list-main">
        <div class="list-title">${esc(p.display_title || p.title)}</div>
        <div class="list-sub">
          <span>${esc((p.authors || []).slice(0, 3).join('、') || '作者未知')}${(p.authors || []).length > 3 ? ' 等' : ''}</span>
          <span>${p.year || '年份未知'}</span>
          ${p.venue ? `<span>${esc(p.venue)}</span>` : ''}
          ${badgeOf(p.verify_level)}
        </div>
      </div>
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
        <div class="page-title">${esc(p.display_title || p.title)}</div>
        <div class="page-sub">
          ${esc((p.authors || []).join('、') || '作者未知')} · ${p.year || '年份未知'}
          ${p.venue ? ' · ' + esc(p.venue) : ''}
        </div>
        <div class="row wrap" style="margin-top:10px">
          ${badgeOf(p.verify_level)}
          ${p.doi ? `<span class="mono muted">DOI: ${esc(p.doi)}</span>` : ''}
          <span class="spacer"></span>
          <button class="btn sm" id="btnReanalyze">重新解读</button>
        </div>
      </div>

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
          <div class="desc">点击「重新解读」生成结构化解读（研究问题 / 方法 / 实验 / 结论 / 创新点 / 局限）。</div>
        </div>
      </div></div>`}

      ${concepts.length ? `
      <div class="card">
        <div class="card-head"><span class="card-title">术语</span>
          <span class="badge neutral">${concepts.length}</span></div>
        <div class="card-body">
          <table class="table">
            <thead><tr><th style="width:150px">术语</th><th>文中含义</th></tr></thead>
            <tbody>${concepts.map(c => `
              <tr><td><strong>${esc(c.term)}</strong></td><td>${esc(c.definition || '—')}</td></tr>`).join('')}
            </tbody>
          </table>
        </div>
      </div>` : ''}
    `;

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

function analysisSection(label, text) {
  if (!text) return '';
  return `<div style="margin-bottom:16px">
    <div style="font-size:11px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;color:var(--muted);margin-bottom:5px">${label}</div>
    <div style="font-size:13.5px;line-height:1.75;white-space:pre-wrap">${esc(text)}</div>
  </div>`;
}

/* ==========================================================================
   对话
   ========================================================================== */
async function loadChat() {
  const box = $('#chatDetail');
  if (!S.activeConv) {
    box.innerHTML = `<div class="empty" style="padding-top:120px">
      <div class="ico">💬</div>
      <div class="title">没有选中的对话</div>
      <div class="desc">从左侧选择一个会话，或新建一个。</div>
    </div>`;
    return;
  }

  try {
    const d = await api(`/api/conversations/${encodeURIComponent(S.activeConv)}/messages`);
    const msgs = d.items || [];
    $('#tbTitle').textContent = d.conversation.title || '对话';

    box.innerHTML = `
      <div style="max-width:780px;margin:0 auto;padding:20px 24px 40px">
        <div class="page-head">
          <div class="page-title">${esc(d.conversation.title)}</div>
          <div class="page-sub">${msgs.length} 条消息 · 锚点 ${String(d.conversation.compact_anchor_seq || 0).padStart(5, '0')}</div>
        </div>
        ${msgs.length ? msgs.map(renderMessage).join('') : `
          <div class="empty"><div class="ico">✎</div>
            <div class="title">这个会话还没有消息</div>
            <div class="desc">对话界面将在接入问答链路后可用。当前可先用 CLI：<code>litprism ask "问题"</code></div>
          </div>`}
        <div class="card" style="margin-top:16px"><div class="card-body">
          <div class="row" style="gap:8px">
            <input class="input" id="chatInput" placeholder="基于知识库提问（待接入）" disabled>
            <button class="btn primary" disabled>发送</button>
          </div>
          <div style="margin-top:8px;font-size:11.5px;color:var(--muted)">
            每条回答都会做引用回溯校验；无法追溯的结论会被标注。
          </div>
        </div></div>
      </div>`;

    loadInspectorContext();
  } catch (e) {
    box.innerHTML = `<div class="empty"><div class="title">加载失败</div><div class="desc">${esc(e.message)}</div></div>`;
  }
}

function renderMessage(m) {
  const isUser = m.role === 'user';
  const isCheckpoint = m.kind === 'compact-checkpoint';
  return `
    <div style="margin-bottom:14px;display:flex;gap:10px;flex-direction:${isUser ? 'row-reverse' : 'row'}">
      <div style="flex:none;width:26px;height:26px;border-radius:7px;display:grid;place-items:center;
                  font-size:11px;font-weight:600;
                  background:${isUser ? 'var(--accent)' : 'var(--panel-3)'};
                  color:${isUser ? '#fff' : 'var(--text-2)'}">
        ${isUser ? '我' : 'AI'}
      </div>
      <div style="flex:1;min-width:0;max-width:88%">
        ${isCheckpoint ? `<div class="badge accent" style="margin-bottom:6px">压缩检查点</div>` : ''}
        <div style="background:${isUser ? 'var(--accent-soft)' : 'var(--panel)'};
                    border:1px solid ${isUser ? 'var(--accent-line)' : 'var(--border)'};
                    border-radius:var(--radius);padding:10px 13px;
                    font-size:13px;line-height:1.72;white-space:pre-wrap;
                    ${isCheckpoint ? 'font-family:var(--mono);font-size:11.5px;color:var(--text-2)' : ''}">
          ${esc(short(m.content, isCheckpoint ? 400 : 4000))}
        </div>
        <div style="margin-top:4px;font-size:10.5px;color:var(--muted);text-align:${isUser ? 'right' : 'left'}">
          ${esc(m.ref)} · ${esc((m.created_at || '').slice(0, 16).replace('T', ' '))}
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
      <button class="btn sm" data-deliv="${esc(x.id)}">查看</button>
    </div>`).join('');

  $$('[data-deliv]', box).forEach(b => b.onclick = () => showDeliverable(b.dataset.deliv));
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
    </div></div>`;
  } catch (e) {
    box.innerHTML = `<div class="card" style="margin-top:12px"><div class="card-body">
      <div class="badge danger">核验失败</div>
      <div style="margin-top:8px;font-size:12.5px">${esc(e.message)}</div></div></div>`;
  }
}

/* ==========================================================================
   设置
   ========================================================================== */
const SETTINGS_SECTIONS = [
  { key: 'general',  label: '通用' },
  { key: 'model',    label: '模型' },
  { key: 'sources',  label: '数据源' },
  { key: 'verify',   label: '核验与白名单' },
  { key: 'retrieval',label: '检索' },
  { key: 'export',   label: '导出' },
  { key: 'about',    label: '关于' },
];

async function loadSettings() {
  if (!S.status) S.status = await api('/api/status');
  const c = S.status.config || {};
  const nav = $('#settingsNav');
  const panes = $('#settingsPanes');

  nav.innerHTML = SETTINGS_SECTIONS.map((s, i) =>
    `<button class="${i === 0 ? 'active' : ''}" data-set="${s.key}">${s.label}</button>`).join('');

  panes.innerHTML = SETTINGS_SECTIONS.map((s, i) => `
    <div class="settings-pane ${i === 0 ? 'active' : ''}" data-setpane="${s.key}">
      <div class="card"><div class="card-body">${settingsBody(s.key, c)}</div></div>
    </div>`).join('');

  $$('[data-set]', nav).forEach(b => b.onclick = () => {
    $$('[data-set]', nav).forEach(x => x.classList.toggle('active', x === b));
    $$('[data-setpane]').forEach(p => p.classList.toggle('active', p.dataset.setpane === b.dataset.set));
  });
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
        row('语言', '界面语言（一期仅简体中文）。', `<input class="input" value="简体中文" disabled>`);

    case 'model':
      return row('提供商', '当前生效的大模型。API Key 加密存储，不写入配置文件。',
          `<div style="font-size:12.5px">${esc(llm.provider || '—')} / ${esc(llm.model || '—')}</div>
           <div style="margin-top:4px">${llm.mode === 'offline'
             ? '<span class="badge warn">离线占位</span>'
             : '<span class="badge ok">已连接</span>'}</div>`) +
        row('温度 / 最大输出', '按任务类型分别设置；解读类任务用低温度以保证稳定。',
          `<div class="mono" style="font-size:11.5px">temperature: ${c.qa?.temperature ?? '—'} · max_tokens: ${c.qa?.max_tokens ?? '—'}</div>`);

    case 'sources':
      return Object.entries(c.sources || {}).map(([k, v]) =>
        row(k, v?.enabled ? '已启用' : '未启用',
          `<span class="badge ${v?.enabled ? 'ok' : 'neutral'}">${v?.enabled ? '启用' : '关闭'}</span>`)
      ).join('') || '<div class="muted">无数据源配置</div>';

    case 'verify':
      return row('相似度阈值', '标题相似度达到该值才判定为疑似匹配。',
          `<div class="mono" style="font-size:12px">${c.verification?.threshold ?? '—'}</div>`) +
        row('人工确认白名单', '自动核验未通过但经人工确认放行的文献。徽章显示为「🔵人工确认」。',
          `<button class="btn sm" id="setGoWhitelist">查看（${S.whitelist.length} 条）</button>`) +
        row('判定等级', '五态：已核验 / 人工确认 / 疑似 / 未核到 / 无法判定。只有前两者可引用。',
          `<div class="row wrap" style="gap:4px">${badgeOf('verified')}${badgeOf('manual_confirmed')}${badgeOf('likely')}${badgeOf('not_found')}${badgeOf('unchecked')}</div>`);

    case 'retrieval':
      return row('分块大小 / 重叠', '影响检索粒度。块过大会稀释相似度，过小会切断上下文。',
          `<div class="mono" style="font-size:12px">${c.retrieval?.chunk_size ?? '—'} / ${c.retrieval?.chunk_overlap ?? '—'}</div>`) +
        row('召回数', '每次检索返回的片段数。受上下文预算约束，实际会动态调整。',
          `<div class="mono" style="font-size:12px">${c.retrieval?.top_k ?? '—'}</div>`) +
        row('上下文预算', '压缩在「新增量」上动手，不在「窗口位置」上动手。',
          `<div class="mono" style="font-size:11.5px">window ${c.context?.window_tokens ?? '—'} · step ${c.context?.step_tokens ?? '—'}</div>`);

    case 'export':
      return row('默认格式', '导出笔记与交付物的默认格式。',
          `<div class="mono" style="font-size:12px">${esc((c.export?.formats || []).join(', ') || 'markdown')}</div>`) +
        row('笔记目录', '导出文件的落盘位置。',
          `<div class="mono" style="font-size:11.5px">${esc(c.export?.notes_dir || '—')}</div>`);

    case 'about':
      return row('版本', '应用与数据库 schema 版本。',
          `<div class="mono" style="font-size:12px">v1.0.0</div>`) +
        row('知识库', '当前库内数据量。',
          `<div class="mono" style="font-size:11.5px">
            ${S.status?.stats?.papers ?? 0} 篇论文 · ${fmtNum(S.status?.stats?.chunks)} 块
          </div>`) +
        row('MCP 服务', '一期只做对外 Server，不接入外部 MCP 服务。默认只读。',
          `<div class="row" style="gap:6px">
            <span class="badge ${c.mcp?.readonly ? 'ok' : 'warn'}">${c.mcp?.readonly ? '只读' : '读写'}</span>
            <span class="badge neutral">${c.mcp?.enabled ? '随应用启动' : '未启用'}</span>
          </div>`) +
        row('系统提示词', '学术规范与防幻觉约束为框架内置，不在此处暴露 —— 防止无意改坏可信度地基。',
          `<span class="badge neutral">不可更改</span>`);
  }
  return '';
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
      <div class="metric"><span class="k">发送视图</span><span class="v">${fmtNum(occ.surface_tokens)}</span></div>
      <div style="font-size:11px;color:var(--muted);margin-top:6px;line-height:1.6">
        记账含已压缩原文（降得慢）；发送视图是实际发给模型的大小。
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
  // 主题
  $('#btnTheme').onclick = () => {
    S.theme = S.theme === 'dark' ? 'light' : 'dark';
    localStorage.setItem('litprism-theme', S.theme);
    applyTheme();
    if (S.activeView === 'settings') loadSettings();
  };

  // 右侧面板开关
  $('#btnInspector').onclick = () => {
    S.inspector = !S.inspector;
    localStorage.setItem('litprism-inspector', S.inspector ? '1' : '0');
    applyInspector();
  };

  // 侧边栏折叠
  $$('[data-toggle]').forEach(h => h.onclick = () => {
    h.classList.toggle('collapsed');
    const box = h.nextElementSibling;
    if (box) box.classList.toggle('hidden');
  });

  // 品牌 → 关于
  $('#brand').onclick = () => openView('settings');

  // 视图入口
  $$('[data-view]').forEach(b => b.onclick = () => openView(b.dataset.view));

  // 知识库搜索
  $('#libSearch').oninput = () => { if (S.activeView === 'library') loadLibrary(); };

  // Skill 搜索与刷新
  $('#skillSearch').oninput = () => { if (S.activeView === 'skills') loadSkills(); };
  $('#btnSkillRefresh').onclick = () => loadSkills().then(() => toast('已刷新'));

  // 核验
  $('#btnVerify').onclick = doVerify;
  $('#verifyInput').onkeydown = e => { if (e.key === 'Enter') doVerify(); };

  // 导出全部
  $('#btnExportAll').onclick = () => toast('批量导出将在后续版本提供');

  // 右侧面板 tab
  $$('[data-insp]').forEach(t => t.onclick = () => {
    $$('[data-insp]').forEach(x => x.classList.toggle('active', x === t));
    $$('[data-insp-pane]').forEach(p => p.classList.toggle('active', p.dataset.inspPane === t.dataset.insp));
    const loader = { context: loadInspectorContext, graph: loadInspectorGraph, deliv: loadInspectorDeliv }[t.dataset.insp];
    if (loader) loader().catch(e => toast(e.message, true));
  });

  // 快捷键
  document.addEventListener('keydown', e => {
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
  bindGlobal();

  try {
    S.status = await api('/api/status');
    $('#brandVersion').textContent = 'v' + (S.status.config?.app?.version || '1.0.0');
  } catch (e) {
    toast('无法连接后端：' + e.message, true);
  }

  await loadSidebar();
  openView('library');
  loadInspectorGraph().catch(() => {});
  loadInspectorDeliv().catch(() => {});
}

boot();
