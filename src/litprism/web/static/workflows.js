/* 研究任务交互。模型调用与引用校验均由后端执行。 */
S.selectedPapers = new Set();
S.collections = [];

async function waitTask(id) {
  const card = document.createElement('div');
  card.className = 'task-progress';
  card.innerHTML = '<span role="status">等待任务…</span><button class="btn sm">取消后续处理</button>';
  const dialog = $('#researchTask[open]');
  if (dialog) card.classList.add('task-progress-inline');
  (dialog ? $('.task-body', dialog) : document.body).appendChild(card);
  const rehome = () => { card.classList.remove('task-progress-inline'); document.body.appendChild(card); };
  dialog?.addEventListener('close', rehome, { once: true });
  const label = $('span', card), cancel = $('button', card);
  cancel.onclick = async () => {
    cancel.disabled = true;
    try { await api('/api/tasks/'+id+'/cancel', { method: 'POST' }); label.textContent = '正在取消；等待当前请求返回。已完成导入不会撤销。'; }
    catch (e) { label.textContent = e.message; cancel.disabled = false; }
  };
  try {
    for (;;) {
      if (!card.isConnected) rehome();
      const task = await api('/api/tasks/'+id);
      if (task.status === 'succeeded') return task.result;
      if (['failed', 'cancelled'].includes(task.status)) {
        const error = new Error(task.error || '任务已取消，可重新发起。');
        error.cancelled = task.status === 'cancelled';
        throw error;
      }
      label.textContent = task.status === 'cancelling' ? '等待当前请求结束后取消；不撤销已完成导入。' : task.progress || '正在处理…';
      await new Promise(resolve => setTimeout(resolve, 300));
    }
  } finally { dialog?.removeEventListener('close', rehome); card.remove(); }
}

async function runTask(operation, args) {
  const task = await api('/api/tasks', { method: 'POST', body: { operation, args } });
  return waitTask(task.id);
}

function workflowDialog(title, body, submit, action = '确定') {
  $('#researchTask')?.remove();
  const dialog = document.createElement('dialog');
  dialog.id = 'researchTask';
  dialog.className = 'task-dialog';
  dialog.innerHTML = `<form><header><h2>${esc(title)}</h2><button type="button" data-cancel aria-label="关闭">${icon('close')}</button></header>
    <div class="task-body">${body}</div><p class="task-error" role="alert"></p>
    <footer><button type="button" class="btn" data-cancel>取消</button>${submit ? `<button class="btn primary" type="submit">${esc(action)}</button>` : ''}</footer></form>`;
  document.body.appendChild(dialog);
  $$('[data-cancel]', dialog).forEach(b => b.onclick = () => dialog.close());
  dialog.addEventListener('close', () => dialog.remove());
  $('form', dialog).onsubmit = async e => {
    e.preventDefault();
    if (!submit) return;
    const button = $('[type=submit]', dialog);
    if (button.disabled) return;
    button.disabled = true;
    try {
      const data = new FormData(e.target);
      const values = Object.fromEntries(data);
      $$('select[multiple][name]', dialog).forEach(s => { values[s.name] = data.getAll(s.name); });
      await submit(values, dialog); dialog.close();
    }
    catch (error) { $('.task-error', dialog).textContent = error.message; }
    finally { button.disabled = false; }
  };
  dialog.showModal();
  return dialog;
}

const analysisStages = { extract: '原文提取', concepts: '术语提取', semantic_review: '逐项复核', semantic_cross_review: '跨批核对' };
const analysisRunStatuses = { queued: '等待执行', running: '正在执行或上次中断', paused_budget: '预算暂停', needs_retry: '需确认重试', cancelled: '已取消', partial: '仍有待审项', completed: '机器复核通过', failed: '运行失败', stale: '版本已变化' };

async function loadAnalysisRuns(pid) {
  const box = $('#analysisRuns');
  if (!box) return;
  try {
    const { items } = await api('/api/paper/'+encodeURIComponent(pid)+'/analysis-runs');
    if (S.activePaper !== pid || $('#analysisRuns') !== box) return;
    box.innerHTML = `<details ${items.length ? 'open' : ''}><summary>分阶段运行记录（${items.length}${items.length === 30 ? '，最近' : ''}）</summary>
      <p class="source-intro">暂停时保留原解读。刷新或重新打开应用不会自动恢复调用。机器复核通过仍不等于人工确认。</p>
      <button class="btn sm" data-refresh-runs>刷新记录</button>
      ${items.map(run => `<div class="analysis-run" data-run-id="${esc(run.id)}">
        <div class="row wrap"><strong>${esc(analysisRunStatuses[run.status] || run.status)}</strong><span class="source-intro">${esc(run.model)} · ${esc(run.updated_at)}</span>
        ${!['completed', 'stale'].includes(run.status) ? `<button class="btn sm" data-resume-run="${esc(run.id)}">检查并恢复</button>` : ''}</div>
        <p class="source-intro">${Object.entries(analysisStages).filter(([key]) => key !== 'concepts' || run.with_concepts).map(([key, label]) => `${label} ${run.spent[key]}/${run.budgets[key]}`).join(' · ')}（已用/上限）</p>
        ${(run.steps || []).some(s => s.request_settings?.max_tokens) ? `<details><summary>各次请求的实际输出额度</summary>${run.steps.filter(s => s.request_settings?.max_tokens).map(s => `<p class="source-intro">${esc(analysisStages[s.task] || s.task)} · 第 ${s.step} 步 / 尝试 ${s.attempt} · ${s.request_settings.output_mode === 'automatic' ? '自动分配' : '历史固定额度'} ${fmtNum(s.request_settings.max_tokens)} token${s.request_settings.estimated_input_tokens == null ? '' : ` · 输入估算 ${fmtNum(s.request_settings.estimated_input_tokens)}`}</p>`).join('')}</details>` : ''}
        <p class="source-intro">已报告用量：输入 ${run.usage.input} / 输出 ${run.usage.output} token；${run.usage.unknown_usage_calls} 次用量未知。${esc(run.stop_reason || '')}</p>
      </div>`).join('') || '<p class="source-intro">尚无分阶段记录。可用上方“分阶段解读”设置预算。</p>'}</details>`;
    $('[data-refresh-runs]', box).onclick = () => loadAnalysisRuns(pid);
    $$('[data-resume-run]', box).forEach(button => button.onclick = async () => {
      button.disabled = true;
      try {
        const run = await api('/api/analysis-runs/'+encodeURIComponent(button.dataset.resumeRun));
        analysisRunDialog(pid, run);
      } catch (error) { toast(error.message, true); }
      finally { button.disabled = false; }
    });
  } catch (error) {
    if (S.activePaper === pid && $('#analysisRuns') === box) {
      box.textContent = '运行记录加载失败：'+error.message+'。';
      const retry = document.createElement('button'); retry.className = 'btn sm'; retry.textContent = '重试加载';
      retry.onclick = () => loadAnalysisRuns(pid); box.appendChild(retry);
    }
  }
}

function analysisRunDialog(pid, run = null) {
  const limits = run?.budgets || { extract: 12, concepts: 12, semantic_review: 48, semantic_cross_review: 40 };
  workflowDialog(run ? '检查并恢复解读' : '设置分阶段预算', `<p class="source-intro">上限按本次运行累计请求次数计算，包含失败与结果未知的尝试。精确匹配的成功请求直接复用，不再发送。预算耗尽后暂停并保留原解读。</p>
    ${Object.entries(analysisStages).map(([key, label]) => `<label>${label}请求上限${run ? `（已用 ${run.spent[key]}）` : ''}<input class="input" name="${key}" type="number" min="${run ? limits[key] : 0}" max="1000" step="1" required value="${limits[key]}"></label>`).join('')}
    <p class="source-intro">逐项复核和跨批核对还受原有安全上限约束（每轮最多 48 / 40 次）；增加预算不保证整篇通过。次数上限不是金额承诺，服务未报告的用量保持未知。</p>
    ${run ? `<p class="source-intro">术语提取：${run.with_concepts ? '启用' : '关闭'}；恢复前核对原文、模型、协议、参数和分析版本。${esc(run.stop_reason || '')}</p>` : '<label><input type="checkbox" name="with_concepts" checked> 同时提取术语</label>'}
    ${run?.retry_required ? '<label><input type="checkbox" name="retry_failed"> 允许重试失败或结果未知的请求</label><p class="source-intro">这些请求可能已产生费用；重试及后续重新生成都会占用累计预算。未勾选时不会自动重试。</p>' : ''}`, async values => {
      const body = { budgets: Object.fromEntries(Object.keys(analysisStages).map(key => [key, Number(values[key])])) };
      if (run) body.retry_failed = values.retry_failed === 'on';
      else body.with_concepts = values.with_concepts === 'on';
      const endpoint = run ? '/api/analysis-runs/'+encodeURIComponent(run.id)+'/resume' : '/api/paper/'+encodeURIComponent(pid)+'/analysis-runs';
      const started = await api(endpoint, { method: 'POST', body });
      try {
        const result = await waitTask(started.task_id);
        toast(analysisRunStatuses[result.status] || result.status, ['failed', 'stale', 'needs_retry'].includes(result.status));
      } finally {
        if (S.activePaper === pid) await loadPaperDetail();
      }
    }, run ? '按预算恢复' : '按预算开始');
}

async function downloadApi(path, filename, body) {
  await ENV_READY;
  const env = apiEnv();
  const headers = env.token ? { 'X-LitPrism-Token': env.token } : {};
  if (body) headers['Content-Type'] = 'application/json';
  const response = await fetch((env.base || '') + path, { method: body ? 'POST' : 'GET', headers, body: body ? JSON.stringify(body) : undefined });
  if (!response.ok) { const data = await response.json(); throw new Error(data.detail || data.error || '导出失败'); }
  const url = URL.createObjectURL(await response.blob());
  const a = document.createElement('a');
  a.href = url; a.download = filename; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function refreshLibrary() {
  S.status = await api('/api/status');
  await loadSidebar();
  S.selectedPapers = new Set([...S.selectedPapers].filter(id => S.papers.some(p => p.id === id)));
  $('#btnCompareSelected').disabled = S.selectedPapers.size < 2;
  await loadLibrary();
}

async function refreshCollections() {
  const d = await api('/api/collections');
  S.collections = d.items || [];
  const filter = $('#collectionFilter');
  const value = filter.value;
  filter.innerHTML = '<option value="">全部分组</option>' + S.collections.map(c => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('');
  filter.value = S.collections.some(c => c.id === value) ? value : '';
  $('#collectionChips').innerHTML = S.collections.map(c => `<button class="btn sm" draggable="true" data-group-chip="${esc(c.id)}">${esc(collectionLabel(c.id))}</button>`).join('');
  $$('[data-group-chip]').forEach(chip => {
    chip.onclick = () => { filter.value = chip.dataset.groupChip; loadLibrary(); };
    chip.ondragstart = e => e.dataTransfer.setData('application/litprism-group', chip.dataset.groupChip);
    chip.ondragover = e => e.preventDefault();
    chip.ondrop = async e => {
      e.preventDefault(); e.stopPropagation();
      const pid = e.dataTransfer.getData('application/litprism-paper');
      const group = e.dataTransfer.getData('application/litprism-group');
      try {
        if (pid) await api('/api/paper/'+pid, { method: 'PATCH', body: { collection_id: chip.dataset.groupChip } });
        if (group && group !== chip.dataset.groupChip) {
          const ids = S.collections.map(c => c.id).filter(id => id !== group);
          ids.splice(ids.indexOf(chip.dataset.groupChip), 0, group);
          for (const [sort_order, id] of ids.entries()) await api('/api/collections/'+id, { method: 'PATCH', body: { sort_order } });
        }
        await refreshCollections(); await refreshLibrary();
      } catch (err) { toast(err.message, true); }
    };
  });
}

function collectionLabel(id, visited = new Set()) {
  const c = S.collections.find(v => v.id === id);
  if (!c || visited.has(id)) return '';
  visited.add(id);
  return (c.parent_id ? collectionLabel(c.parent_id, visited)+' / ' : '')+c.name;
}

function collectionDescendants(id) {
  const found = new Set([id]);
  for (let i = 0; i < S.collections.length; i++) S.collections.forEach(c => { if (found.has(c.parent_id)) found.add(c.id); });
  return [...found];
}

function editConversation(c) {
  workflowDialog('管理会话', `<label>名称<input class="input" name="title" value="${esc(c.title)}" required></label><label><input type="checkbox" name="pinned" ${c.pinned ? 'checked' : ''}>置顶</label>`, async v => {
    await api('/api/conversations/'+c.id, { method: 'PATCH', body: { title: v.title, pinned: v.pinned === 'on' } });
    await loadSidebar();
    if (S.activeConv === c.id) $('#tbTitle').textContent = v.title;
  });
}

async function editDeliverable(id) {
  try {
    const d = await api('/api/deliverables/'+id);
    workflowDialog('编辑研究产出', `<label>标题<input class="input" name="title" value="${esc(d.title)}" required></label><label>正文<textarea class="textarea" name="content" rows="12" required>${esc(d.content)}</textarea></label><p>保存时重新检查原文来源与正文引用。</p>`, async values => {
      await api('/api/deliverables/'+id, { method: 'PUT', body: values }); await loadDeliverables();
    });
  } catch (e) { toast(e.message, true); }
}

function showConversationDialog() {
  const options = S.papers.map(p => `<option value="p:${esc(p.id)}">${esc(short(p.display_title || p.title, 42))}</option>`).join('') +
    S.collections.map(c => `<option value="g:${esc(c.id)}">分组：${esc(c.name)}</option>`).join('');
  workflowDialog('新建研究对话', `<label>对话名称<input class="input" name="title" value="新研究对话" required></label>
    <label>文献范围<select class="select" name="scope"><option value="">全部可引用文献与已审核知识</option>${S.selectedPapers.size ? '<option value="selected">知识库中勾选的文献</option>' : ''}${options}</select></label>`, async values => {
    let scope = {};
    if (values.scope === 'selected') scope = { paper_ids: [...S.selectedPapers] };
    else if (values.scope.startsWith('p:')) scope = { paper_ids: [values.scope.slice(2)] };
    else if (values.scope.startsWith('g:')) scope = { collection_id: values.scope.slice(2) };
    const r = await api('/api/conversations', { method: 'POST', body: { title: values.title, scope } });
    await loadSidebar();
    openView('chat', { convId: r.id });
  }, '开始研究');
}

async function importFiles(files) {
  if (!files.length) return;
  if ($('#btnImport').disabled) return toast('正在导入，请等待本批完成');
  const form = new FormData();
  for (const file of files) form.append('files', file);
  const button = $('#btnImport');
  button.disabled = true;
  $('#importResults').textContent = '正在解析并核验文献，请稍候…';
  try {
    await ENV_READY;
    const env = apiEnv();
    const res = await fetch((env.base || '') + '/api/tasks/upload', {
      method: 'POST', headers: env.token ? { 'X-LitPrism-Token': env.token } : {}, body: form,
    });
    const started = await res.json();
    if (!res.ok) throw new Error(started.detail || started.error || '导入失败');
    const data = await waitTask(started.id);
    $('#importResults').innerHTML = (data.results || []).map(r => `<div class="${r.ok ? 'insp-note' : 'lui-degraded'}">
      <strong>${esc(r.file)}</strong>：${esc(r.ok ? '核验通过，已导入；打开文献可生成解读。' : r.error)}
      ${!r.ok && r.title ? `<button class="btn sm" data-import-verify="${esc(r.title)}">查看核验 / 人工确认</button>` : ''}</div>`).join('');
    $$('[data-import-verify]').forEach(b => b.onclick = () => { openView('verify'); $('#verifyInput').value = b.dataset.importVerify; doVerify(); });
    await refreshLibrary();
  } catch (error) { $('#importResults').textContent = error.message; }
  finally { button.disabled = false; $('#importFiles').value = ''; }
}

async function loadComparison() {
  const box = $('#comparisonResult');
  const ids = [...S.selectedPapers];
  if (ids.length < 2) { box.innerHTML = '<div class="empty">请先在知识库勾选至少两篇文献，然后点击「对比所选」。</div>'; return; }
  box.innerHTML = '<div class="insp-note">正在读取原文并对比，完成后检查引用…</div>';
  const key = [...ids].sort().join(',');
  if (S.compareKey !== key) {
    try { S.comparison = await runTask('compare', { refs: ids }); }
    catch (error) { box.textContent = error.message; return; }
    S.compareKey = key;
  }
  const r = S.comparison;
  S.compareSources = r.sources || [];
  refreshInspector();
  const check = r.data._citation_check || {};
  box.innerHTML = `<div class="source-intro">模型：${esc(r.data._model || '未知')}</div>
    ${check.passed === false ? `<div class="lui-degraded">${esc((check.issues || []).join('；'))}</div>` : '<button class="btn" id="btnSaveComparison">保存为对比报告</button>'}
    <div class="reading-report">${LitUI.md(r.data.summary || '')}
      ${coverageNote(r.data._coverage)}
      ${semanticReviewPanel(r.data._semantic_review, {kind: 'run', id: r.data._semantic_run_id})}
      ${(r.data.dimensions || []).length ? `<div style="overflow-x:auto"><table class="table"><thead><tr><th>维度</th>${(r.data._papers || []).map(p => `<th>${esc(p.title)}</th>`).join('')}</tr></thead>
        <tbody>${r.data.dimensions.map(d => `<tr><td>${esc(d.dimension)}</td>${(r.data._papers || []).map(p => `<td>${LitUI.md(String((d.values || {})[p.tag] || '当前提取未找到依据'))}</td>`).join('')}</tr>`).join('')}</tbody></table></div>` : ''}
      ${['convergence', 'divergence'].map(k => (r.data[k] || []).length ? `<h3>${k === 'convergence' ? '一致点' : '分歧点'}</h3>${r.data[k].map(v => `<p>${LitUI.md(v)}</p>`).join('')}` : '').join('')}
      ${(r.data.evidence_map || []).length ? `<h3>结论与证据映射</h3>${r.data.evidence_map.map(e => `<p>${esc(e.claim)} — ${esc((e.sources || []).join('、'))}</p>`).join('')}` : ''}</div>`;
  $('#btnSaveComparison')?.addEventListener('click', async () => {
    try {
      await api('/api/deliverables', { method: 'POST', body: { type: 'comparison', title: '论文对比报告', content: r.markdown,
        citations: r.data._citations, model: r.data._model, source_scope: { citation_check: check, semantic_run_id: r.data._semantic_run_id } } });
      toast('已保存到研究产出');
    } catch (error) { toast(error.message, true); }
  });
}

async function recommendPapers(event) {
  event.preventDefault();
  const button = $('button', event.target);
  if (button.disabled) return;
  button.disabled = true;
  const box = $('#recommendResults');
  box.textContent = '正在检索学术数据库…';
  try {
    const result = await api('/api/recommend', { method: 'POST', body: { topic: $('#recommendTopic').value, import_to_kb: false } });
    const data = result.data;
    box.innerHTML = `<div class="insp-note">${esc(data.disclaimer || '')} 元数据导入不等于已经获得全文。</div>` +
      (data.items || []).map((p, i) => `<div class="card"><div class="card-body"><strong>${esc(p.title)}</strong><p>${esc(p.reason || p.abstract || '')}</p>
        ${badgeOf(p.verification?.level)} <button class="btn sm" data-recommend-import="${i}" ${p._paper_id ? 'disabled' : ''}>${p._paper_id ? '已在知识库' : '核验并导入题录'}</button></div></div>`).join('') +
      (!(data.items || []).length ? '<div class="empty">没有找到候选，请调整关键词或检查数据源设置。</div>' : '');
    $$('[data-recommend-import]', box).forEach(b => b.onclick = async () => {
      b.disabled = true;
      try {
        const p = data.items[Number(b.dataset.recommendImport)];
        await api('/api/import', { method: 'POST', body: { title: p.title, doi: p.doi } });
        b.textContent = '已导入题录'; await loadSidebar();
      } catch (error) { b.disabled = false; toast(error.message, true); }
    });
  } catch (error) { box.textContent = error.message; }
  finally { button.disabled = false; }
}

function showSkillRun(skill) {
  workflowDialog('运行：' + skill.name, (skill.inputs || []).map(i => `<label>${esc(i.label || i.key)}
    ${i.type === 'papers' ? `<select class="select" multiple name="${esc(i.key)}" ${i.required ? 'required' : ''}>${S.papers.map(p => `<option value="${esc(p.id)}">${esc(p.display_title || p.title)}</option>`).join('')}</select>` :
      i.type === 'collection' ? `<select class="select" name="${esc(i.key)}" ${i.required ? 'required' : ''}><option value="">请选择分组</option>${S.collections.map(c => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('')}</select>` :
      `<input class="input" name="${esc(i.key)}" ${i.required ? 'required' : ''} placeholder="${esc(i.key)}">`}</label>`).join('') +
    '<p class="source-intro">只执行声明式预取和提示词；没有原文依据时拒绝生成。</p>', async values => {
      const r = await runTask('skill', { skill_id: skill.id, request: { inputs: values } });
      if (!r.ok) throw new Error(r.error || (r.citation_check?.issues || []).join('；') || '引用检查失败');
      $('#skillRunResult')?.remove();
      $('#skillCards').insertAdjacentHTML('afterbegin', `<div class="card" id="skillRunResult"><div class="card-body">${LitUI.md(r.answer || '')}${r.deliverable_id ? '<p class="insp-note">已保存到研究产出。</p>' : ''}</div></div>`);
    }, '运行并检查引用');
}

function coverageNote(c) {
  return c ? `<div class="insp-note">原文覆盖：${esc(c.read_chunks)} / ${esc(c.total_chunks)} 个分块，${esc(c.batches)} 批；${esc(c.note || '')}</div>` : '';
}

/**
 * 证据定位：在原文里找出最可能支撑这条陈述的那一句并高亮。
 *
 * 为什么放在前端做：这只是**展示辅助**，不参与任何判定 —— 机器给出的
 * 支持/不符结论不变，这里只解决"人点进来看哪一句"的问题。
 * 算法故意保守：分数不够高就不标（宁可不高亮，也不误导性地标错）。
 */
function evGrams(text) {
  const out = new Set();
  const clean = String(text || '').replace(/\[[^\]]*\]/g, ' ').toLowerCase();
  (clean.match(/[a-z0-9][a-z0-9.+-]{1,}/g) || []).forEach(w => out.add(w));
  const cjk = clean.replace(/[^\u4e00-\u9fff]/g, '');
  for (let i = 0; i < cjk.length - 1; i++) out.add(cjk.slice(i, i + 2));
  return out;
}

function evSentences(text) {
  const s = String(text || ''), parts = [];
  let start = 0;
  for (let i = 0; i < s.length; i++) {
    if ('。！？!?\n'.includes(s[i])) { if (i + 1 > start) parts.push([start, i + 1]); start = i + 1; }
  }
  if (start < s.length) parts.push([start, s.length]);
  return parts.filter(([a, b]) => s.slice(a, b).trim().length > 1);
}

function evHit(text, claim) {
  const s = String(text || ''), claimGrams = evGrams(claim);
  if (!claimGrams.size) return null;
  let best = null, bestScore = 0;
  for (const [a, b] of evSentences(s)) {
    const g = evGrams(s.slice(a, b));
    if (!g.size) continue;
    let inter = 0;
    g.forEach(x => { if (claimGrams.has(x)) inter += 1; });
    const score = inter / Math.sqrt(g.size * claimGrams.size);
    if (score > bestScore) { bestScore = score; best = [a, b]; }
  }
  return bestScore >= 0.12 ? best : null;
}

function renderEvidenceText(text, claim) {
  const s = String(text || '');
  const hit = evHit(s, claim);
  if (!hit) return esc(s);
  return esc(s.slice(0, hit[0])) + '<mark class="ev-hit">' + esc(s.slice(hit[0], hit[1])) + '</mark>' + esc(s.slice(hit[1]));
}

/**
 * 字段名的中文标签。与 lit-ui 的解读字段保持一致。
 */
const SEM_FIELD_LABELS = {
  research_question: '研究问题', methodology: '方法', experiment_design: '实验设计',
  key_conclusions: '主要结论', innovations: '创新点', limitations: '局限',
};

function semFieldLabel(key) {
  return SEM_FIELD_LABELS[key] || key || '正文';
}

/**
 * 覆盖率详情：**必须让人一眼看出是哪些字段没核完**。
 *
 * 只写一句「未覆盖全部陈述」是不够的 —— 用户无法判断自己看到的内容
 * 在哪些维度上还靠不住。这里把已核完 / 未核完的字段名直接列出来，
 * 并明确「未覆盖 = 待人工核对」，不写成「通过」。
 */
function coverageDetail(c) {
  if (!c || c.complete !== false) return '';
  const done = (c.fields_done || []).map(semFieldLabel);
  const pending = (c.fields_pending || []).map(semFieldLabel);
  const skipped = (c.skipped_claim_ids || []).length;
  const totalFields = done.length + pending.length;

  let head = '整体差异核查未覆盖全部陈述';
  if (totalFields && pending.length) {
    head += `：已核完 ${done.length}/${totalFields} 个字段，未核完 ${pending.length} 个 —— `;
  } else if (totalFields) {
    // 字段都跑完了，但仍有陈述被跳过（例如单对超预算）
    head += `：${totalFields} 个字段均已比较，但仍有陈述未覆盖`;
  }
  const pendText = pending.length
    ? `<strong>${pending.map(esc).join('、')}</strong>`
    : '';
  const doneText = done.length
    ? `<div style="margin-top:4px;opacity:.85">已核完：${done.map(esc).join('、')}</div>`
    : '';
  const skippedText = skipped
    ? `<div style="margin-top:4px;opacity:.85">涉及 ${skipped} 条陈述，保持「待人工核对」，不会记为通过。</div>`
    : '';
  const why = c.error === 'output_limit'
    ? '（跨批核查的输出/推理预算耗尽）'
    : c.error === 'call_budget' ? '（整体调用预算已用尽）'
    : (c.calls != null && c.max_calls != null ? `（已用 ${c.calls}/${c.max_calls} 次比较预算）` : '');

  return `<div class="lui-degraded">${esc(head)}${pendText}${esc(why)}${doneText}${skippedText}</div>`;
}

function semanticReviewPanel(report, entity) {
  if (!report || report.status === 'not_run') return '<p class="source-intro">尚未运行整体语义核查；引用能定位不等于引文支持结论。重新解读后可查看。</p>';
  const items = report.items || [];
  // 待办排序：机器发现不符的最先，其次是没核完的，最后才是普通待确认。
  // 目的是让用户先处理"可能出错"的，而不是从头一条条点。
  const risk = i => (i.status === 'contradicted' ? 0
    : /未覆盖|预算|差异候选/.test(i.reason || '') ? 1 : 2);
  const chip = i => (i.status === 'contradicted'
    ? '<span class="ev-claim-chip bad">机器发现不符</span>'
    : /未覆盖|预算/.test(i.reason || '') ? '<span class="ev-claim-chip warn">未核对</span>' : '');
  const pending = items.filter(i => i.status !== 'machine_supported')
    .sort((a, b) => risk(a) - risk(b));
  const link = i => `<p><a href="#semantic-${esc(i.id)}" data-semantic-claim="${esc(i.id)}" data-semantic-kind="${esc(entity.kind)}" data-semantic-id="${esc(entity.id)}">第 ${esc(i.batch)} 批 · ${esc(i.text)}</a>${chip(i)}</p>`;
  return `<details class="card" ${pending.length ? 'open' : ''}><summary>语义核查：${esc(report.supported || 0)} 项机器支持 · ${pending.length} 项待核对 · ${(report.differences || []).length} 组差异</summary><div class="card-body"><p class="source-intro">${esc(report.note)}</p>
    ${coverageDetail(report.coverage)}
    ${entity.id ? pending.slice(0,8).map(link).join('') : ''}
    ${entity.id && pending.length > 8 ? `<details><summary>其余 ${pending.length-8} 项待核对陈述</summary>${pending.slice(8).map(link).join('')}</details>` : ''}
    ${items.some(i => i.status === 'machine_supported') && entity.id ? `<details><summary>查看机器支持项与引文</summary>${items.filter(i => i.status === 'machine_supported').map(link).join('')}</details>` : ''}</div></details>`;
}

/** 右栏展示候选来源原文，并高亮最可能支撑该陈述的句子。 */
function renderSourceBlocks(claim) {
  const candidates = (claim.candidate_tags || []).filter(t => S.semanticSources?.[t]?.text);
  const quoted = (claim.quotes || []).map(q => q.tag).filter(t => S.semanticSources?.[t]?.text);
  const order = [...new Set([...quoted, ...candidates])];
  if (!order.length) return '';
  const blocks = order.map((t, index) => {
    const src = S.semanticSources[t];
    const label = `[${esc(t)}] ${esc(src.paper_title || '原文')}${src.page == null ? '' : ' p.' + esc(src.page)}`;
    return `<details class="ev-source"${index === 0 ? ' open' : ''}><summary>${label}</summary>`
      + `<blockquote class="reading-text">${renderEvidenceText(src.text, claim.text)}</blockquote></details>`;
  }).join('');
  return `<p class="source-intro">下方原文中高亮的是最可能支撑本条陈述的句子 —— 仅辅助定位，不构成判定。</p>${blocks}`;
}

function semanticEndpoint(ref) {
  return ref.kind === 'run' ? '/api/semantic-runs/'+encodeURIComponent(ref.id) : '/api/paper/'+encodeURIComponent(ref.id)+'/semantic';
}

function showSemantic(ref) {
  S.evidenceRef = null; S.semanticRef = ref; S.inspectorTab = 'sources';
  $$('[data-insp]').forEach(b => b.classList.toggle('active', b.dataset.insp === 'sources'));
  $$('[data-insp-pane]').forEach(p => p.classList.toggle('active', p.dataset.inspPane === 'sources'));
  toggleInspector(true);
}

async function loadSemanticDetail(ref) {
  const box = $('#inspSources');
  const seq = S.evidenceLoadSeq = (S.evidenceLoadSeq || 0)+1;
  box.innerHTML = '<p role="status">正在读取陈述、差异与来源…</p>';
  try {
    const d = await api(semanticEndpoint(ref));
    if (S.semanticRef !== ref || seq !== S.evidenceLoadSeq) return;
    const claim = (d.report.items || []).find(i => i.id === ref.claimId);
    if (!claim) throw new Error('陈述不在当前解读版本中，请重新打开核查列表。');
    const reviews = (d.reviews || []).filter(i => i.claim_id === claim.id);
    const last = reviews.at(-1);
    const related = (d.report.differences || []).filter(i => i.claim_ids.includes(claim.id));
    const labels = {machine_supported: '机器支持 · 尚未经人工确认', needs_review: '待人工核对', contradicted: '机器发现不符 · 待人工核对'};
    const fields = {research_question:'研究问题', methodology:'方法', experiment_design:'实验设计', key_conclusions:'主要结论', innovations:'创新点', limitations:'局限', summary:'总体结论', convergence:'一致点', divergence:'分歧点'};
    const renderClaim = c => `<h4>第 ${esc(c.batch)} 批 · ${esc(fields[c.field] || '对比陈述')}</h4><div class="reading-text">${esc(c.text)}</div><p class="source-intro">${esc(labels[c.status] || c.status)}</p><p>机器核查说明：${esc(c.reason)}</p>${(c.quotes || []).map(q => `<details><summary>引文 [${esc(q.tag)}] ${q.page == null ? '' : 'p.'+esc(q.page)} · ${esc(q.quote.slice(0,70))}${q.quote.length > 70 ? '…' : ''}</summary><blockquote class="reading-text">${renderEvidenceText(q.quote, c.text)}</blockquote></details>`).join('')}<p>候选来源：${(c.candidate_tags || []).map(t => {
      const stored = S.semanticSources?.[t];
      return stored ? `<a href="#evidence-${esc(stored.chunk_id)}" data-evidence-ref="${esc(stored.chunk_id)}">[${esc(t)}] ${esc(stored.paper_title || '原文')} ${stored.page == null ? '' : 'p.'+esc(stored.page)}</a>` : esc('['+t+']');
    }).join(' · ')}</p>${renderSourceBlocks(c)}`;
    // Resolve original immutable chunk IDs even when the reviewer supplied no valid quote.
    S.semanticSources = {};
    const ids = new Set([claim.id, ...related.flatMap(i => i.claim_ids)]);
    const linked = (d.report.items || []).filter(i => ids.has(i.id));
    const sourceRecords = d.sources || [];
    sourceRecords.forEach(s => { S.semanticSources[s.tag] = s; });
    box.innerHTML = `<button class="btn sm" id="btnSemanticBack">返回来源列表</button><h3>陈述与整体差异核对</h3><p class="source-intro">机器核查不等于人工确认。待核对项保留在此处，不作为正常结论。</p>${renderClaim(claim)}
      ${related.map(diff => `<details open><summary>差异候选：${esc(diff.reason)}</summary>${linked.filter(c => c.id !== claim.id && diff.claim_ids.includes(c.id)).map(renderClaim).join('')}</details>`).join('')}
      <form id="semanticReview"><label>核对后的知识正文（可修正原陈述）<textarea class="textarea" rows="5" name="reviewed_text" maxlength="20000">${esc(last?.reviewed_text || claim.text)}</textarea></label><label>人工核对依据<textarea class="textarea" name="note" maxlength="5000" required placeholder="核对双方出处、条件、数字与适用范围后填写"></textarea></label><p class="source-intro">确认后单独加入可信知识库，原始解读与论文核验状态保留。</p><div class="row wrap"><button class="btn primary" name="action" value="confirm" ${last?.action === 'confirm' ? 'disabled' : ''}>${last?.action === 'confirm' ? '已人工确认入库' : '确认正文并入库'}</button><button class="btn" name="action" value="reject" ${last?.action === 'reject' ? 'disabled' : ''}>拒绝陈述</button><button class="btn" name="action" value="revoke" ${last?.action !== 'confirm' ? 'disabled' : ''}>撤销入库</button></div><p class="task-error" role="alert"></p></form>
      ${last?.knowledge_id ? `<p><a href="#evidence-${esc(last.knowledge_id)}" data-evidence-ref="${esc(last.knowledge_id)}">查看知识条目与来源快照</a></p>` : ''}
      ${reviews.length ? `<details><summary>人工审核历史</summary>${reviews.map(r => `<p>${esc(sourceTime(r.created_at))} · ${esc(({confirm:'确认入库', reject:'拒绝', revoke:'撤销'})[r.action])}<br>${esc(r.note)}</p>`).join('')}</details>` : ''}`;
    $('#btnSemanticBack').onclick = () => { S.semanticRef = null; refreshInspector(); };
    $('#semanticReview').addEventListener('submit', async event => {
      event.preventDefault(); const form = event.currentTarget;
      const buttons = $$('button', form), disabled = buttons.map(b => b.disabled);
      const values = Object.fromEntries(new FormData(form));
      const action = event.submitter?.value;
      buttons.forEach(b => { b.disabled = true; });
      try {
        await api(semanticEndpoint(ref)+'/'+encodeURIComponent(ref.claimId)+'/review', {method:'POST', body:{...values, action, analysis_hash:d.analysis_hash}});
        toast('人工审核已留档'); await loadSemanticDetail(ref);
      } catch (error) { $('.task-error', form).textContent = error.message; buttons.forEach((b,i) => {b.disabled=disabled[i];}); }
    });
  } catch (error) { if (S.semanticRef === ref && seq === S.evidenceLoadSeq) box.textContent = error.message; }
}

document.addEventListener('click', event => {
  const link = event.target.closest('[data-semantic-claim]');
  if (!link) return;
  event.preventDefault(); showSemantic({kind:link.dataset.semanticKind, id:link.dataset.semanticId, claimId:link.dataset.semanticClaim});
});

function trustLabel(level) {
  return ({ verified: '自动核验通过', manual_confirmed: '人工确认', pending: '待人工审核', likely: '疑似匹配',
    unchecked: '无法判定', not_found: '未能核到', rejected: '已拒绝 · 不可引用', revoked: '已撤销 · 不可引用', deleted: '已回收 · 不可引用' })[level] || '状态未知';
}

function safeSourceUrl(url) {
  try { const u = new URL(url); return ['http:', 'https:'].includes(u.protocol) ? u.href : ''; }
  catch { return ''; }
}

function sourceTime(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN', { hour12: false });
}

async function showEvidence(ref) {
  S.semanticRef = null;
  S.evidenceRef = ref;
  S.inspectorTab = 'sources';
  $$('[data-insp]').forEach(b => b.classList.toggle('active', b.dataset.insp === 'sources'));
  $$('[data-insp-pane]').forEach(p => p.classList.toggle('active', p.dataset.inspPane === 'sources'));
  toggleInspector(true);
}

async function loadEvidenceDetail(ref) {
  const loadSeq = S.evidenceLoadSeq = (S.evidenceLoadSeq || 0)+1;
  const box = $('#inspSources');
  box.innerHTML = '<p role="status">正在读取来源快照…</p>';
  try {
    const d = await api('/api/evidence/'+encodeURIComponent(ref));
    if (S.evidenceRef !== ref || loadSeq !== S.evidenceLoadSeq) return;
    const kind = ({ paper: '论文原文', web: '网页摘要快照', knowledge: '知识条目快照' })[d.source_kind] || '来源';
    const url = safeSourceUrl(d.url || (d.doi ? 'https://doi.org/'+encodeURIComponent(d.doi) : ''));
    const canReview = d.source_kind !== 'paper' || (!d.citable && d.trust_level !== 'deleted');
    box.innerHTML = `<button class="btn sm" id="btnSourceBack">查看本轮其他来源</button><h3>${esc(d.title || d.paper_title)}</h3>
      <div class="source-intro">${esc(kind)} · ${esc(trustLabel(d.trust_level))} · ${d.page == null ? '无页码' : '第 '+esc(d.page)+' 页'}</div>
      ${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">打开原始来源 ↗</a><p class="source-url">${esc(url)}</p>` : '<p class="source-intro">来源：本地文献或用户提供的知识</p>'}
      ${d.doi ? `<p>DOI：${esc(d.doi)}</p>` : ''}
      ${d.authors ? `<p>作者：${esc(Array.isArray(d.authors) ? d.authors.join('、') : d.authors)}</p>` : ''}
      <p class="source-intro">${d.citable ? '当前可以作为证据；人工确认不等于自动核验。' : '当前不可作为知识库回答的事实依据。'}</p>
      <div class="reading-text evidence-excerpt">${esc(d.text || '')}</div>
      ${d.content && d.content !== d.text ? `<details><summary>查看完整审核快照（${d.content.length} 字符）</summary><div class="reading-text">${esc(d.content)}</div></details>` : ''}
      ${d.review_note ? `<p>审核依据：${esc(d.review_note)}</p><p class="source-intro">审核时间（本地）：${esc(sourceTime(d.reviewed_at))}</p>` : ''}
      ${d.source_kind === 'paper' && d.verify_evidence ? `<details><summary>核验记录</summary><pre class="reading-text">${esc(typeof d.verify_evidence === 'string' ? d.verify_evidence : JSON.stringify(d.verify_evidence, null, 2))}</pre></details>` : ''}
      ${canReview ? `<form id="evidenceReview"><label>人工审核依据<textarea class="textarea" name="note" required placeholder="核对出处、原文与适用范围后填写"></textarea></label><div class="row wrap"><button class="btn primary" name="action" value="confirm" ${d.citable ? 'disabled' : ''}>${d.citable ? '已加入可信知识库' : '确认可信并加入知识库'}</button>${d.source_kind !== 'paper' ? `<button class="btn" name="action" value="reject" ${d.trust_level === 'rejected' ? 'disabled' : ''}>拒绝</button><button class="btn" name="action" value="revoke" ${!d.citable ? 'disabled' : ''}>撤销可信</button>` : ''}</div><p class="task-error" role="alert"></p></form>` : ''}
      ${(d.reviews || []).length ? `<details><summary>审核历史</summary>${d.reviews.map(r => `<p>${esc(sourceTime(r.created_at))} · ${esc(({confirm: '确认加入', reject: '拒绝', revoke: '撤销'})[r.action] || r.action)}<br>${esc(r.note)}</p>`).join('')}</details>` : ''}`;
    $('#btnSourceBack').onclick = () => { S.evidenceRef = null; refreshInspector(); };
    $('#evidenceReview')?.addEventListener('submit', async e => {
      e.preventDefault();
      const form = e.currentTarget;
      const buttons = $$('button', form);
      const disabled = buttons.map(b => b.disabled);
      buttons.forEach(b => { b.disabled = true; });
      try {
        await api('/api/evidence/'+encodeURIComponent(ref)+'/review', { method: 'POST', body: {
          action: e.submitter.value, note: new FormData(form).get('note'), content_hash: d.content_hash } });
        toast('审核结果已留档'); await loadEvidenceDetail(ref);
        if (S.activeView === 'knowledge') await loadKnowledge();
      } catch (error) { $('.task-error', form).textContent = error.message; buttons.forEach((b, i) => { b.disabled = disabled[i]; }); }
    });
  } catch (error) { if (S.evidenceRef === ref && loadSeq === S.evidenceLoadSeq) box.textContent = error.message; }
}

async function loadKnowledge() {
  const d = await api('/api/evidence');
  $('#knowledgeList').innerHTML = d.items.length ? d.items.map(s => `<div class="list-item"><div class="list-main"><a href="#evidence-${esc(s.id)}" data-evidence-ref="${esc(s.id)}">${esc(s.title)}</a><p>${esc(s.kind === 'web' ? '网页' : '知识条目')} · ${esc(trustLabel(s.status))}</p></div></div>`).join('') : '<div class="empty">尚无知识或网页候选。保存原文片段后，在右栏人工审核。</div>';
}

document.addEventListener('click', e => {
  const link = e.target.closest('[data-evidence-ref]');
  if (!link) return;
  e.preventDefault(); showEvidence(link.dataset.evidenceRef);
});

$('#btnAddKnowledge').onclick = () => workflowDialog('保存待审核原文', '<label>类型<select class="select" name="kind"><option value="knowledge">知识条目</option><option value="web">网页片段</option></select></label><label>标题<input class="input" name="title" required></label><label>原始 URL（网页必填）<input class="input" name="url"></label><label>原文快照<textarea class="textarea" name="content" rows="8" required></textarea></label><p>保存为待审核候选；下一步在右栏核对后确认。</p>', async values => {
  const source = await api('/api/evidence', { method: 'POST', body: values }); await loadKnowledge(); showEvidence(source.id);
}, '保存候选');

$('#btnSearchEvidence').onclick = async () => {
  const b = $('#btnSearchEvidence'); b.disabled = true;
  try { const r = await api('/api/evidence/search', { method: 'POST', body: { query: $('#webEvidenceQuery').value } }); toast(r.note); await loadKnowledge(); }
  catch (e) { toast(e.message, true); }
  finally { b.disabled = false; }
};

async function loadMemories() {
  const d = await api('/api/memories');
  $('#memoryList').innerHTML = (d.items || []).map(m => `<div class="list-item"><div class="list-main"><strong>${esc(m.key || m.kind)}</strong><p>${esc(m.value)}</p></div>
    <button class="btn sm" data-edit-memory="${esc(m.id)}">编辑</button><button class="btn sm" data-delete-memory="${esc(m.id)}">删除</button></div>`).join('') || '<div class="empty">尚未设置研究偏好。</div>';
  $$('[data-edit-memory]').forEach(b => b.onclick = () => editMemory(d.items.find(m => m.id === b.dataset.editMemory)));
  $$('[data-delete-memory]').forEach(b => b.onclick = async () => {
    try { await api('/api/memories/' + b.dataset.deleteMemory, { method: 'DELETE' });
      S.deletedMemory = b.dataset.deleteMemory; await loadMemories();
    } catch (error) { toast(error.message, true); }
  });
  if (S.deletedMemory) {
    $('#memoryList').insertAdjacentHTML('afterbegin', '<button class="btn" id="btnUndoMemory">恢复最近删除的偏好</button>');
    $('#btnUndoMemory').onclick = async () => { await api('/api/memories/' + S.deletedMemory + '/restore', { method: 'POST' }); S.deletedMemory = null; loadMemories(); };
  }
}

function editMemory(memory = {}) {
  workflowDialog(memory.id ? '编辑偏好' : '添加偏好', `<label>类型<select class="select" name="kind">${[['style_preference', '表达偏好'], ['term_preference', '术语偏好'], ['research_interest', '研究方向']].map(([v, label]) => `<option value="${v}" ${memory.kind === v ? 'selected' : ''}>${label}</option>`).join('')}</select></label>
    <label>内容<textarea class="textarea" name="value" required>${esc(memory.value || '')}</textarea></label>`, async values => {
    const r = await api('/api/memories' + (memory.id ? '/' + memory.id : ''), { method: memory.id ? 'PATCH' : 'POST', body: values });
    if (r.action === 'rejected') throw new Error(r.reason);
    await loadMemories();
  }, '保存偏好');
}

async function loadTrash() {
  const data = await api('/api/trash');
  $('#trashList').innerHTML = (data.items || []).map(p => `<div class="list-item"><div class="list-main">${esc(p.display_title || p.title)}<p class="muted">${esc(p.deleted_at)}</p></div><button class="btn" data-restore-paper="${esc(p.id)}">恢复文献</button></div>`).join('') || '<div class="empty">回收站为空。</div>';
  $$('[data-restore-paper]').forEach(b => b.onclick = async () => {
    try { await api('/api/trash/' + b.dataset.restorePaper + '/restore', { method: 'POST' }); await loadTrash(); await refreshLibrary(); }
    catch (error) { toast(error.message, true); }
  });
}

async function manageCollections() {
  await refreshCollections();
  const parents = '<option value="">顶层</option>'+S.collections.map(c => `<option value="${esc(c.id)}">${esc(collectionLabel(c.id))}</option>`).join('');
  const d = workflowDialog('管理分组', `<label>新分组名称<input class="input" name="name" required></label><label>父分组<select class="select" name="parent_id">${parents}</select></label>` + S.collections.map(c => `<div class="row"><span class="spacer">${esc(collectionLabel(c.id))}</span><button type="button" class="btn sm" data-rename-group="${esc(c.id)}">编辑</button><button type="button" class="btn sm" data-delete-group="${esc(c.id)}">删除分组</button></div>`).join(''), async v => {
    await api('/api/collections', { method: 'POST', body: v }); await refreshCollections();
  }, '创建分组');
  $$('[data-rename-group]', d).forEach(b => b.onclick = () => {
    const c = S.collections.find(x => x.id === b.dataset.renameGroup); d.close();
    const descendants = collectionDescendants(c.id);
    workflowDialog('编辑分组', `<input class="input" name="name" value="${esc(c.name)}" required><label>父分组<select class="select" name="parent_id"><option value="">顶层</option>${S.collections.filter(v => !descendants.includes(v.id)).map(v => `<option value="${esc(v.id)}" ${v.id === c.parent_id ? 'selected' : ''}>${esc(collectionLabel(v.id))}</option>`).join('')}</select></label>`, async v => { await api('/api/collections/' + c.id, { method: 'PATCH', body: v }); await refreshCollections(); await refreshLibrary(); });
  });
  $$('[data-delete-group]', d).forEach(b => b.onclick = async () => {
    if (!confirm('删除此分组？文献会保留在未分组中。')) return;
    try { await api('/api/collections/' + b.dataset.deleteGroup, { method: 'DELETE' }); d.close(); await refreshCollections(); await refreshLibrary(); }
    catch (error) { toast(error.message, true); }
  });
}

$('#btnImport').onclick = () => $('#importFiles').click();
$('#importFiles').onchange = e => importFiles([...e.target.files]);
$('#collectionFilter').onchange = () => loadLibrary();
$('#btnCollection').onclick = () => manageCollections().catch(e => toast(e.message, true));
$('#btnCompareSelected').onclick = () => { S.compareKey = null; openView('compare'); };
$('#btnAddMemory').onclick = () => editMemory();
let convSearchTimer;
$('#convSearch').oninput = () => { clearTimeout(convSearchTimer); convSearchTimer = setTimeout(() => loadSidebar(), 250); };
for (const [selector, operation] of [['#btnBatchDelete', 'delete'], ['#btnBatchVerify', 'reverify']]) {
  $(selector).onclick = async () => {
    if (!S.selectedPapers.size) return toast('请先勾选文献');
    if (!confirm(operation === 'delete' ? '将所选文献移入回收站？' : '重新核验所选文献？未通过的文献将停止作为引用来源。')) return;
    try { const r = await runTask('batch', { operation, paper_ids: [...S.selectedPapers] }); toast(`已处理 ${r.items.length} 篇`); await refreshLibrary(); }
    catch (e) { toast(e.message, true); await refreshLibrary(); }
  };
}
$('#recommendForm').onsubmit = recommendPapers;
$('#btnExportAll').onclick = () => downloadApi('/api/deliverables-archive', 'litprism-deliverables.zip').catch(e => toast(e.message, true));
$('#btnMovePapers').onclick = () => {
  if (!S.selectedPapers.size) return toast('请先勾选文献');
  workflowDialog('移动到分组', `<select class="select" name="collection_id"><option value="">未分组</option>${S.collections.map(c => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('')}</select>`, async values => {
    for (const pid of S.selectedPapers) await api('/api/paper/' + pid, { method: 'PATCH', body: values });
    await refreshLibrary();
  });
};
$('#btnPaperExport').onclick = () => {
  workflowDialog('导出文献', '<p class="source-intro">导出勾选文献；未勾选时导出全库。仅接受核验可引用的文献。</p><select class="select" name="format"><option value="bibtex">BibTeX</option><option value="ris">RIS</option><option value="json">JSON</option><option value="markdown">Markdown 解读与引用</option></select>', async values => {
    const extensions = { bibtex: 'bib', ris: 'ris', json: 'json', markdown: 'md' };
    await downloadApi('/api/library/export', 'litprism.' + extensions[values.format], { format: values.format, ...(S.selectedPapers.size ? { paper_ids: [...S.selectedPapers] } : {}) });
  }, '下载');
};
document.addEventListener('dragover', e => { if (e.dataTransfer?.types.includes('Files')) e.preventDefault(); });
document.addEventListener('drop', e => {
  if (!e.dataTransfer?.files.length) return;
  e.preventDefault(); openView('library'); importFiles([...e.dataTransfer.files]);
});
refreshCollections().catch(e => toast(e.message, true));
