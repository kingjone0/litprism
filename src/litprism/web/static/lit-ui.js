/* ==========================================================================
   lit-ui · 生成式 UI 渲染器
   输入是后端 `genui.process()` 产出的 segments（已注入框架字段）。
   组件白名单制：模型无法注入 HTML 或脚本；所有文本一律 esc()。
   ========================================================================== */

const LitUI = (() => {

  /* ---------------------------------------------------------------- 工具 */
  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  function badge(level, text) {
    const map = {
      verified: 'ok', manual_confirmed: 'manual', likely: 'warn',
      not_found: 'danger', unchecked: 'neutral',
    };
    return `<span class="badge ${map[level] || 'neutral'}">${esc(text || level || '未知')}</span>`;
  }

  /* ---------------------------------------------------------------- 极简 Markdown */
  // 只支持回复里实际会出现的语法，不引第三方库。
  function md(text) {
    let s = esc(text);

    // 代码块 ```lang\n...\n```
    s = s.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) =>
      `<pre style="background:var(--panel-2);border:1px solid var(--border);border-radius:6px;` +
      `padding:10px 12px;overflow-x:auto;font-size:12px;line-height:1.6;margin:10px 0">` +
      `<code>${code.replace(/\n$/, '')}</code></pre>`);

    // 行内代码
    s = s.replace(/`([^`\n]+)`/g,
      '<code style="background:var(--panel-2);border:1px solid var(--border);border-radius:4px;' +
      'padding:1px 5px;font-size:12px;font-family:var(--mono)">$1</code>');

    // 标题
    s = s.replace(/^### (.+)$/gm, '<div style="font-size:13px;font-weight:650;margin:14px 0 6px">$1</div>');
    s = s.replace(/^## (.+)$/gm, '<div style="font-size:14.5px;font-weight:650;margin:16px 0 7px">$1</div>');
    s = s.replace(/^# (.+)$/gm, '<div style="font-size:16px;font-weight:650;margin:18px 0 8px">$1</div>');

    // 粗体 / 斜体
    s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');

    // 引用
    s = s.replace(/^&gt; (.+)$/gm,
      '<div style="border-left:2px solid var(--accent-line);padding:2px 0 2px 10px;' +
      'color:var(--text-2);margin:6px 0">$1</div>');

    // 列表
    s = s.replace(/^[-*] (.+)$/gm,
      '<div style="display:flex;gap:7px;margin:3px 0"><span style="color:var(--muted)">·</span>' +
      '<span style="flex:1">$1</span></div>');
    s = s.replace(/^(\d+)\. (.+)$/gm,
      '<div style="display:flex;gap:7px;margin:3px 0"><span style="color:var(--muted);' +
      'font-family:var(--mono);font-size:11.5px;min-width:16px">$1.</span>' +
      '<span style="flex:1">$2</span></div>');

    // 段落：连续非空行包成 p
    const blocks = s.split(/\n{2,}/).map(b => {
      const t = b.trim();
      if (!t) return '';
      if (/^<(div|pre|table|ul|ol)/.test(t)) return t;
      return `<p style="margin:0 0 9px">${t.replace(/\n/g, '<br>')}</p>`;
    });
    return blocks.filter(Boolean).join('');
  }

  /* ================================================================
     组件渲染
     ================================================================ */
  const RENDERERS = {

    /* ---------------- 论文卡片 ---------------- */
    'paper-card'(c) {
      if (c._missing) return `<div class="lui"><div class="lui-body"><div class="lui-missing">
        未在本地知识库中找到该论文（${esc(c.paper_id)}）。请先用检索或导入确认它存在。</div></div></div>`;
      const authors = (c.authors || []).slice(0, 4).join('、') +
        ((c.authors || []).length > 4 ? ' 等' : '');
      return `<div class="lui">
        <div class="lui-head"><span class="lui-kind">论文</span><span>本地知识库</span></div>
        <div class="lui-pc">
          <div class="lui-pc-top">
            <div class="lui-pc-title">${esc(c.title)}</div>
            ${badge(c.verify_level, c.verify_badge)}
          </div>
          <div class="lui-pc-meta">
            <span>${esc(authors || '作者未知')}</span>
            <span>${c.year || '年份未知'}</span>
            ${c.venue ? `<span>${esc(c.venue)}</span>` : ''}
          </div>
          <div class="lui-pc-foot">
            ${c.doi ? `<span class="mono">DOI: ${esc(c.doi)}</span>` : '<span>无 DOI</span>'}
            <span style="flex:1"></span>
            <span>${c.citable ? '可引用' : '不可引用'}</span>
          </div>
        </div>
      </div>`;
    },

    /* ---------------- 论文列表 ---------------- */
    'paper-list'(c) {
      const items = c.items || [];
      if (!items.length) return '';
      return `<div class="lui">
        <div class="lui-head"><span class="lui-kind">论文列表</span><span>${items.length} 篇</span></div>
        <div class="lui-plist">
          ${items.map(p => p._missing
            ? `<div class="lui-plist-item"><div class="lui-missing">未找到 ${esc(p.id)}</div></div>`
            : `<div class="lui-plist-item">
                <div class="t">${esc(p.title)}</div>
                <div class="m">
                  <span>${esc((p.authors || []).slice(0, 3).join('、') || '作者未知')}</span>
                  <span>${p.year || '—'}</span>
                  ${badge(p.verify_level, p.verify_badge)}
                </div>
              </div>`).join('')}
        </div>
      </div>`;
    },

    /* ---------------- 结构化解读 ---------------- */
    'analysis'(c) {
      const LABELS = {
        research_question: '研究问题', methodology: '方法', experiment_design: '实验设计',
        key_conclusions: '主要结论', innovations: '创新点', limitations: '局限',
      };
      return `<div class="lui">
        <div class="lui-head">
          <span class="lui-kind">结构化解读</span>
          <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(c.title || '')}</span>
          ${badge(c.verify_level, c.verify_badge)}
        </div>
        <div class="lui-body lui-analysis">
          ${c._mock ? '<div class="lui-degraded">离线占位解读，仅用于演示。</div>' : ''}
          ${c._model ? `<div class="muted">模型：${esc(c._model)}</div>` : ''}
          ${(c.fields || []).map(f => `
            <div class="lui-field">
              <div class="lui-field-label">${esc(LABELS[f.key] || f.key)}</div>
              <div class="lui-field-text">${esc(f.text || '（论文未提及）')}</div>
            </div>`).join('') || '<div class="muted">无字段</div>'}
        </div>
      </div>`;
    },

    /* ---------------- 对比矩阵 ---------------- */
    'comparison-table'(c) {
      const papers = c.papers || [];
      const dims = c.dimensions || [];
      if (!papers.length || !dims.length) return '';
      return `<div class="lui">
        <div class="lui-head"><span class="lui-kind">对比</span><span>${papers.length} 篇 · ${dims.length} 个维度</span></div>
        <div class="lui-cmp">
          <table>
            <thead><tr>
              <th></th>
              ${papers.map(p => `<th>${esc(p.title || p.id)}<span class="y">${p.year || ''}</span></th>`).join('')}
            </tr></thead>
            <tbody>
              ${dims.map(d => `<tr>
                <td class="dim">${esc(d.label || d.key)}</td>
                ${papers.map((_, i) => `<td>${esc((d.values || [])[i] || '未提及')}</td>`).join('')}
              </tr>`).join('')}
            </tbody>
          </table>
        </div>
      </div>`;
    },

    /* ---------------- 术语表 ---------------- */
    'concept-list'(c) {
      const items = c.items || [];
      if (!items.length) return '';
      return `<div class="lui">
        <div class="lui-head"><span class="lui-kind">术语</span><span>${items.length} 条</span></div>
        <div class="lui-concepts"><table>
          ${items.map(i => `<tr>
            <td class="term">${esc(i.term)}</td>
            <td>${esc(i.definition || '（论文未给出定义）')}</td>
          </tr>`).join('')}
        </table></div>
      </div>`;
    },

    /* ---------------- 引用片段 ---------------- */
    'citation-list'(c) {
      const items = c.items || [];
      if (!items.length) return '';
      return `<div class="lui">
        <div class="lui-head">
          <span class="lui-kind">引用片段</span>
          <span>${items.length} 段</span>
          <span style="flex:1"></span>
          <span title="页码与原文由框架从数据库注入，模型不参与">框架注入</span>
        </div>
        <div class="lui-cites">
          ${items.map((i, idx) => i._missing
            ? `<div class="lui-cite"><div class="lui-missing">片段 ${esc(i.chunk_id)} 已失效</div></div>`
            : `<div class="lui-cite">
                <div class="lui-cite-head" data-cite="${idx}">
                  <span class="pg">${i.page == null ? '无页码' : 'p.'+esc(i.page)}</span>
                  ${i.section ? `<span class="sec">${esc(i.section)}</span>` : ''}
                  <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                    ${esc(i.paper_title || i.paper_id || '')}
                  </span>
                  <span class="toggle">▾</span>
                </div>
                ${i.citable === false ? '<div class="lui-degraded">当前来源不可作为事实证据，请在来源详情核对状态。</div>' : ''}
                <div class="lui-cite-text" data-cite-text="${idx}">${esc(i.text || '')}</div>
              </div>`).join('')}
        </div>
      </div>`;
    },

    /* ---------------- 核验结果 ---------------- */
    'verify-result'(c) {
      const matches = c.matches || [];
      return `<div class="lui">
        <div class="lui-head"><span class="lui-kind">核验</span><span>${esc(c.title || '')}</span></div>
        <div class="lui-body lui-verify">
          <div class="lui-v-top">
            ${badge(c.level, c.badge)}
            <span class="mono muted" style="font-size:11px">置信度 ${(c.confidence ?? 0).toFixed(3)}</span>
            ${c.citable === false ? '<span class="badge danger">不可引用</span>' : ''}
          </div>
          <div class="lui-v-msg">${esc(c.message || '')}</div>
          ${matches.length ? `
            <div class="lui-v-metrics">
              <div style="font-size:10.5px;font-weight:650;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin-bottom:4px">候选匹配</div>
              ${matches.slice(0, 5).map(m => `<div class="lui-v-match">
                <span class="sim">${(m.similarity ?? 0).toFixed(3)}</span>
                <span class="t">${esc(m.title || '')}</span>
                <span class="muted" style="font-size:11px">${esc(m.source || '')}</span>
              </div>`).join('')}
            </div>` : ''}
        </div>
      </div>`;
    },

    /* ---------------- 提示块 ---------------- */
    'callout'(c) {
      const META = {
        fact:      ['📌', '原文事实'],
        claim:     ['💬', '作者主张'],
        inference: ['🔍', '模型推断（非原文结论）'],
        warn:      ['⚠️', '注意'],
        note:      ['ℹ️', '说明'],
      };
      const [ico, tag] = META[c.kind] || META.note;
      return `<div class="lui-callout ${esc(c.kind || 'note')}">
        <span class="lui-co-ico">${ico}</span>
        <div class="lui-co-body"><span class="lui-co-tag">${tag}</span>${esc(c.text || '')}</div>
      </div>`;
    },
  };

  /* ================================================================
     对外接口
     ================================================================ */
  function renderComponent(c) {
    const fn = RENDERERS[c && c.type];
    if (!fn) {
      return `<div class="lui-degraded">未知组件「${esc(c && c.type)}」，已跳过渲染。</div>`;
    }
    try {
      return fn(c);
    } catch (e) {
      return `<div class="lui-degraded">组件「${esc(c.type)}」渲染失败：${esc(e.message)}</div>`;
    }
  }

  function renderSegments(segments) {
    if (!Array.isArray(segments)) return '';
    return segments.map(s => {
      if (s.kind === 'component') return renderComponent(s.component);
      if (s.kind === 'degraded') return `<div class="lui-degraded">${esc(s.text)}</div>`;
      return md(s.text || '');
    }).join('');
  }

  /* 客户端解析：仅用于预览 / 流式场景；权威结果来自后端 genui.process() */
  function parseFences(text) {
    const re = /```lit-ui[ \t]*\r?\n([\s\S]*?)```/g;
    const out = [];
    let cursor = 0, m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > cursor) out.push({ kind: 'text', text: text.slice(cursor, m.index).trim() });
      try {
        out.push({ kind: 'component', component: JSON.parse(m[1].trim()) });
      } catch (e) {
        out.push({ kind: 'degraded', text: '结构化内容解析失败：' + e.message });
      }
      cursor = m.index + m[0].length;
    }
    if (cursor < text.length) out.push({ kind: 'text', text: text.slice(cursor).trim() });
    return out.filter(s => (s.kind === 'text' ? s.text : true));
  }

  /* 给引用片段绑定展开/折叠 */
  function bind(root) {
    (root || document).querySelectorAll('[data-cite]').forEach(h => {
      h.onclick = () => {
        const t = h.parentElement.querySelector('[data-cite-text]');
        const toggle = h.querySelector('.toggle');
        if (!t) return;
        const hidden = t.classList.toggle('collapsed');
        if (toggle) toggle.textContent = hidden ? '▸' : '▾';
      };
    });
    // 默认折叠引用原文，避免一次铺开太长
    (root || document).querySelectorAll('[data-cite-text]').forEach(t => {
      t.classList.add('collapsed');
    });
  }

  return { renderComponent, renderSegments, parseFences, md, esc, bind };
})();
