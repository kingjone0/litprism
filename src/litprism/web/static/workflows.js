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

async function startDurableChat(convId, request) {
  const key = 'litprism-pending-chat:'+convId;
  let pending;
  try { pending = JSON.parse(sessionStorage.getItem(key) || 'null'); } catch (_) {}
  if (!pending || pending.question !== request.question || pending.web_search !== request.web_search) {
    pending = {...request, request_id:crypto.randomUUID()};
    sessionStorage.setItem(key, JSON.stringify(pending));
  }
  const receipt = await api('/api/conversations/'+encodeURIComponent(convId)+'/chat-runs', {method:'POST', body:pending});
  sessionStorage.removeItem(key);
  await refreshChatRuns(convId);
  return waitChatRun(receipt.run_id,convId);
}

async function waitChatRun(rid,convId = S.activeConv) {
  observeChatStream(rid,convId);
    for (;;) {
      const current = await api('/api/chat-runs/'+encodeURIComponent(rid));
      if (current.status === 'completed') {
        clearChatStream(rid);
        return await api('/api/chat-runs/'+encodeURIComponent(rid)+'/result');
      }
      if (!current.active && !['queued','running'].includes(current.status)) {
        const error = new Error(current.stop_reason || '请求已停止，可在本轮请求记录中继续检查。');
        error.cancelled = current.status === 'cancelled';
        throw error;
      }
      await new Promise(resolve => setTimeout(resolve, 400));
    }
}

async function refreshChatRuns(convId) {
  if (S.activeConv !== convId || S.activeView !== 'chat') return;
  clearTimeout(S.chatRunTimer);
  let panel = $('#chatRunPanel');
  if (!panel) {
    panel = document.createElement('details'); panel.id = 'chatRunPanel'; panel.className = 'chat-run-panel';
    $('#chatStream')?.before(panel);
    panel.addEventListener('click', event => {
      if (event.target.closest('summary') === panel.firstElementChild) {
        const view = S.chatRunViews.get(convId);
        if (view) view.manualOpen = !panel.open;
      }
    });
  }
  try {
    const d = await api('/api/conversations/'+encodeURIComponent(convId)+'/chat-runs');
    if (!panel.isConnected || S.activeConv !== convId || S.activeView !== 'chat') return;
    const latest=d.items[0];
    const live=d.items.find(r=>r.active) || (latest&&!['completed','stale'].includes(latest.status) ? latest : null);
    if(live)observeChatStream(live.id,convId);
    const labels = {queued:'待执行', running:'执行中', completed:'结果已保存', interrupted:'执行已中断',
      needs_retry:'需确认重试', failed:'调用或发布失败', cancelled:'已停止', stale:'依据已变化'};
    const incomplete = d.items.filter(r => r.status !== 'completed');
    const attention = incomplete.filter(r=>!r.active);
    S.chatActiveRunning = d.items.some(r => r.active);
    if ($('#btnSend')) $('#btnSend').disabled = S.sending || S.chatActiveRunning || !!S.modelSwitch || modelReadBlocksSend();
    panel.innerHTML = `<summary>本轮请求记录 · ${S.chatActiveRunning ? '生成中' : incomplete.length ? incomplete.length+' 项未发布' : '结果已保存'}</summary>
      <div class="chat-run-list">${d.items.map(r => `<section class="chat-run-item"><strong>${esc(r.question)}</strong>
        <p class="source-intro">${esc(labels[r.status] || r.status)} · ${r.usage.calls} 次模型调用 · ${fmtNum(r.usage.input+r.usage.output)} 已报告 token${r.usage.unknown_usage_calls ? ' · '+r.usage.unknown_usage_calls+' 次用量未知' : ''}</p>
        ${r.stop_reason ? `<p>${esc(r.stop_reason)}</p>` : ''}
        ${r.retry_required && !r.active ? '<p class="source-intro">重试可能再次计费。已保存且完全匹配的响应会复用，旧失败记录保留。</p>' : ''}
        <div class="row wrap">${r.status === 'completed' ? `<button class="btn sm" data-chat-result="${esc(r.id)}">查看已存结果</button>` :
          r.status === 'stale' ? '<span class="source-intro">请依据当前材料重新提问。</span>' :
          `<button class="btn sm" data-chat-resume="${esc(r.id)}" data-retry="${r.retry_required}" data-active="${r.active}">${r.active ? '跟进当前请求' : r.retry_required ? '重新请求（可能再次计费）' : '继续处理已存请求'}</button>`}
          ${!r.active && !['completed','cancelled'].includes(r.status) ? `<button class="btn sm" data-chat-stop="${esc(r.id)}">结束此请求</button>` : ''}</div>
        <details><summary>查看逐次调用记录</summary>${r.calls.length ? r.calls.map(c => `<p class="source-intro">${esc(c.task)} · 第 ${c.step} 步 / 尝试 ${c.attempt} · ${esc(({started:'发送已登记，结果未知', succeeded:'响应已保存', failed:'失败'})[c.status])}${c.error_type ? ' · '+esc(c.error_type) : ''}</p>`).join('') : '<p>尚无调用登记。</p>'}</details>
      </section>`).join('') || '<p>尚无持久请求；旧消息不自动补建调用记录。</p>'}</div><p class="task-error" role="alert"></p>`;
    panel.hidden = !d.items.length;
    const view = S.chatRunViews.get(convId) || { manualOpen: null, attentionKey: '' };
    const attentionKey = attention.map(r => r.id + ':' + r.status).join('|');
    if (attentionKey && attentionKey !== view.attentionKey && view.manualOpen === false) view.manualOpen = null;
    view.attentionKey = attentionKey;
    S.chatRunViews.set(convId, view);
    panel.open = view.manualOpen ?? Boolean(attention.length);
    $$('[data-chat-result]', panel).forEach(b => b.onclick = async () => {
      b.disabled = true;
      try {
        const result = await api('/api/chat-runs/'+b.dataset.chatResult+'/result');
        if (S.activeConv !== convId || S.activeView !== 'chat') return;
        await loadChat();
        document.querySelector(`[data-message-ref="${CSS.escape(result.assistant.ref)}"]`)?.scrollIntoView({block:'center'});
      } catch (error) { if (panel.isConnected) $('.task-error', panel).textContent = error.message; }
      finally { b.disabled = false; }
    });
    $$('[data-chat-stop]', panel).forEach(b => b.onclick = async () => {
      b.disabled = true;
      try { await api('/api/chat-runs/'+b.dataset.chatStop+'/stop', {method:'POST',body:{}}); await refreshChatRuns(convId); }
      catch (error) { if (panel.isConnected) $('.task-error', panel).textContent = error.message; }
      finally { b.disabled = false; }
    });
    $$('[data-chat-resume]', panel).forEach(b => b.onclick = async () => {
      b.disabled = true;
      try {
        if (b.dataset.active !== 'true') await api('/api/chat-runs/'+b.dataset.chatResume+'/resume', {
          method:'POST', body:{retry_failed:b.dataset.retry === 'true'}});
        await waitChatRun(b.dataset.chatResume,convId);
        if (S.activeConv === convId && S.activeView === 'chat') await loadChat();
      } catch (e) {
        if (S.activeConv === convId && S.activeView === 'chat') {
          await refreshChatRuns(convId);
          $('#chatRunPanel .task-error').textContent = e.message;
        }
      } finally { b.disabled = false; }
    });
    if (d.items.some(r => r.active)) S.chatRunTimer = setTimeout(() => refreshChatRuns(convId), 1200);
  } catch (error) {
    if (panel.isConnected) {
      panel.innerHTML = `<summary>请求记录暂时不可读</summary><p role="alert">${esc(error.message)}</p><button class="btn sm" id="btnReloadChatRuns">重新读取状态</button>`;
      $('#btnReloadChatRuns').onclick = () => refreshChatRuns(convId);
    }
  }
}

function workflowDialog(title, body, submit, action = '确定') {
  $('#researchTask')?.remove();
  const dialog = document.createElement('dialog');
  dialog.id = 'researchTask';
  dialog.className = 'task-dialog';
  dialog.setAttribute('aria-labelledby', 'researchTaskTitle');
  dialog.innerHTML = `<form><header><h2 id="researchTaskTitle">${esc(title)}</h2><button type="button" data-cancel aria-label="关闭">${icon('close')}</button></header>
    <div class="task-body">${body}</div><p class="task-error" role="alert" tabindex="-1"></p>
    <footer><button type="button" class="btn" data-cancel>取消</button>${submit ? `<button class="btn primary" type="submit">${esc(action)}</button>` : ''}</footer></form>`;
  document.body.appendChild(dialog);
  $$('[data-cancel]', dialog).forEach(b => b.onclick = () => dialog.close());
  dialog.addEventListener('close', () => {
    dialog.remove();
    applyOverlayFocus();
  });
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
      if (await submit(values, dialog) !== false) dialog.close();
    }
    catch (error) {
      const message = $('.task-error', dialog);
      message.textContent = error.message;
      if (dialog.open) message.focus();
    }
    finally { button.disabled = button.hidden; }
  };
  dialog.showModal();
  return dialog;
}

const analysisStages = { extract: '原文提取', concepts: '术语提取', semantic_review: '逐项复核', semantic_cross_review: '跨批核对' };
const analysisRunStatuses = { queued: '等待执行', running: '正在执行或上次中断', paused_budget: '旧版预算暂停', needs_retry: '需确认重试', cancelled: '已取消', partial: '仍有待审项', completed: '机器复核通过', failed: '运行失败', stale: '版本已变化' };

function paperMaterialLabel(level) {
  return {metadata:'仅题录',abstract:'摘要',partial:'部分正文',fulltext:'已解析正文'}[level] || '完整度未知';
}

function readingMaterialSummary(prepared) {
  const note = {
    metadata:'仅有题录信息，不能视为已取得论文正文。',
    abstract:'本次仅依据已保存的摘要，不代表已阅读论文全文。',
    partial:'本次仅依据已保存的部分正文。',
    fulltext:'本次使用已解析正文，不保证原文件的每页、表格或公式均已完整解析。',
  }[prepared.level] || '材料完整度未知，本次只依据当前已索引文本。';
  return `<p class="reading-material"><strong>本次材料：${esc(prepared.label)}</strong> · ${fmtNum(prepared.chunks)} 个分块</p><p class="source-intro">${esc(note)}</p>`;
}

async function loadAnalysisRuns(pid) {
  const box = $('#analysisRuns');
  if (!box) return;
  try {
    const { items } = await api('/api/paper/'+encodeURIComponent(pid)+'/analysis-runs');
    if (S.activeView !== 'paper' || S.activePaper !== pid || $('#analysisRuns') !== box) return;
    const entry = $('#btnReanalyze');
    if (entry) {
      entry.dataset.readingLabel ||= entry.textContent;
      entry.textContent=items.some(run=>run.active) ? '查看解读进度' : entry.dataset.readingLabel;
    }
    box.innerHTML = `<details class="run-log" ${items.length ? 'open' : ''}><summary>解读运行记录（${items.length}${items.length === 30 ? '，最近' : ''}）</summary>
      <p class="source-intro">暂停时保留原解读。刷新或重新打开应用不会自动恢复调用。机器复核通过仍不等于人工确认。</p>
      <button class="btn sm" data-refresh-runs>刷新记录</button>
      ${items.map(run => `<div class="analysis-run" data-run-id="${esc(run.id)}">
        <div class="row wrap"><strong>${esc(analysisRunStatuses[run.status] || run.status)}</strong><span class="source-intro">${esc(run.model)} · ${esc(run.updated_at)}</span>
        ${run.active || !['completed', 'stale'].includes(run.status) ? `<button class="btn sm" data-resume-run="${esc(run.id)}">${run.active ? '查看进度' : '检查并恢复'}</button>` : ''}</div>
        ${run.capability_missing ? `<p class="lui-degraded">本次未解析到模型能力（${run.capability_missing === 'no_active_model' ? '未选择模型' : '未成功刷新模型目录'}），按配置的固定额度运行。</p>` : ''}
        <p class="source-intro">${Object.entries(analysisStages).filter(([key]) => key !== 'concepts' || run.with_concepts).map(([key, label]) => `${label} ${run.spent[key]} 次`).join(' · ')}（已用调用次数）</p>
        ${(run.steps || []).some(s => s.request_settings?.max_tokens) ? `<details><summary>各次请求的实际输出额度</summary>${run.steps.filter(s => s.request_settings?.max_tokens).map(s => `<p class="source-intro">${esc(analysisStages[s.task] || s.task)} · 第 ${s.step} 步 / 尝试 ${s.attempt} · ${s.request_settings.output_mode === 'automatic' ? '自动分配' : '历史固定额度'} ${fmtNum(s.request_settings.max_tokens)} token${s.request_settings.estimated_input_tokens == null ? '' : ` · 输入估算 ${fmtNum(s.request_settings.estimated_input_tokens)}`}</p>`).join('')}</details>` : ''}
        <p class="source-intro">已报告用量：输入 ${run.usage.input} / 输出 ${run.usage.output} token；${run.usage.unknown_usage_calls} 次用量未知。${esc(run.stop_reason || '')}</p>
        ${run.candidate_count ? `<p class="source-intro">已保留 ${fmtNum(run.candidate_count)} 条提取候选；机器复核未完成也可在候选列表核对原文并人工审核。</p>` : ''}
      </div>`).join('') || '<p class="source-intro">尚无解读记录。使用上方“解读论文”开始。</p>'}</details>`;
    $('[data-refresh-runs]', box).onclick = () => loadAnalysisRuns(pid);
    $$('[data-resume-run]', box).forEach(button => button.onclick = async () => {
      button.disabled = true;
      try {
        await analysisRunDialog(pid, {id:button.dataset.resumeRun});
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

let analysisDialogRequest = 0;
async function analysisRunDialog(pid, run = null) {
  const request = ++analysisDialogRequest, revision = S.viewRevision;
  const current = () => request === analysisDialogRequest && revision === S.viewRevision && S.activeView === 'paper' && S.activePaper === pid;
  const entry = $('#btnReanalyze');
  if (entry) entry.disabled = true;
  try {
  const prepared = await api('/api/paper/'+encodeURIComponent(pid)+'/reading');
  run = run ? await api('/api/analysis-runs/'+encodeURIComponent(run.id)) : prepared.active_run;
  if (!current()) return;
  const title = run ? (run.active ? '查看解读进度' : '检查并恢复解读') : (prepared.has_analysis ? '重新解读' : '解读论文');
  let receipt;
  const dialog = workflowDialog(title, `${readingMaterialSummary(prepared)}
    ${!prepared.ready ? `<p class="task-error">${esc(prepared.reason)}</p>` : ''}
    ${!run && prepared.has_analysis ? '<p class="source-intro">将新建一次解读。完成发布前保留原分析；新的模型请求会产生用量。</p>' : ''}
    ${run ? '<p class="source-intro">复用本次运行中精确匹配的成功响应；失败或结果未知的请求需明确允许重试。</p>' : ''}
    ${run ? `<p class="source-intro">已用调用：${Object.entries(run.spent).map(([k, v]) => `${analysisStages[k] || k} ${v} 次`).join(' · ')}</p>` : ''}
    ${run ? `<p class="source-intro">术语提取：${run.with_concepts ? '启用' : '关闭'}；机器复核：${run.semantic_review ? '启用' : '按设置跳过'}。恢复前核对原文、模型、协议、参数和分析版本。${esc(run.stop_reason || '')}</p>`
      : `<label><input type="checkbox" name="with_concepts" checked> 同时提取术语</label>
         <label><input type="checkbox" name="semantic_review" checked> 运行机器复核（关闭则只产出候选，由你直接核对原文后确认）</label>`}
    ${run?.retry_required ? '<label><input type="checkbox" name="retry_failed"> 允许重试失败或结果未知的请求</label><p class="source-intro">这些请求可能已产生费用；未勾选时不会自动重试。</p>' : ''}`, async values => {
      const body = {};
      if (run) body.retry_failed = values.retry_failed === 'on';
      else {
        body.with_concepts = values.with_concepts === 'on';
        body.semantic_review = values.semantic_review === 'on';
      }
      body.material_id = prepared.material_id;
      const endpoint = run ? '/api/analysis-runs/'+encodeURIComponent(run.id)+'/resume' : '/api/paper/'+encodeURIComponent(pid)+'/analysis-runs';
      try {
        receipt = run?.active ? {run_id:run.id,task_id:run.task_id} : await api(endpoint, { method: 'POST', body });
      } catch (error) {
        // A lost start receipt may already have created a paid run. Do not offer blind resubmission.
        $('[type=submit]', dialog).hidden = true;
        $('[type=submit]', dialog).disabled = true;
        const inspect = document.createElement('button'); inspect.type='button'; inspect.className='btn'; inspect.textContent='查看运行记录';
        inspect.onclick=()=>{dialog.close();if(current())loadPaperDetail();};
        $('.task-body',dialog).append(inspect);
        throw new Error(error.message+' 请先查看运行记录，再决定恢复或新建。');
      }
      try {
        if (current()) await loadAnalysisRuns(pid);
        const result = await waitTask(receipt.task_id);
        toast(analysisRunStatuses[result.status] || result.status, ['failed', 'stale', 'needs_retry'].includes(result.status));
      } catch (error) {
        if (current()) {
          await loadPaperDetail();
          const stopped = await analysisRunDialog(pid, {id:receipt.run_id});
          if (stopped) $('.task-error',stopped).textContent=error.message;
        }
        return false;
      } finally {
        if (current()) await loadPaperDetail();
      }
    }, run ? (run.active ? '查看进度' : '检查并恢复') : (prepared.has_analysis ? '重新解读' : '开始解读'));
  if (!prepared.ready && !run?.active) $('[type=submit]',dialog).disabled=true;
  if (!run) {
    const actions=document.createElement('div');actions.className='row wrap';
    actions.innerHTML='<button class="btn sm" type="button" data-reading-text>查看当前材料</button><button class="btn sm" type="button" data-reading-import>导入本地材料</button>';
    $('.task-body',dialog).append(actions);
    $('[data-reading-text]',actions).onclick=()=>{dialog.close();if(current())$('[data-ptab=text]').click();};
    $('[data-reading-import]',actions).onclick=()=>{dialog.close();if(current()&&openView('library'))$('#importFiles').click();};
  }
  return dialog;
  } catch (error) {
    if (current()) toast(error.message,true);
  } finally { if (entry?.isConnected) entry.disabled=false; }
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
  S.libraryRevision = (S.libraryRevision || 0) + 1;
  S.compareKey = null;
  S.comparison = null;
  S.compareSources = [];
  S.status = await api('/api/status');
  await loadSidebar();
  S.selectedPapers = new Set([...S.selectedPapers].filter(id => S.papers.some(p => p.id === id)));
  syncBatchActions();
  await loadLibrary();
  if (S.activeView === 'compare') {
    $('#comparisonResult').textContent = '文献已变化，请重新选择文献进行对比。';
    refreshInspector();
  }
}

async function refreshCollections() {
  const d = await api('/api/collections');
  S.collections = d.items || [];
  const filter = $('#collectionFilter');
  const value = filter.value;
  filter.innerHTML = '<option value="">全部分组</option>' + S.collections.map(c => `<option value="${esc(c.id)}">${esc(collectionLabel(c.id))}</option>`).join('');
  filter.value = S.collections.some(c => c.id === value) ? value : '';
  if (S.activeView === 'chat' && S.convScope?.raw) {S.convScope=parseScope(S.convScope.raw);renderLocation();}
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
    if (S.activeView === 'chat' && S.activeConv === c.id) {
      $('#tbTitle').textContent = v.title;
      setLocation(v.title);
    }
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
    S.collections.map(c => `<option value="g:${esc(c.id)}">分组：${esc(collectionLabel(c.id))}</option>`).join('');
  workflowDialog('新建研究对话', `<label>对话名称<input class="input" name="title" value="新研究对话" required></label>
    <label>回答范围<select class="select" name="scope"><option value="">全库知识与联网（默认）</option>${S.selectedPapers.size ? '<option value="selected">知识库中勾选的文献</option>' : ''}${options}</select></label>
    <p class="source-intro">通常直接开始即可。主动选择文献或分组时，仅使用该范围，不额外联网。</p>`, async values => {
    let scope = {};
    if (values.scope === 'selected') scope = { paper_ids: [...S.selectedPapers] };
    else if (values.scope.startsWith('p:')) scope = { paper_ids: [values.scope.slice(2)] };
    else if (values.scope.startsWith('g:')) scope = { collection_id: values.scope.slice(2) };
    if (values.scope) scope.mode = 'strict';
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
    $$('[data-import-verify]').forEach(b => b.onclick = () => { if (!openView('verify')) return; $('#verifyInput').value = b.dataset.importVerify; doVerify(); });
    await refreshLibrary();
  } catch (error) { $('#importResults').textContent = error.message; }
  finally { button.disabled = false; $('#importFiles').value = ''; }
}

async function loadComparison() {
  const box = $('#comparisonResult');
  const request = S.compareRequest = (S.compareRequest || 0) + 1;
  const ids = [...S.selectedPapers];
  if (ids.length < 2) { box.innerHTML = '<div class="empty">请先在知识库勾选至少两篇文献，然后点击「对比所选」。</div>'; return; }
  box.innerHTML = '<div class="insp-note">正在读取原文并对比，完成后检查引用…</div>';
  const key = [...ids].sort().join(',');
  if (S.compareKey !== key) {
    const revision = S.libraryRevision || 0;
    try {
      const result = await runTask('compare', { refs: ids });
      if (request !== S.compareRequest) return;
      if (revision !== (S.libraryRevision || 0) || [...S.selectedPapers].sort().join(',') !== key) {
        box.textContent = '文献已变化，请重新选择文献进行对比。';
        return;
      }
      S.comparison = result;
    }
    catch (error) { if (request === S.compareRequest) box.textContent = error.message; return; }
    S.compareKey = key;
  }
  const r = S.comparison;
  S.compareSources = r.sources || [];
  refreshInspector();
  const check = r.data._citation_check || {};
  box.innerHTML = `<div class="source-intro">模型：${esc(r.data._model || '未知')}</div>
    ${check.passed === false ? `<div class="lui-degraded">${esc((check.issues || []).join('；'))}</div>` : '<button class="btn" id="btnSaveComparison">保存为对比报告</button>'}
    <div class="reading-report">${LitUI.md(r.data.summary || '')}
      ${materialNote(r.data._coverage)}
      ${coverageNote(r.data._coverage)}
      ${semanticReviewPanel(r.data._semantic_review, {kind: 'run', id: r.data._semantic_run_id}, r.data._candidates)}
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
      i.type === 'collection' ? `<select class="select" name="${esc(i.key)}" ${i.required ? 'required' : ''}><option value="">请选择分组</option>${S.collections.map(c => `<option value="${esc(c.id)}">${esc(collectionLabel(c.id))}</option>`).join('')}</select>` :
      `<input class="input" name="${esc(i.key)}" ${i.required ? 'required' : ''} placeholder="${esc(i.key)}">`}</label>`).join('') +
    '<p class="source-intro">只执行声明式预取和提示词；没有原文依据时拒绝生成。</p>', async values => {
      const r = await runTask('skill', { skill_id: skill.id, request: { inputs: values } });
      if (!r.ok) throw new Error(r.error || (r.citation_check?.issues || []).join('；') || '引用检查失败');
      $('#skillRunResult')?.remove();
      $('#skillCards').insertAdjacentHTML('afterbegin', `<div class="card" id="skillRunResult"><div class="card-body">${LitUI.md(r.answer || '')}${r.deliverable_id ? '<p class="insp-note">已保存到研究产出。</p>' : ''}</div></div>`);
    }, '运行并检查引用');
}

function materialNote(c) {
  if (!c || !c.material_level) return '';
  const label = c.material_label || c.material_level;
  const warn = c.partial_material
    ? `<div class="lui-degraded">材料完整度：${esc(label)} —— 覆盖 ${esc(c.read_chunks)}/${esc(c.total_chunks)} 块只代表"已索引内容"，不等于整篇论文已读完。</div>`
    : '';
  return warn || `<div class="insp-note">材料完整度：${esc(label)}</div>`;
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

/**
 * 作者行折叠：30 位作者全列会把右栏撑满，且看不出重点。
 * 8 人以内全列，超过则前 6 位 + 「等 N 人」，完整名单放 title。
 */
function authorLine(authors) {
  const list = (Array.isArray(authors) ? authors : String(authors || '').split(/[;,]/))
    .map(a => String(a).trim()).filter(Boolean);
  if (!list.length) return '';
  return list.length <= 8 ? list.join('、') : `${list.slice(0, 6).join('、')} 等 ${list.length} 人`;
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
    : c.error === 'call_budget' ? '（客户端剩余调用数已用尽）'
    : (c.calls != null ? `（已比较 ${c.calls} 对）` : '');

  return `<div class="lui-degraded">${esc(head)}${pendText}${esc(why)}${doneText}${skippedText}</div>`;
}

function semanticReviewPanel(report, entity, candidates) {
  const pool = (candidates || []).map(c => ({...c, status: 'candidate', reason: '候选陈述 · 未经机器核查'}));
  if (!report || report.status === 'not_run' || report.status === 'skipped_by_user') {
    const note = report?.status === 'skipped_by_user'
      ? '已按设置跳过机器语义核查。下面是提取出的候选陈述，可直接点开核对原文并确认入库。'
      : '尚未运行机器语义核查；候选陈述可直接核对，引用能定位不等于引文支持结论。';
    if (!pool.length) return `<p class="source-intro">${note}</p>`;
    return `<details class="card" open><summary>候选陈述：${pool.length} 条待人工核对</summary><div class="card-body"><p class="source-intro">${note}</p>${pool.map(i => `<p><a href="#semantic-${esc(i.id)}" data-semantic-claim="${esc(i.id)}" data-semantic-kind="${esc(entity.kind)}" data-semantic-id="${esc(entity.id)}">第 ${esc(i.batch)} 批 · ${esc(i.text)}</a><span class="ev-claim-chip warn">未核实</span></p>`).join('')}</div></details>`;
  }
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

function confirmReviewLeave() {
  const dirty = $$('#inspSources form').some(form => form.dataset.reviewDirty === 'true' && form.getAttribute('aria-busy') !== 'true');
  return !dirty || confirm('离开当前审核？尚未提交的修改将被丢弃。');
}

for (const type of ['input', 'change']) document.addEventListener(type, event => {
  const form = event.target.closest('#inspSources form');
  if (event.isTrusted && form) form.dataset.reviewDirty = 'true';
});

function showSemantic(ref) {
  if (!confirmReviewLeave()) return;
  S.evidenceRef = null; S.semanticRef = ref; S.inspectorTab = 'sources';
  $$('[data-insp]').forEach(b => b.classList.toggle('active', b.dataset.insp === 'sources'));
  $$('[data-insp-pane]').forEach(p => p.classList.toggle('active', p.dataset.inspPane === 'sources'));
  toggleInspector(true);
}

async function loadSemanticDetail(ref) {
  if (S.semanticRef !== ref) return;
  if (ref.kind === 'chat') return loadChatReview(ref, ref.unitId);
  const box = $('#inspSources');
  const seq = S.evidenceLoadSeq = (S.evidenceLoadSeq || 0)+1;
  box.innerHTML = '<p role="status">正在读取陈述、差异与来源…</p>';
  try {
    const d = await api(semanticEndpoint(ref));
    if (S.semanticRef !== ref || seq !== S.evidenceLoadSeq) return;
    const cand = (d.candidates || []).find(i => i.id === ref.claimId);
    const claim = (d.report.items || []).find(i => i.id === ref.claimId)
      || (cand ? {...cand, status: 'candidate', reason: '候选陈述 · 未经机器核查'} : null);
    if (!claim) throw new Error('陈述不在当前解读版本中，请重新打开核查列表。');
    const reviews = (d.reviews || []).filter(i => i.claim_id === claim.id);
    const last = reviews.at(-1);
    const related = (d.report.differences || []).filter(i => i.claim_ids.includes(claim.id));
    const labels = {machine_supported: '机器支持 · 尚未经人工确认', needs_review: '待人工核对', contradicted: '机器发现不符 · 待人工核对', candidate: '候选陈述 · 未经机器核查，可直接确认入知识库'};
    const fields = {research_question:'研究问题', methodology:'方法', experiment_design:'实验设计', key_conclusions:'主要结论', innovations:'创新点', limitations:'局限', summary:'总体结论', convergence:'一致点', divergence:'分歧点'};
    const renderClaim = c => `<h4>第 ${esc(c.batch)} 批 · ${esc(fields[c.field] || '对比陈述')}</h4><div class="reading-text">${esc(c.text)}</div><p class="source-intro">${esc(labels[c.status] || c.status)}</p>${c.reason ? `<p>${c.status === 'candidate' ? '状态' : '机器核查说明'}：${esc(c.reason)}</p>` : ''}${(c.quotes || []).map(q => `<details><summary>引文 [${esc(q.tag)}] ${q.page == null ? '' : 'p.'+esc(q.page)} · ${esc(q.quote.slice(0,70))}${q.quote.length > 70 ? '…' : ''}</summary><blockquote class="reading-text">${renderEvidenceText(q.quote, c.text)}</blockquote></details>`).join('')}<p>候选来源：${(c.candidate_tags || []).map(t => {
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
    $('#btnSemanticBack').onclick = () => { if (!confirmReviewLeave()) return; S.semanticRef = null; refreshInspector(); };
    $('#semanticReview').addEventListener('submit', async event => {
      event.preventDefault(); const form = event.currentTarget;
      if (S.semanticRef !== ref || !form.isConnected || form.getAttribute('aria-busy') === 'true') return;
      const buttons = $$('button', form), disabled = buttons.map(b => b.disabled);
      const values = Object.fromEntries(new FormData(form));
      const action = event.submitter?.value;
      form.setAttribute('aria-busy', 'true');
      buttons.forEach(b => { b.disabled = true; });
      try {
        await api(semanticEndpoint(ref)+'/'+encodeURIComponent(ref.claimId)+'/review', {method:'POST', body:{...values, action, analysis_hash:d.analysis_hash}});
        toast('人工审核已留档');
        if (S.semanticRef === ref) await loadSemanticDetail(ref);
      } catch (error) { $('.task-error', form).textContent = error.message; buttons.forEach((b,i) => {b.disabled=disabled[i];}); }
      finally { form.removeAttribute('aria-busy'); }
    });
  } catch (error) { if (S.semanticRef === ref && seq === S.evidenceLoadSeq) box.textContent = error.message; }
}

async function loadChatReview(ref, openUnitId = null) {
  if (S.semanticRef !== ref) return;
  delete ref.unitId;
  const box = $('#inspSources');
  const seq = S.evidenceLoadSeq = (S.evidenceLoadSeq || 0)+1;
  box.innerHTML = '<p role="status">正在读取回复与原文…</p>';
  const endpoint = '/api/chat-reviews/'+encodeURIComponent(ref.id);
  try {
    const d = await api(endpoint);
    if (S.semanticRef !== ref || seq !== S.evidenceLoadSeq) return;
    const last = d.reviews.at(-1);
    const confirmed = last?.action === 'confirm';
    const sources = [...d.sources].sort((a,b) => Number(b.cited)-Number(a.cited));
    const unitList = d.units?.length ? `<section class="qa-unit-list" aria-label="逐条核对与入库"><h4>逐条整理（${d.units.length} 条）</h4>
      <p class="source-intro">展开条目核对证据，按需保存为可信知识。</p>
      <p class="source-intro qa-review-progress" role="status" data-review-summary-state="${esc(d.review_summary.state)}">${esc(d.review_summary.label || `尚未整理 · ${d.units.length} 条可选入库`)}</p>
      ${d.units.map(u => `<details class="qa-unit-row" data-unit-id="${esc(u.id)}"><summary data-qa-unit="${esc(u.id)}"><span class="reading-text">${esc(u.text)}</span><span class="source-intro qa-unit-row-state">${esc(unitDecisionLabel(u))}</span></summary><div data-unit-slot></div></details>`).join('')}
      ${d.review_mode === 'reply' ? '<p class="source-intro">此回复已有整答裁决，请继续使用下方整答入口。</p>' : ''}</section>` : '';
    const audit = d.evidence_audit || {state:'missing', items:[], issues:[]};
    const mapping = `<details class="qa-claim-map"><summary>来源定位记录（辅助）</summary>
      <p class="source-intro">对象和依据类型由模型提出。原句可定位不等于支持结论，请核对是否谈论同一对象。</p>
      ${audit.state === 'missing' ? '<p>此回复没有保存陈述对应记录，请直接对照草稿与原文。</p>' : ''}
      ${(audit.items || []).map(item => `<section><h4>${esc(item.answer_binding?.state === 'bound' ? item.answer_binding.span.text : item.claim || '未对应到正文的记录')}</h4>
        <p class="source-intro qa-answer-binding">${item.answer_binding?.state === 'bound' ? '已对应回答原文' : item.answer_binding?.state === 'ambiguous' ? '回答中有多处相同文字，尚未确定对应位置。' : item.answer_binding ? '模型辅助记录尚未对应回答原文，请对照回复核对。' : '旧记录未保存回答位置，请对照回复核对。'}</p>
        <p>${esc(item.subject || '未标明对象')} · ${esc(({direct:'原文描述（模型标注）', inference:'推断（模型标注）', metadata:'程序记录（模型标注）', not_located:'尚未定位（模型标注）'})[item.basis] || '旧格式，未提供原句')}</p>
        ${(item.quotes || []).map(q => { const s = sources.find(s => s.tag === q.source);
          return `${q.kind === 'program_metadata' ? '<p class="source-intro">程序记录 · 非论文原句</p>' : q.kind === 'source_anchor' ? `<p class="source-intro">${q.granularity === 'legacy_paragraph' ? '旧段落锚点' : '试验锚点'} · 程序回填原文</p>` : ''}<blockquote class="reading-text">${esc(q.quote)}</blockquote>${q.continues_before || q.continues_after ? '<p class="source-intro">这是较长原文的一部分，请查看来源前后文。</p>' : ''}${s ? `<button type="button" class="btn sm" data-qa-map-source="${esc(s.tag)}">查看 [${esc(s.tag)}] ${esc(s.title || '来源已不可用')}${s.page == null ? '' : ' · p.'+esc(s.page)}</button>${s.citable ? '' : `<p class="task-error">${esc(s.issue || '当前来源不可引用')}</p>`}` : ''}`;
        }).join('')}
        ${(item.blocking_issues || item.issues || []).length ? `<p class="task-error">${(item.blocking_issues || item.issues).map(esc).join('；')}</p>` : ''}
        ${(item.warnings || []).length ? `<p class="source-intro">辅助核对提示：${item.warnings.map(esc).join('；')}</p>` : ''}
      </section>`).join('')}
      <p class="source-intro">记录可能未覆盖全部陈述。入库时核对实际来源，正文正确即可保留。</p></details>`;
    const renderSource = s => `<details class="ev-source" ${s.cited ? 'open' : ''}><summary>[${esc(s.tag)}] ${esc(s.title || '来源已不可用')}${s.page == null ? '' : ' · p.'+esc(s.page)} · ${s.cited ? '草稿引用' : '本轮其他来源'}</summary>
      <p class="source-intro">${esc(({paper:'论文原文', knowledge:'知识快照', web:'网页快照'})[s.source_kind] || '来源')} · ${esc(trustLabel(s.trust_level))}${s.issue ? ' · '+esc(s.issue) : ''}</p>
      <blockquote class="reading-text">${esc(s.text || '原文不可读取')}</blockquote>
      <label><input type="checkbox" name="source_tags" value="${esc(s.tag)}" ${!s.citable || confirmed ? 'disabled' : ''}> 以 [${esc(s.tag)}] 支持修订正文</label></details>`;
    box.innerHTML = `<button class="btn sm" id="btnChatReviewBack">返回来源列表</button><h3>核对回复与知识入库</h3>
      <details><summary>查看本轮问题</summary><p>${esc(d.question)}</p></details>
      ${unitList}
      <details ${['needs_review','invalid_citation'].includes(d.status) ? 'open' : ''}><summary>原始模型回复（留档）</summary><div class="reading-text qa-review-draft">${esc(d.draft)}</div></details>
      ${mapping}
      ${d.issues.length ? `<p class="task-error">${d.issues.map(esc).join('；')}</p>` : ''}
      <details id="qaWholeReplyReview" ${!d.units?.length || d.review_mode === 'reply' ? 'open' : ''}><summary>整答审核（兼容入口）</summary>
      <form id="chatReviewForm"><fieldset><legend>选择支持修订正文的原文</legend>
        <p class="source-intro">请选择至少一处；正文使用对应 [P#]。页码来自原文，显式错页必须修正。</p>
        ${sources.filter(s => s.cited).map(renderSource).join('')}
        ${sources.some(s => !s.cited) ? `<details><summary>本轮其他原文（${sources.filter(s => !s.cited).length}）</summary>${sources.filter(s => !s.cited).map(renderSource).join('')}</details>` : ''}
      </fieldset><label>核对后的知识正文<textarea class="textarea" name="reviewed_text" maxlength="20000" rows="6" ${confirmed ? 'readonly' : ''}>${esc(last?.reviewed_text || d.draft)}</textarea></label>
      <label>人工核对依据<textarea class="textarea" name="note" maxlength="5000" required placeholder="说明如何核对出处、条件和结论；拒绝或撤销也需填写理由"></textarea></label>
      <p class="source-intro">入库只保存修订正文与所选原文。人工裁决后，原回复仅供审计，后续回答需重新检索证据。</p>
      <div class="row wrap"><button class="btn primary" name="action" value="confirm" ${confirmed || !sources.some(s => s.citable) ? 'disabled' : ''}>${confirmed ? '已确认入库' : '确认正文并入库'}</button>
      <button class="btn" name="action" value="reject" ${last?.action === 'reject' ? 'disabled' : ''}>拒绝回复</button>
      <button class="btn" name="action" value="revoke" ${!last?.knowledge_id || last?.action === 'revoke' ? 'disabled' : ''}>撤销入库</button></div><p class="task-error" role="alert"></p></form></details>
      ${last?.knowledge_id ? `<p><a href="#evidence-${esc(last.knowledge_id)}" data-evidence-ref="${esc(last.knowledge_id)}">查看知识条目</a> · ${esc(trustLabel(d.knowledge_status))}</p>` : ''}
      ${d.reviews.length ? `<details open><summary>人工审核历史</summary>${d.reviews.map(r => `<p>${esc(sourceTime(r.created_at))} · ${esc(({confirm:'确认入库', reject:'拒绝', revoke:'撤销'})[r.action])}<br>${esc(r.note)}</p>`).join('')}</details>` : ''}`;
    $('#btnChatReviewBack').onclick = () => { if (!confirmReviewLeave()) return; S.semanticRef = null; refreshInspector(); };
    const rows = $$('.qa-unit-row', box);
    rows.forEach(row => row.addEventListener('toggle', () => {
      const slot = $('[data-unit-slot]', row);
      if (!row.open) {
        slot.replaceChildren(); delete slot.dataset.loaded;
        if (ref.unitId === row.dataset.unitId) delete ref.unitId;
        return;
      }
      if (slot.dataset.loaded) return;
      rows.filter(other => other !== row).forEach(other => {other.open=false;});
      slot.dataset.loaded='true';
      loadChatUnitReview(ref, row.dataset.unitId, slot);
    }));
    if (d.review_mode === 'units') {
      $('#chatReviewForm').insertAdjacentHTML('beforebegin', '<p class="source-intro">此回复按条目整理，完成情况见上方。整答兼容入口已停用。</p>');
      $$('input, textarea, button', $('#chatReviewForm')).forEach(el => { el.disabled = true; });
    }
    $$('[data-qa-map-source]', box).forEach(button => button.onclick = () => {
      const input = $$('input[name=source_tags]', box).find(el => el.value === button.dataset.qaMapSource);
      if (!input) return;
      for (let parent = input.parentElement; parent && parent !== box; parent = parent.parentElement) {
        if (parent.tagName === 'DETAILS') parent.open = true;
      }
      const target = input.closest('details').querySelector('summary');
      target.focus();
      target.scrollIntoView({block:'nearest'});
    });
    $('#chatReviewForm').addEventListener('submit', async event => {
      event.preventDefault();
      const form = event.currentTarget, values = new FormData(form);
      if (S.semanticRef !== ref || !form.isConnected || form.getAttribute('aria-busy') === 'true') return;
      const buttons = $$('button', form), disabled = buttons.map(b => b.disabled);
      form.setAttribute('aria-busy', 'true');
      buttons.forEach(b => { b.disabled = true; });
      try {
        await api(endpoint+'/review', {method:'POST', body:{action:event.submitter?.value,
          reviewed_text:values.get('reviewed_text'), note:values.get('note'), source_tags:values.getAll('source_tags'),
          snapshot_hash:d.snapshot_hash, review_token:d.review_token}});
        toast('回复审核已留档');
        await refreshReviewedTurn(ref);
        if (S.semanticRef === ref) await loadChatReview(ref);
      } catch (error) {
        if (form.isConnected) {
          $('.task-error', form).textContent = error.message;
          buttons.forEach((b,i) => { b.disabled=disabled[i]; });
        }
      } finally { form.removeAttribute('aria-busy'); }
    });
    if (openUnitId) {
      const row = rows.find(row => row.dataset.unitId === openUnitId);
      if (row) {
        const slot = $('[data-unit-slot]', row);
        slot.dataset.loaded='true'; row.open=true;
        await loadChatUnitReview(ref, openUnitId, slot);
      }
    }
  } catch (error) {
    if (S.semanticRef === ref && seq === S.evidenceLoadSeq) {
      box.innerHTML = `<p role="alert">${esc(error.message)}</p><button class="btn" id="btnRetryChatReview">重新读取</button>`;
      $('#btnRetryChatReview').onclick = () => loadChatReview(ref);
    }
  }
}

async function refreshReviewedTurn(ref) {
  if (S.semanticRef !== ref || S.activeView !== 'chat') return;
  try {
    const conv = S.activeConv;
    const history = await api('/api/conversations/'+encodeURIComponent(conv)+'/messages');
    if (S.semanticRef !== ref || S.activeConv !== conv || S.activeView !== 'chat') return;
    for (const message of history.items.filter(m => m.role === 'assistant')) {
      const turn = $$('.chat-turn', $('#chatStream')).find(el => el.dataset.messageRef === message.ref);
      if (turn && (message.qa_review_id === ref.id || turn.dataset.messageStatus !== (message.status || 'answered'))) {
        turn.outerHTML = renderMessage(message);
      }
    }
    LitUI.bind($('#chatStream'));
  } catch (_) { /* Decision is durable; reopening refreshes the turn. */ }
}

async function loadChatUnitReview(ref, uid, target = null) {
  if (S.semanticRef !== ref || target && !target.isConnected) return;
  ref.unitId = uid;
  const box = target || $('#inspSources');
  const seq = S.evidenceLoadSeq = (S.evidenceLoadSeq || 0)+1;
  const endpoint = '/api/chat-reviews/'+encodeURIComponent(ref.id)+'/units/'+encodeURIComponent(uid);
  box.innerHTML = '<p role="status">正在读取回答单元…</p>';
  try {
    const d = await api(endpoint);
    if (S.semanticRef !== ref || seq !== S.evidenceLoadSeq || target && !target.closest('.qa-unit-row')?.open) return;
    const active = ['confirm','revise'].includes(d.decision);
    const actionName = {confirm:'确认入库',revise:'修订入库',reject:'拒绝',revoke:'撤销'};
    const preferredTags = [...new Set([...d.suggested_evidence.map(q => q.source), ...d.original_evidence.map(q => q.source), ...d.sources])];
    const priority = s => preferredTags.includes(s.tag) ? preferredTags.indexOf(s.tag) : preferredTags.length;
    const available = [...d.sources_available].sort((a,b) => priority(a)-priority(b));
    box.innerHTML = `<button class="btn sm" id="btnUnitReviewBack">${target ? '收起此条' : '返回回复审核'}</button>
      ${target ? '' : `<h3>单元人工核对</h3><h4>原回答</h4><blockquote class="reading-text qa-unit-original">${esc(d.text)}</blockquote>`}
      <p class="source-intro">状态：${esc(actionName[d.decision] || '未审核')}${d.knowledge_status ? ' · '+esc(trustLabel(d.knowledge_status)) : ''}</p>
      <form id="chatUnitReviewForm"><fieldset><legend>本次采用的证据</legend>
        <p class="source-intro">已选原文可直接保留，需要时补选前后句。</p>
        <p id="unitEvidenceCount" class="source-intro" role="status">正在读取已选原文…</p>
        <div id="unitEvidenceChoices"></div>
        <details class="qa-unit-other-sources"><summary>查找其他来源与原文</summary>
          <label>按编号、标题或页码筛选<input class="input" type="search" id="qaUnitSourceFilter" placeholder="例如 P19 或论文标题"></label>
          <label>本轮实际来源<select class="input" id="qaUnitSource">${available.map(s => `<option value="${esc(s.tag)}" ${s.citable ? '' : 'disabled'}>[${esc(s.tag)}] ${esc(s.title)}${s.page == null ? '' : ' · p.'+esc(s.page)}${preferredTags.includes(s.tag) ? ' · 本条相关' : ''}${s.citable ? '' : ' · 来源失效'}</option>`).join('')}</select></label>
          <button type="button" class="btn sm" id="btnUnitContext">打开来源与前后文</button>
        </details></fieldset>
        <label>核对后的单元知识<textarea class="textarea" name="reviewed_text" rows="5" maxlength="20000">${esc(d.suggested_text)}</textarea></label>
        <label>人工核对依据<textarea class="textarea" name="note" rows="3" maxlength="5000" required placeholder="说明如何核对原文，或说明拒绝、撤销原因"></textarea></label>
        ${active ? '<p class="source-intro">修订后会替换原知识。</p>' : ''}
        <div class="row wrap qa-unit-actions"><button class="btn primary" name="action" value="confirm" ${active ? 'disabled' : ''}>确认此单元并入库</button>
          <button class="btn" name="action" value="revise" ${active ? '' : 'disabled'}>修订并重新入库</button>
          <button class="btn" name="action" value="reject" ${d.decision === 'reject' ? 'disabled' : ''}>拒绝此单元</button>
          <button class="btn" name="action" value="revoke" ${active ? '' : 'disabled'}>撤销此单元入库</button></div>
        <p class="task-error" role="alert"></p></form>
      ${d.knowledge_id ? `<p><a href="#evidence-${esc(d.knowledge_id)}" data-evidence-ref="${esc(d.knowledge_id)}">查看知识条目</a></p>` : ''}
      <details><summary>原回答引用的证据</summary>${d.original_evidence.map(q => `<p>[${esc(q.source)}]</p><blockquote class="reading-text">${esc(q.quote)}</blockquote>`).join('') || '<p>原回答未保存选证记录，请选择支持正文的原文。</p>'}</details>
      ${d.reviews.length ? `<details><summary>这个单元的审核历史</summary>${d.reviews.map(r => `<p>${esc(sourceTime(r.created_at))} · ${esc(actionName[r.action] || r.action)}<br>${esc(r.note)}</p>`).join('')}</details>` : ''}`;
    $('#btnUnitReviewBack').onclick = () => {if (target) target.closest('.qa-unit-row').open=false; else loadChatReview(ref);};
    const form = $('#chatUnitReviewForm'), loaded = new Set();
    const preselected = new Set(d.suggested_evidence.map(q => q.source+':'+q.anchor_id));
    const modelSelected = new Set(d.original_evidence.map(q => q.source+':'+q.anchor_id));
    const updateCount = () => { $('#unitEvidenceCount').textContent = `已选 ${$$('[data-unit-anchor]:checked', form).length} 段原文`; };
    form.addEventListener('change', updateCount);
    async function addContext(tag) {
      if (!tag || loaded.has(tag)) return;
      loaded.add(tag);
      const container = document.createElement('section');
      container.className = 'qa-unit-context';
      container.innerHTML = `<p role="status">正在读取 [${esc(tag)}] 原文…</p>`;
      $('#unitEvidenceChoices').appendChild(container);
      try {
        const c = await api(endpoint+'/sources/'+encodeURIComponent(tag));
        if (!form.isConnected || S.semanticRef !== ref || seq !== S.evidenceLoadSeq) return;
        const chosen = c.anchors.filter(a => preselected.has(tag+':'+a.id));
        const others = c.anchors.filter(a => !preselected.has(tag+':'+a.id));
        const renderAnchor = a => `<label class="qa-unit-evidence"><input type="checkbox" data-unit-anchor="${esc(a.id)}" data-unit-source="${esc(tag)}" ${preselected.has(tag+':'+a.id) ? 'checked' : ''}> ${modelSelected.has(tag+':'+a.id) ? '原回答已选 · 核对后保留' : preselected.has(tag+':'+a.id) ? '上次人工已选 · 核对后保留' : '补充可选原文'}<span class="reading-text">${esc(a.quote)}</span>${a.continues_before || a.continues_after ? '<span class="source-intro">此段有前后延续，请一并核对。</span>' : ''}</label>`;
        container.innerHTML = `<h4>[${esc(c.tag)}] ${esc(c.title)}${c.page == null ? '' : ' · p.'+esc(c.page)}</h4>
          ${chosen.map(renderAnchor).join('')}
          ${!chosen.length ? '<p class="source-intro">此来源尚无可沿用的细原文选择，请展开下方内容明确勾选。</p>' : ''}
          ${others.length ? `<details class="qa-unit-supplement" ${!chosen.length ? 'open' : ''}><summary>补选同一来源的前后句（${others.length} 段）</summary>${others.map(renderAnchor).join('')}</details>` : ''}
          <details><summary>查看完整来源前后文</summary><p class="source-intro">${esc(c.notice)}</p><blockquote class="reading-text">${esc(c.text)}</blockquote></details>`;
        updateCount();
      } catch (error) {
        if (!form.isConnected) return;
        loaded.delete(tag);
        container.innerHTML = `<p class="task-error">${esc(error.message)}</p><button type="button" class="btn sm">重新读取 [${esc(tag)}]</button>`;
        $('button', container).onclick = () => { container.remove(); addContext(tag); };
      }
    }
    $('#btnUnitContext').onclick = () => addContext($('#qaUnitSource').value);
    $('#qaUnitSourceFilter').oninput = event => {
      const query = event.target.value.trim().toLocaleLowerCase();
      const select = $('#qaUnitSource');
      [...select.options].forEach(option => {option.hidden = !option.textContent.toLocaleLowerCase().includes(query);});
      if (select.selectedOptions[0]?.hidden) select.value = [...select.options].find(o => !o.hidden && !o.disabled)?.value || '';
    };
    if (d.review_mode === 'reply') $$('input, textarea, select, button', form).forEach(el => {el.disabled=true;});
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (S.semanticRef !== ref || !form.isConnected || form.getAttribute('aria-busy') === 'true') return;
      const values = new FormData(form), controls = $$('fieldset, button, input, textarea, select', form), disabled = controls.map(el => el.disabled);
      form.setAttribute('aria-busy', 'true');
      controls.forEach(el => {el.disabled=true;});
      try {
        const evidence = $$('[data-unit-anchor]:checked', form).map(el => ({source:el.dataset.unitSource,anchor_id:el.dataset.unitAnchor}));
        await api(endpoint+'/review', {method:'POST',body:{action:event.submitter?.value,reviewed_text:values.get('reviewed_text'),
          note:values.get('note'),evidence,snapshot_hash:d.snapshot_hash,review_token:d.review_token}});
        toast('单元裁决已留档');
        await refreshReviewedTurn(ref);
        if (S.semanticRef === ref && seq === S.evidenceLoadSeq && form.isConnected) {
          const scrollTop = $('#inspSources').scrollTop;
          await loadChatReview(ref, uid);
          if (S.semanticRef === ref) $('#inspSources').scrollTop=scrollTop;
        }
      } catch (error) {
        if (form.isConnected) { $('[role=alert]', form).textContent=error.message; controls.forEach((el,i) => {el.disabled=disabled[i];}); form.removeAttribute('aria-busy'); }
      } finally { form.removeAttribute('aria-busy'); }
    });
    for (const tag of new Set(d.suggested_evidence.length ? d.suggested_evidence.map(q => q.source) : preferredTags.slice(0,1))) await addContext(tag);
    if (form.isConnected && S.semanticRef === ref && seq === S.evidenceLoadSeq) updateCount();
    if (d.review_mode === 'reply') $$('input, textarea, select, button', form).forEach(el => {el.disabled=true;});
  } catch (error) {
    if (S.semanticRef === ref && seq === S.evidenceLoadSeq) {
      box.innerHTML = `<p role="alert">${esc(error.message)}</p><button class="btn" id="btnUnitRetry">重新读取单元</button>`;
      $('#btnUnitRetry').onclick = () => loadChatUnitReview(ref, uid, target);
    }
  }
}

document.addEventListener('click', event => {
  const unit = event.target.closest('[data-chat-unit-review]');
  if (unit) { event.preventDefault(); showSemantic({kind:'chat', id:unit.dataset.chatUnitReview, unitId:unit.dataset.chatUnit}); return; }
  const chat = event.target.closest('[data-chat-review]');
  if (chat) { event.preventDefault(); showSemantic({kind:'chat', id:chat.dataset.chatReview}); return; }
  const link = event.target.closest('[data-semantic-claim]');
  if (!link) return;
  event.preventDefault(); showSemantic({kind:link.dataset.semanticKind, id:link.dataset.semanticId, claimId:link.dataset.semanticClaim});
});

function trustLabel(level) {
  return ({ verified: '自动核验通过', manual_confirmed: '人工确认', pending: '待人工审核', needs_review:'依据失效 · 待复核', historical:'历史材料依据', likely: '疑似匹配',
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
  if (!confirmReviewLeave()) return;
  S.semanticRef = null;
  S.evidenceRef = ref;
  S.inspectorTab = 'sources';
  $$('[data-insp]').forEach(b => b.classList.toggle('active', b.dataset.insp === 'sources'));
  $$('[data-insp-pane]').forEach(p => p.classList.toggle('active', p.dataset.inspPane === 'sources'));
  toggleInspector(true);
}

function returnToSourceList() {
  if (!confirmReviewLeave()) return;
  S.evidenceRef = null;
  if (S.activeView === 'knowledge') toggleInspector(false);
  else refreshInspector();
}

async function loadEvidenceDetail(ref) {
  if (S.evidenceRef !== ref) return;
  const loadSeq = S.evidenceLoadSeq = (S.evidenceLoadSeq || 0)+1;
  const box = $('#inspSources');
  box.innerHTML = '<p role="status">正在读取来源快照…</p>';
  try {
    const d = await api('/api/evidence/'+encodeURIComponent(ref));
    if (S.evidenceRef !== ref || loadSeq !== S.evidenceLoadSeq) return;
    const kind = ({ paper: '论文原文', web: '网页摘要', knowledge: '知识条目' })[d.source_kind] || '来源';
    const url = safeSourceUrl(d.url || (d.doi ? 'https://doi.org/'+encodeURIComponent(d.doi) : ''));
    const canReview = d.source_kind !== 'paper' || (!d.citable && d.trust_level !== 'deleted');
    box.innerHTML = `<button class="btn sm" id="btnSourceBack">${S.activeView === 'knowledge' ? '返回来源列表' : '查看本轮其他来源'}</button><h3>${esc(d.title || d.paper_title)}</h3>
      <div class="source-intro">${esc(kind)} · ${esc(trustLabel(d.trust_level))} · ${d.page == null ? '无页码' : '第 '+esc(d.page)+' 页'}</div>
      ${d.dependency_status?.refs?.length || d.dependency_status?.issues?.length ? `<details open><summary>知识依据与当前状态</summary>${[...(d.dependency_status.issues || []),...(d.dependency_status.warnings || [])].map(s => `<p>${esc(s)}</p>`).join('')}${d.dependency_status.refs.map(s => `<p><a href="#evidence-${esc(s.chunk_id)}" data-evidence-ref="${esc(s.chunk_id)}">${esc(s.title || '上游来源已不可用')}${s.page == null ? '' : ' · p.'+esc(s.page)}</a>${s.historical ? ' · 历史材料' : ''}${s.issues.length ? ' · '+s.issues.map(esc).join('；') : ''}</p>`).join('')}</details>` : ''}
      ${d.acquired_at ? `<p class="source-intro">搜索摘要获取时间：${esc(sourceTime(d.acquired_at))}</p>` : ''}
      ${d.material ? `<p class="source-intro">${d.material.status === 'current' ? '当前材料' : '历史材料'} · ${esc(materialTime(d.material.imported_at))} · 原引用仍指向这份材料。</p>` : ''}
      ${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">打开原始来源 ↗</a><p class="source-url">${esc(url)}</p>` : '<p class="source-intro">来源：本地文献或用户提供的知识</p>'}
      ${d.doi ? `<p>DOI：${esc(d.doi)}</p>` : ''}
      ${d.authors ? `<p>作者：<span title="${esc((Array.isArray(d.authors) ? d.authors : [d.authors]).join('、'))}">${esc(authorLine(d.authors))}</span></p>` : ''}
      ${!d.citable ? `<p class="source-intro">${d.trust_level === 'pending' && d.acquisition === 'search_summary' ? '搜索摘要，尚未人工确认。' : '该来源当前不可用，请选择其他依据。'}</p>` : ''}
      <div class="reading-text evidence-excerpt">${esc(d.text || '')}</div>
      ${d.content && d.content !== d.text ? `<details><summary>查看完整内容</summary><div class="reading-text">${esc(d.content)}</div></details>` : ''}
      ${d.review_note ? `<p>审核依据：${esc(d.review_note)}</p><p class="source-intro">审核时间（本地）：${esc(sourceTime(d.reviewed_at))}</p>` : ''}
      ${d.source_kind === 'paper' && d.verify_evidence ? `<details><summary>核验记录</summary><pre class="reading-text">${esc(typeof d.verify_evidence === 'string' ? d.verify_evidence : JSON.stringify(d.verify_evidence, null, 2))}</pre></details>` : ''}
      ${canReview ? `<form id="evidenceReview"><label>人工审核依据<textarea class="textarea" name="note" required placeholder="核对出处、原文与适用范围后填写"></textarea></label><div class="row wrap"><button class="btn primary" name="action" value="confirm" ${d.status === 'manual_confirmed' || d.citable ? 'disabled' : ''}>${d.citable ? '已加入可信知识库' : d.trust_level === 'needs_review' ? '依据失效，需修订知识' : '确认可信并加入知识库'}</button>${d.source_kind !== 'paper' ? `<button class="btn" name="action" value="reject" ${d.trust_level === 'rejected' ? 'disabled' : ''}>拒绝</button><button class="btn" name="action" value="revoke" ${d.status !== 'manual_confirmed' ? 'disabled' : ''}>撤销可信</button>` : ''}</div><p class="task-error" role="alert"></p></form>` : ''}
      ${(d.reviews || []).length ? `<details><summary>审核历史</summary>${d.reviews.map(r => `<p>${esc(sourceTime(r.created_at))} · ${esc(({confirm: '确认加入', reject: '拒绝', revoke: '撤销'})[r.action] || r.action)}<br>${esc(r.note)}</p>`).join('')}</details>` : ''}`;
    $('#btnSourceBack').onclick = returnToSourceList;
    $('#evidenceReview')?.addEventListener('submit', async e => {
      e.preventDefault();
      const form = e.currentTarget;
      if (S.evidenceRef !== ref || !form.isConnected || form.getAttribute('aria-busy') === 'true') return;
      const buttons = $$('button', form);
      const disabled = buttons.map(b => b.disabled);
      form.setAttribute('aria-busy', 'true');
      buttons.forEach(b => { b.disabled = true; });
      try {
        await api('/api/evidence/'+encodeURIComponent(ref)+'/review', { method: 'POST', body: {
          action: e.submitter.value, note: new FormData(form).get('note'), content_hash: d.content_hash } });
        toast('审核结果已留档');
        if (S.evidenceRef === ref) await loadEvidenceDetail(ref);
        if (S.activeView === 'knowledge') await loadKnowledge();
      } catch (error) { $('.task-error', form).textContent = error.message; buttons.forEach((b, i) => { b.disabled = disabled[i]; }); }
      finally { form.removeAttribute('aria-busy'); }
    });
  } catch (error) {
    if (S.evidenceRef !== ref || loadSeq !== S.evidenceLoadSeq) return;
    box.innerHTML = `<p class="task-error" role="alert">${esc(error.message)}</p><div class="row wrap">
      <button class="btn" id="btnRetryEvidence">重新读取</button>
      <button class="btn" id="btnSourceBack">返回来源列表</button></div>`;
    $('#btnRetryEvidence').onclick = () => loadEvidenceDetail(ref);
    $('#btnSourceBack').onclick = returnToSourceList;
  }
}

async function loadKnowledge() {
  const d = await readPanel($('#knowledgeList'), '/api/evidence', loadKnowledge, () => S.activeView === 'knowledge');
  if (!d) return false;
  $('#knowledgeList').innerHTML = d.items.length ? d.items.map(s => `<div class="list-item"><div class="list-main"><a href="#evidence-${esc(s.id)}" data-evidence-ref="${esc(s.id)}">${esc(s.title)}</a><p>${esc(s.kind === 'web' ? '网页' : '知识条目')} · ${esc(trustLabel(s.effective_status || s.status))}</p></div></div>`).join('')
    : emptyState({ icon: 'library', title: '还没有知识条目', desc: '在右栏把可引用的原文片段或网页快照人工确认后，会出现在这里。' });
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

const memoryKindLabels = {style_preference:'表达偏好', term_preference:'术语偏好', research_interest:'研究方向', misc:'其他偏好'};
async function loadMemories() {
  const d = await readPanel($('#memoryList'), '/api/memories', loadMemories, () => S.activeView === 'memories');
  if (!d) return false;
  $('#memoryList').innerHTML = (d.items || []).map(m => `<div class="list-item"><div class="list-main"><strong>${esc(memoryKindLabels[m.key || m.kind] || m.key || m.kind || '其他偏好')}</strong><p>${esc(m.value)}</p></div>
    <button class="btn sm" data-edit-memory="${esc(m.id)}">编辑</button><button class="btn sm" data-delete-memory="${esc(m.id)}">删除</button></div>`).join('')
    || emptyState({ icon: 'settings', title: '还没有研究偏好', desc: '研究方向、术语与表达习惯记在这里；论文事实仍必须走核验知识库。' });
  $$('[data-edit-memory]').forEach(b => b.onclick = () => editMemory(d.items.find(m => m.id === b.dataset.editMemory)));
  $$('[data-delete-memory]').forEach(b => b.onclick = async () => {
    try { await api('/api/memories/' + b.dataset.deleteMemory, { method: 'DELETE' });
      S.deletedMemory = b.dataset.deleteMemory; await loadMemories();
    } catch (error) { toast(error.message, true); }
  });
  if (S.deletedMemory) {
    $('#memoryList').insertAdjacentHTML('afterbegin', '<button class="btn" id="btnUndoMemory">恢复最近删除的偏好</button>');
    $('#btnUndoMemory').onclick = async event => {
      const button=event.currentTarget, id=S.deletedMemory;
      if (button.disabled) return;
      button.disabled=true;
      $('#memoryList [data-memory-action-error]')?.remove();
      try {
        await api('/api/memories/'+encodeURIComponent(id)+'/restore',{method:'POST'});
        if (S.deletedMemory===id) S.deletedMemory=null;
        if (S.activeView==='memories') await loadMemories();
      } catch (error) {
        if (button.isConnected && S.activeView==='memories') {
          const message=document.createElement('p');message.dataset.memoryActionError='';message.className='task-error';message.setAttribute('role','alert');message.textContent='恢复未完成：'+error.message;button.after(message);
        } else toast('偏好恢复未完成：'+error.message,true);
      } finally {if(button.isConnected)button.disabled=false;}
    };
  }
}

function editMemory(memory = {}) {
  workflowDialog(memory.id ? '编辑偏好' : '添加偏好', `<label>类型<select class="select" name="kind">${['style_preference','term_preference','research_interest'].map(v => `<option value="${v}" ${memory.kind === v ? 'selected' : ''}>${memoryKindLabels[v]}</option>`).join('')}</select></label>
    <label>内容<textarea class="textarea" name="value" required>${esc(memory.value || '')}</textarea></label>`, async values => {
    const r = await api('/api/memories' + (memory.id ? '/' + memory.id : ''), { method: memory.id ? 'PATCH' : 'POST', body: values });
    if (r.action === 'rejected') throw new Error(r.reason);
    await loadMemories();
  }, '保存偏好');
}

async function loadTrash() {
  const data = await readPanel($('#trashList'), '/api/trash', loadTrash, () => S.activeView === 'trash');
  if (!data) return false;
  $('#trashList').innerHTML = (data.items || []).map(p => `<div class="list-item"><div class="list-main">${esc(p.display_title || p.title)}<p class="muted">${esc(p.deleted_at)}</p></div><button class="btn" data-restore-paper="${esc(p.id)}">恢复文献</button></div>`).join('')
    || emptyState({ icon: 'trash', title: '回收站为空', desc: '删除的文献会在这里保留至少 30 天，恢复前不参与检索。' });
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
    workflowDialog('编辑分组', `<label>分组名称<input class="input" name="name" value="${esc(c.name)}" required></label><label>父分组<select class="select" name="parent_id"><option value="">顶层</option>${S.collections.filter(v => !descendants.includes(v.id)).map(v => `<option value="${esc(v.id)}" ${v.id === c.parent_id ? 'selected' : ''}>${esc(collectionLabel(v.id))}</option>`).join('')}</select></label>`, async v => { await api('/api/collections/' + c.id, { method: 'PATCH', body: v }); await refreshCollections(); await refreshLibrary(); });
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
  const ids = [...S.selectedPapers];
  const titles = new Map(ids.map(id => [id, S.papers.find(p=>p.id===id)?.display_title || S.papers.find(p=>p.id===id)?.title || id]));
  const completed = new Set(), failures = new Map();
  let running=false, stopped=false, target;
  const dialog = workflowDialog('移动到分组', `<p>本次选择 ${ids.length} 篇文献。</p><label>目标分组<select class="select" name="collection_id"><option value="">未分组</option>${S.collections.map(c => `<option value="${esc(c.id)}">${esc(collectionLabel(c.id))}</option>`).join('')}</select></label><div data-move-result tabindex="-1" hidden></div>`, async values => {
    target ??= values.collection_id;
    running=true; stopped=false; failures.clear();
    const select = $('[name=collection_id]', dialog), result = $('[data-move-result]', dialog);
    const cancel = $('footer [data-cancel]', dialog), submit = $('[type=submit]', dialog);
    select.disabled=true; cancel.textContent='停止后续移动';
    $$('[data-cancel]', dialog).forEach(b=>{b.disabled=false;});
    result.hidden=false; result.setAttribute('role','status'); result.textContent='正在移动；停止后续处理不会撤销已完成项。';
    for (const id of ids.filter(id=>!completed.has(id))) {
      if (stopped) break;
      try { await api('/api/paper/'+encodeURIComponent(id),{method:'PATCH',body:{collection_id:target || null}}); completed.add(id); }
      catch (error) {
        // A failed response may follow a committed write; reconcile before offering a retry.
        try {
          const actual=await api('/api/paper/'+encodeURIComponent(id));
          if ((actual.paper.collection_id || '')===target) {completed.add(id);continue;}
        } catch (_) { /* Keep this item unresolved and visible. */ }
        failures.set(id,error.message);
      }
    }
    let refreshError='';
    try {
      await refreshLibrary();
      if ([...completed].some(id=>(S.papers.find(p=>p.id===id)?.collection_id || '')!==target)) throw new Error('列表尚未同步');
    } catch (error) { refreshError='移动处理已结束，但列表刷新未完成：'+error.message; }
    running=false;
    $$('[data-cancel]', dialog).forEach(b=>{b.disabled=false;}); cancel.textContent='关闭';
    const pending=ids.filter(id=>!completed.has(id)), unprocessed=pending.filter(id=>!failures.has(id));
    const summary=`已移动 ${completed.size} / ${ids.length} 篇；未完成 ${pending.length} 篇（失败 ${failures.size}，未处理 ${unprocessed.length}）。`;
    result.setAttribute('role',pending.length || refreshError ? 'alert' : 'status');
    result.innerHTML=`<p>${esc(summary)}</p>${stopped ? '<p>已停止后续移动；已完成项不会撤销。</p>' : ''}<ul>${ids.map(id=>`<li>${esc(titles.get(id))}：${completed.has(id)?'已移动':failures.has(id)?'未完成 · '+esc(failures.get(id)):'未处理'}</li>`).join('')}</ul>${refreshError?`<p class="task-error">${esc(refreshError)}</p>`:''}`;
    submit.textContent=pending.length?'重试未完成项':'刷新列表';
    if (pending.length || refreshError) {if(dialog.open)result.focus();return false;}
    toast(summary);
  });
  const stop = () => {
    if (!running) {dialog.close();return;}
    stopped=true;
    $$('[data-cancel]', dialog).forEach(b=>{b.disabled=true;});
    $('[data-move-result]', dialog).textContent='正在等待当前移动返回；随后停止，已完成项不会撤销。';
  };
  $$('[data-cancel]', dialog).forEach(b=>{b.onclick=stop;});
  dialog.addEventListener('cancel',event=>{if(running){event.preventDefault();stop();}});
};
$('#btnPaperExport').onclick = () => {
  const paperIds = [...S.selectedPapers];
  if (!paperIds.length) return toast('请先勾选文献');
  workflowDialog('导出文献', `<p class="source-intro">导出本次选择的 ${paperIds.length} 篇文献。仅接受核验可引用的文献。</p><label>导出格式<select class="select" name="format"><option value="bibtex">BibTeX</option><option value="ris">RIS</option><option value="json">JSON</option><option value="markdown">Markdown 解读与引用</option></select></label>`, async values => {
    const extensions = { bibtex: 'bib', ris: 'ris', json: 'json', markdown: 'md' };
    await downloadApi('/api/library/export', 'litprism.' + extensions[values.format], { format: values.format, paper_ids: paperIds });
  }, '下载');
};
document.addEventListener('dragover', e => { if (e.dataTransfer?.types.includes('Files')) e.preventDefault(); });
document.addEventListener('drop', e => {
  if (!e.dataTransfer?.files.length) return;
  e.preventDefault(); if (openView('library')) importFiles([...e.dataTransfer.files]);
});
refreshCollections().catch(e => toast(e.message, true));


/* ---------------------------------------------------------------------------
 * 候选池：发现结果的留档区，与知识库双向对账
 *
 * 刻意不做的事：这里没有"自动去出版商页面抓 PDF"。只有 arXiv 能免费直连，
 * 其余来源一律引导用户手动补充/手动导入 —— 版权与登录问题不在这层解决。
 * ------------------------------------------------------------------------ */
const POOL_FILTERS = { open: '待处理', library: '已在库', all: '全部' };

/**
 * 批量动作都依赖「已选」。未选中时全部禁用 —— 否则点了没有任何反应，
 * 用户会以为功能坏了。对比需要至少两篇。
 */
function syncBatchActions() {
  const none = S.selectedPapers.size === 0;
  ['#btnMovePapers', '#btnBatchDelete', '#btnPaperExport', '#btnBatchVerify'].forEach(sel => {
    const el = $(sel);
    if (el) el.disabled = none;
  });
  const cmp = $('#btnCompareSelected');
  if (cmp) cmp.disabled = S.selectedPapers.size < 2;
}

function poolStatusChip(item) {
  if (item.in_library) return '<span class="ev-claim-chip ok">已在库</span>';
  if (item.status === 'ignored') return '<span class="ev-claim-chip">已忽略</span>';
  if (item.status === 'rejected') return '<span class="ev-claim-chip warn">已拒绝</span>';
  return '<span class="ev-claim-chip">待处理</span>';
}

function poolArxivId(item) {
  const raw = `${item.arxiv_id || ''} ${item.url || ''} ${item.doi || ''}`;
  const m = /(\d{4}\.\d{4,5}(?:v\d+)?)/.exec(raw.replace(/10\.48550\/arxiv\.?/i, ''));
  return m ? m[1] : '';
}

function poolRow(item) {
  const arxiv = poolArxivId(item);
  const meta = [item.year, item.source, item.doi ? 'DOI ' + item.doi : '', arxiv ? 'arXiv ' + arxiv : '']
    .filter(Boolean).map(esc).join(' · ');
  const title = item.in_library && item.library_paper_id
    ? `<a href="#paper-${esc(item.library_paper_id)}" data-pool-open="${esc(item.library_paper_id)}">${esc(item.title)}</a>`
    : `<a href="${esc(item.url || '#')}" target="_blank" rel="noreferrer">${esc(item.title)}</a>`;
  const acts = [];
  if (!item.in_library && arxiv) acts.push(`<button class="btn sm primary" data-pool-fetch="${esc(item.id)}">取全文</button>`);
  // 非 arXiv 的论文只能导题录；网页候选不冒充论文，保持忽略/拒绝
  if (!item.in_library && !arxiv && item.kind === 'paper') acts.push(`<button class="btn sm" data-pool-import="${esc(item.id)}">导入题录</button>`);
  if (item.in_library && item.library_paper_id) acts.push(`<button class="btn sm" data-pool-open="${esc(item.library_paper_id)}">打开论文</button>`);
  if (!item.in_library && item.status === 'new') {
    acts.push(`<button class="btn sm" data-pool-ignore="${esc(item.id)}">忽略</button>`);
    acts.push(`<button class="btn sm" data-pool-reject="${esc(item.id)}">拒绝</button>`);
  }
  return `<li class="pool-row">
    <div class="pool-main">
      <p class="pool-title">${title}</p>
      <p class="pool-meta">${meta || '无元数据'}</p>
      ${item.query && item.query !== '手动补充' ? `<p class="pool-meta">来自检索「${esc(item.query)}」</p>` : ''}
    </div>
    <div class="pool-side">${poolStatusChip(item)}<div class="pool-actions">${acts.join('')}</div></div>
  </li>`;
}

let candidateRequest = 0;
async function loadCandidates() {
  const request = ++candidateRequest;
  const box = $('#candidatePool');
  if (!box) return;
  const filter = S.poolFilter || 'open';
  const isCurrent = () => request === candidateRequest && filter === (S.poolFilter || 'open');
  box.innerHTML = `<div class="pool-head"><h2>候选池</h2><p class="pool-sub">检索到的东西先落在这里；arXiv 可直接取全文，其余请手动导入。</p></div>
    <p class="pool-meta" role="status">正在读取候选…</p>`;
  const query = filter === 'open' ? '?hide_in_library=1&status=new'
    : filter === 'library' ? '?status=imported' : '?limit=100';
  let data;
  try { data = await api('/api/candidates' + query); }
  catch (error) {
    if (!isCurrent()) return;
    box.innerHTML = `<div class="pool-head"><h2>候选池</h2></div>
      <p class="lui-degraded">候选读取失败：${esc(error.message)}</p>`;
    return;
  }
  if (!isCurrent()) return;
  const items = data.items || [];
  const groups = new Map();
  items.forEach(i => { const k = i.query || '（无查询）'; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(i); });
  const list = items.length
    ? `<ul class="pool-list">${[...groups].map(([q, rows]) => `
        ${filter === 'all' ? `<li class="pool-group">${esc(q)}<span>${rows.length}</span></li>` : ''}
        ${rows.map(poolRow).join('')}`).join('')}</ul>`
    : emptyState({
        icon: 'search',
        title: filter === 'library' ? '还没有已入库的候选' : '候选池为空',
        desc: filter === 'library'
          ? '导入论文后点「与知识库核对」，这里会自动更新。'
          : '用上方检索获取候选，或手动补充一条题录。',
      });
  box.innerHTML = `<div class="pool-head">
      <h2>候选池<span class="pool-count">${items.length}</span></h2>
      <div class="pool-tools">
        <div class="pool-seg" role="group" aria-label="筛选候选">
          ${Object.entries(POOL_FILTERS).map(([k, label]) =>
            `<button data-pool-filter="${k}" class="${filter === k ? 'active' : ''}">${label}</button>`).join('')}
        </div>
        <div class="pool-actions">
          <button class="btn sm" data-pool-sync>与知识库核对</button>
          <button class="btn sm" data-pool-add>手动补充</button>
        </div>
      </div>
    </div>
    <form class="pool-form" id="poolAddForm" hidden>
      <label>标题<input class="input" name="title" required maxlength="500" placeholder="论文或网页标题"></label>
      <label>arXiv ID<input class="input" name="arxiv_id" placeholder="1607.01759，可留空"></label>
      <label>链接<input class="input" name="url" placeholder="https://…"></label>
      <div class="row"><button class="btn primary sm">加入候选</button><button class="btn sm" type="button" data-pool-cancel>取消</button></div>
      <p class="pool-hint">只有带 arXiv ID 的候选能自动取全文；其他来源请手动下载后在知识库导入。</p>
      <p class="task-error" role="alert"></p>
    </form>
    ${list}`;

  $$('[data-pool-filter]', box).forEach(b => b.onclick = () => { S.poolFilter = b.dataset.poolFilter; loadCandidates(); });
  $('[data-pool-add]', box).onclick = () => { $('#poolAddForm', box).hidden = false; $('input[name=title]', box).focus(); };
  $('[data-pool-cancel]', box).onclick = () => { $('#poolAddForm', box).hidden = true; };
  $$('[data-pool-open]', box).forEach(b => b.onclick = ev => {
    ev.preventDefault(); openView('paper', { paperId: b.dataset.poolOpen });
  });
  $$('[data-pool-ignore]', box).forEach(b => b.onclick = () => poolAct(b, `/api/candidates/${encodeURIComponent(b.dataset.poolIgnore)}/status`, { status: 'ignored' }));
  $$('[data-pool-reject]', box).forEach(b => b.onclick = () => poolAct(b, `/api/candidates/${encodeURIComponent(b.dataset.poolReject)}/status`, { status: 'rejected' }));
  $$('[data-pool-fetch]', box).forEach(b => b.onclick = () => poolAct(b, `/api/candidates/${encodeURIComponent(b.dataset.poolFetch)}/fetch`, {}, '正在下载并解析…'));
  $$('[data-pool-import]', box).forEach(b => b.onclick = () => poolAct(b, `/api/candidates/${encodeURIComponent(b.dataset.poolImport)}/import`, {}, '正在核验并导入…'));
  $('[data-pool-sync]', box).onclick = async ev => {
    const btn = ev.currentTarget; btn.disabled = true;
    try {
      const r = await api('/api/candidates/sync', { method: 'POST' });
      toast(r.linked ? `已把 ${r.linked} 条候选标记为已在库` : '没有新的已在库候选');
      await loadCandidates();
    } catch (error) { toast(error.message, true); btn.disabled = false; }
  };
  const form = $('#poolAddForm', box);
  form.onsubmit = async ev => {
    ev.preventDefault();
    const values = Object.fromEntries(new FormData(form));
    const btn = $('button.primary', form); btn.disabled = true;
    try {
      const r = await api('/api/candidates', { method: 'POST', body: values });
      toast(r.created ? '已加入候选' : '这条候选已存在');
      form.hidden = true; form.reset();
      await loadCandidates();
    } catch (error) { $('.task-error', form).textContent = error.message; btn.disabled = false; }
  };
}

async function poolAct(button, url, body, pending) {
  const row = button.closest('.pool-row');
  if (row.dataset.busy) return;
  row.dataset.busy = 'true';
  const actions = $('.pool-actions', row);
  const buttons = $$('button', actions);
  const disabled = buttons.map(b => b.disabled);
  buttons.forEach(b => { b.disabled = true; });
  $('[data-pool-error]', row)?.remove();
  const status = document.createElement('span');
  status.className = 'pool-pending';
  status.setAttribute('role', 'status');
  status.textContent = pending || '处理中…';
  actions.appendChild(status);
  try {
    const r = await api(url, { method: 'POST', body });
    toast(r && r.paper_id ? (r.material_level ? `已导入（${r.material_level}）` : '已取回全文并入库') : '已更新');
    await loadCandidates();
  } catch (error) {
    const message = document.createElement('p');
    message.className = 'lui-degraded';
    message.dataset.poolError = '';
    message.setAttribute('role', 'alert');
    message.textContent = error.message;
    $('.pool-side', row).appendChild(message);
  } finally {
    status.remove();
    buttons.forEach((b, i) => { b.disabled = disabled[i]; });
    delete row.dataset.busy;
  }
}
