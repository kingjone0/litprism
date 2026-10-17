/* 研究任务交互。模型调用与引用校验均由后端执行。 */
S.selectedPapers = new Set();
S.collections = [];

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
    try { await submit(Object.fromEntries(new FormData(e.target)), dialog); dialog.close(); }
    catch (error) { $('.task-error', dialog).textContent = error.message; }
    finally { button.disabled = false; }
  };
  dialog.showModal();
  return dialog;
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
  filter.value = value;
}

function showConversationDialog() {
  const options = S.papers.map(p => `<option value="p:${esc(p.id)}">${esc(short(p.display_title || p.title, 42))}</option>`).join('') +
    S.collections.map(c => `<option value="g:${esc(c.id)}">分组：${esc(c.name)}</option>`).join('');
  workflowDialog('新建研究对话', `<label>对话名称<input class="input" name="title" value="新研究对话" required></label>
    <label>文献范围<select class="select" name="scope"><option value="">全部已核验文献</option>${S.selectedPapers.size ? '<option value="selected">知识库中勾选的文献</option>' : ''}${options}</select></label>`, async values => {
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
    const res = await fetch((env.base || '') + '/api/upload?analyze=false', {
      method: 'POST', headers: env.token ? { 'X-LitPrism-Token': env.token } : {}, body: form,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || data.error || '导入失败');
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
    try { S.comparison = await api('/api/compare', { method: 'POST', body: { refs: ids } }); }
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
      ${(r.data.dimensions || []).length ? `<div style="overflow-x:auto"><table class="table"><thead><tr><th>维度</th>${(r.data._papers || []).map(p => `<th>${esc(p.title)}</th>`).join('')}</tr></thead>
        <tbody>${r.data.dimensions.map(d => `<tr><td>${esc(d.dimension)}</td>${(r.data._papers || []).map(p => `<td>${LitUI.md(String((d.values || {})[p.tag] || '未提及'))}</td>`).join('')}</tr>`).join('')}</tbody></table></div>` : ''}</div>`;
  $('#btnSaveComparison')?.addEventListener('click', async () => {
    try {
      await api('/api/deliverables', { method: 'POST', body: { type: 'comparison', title: '论文对比报告', content: r.markdown,
        citations: r.data._citations, model: r.data._model, source_scope: { citation_check: check } } });
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
    <input class="input" name="${esc(i.key)}" ${i.required ? 'required' : ''} placeholder="${esc(i.type === 'papers' ? '论文 id，以逗号分隔' : i.key)}"></label>`).join('') +
    '<p class="source-intro">只执行声明式预取和提示词；没有原文依据时拒绝生成。</p>', async values => {
      const r = await api(`/api/skills/${encodeURIComponent(skill.id)}/run`, { method: 'POST', body: { inputs: values } });
      if (!r.ok) throw new Error(r.error || (r.citation_check?.issues || []).join('；') || '引用检查失败');
      $('#skillRunResult')?.remove();
      $('#skillCards').insertAdjacentHTML('afterbegin', `<div class="card" id="skillRunResult"><div class="card-body">${LitUI.md(r.answer || '')}${r.deliverable_id ? '<p class="insp-note">已保存到研究产出。</p>' : ''}</div></div>`);
    }, '运行并检查引用');
}

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
    try { await api('/api/trash/' + b.dataset.restorePaper + '/restore', { method: 'POST' }); await loadTrash(); await loadSidebar(); }
    catch (error) { toast(error.message, true); }
  });
}

async function manageCollections() {
  await refreshCollections();
  const d = workflowDialog('管理分组', `<label>新分组名称<input class="input" name="name" required></label>` + S.collections.map(c => `<div class="row"><span class="spacer">${esc(c.name)}</span><button type="button" class="btn sm" data-rename-group="${esc(c.id)}">改名</button><button type="button" class="btn sm" data-delete-group="${esc(c.id)}">删除分组</button></div>`).join(''), async v => {
    await api('/api/collections', { method: 'POST', body: { name: v.name } }); await refreshCollections();
  }, '创建分组');
  $$('[data-rename-group]', d).forEach(b => b.onclick = () => {
    const c = S.collections.find(x => x.id === b.dataset.renameGroup); d.close();
    workflowDialog('重命名分组', `<input class="input" name="name" value="${esc(c.name)}" required>`, async v => { await api('/api/collections/' + c.id, { method: 'PATCH', body: v }); await refreshCollections(); });
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
