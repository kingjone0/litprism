/* Backup data stays on this machine. All content and paths are escaped. */
const backupState = {preview: null, restored: null, request: 0};

async function renderBackupSettings(box) {
  const request = ++backupState.request;
  box.innerHTML = '<p role="status">正在读取备份状态…</p>';
  const loading = box.firstElementChild;
  try {
    const state = await api('/api/backups/status');
    if (request !== backupState.request || box !== $('#settingsPanes') || box.firstElementChild !== loading) return;
    box.innerHTML = `<div class="backup-settings">
      <h2>备份与恢复</h2>
      <p class="source-intro">保存文献、材料版本、引用、审核记录、会话、偏好与研究产出。密钥不随备份迁移，恢复后需要重新配置。</p>
      <p class="source-intro">当前数据目录：<span class="backup-path">${esc(state.data_dir)}</span></p>
      <section aria-labelledby="backupExportHeading">
        <h3 id="backupExportHeading">创建备份</h3>
        <label class="row wrap"><input type="checkbox" id="backupOriginals"> 包含托管目录中的原文文件</label>
        <p class="source-intro">不勾选也保留已解析原文和全部引用。软件外部的原始文件、缓存和密钥库不包含在内。</p>
        <button class="btn primary" id="backupExport">下载备份</button>
      </section>
      <section aria-labelledby="backupRestoreHeading">
        <h3 id="backupRestoreHeading">恢复或迁移到新目录</h3>
        <label class="backup-field">选择备份文件<input class="input" type="file" accept=".zip" id="backupFile"></label>
        <div id="backupPreview" class="backup-preview" aria-live="polite"></div>
        <label class="backup-field">新数据目录<input class="input" id="backupTarget" type="text" placeholder="填写一个尚不存在的目录绝对路径"></label>
        <p class="source-intro">父目录须已存在。恢复会先校验，再写入新目录；原目录保留，不会被覆盖。</p>
        <div class="row wrap">
          <button class="btn primary" id="backupRestore" disabled>恢复备份到新目录</button>
          <button class="btn" id="backupMigrate">复制当前资料到新目录</button>
        </div>
      </section>
      <section aria-labelledby="backupSwitchHeading">
        <h3 id="backupSwitchHeading">选择何时切换</h3>
        <div id="backupRestored" class="source-intro" aria-live="polite"></div>
        <p class="source-intro">切换同时恢复备份中的非密钥设置。重启前仍使用原目录；安排切换后暂停修改数据，可在重启前取消。</p>
        <div class="row wrap">
          <button class="btn" id="backupActivate" disabled>重启后使用恢复目录</button>
          <button class="btn" id="backupCancel" ${state.requires_restart ? '' : 'hidden'}>取消目录切换</button>
        </div>
      </section>
      <p id="backupStatus" role="status" aria-live="polite"></p>
      <p id="backupError" class="task-error" role="alert"></p>
    </div>`;
    const root = box.querySelector('.backup-settings');
    const error = message => { $('#backupError', root).textContent = message || ''; };
    const note = message => { $('#backupStatus', root).textContent = message || ''; };
    const current = () => root === box.querySelector('.backup-settings');
    const busy = async (button, text, fn) => {
      error(''); button.disabled = true; note(text);
      try { await fn(); }
      catch (e) { error(e.message); note('操作未完成，原目录仍保留。'); }
      finally { if (current()) button.disabled = button.id === 'backupRestore' && !backupState.preview; }
    };
    function preview() {
      const manifest = backupState.preview?.manifest;
      const tables = manifest?.content?.database?.tables;
      $('#backupPreview', root).innerHTML = tables ? `<p>校验通过 · ${esc(manifest.created_at)}</p>
        <p>${fmtNum(tables.papers?.rows || 0)} 篇文献 · ${fmtNum(tables.materials?.rows || 0)} 份材料 · ${fmtNum(tables.chunks?.rows || 0)} 个原文块</p>
        <p>${fmtNum(tables.semantic_reviews?.rows || 0)} 条陈述审核 · ${fmtNum(tables.manual_claims?.rows || 0)} 条人工补录</p>
        <p>${manifest.content.include_originals ? '包含托管原文文件 '+Object.keys(manifest.content.files || {}).filter(n => n.startsWith('papers/')).length+' 个' : '未包含原始文件；已解析原文与引用仍保留'}。密钥不包含在内。</p>` : '<p>选择文件后先校验并预览，不会立即恢复或切换。</p>';
      $('#backupRestore', root).disabled = !manifest;
    }
    function restored() {
      const target = state.pending_directory || backupState.restored?.data_dir;
      $('#backupRestored', root).innerHTML = target
        ? `${state.requires_restart ? '已安排重启后切换：' : '已校验并保存，可安排切换：'}<span class="backup-path">${esc(target)}</span>`
        : '尚未恢复或复制资料。';
      $('#backupActivate', root).disabled = !target || state.requires_restart;
    }
    preview(); restored();
    if (state.requires_restart) {
      note('目录切换待重启生效。当前资料仍在原目录，请重启或取消切换。');
      for (const id of ['backupExport', 'backupFile', 'backupRestore', 'backupMigrate']) $('#'+id, root).disabled = true;
    }
    $('#backupExport', root).onclick = e => busy(e.currentTarget, '正在创建一致备份…', async () => {
      await downloadApi('/api/backups/export', 'litprism-backup.zip', {include_originals: $('#backupOriginals', root).checked});
      note('备份已下载。建议将文件另存到其他磁盘。');
    });
    $('#backupFile', root).onchange = async e => {
      const file = e.target.files?.[0];
      const previousToken = backupState.preview?.token;
      backupState.preview = null; preview(); error('');
      if (previousToken) {
        try { await api('/api/backups/previews/'+encodeURIComponent(previousToken), {method:'DELETE'}); }
        catch (e) { if (current()) error(e.message); }
      }
      if (!file) return;
      const input = e.target;
      input.disabled = true; note('正在上传并校验备份…');
      try {
        const form = new FormData(); form.append('file', file);
        const env = apiEnv();
        const response = await fetch((env.base || '')+'/api/backups/inspect', {
          method:'POST', headers: env.token ? {'X-LitPrism-Token':env.token} : {}, body:form,
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || result.error || '备份校验失败');
        backupState.preview = result;
        if (current()) { preview(); note('校验通过。填写新目录后可恢复。'); }
      } catch (e) { if (current()) { error(e.message); note('备份未恢复，原目录未修改。'); } }
      finally { if (current()) input.disabled = false; }
    };
    const prepare = (button, mode) => busy(button, mode === 'restore' ? '正在恢复到新目录…' : '正在复制当前资料…', async () => {
      const target = $('#backupTarget', root).value.trim();
      if (!target) throw new Error('请填写新数据目录的绝对路径。');
      const body = mode === 'restore' ? {target, token:backupState.preview?.token} : {target};
      backupState.restored = await api('/api/backups/'+mode, {method:'POST', body});
      if (mode === 'restore') { backupState.preview = null; preview(); }
      restored(); note('新目录已校验并保存；当前仍使用原目录，尚未切换。');
    });
    $('#backupRestore', root).onclick = e => prepare(e.currentTarget, 'restore');
    $('#backupMigrate', root).onclick = e => prepare(e.currentTarget, 'migrate');
    $('#backupActivate', root).onclick = e => busy(e.currentTarget, '正在保存目录切换…', async () => {
      await api('/api/backups/activate', {method:'POST', body:{target:backupState.restored?.data_dir}});
      if (current()) await renderBackupSettings(box);
    });
    $('#backupCancel', root).onclick = e => busy(e.currentTarget, '正在取消切换…', async () => {
      await api('/api/backups/cancel-switch', {method:'POST'});
      if (current()) await renderBackupSettings(box);
    });
  } catch (e) {
    if (request === backupState.request && box.firstElementChild === loading) box.innerHTML = `<p role="alert">备份页面加载失败：${esc(e.message)}</p>`;
  }
}
