/** N10: real local API + headless Chromium; no remote requests or paid models. */
const { chromium, expect } = require('playwright/test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const base = process.env.LITPRISM_ACCEPTANCE_URL;
  const output = process.env.LITPRISM_UI_OUTPUT;
  if (!base || !output) throw new Error('Use check_workflows.py --suite n10 with LITPRISM_UI_OUTPUT');
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ channel: 'chrome', headless: true,
    args: ['--disable-gpu', '--disable-gpu-compositing', '--no-sandbox'] });
  const results = [], errors = [], external = [];
  const page = await browser.newPage({ viewport: { width: 1360, height: 880 } });
  page.setDefaultTimeout(5000);
  page.on('pageerror', e => errors.push(e.message));
  page.on('dialog', d => d.accept());
  await page.route('**/*', route => {
    if (new URL(route.request().url()).origin === base) return route.continue();
    external.push(route.request().url()); return route.abort();
  });
  const call = (url, method = 'GET', body) => page.evaluate(
    ({ url, method, body }) => api(url, { method, body }), { url, method, body });
  let resetId = 0;
  const reset = async () => {
    await page.setViewportSize({ width: 1360, height: 880 });
    await page.goto(base + '/?n10=' + (++resetId) + '#t=acceptance-only');
    await expect(page.locator('#paperList [data-paper]').first()).toBeVisible();
    // Each case owns its panel baseline; persistence is tested inside its own case.
    await page.evaluate(() => { toggleInspector(false); S.sidebar = true; applySidebar(); });
  };
  const check = async (name, fn) => {
    try { await reset(); await fn(); results.push({ name, passed: true }); }
    catch (e) { results.push({ name, passed: false, error: e.message }); }
  };
  const delayed = async (url, match = () => true, failure = null) => {
    let release, started, used = false;
    const gate = new Promise(resolve => { release = resolve; });
    const ready = new Promise(resolve => { started = resolve; });
    const handler = async route => {
      if (used || !match(route.request())) return route.continue();
      used = true;
      const response = failure ? null : await route.fetch();
      started(); await gate;
      await route.fulfill(failure ? { status: 503, contentType: 'application/json', body: JSON.stringify({ detail: failure }) } : { response });
    };
    await page.route(url, handler);
    return { get ready() { return new Promise((resolve,reject) => {
      const timer=setTimeout(()=>reject(new Error('Expected delayed request did not start: '+url)),10000);
      ready.then(()=>{clearTimeout(timer);resolve();});
    }); }, release, close: async () => { release(); await page.unroute(url, handler); } };
  };
  try {
    await check('settings-keyboard-modal-and-return-focus', async () => {
      await page.locator('[data-view=settings]').click();
      assert(await page.evaluate(() => document.querySelector('#settingsModal').contains(document.activeElement)), 'Focus must enter settings');
      for (let i = 0; i < 24; i++) {
        await page.keyboard.press(i % 3 === 0 ? 'Shift+Tab' : 'Tab');
        assert(await page.evaluate(() => document.querySelector('#settingsModal').contains(document.activeElement)), 'Tab escaped settings');
      }
      await page.keyboard.press('Control+k');
      assert(await page.evaluate(() => document.querySelector('#settingsModal').contains(document.activeElement)), 'Global search stole modal focus');
      await page.keyboard.press('Escape');
      await expect(page.locator('#settingsModal')).not.toBeVisible();
      await expect(page.locator('[data-view=settings]')).toBeFocused();
    });
    await check('escape-closes-only-top-dialog', async () => {
      await page.locator('#btnInspector').click();
      await page.locator('#btnCollection').click();
      await expect(page.locator('#researchTask')).toBeVisible();
      await page.keyboard.press('Escape');
      await expect(page.locator('#researchTask')).toHaveCount(0);
      await expect(page.locator('#btnInspector')).toHaveAttribute('aria-expanded', 'true');
      await expect(page.locator('#btnCollection')).toBeFocused();
    });
    await check('filter-removes-hidden-batch-selections', async () => {
      await page.locator('[data-select-paper]').first().check();
      await page.locator('#libSearch').fill('no-such-paper-n10');
      await expect(page.locator('[data-select-paper]')).toHaveCount(0);
      await expect(page.locator('#btnBatchDelete')).toBeDisabled();
      await expect(page.locator('#btnPaperExport')).toBeDisabled();
      await page.locator('#libSearch').fill('');
      await expect(page.locator('[data-select-paper]').first()).not.toBeChecked();
    });
    await check('export-dialog-never-expands-after-selection-refresh', async () => {
      const checkbox = page.locator('[data-select-paper]').first();
      const pid = await checkbox.getAttribute('data-select-paper');
      await checkbox.check();
      await page.locator('#btnPaperExport').click();
      // Simulate a background refresh while this native modal protects interaction.
      await page.evaluate(() => S.selectedPapers.clear());
      const [request] = await Promise.all([
        page.waitForRequest(r => r.url().endsWith('/api/library/export')),
        page.locator('#researchTask [type=submit]').click(),
      ]);
      assert.deepEqual(request.postDataJSON().paper_ids, [pid], 'Export expanded beyond dialog selection');
      await expect(page.locator('#researchTask')).toHaveCount(0);
    });
    await check('library-refresh-invalidates-comparison', async () => {
      await page.evaluate(async () => {
        S.compareKey = 'old'; S.comparison = { answer: 'old' }; S.compareSources = [{ paper_id: 'old' }];
        await refreshLibrary();
      });
      const state = await page.evaluate(() => ({ key: S.compareKey, result: S.comparison, sources: S.compareSources }));
      assert(!state.key && !state.result && !state.sources.length, 'Old comparison remained cached after library refresh');
    });
    await check('late-comparison-cannot-repopulate-invalidated-cache', async () => {
      const state = await page.evaluate(async () => {
        const original = runTask;
        let finish;
        S.selectedPapers = new Set(['fixture-a', 'fixture-b']);
        S.compareKey = null;
        runTask = () => new Promise(resolve => { finish = resolve; });
        try {
          const pending = loadComparison();
          await refreshLibrary();
          finish({ sources: [], data: { old: true } });
          await pending;
          return { key: S.compareKey, result: S.comparison };
        } finally { runTask = original; }
      });
      assert(!state.key && !state.result, 'Late old result restored stale cache');
    });
    await check('changed-selection-rejects-late-comparison', async () => {
      const result = await page.evaluate(async () => {
        const original = runTask;
        let finish;
        S.selectedPapers = new Set(['fixture-a', 'fixture-b']);
        S.compareKey = null;
        runTask = () => new Promise(resolve => { finish = resolve; });
        try {
          const pending = loadComparison();
          S.selectedPapers = new Set(['fixture-a', 'fixture-c']);
          finish({ sources: [], data: { old: true } });
          await pending;
          return S.comparison;
        } finally { runTask = original; }
      });
      assert(!result, 'Old selection result remained available');
    });
    await check('groups-drag-move-order-and-keyboard-alternative', async () => {
      const a = await call('/api/collections', 'POST', { name: 'N10-A' });
      const b = await call('/api/collections', 'POST', { name: 'N10-B', parent_id: a.id });
      await page.evaluate(async () => { await refreshCollections(); });
      const pid = await page.locator('[data-select-paper]').first().getAttribute('data-select-paper');
      await page.locator(`[data-drag-paper="${pid}"]`).dragTo(page.locator(`[data-group-chip="${b.id}"]`));
      await expect.poll(async () => (await call('/api/paper/' + pid)).paper.collection_id).toBe(b.id);
      await page.locator('#collectionFilter').selectOption(a.id);
      await expect(page.locator(`[data-select-paper="${pid}"]`)).toBeVisible();
      await page.locator(`[data-group-chip="${b.id}"]`).dragTo(page.locator(`[data-group-chip="${a.id}"]`));
      await expect.poll(async () => (await call('/api/collections')).items.map(c => c.id).slice(0, 2)).toEqual([b.id, a.id]);
      await page.locator(`[data-select-paper="${pid}"]`).check();
      await page.locator('#btnMovePapers').focus();
      await page.keyboard.press('Enter');
      await page.locator('#researchTask select').selectOption('');
      await page.locator('#researchTask [type=submit]').click();
      await expect(page.locator('#researchTask')).toHaveCount(0);
      await expect(page.locator('[data-select-paper]')).toHaveCount(0);
      await expect(page.locator('#btnBatchDelete')).toBeDisabled();
      await call('/api/collections/' + a.id, 'DELETE');
      await page.evaluate(async () => { await refreshCollections(); await refreshLibrary(); });
      await expect(page.locator('#collectionFilter')).toHaveValue('');
      assert.equal((await call('/api/collections')).items.find(c => c.id === b.id).parent_id, null);
      await call('/api/collections/' + b.id, 'DELETE');
    });
    await check('recycle-restore-search-and-material-identity', async () => {
      const pid = process.env.LITPRISM_ACCEPTANCE_SEED;
      const before = await call('/api/paper/' + pid + '/text');
      await page.locator(`[data-select-paper="${pid}"]`).check();
      await page.locator('#btnBatchDelete').click();
      await expect(page.locator(`[data-select-paper="${pid}"]`)).toHaveCount(0);
      await expect(page.locator('#btnBatchDelete')).toBeDisabled();
      const found = await call('/api/search', 'POST', { query: 'alphatopic', external: false });
      assert(!found.local.some(r => r.paper_id === pid), 'Recycled paper remained in search');
      await page.locator('[data-view=trash]').click();
      await page.locator(`[data-restore-paper="${pid}"]`).click();
      await expect(page.locator(`[data-restore-paper="${pid}"]`)).toHaveCount(0);
      await page.locator('.workspace-nav [data-view=library]').click();
      await expect(page.locator(`[data-select-paper="${pid}"]`)).toBeVisible();
      assert.deepEqual(await call('/api/paper/' + pid + '/text'), before, 'Restore changed original material');
      const restored = await call('/api/search', 'POST', { query: 'alphatopic', external: false });
      assert(restored.local.some(r => r.paper_id === pid), 'Restored paper absent from search');
    });
    await check('settings-control-names-and-visible-save-feedback', async () => {
      await page.locator('[data-view=settings]').click();
      await expect(page.getByRole('combobox', { name: '主题', exact: true })).toBeVisible();
      for (const section of ['sources', 'verify', 'retrieval', 'export']) {
        await page.locator(`[data-set="${section}"]`).click();
        const unnamed = await page.locator('#settingsPanes').evaluate(root =>
          [...root.querySelectorAll('input, select')].filter(e => !e.disabled && !e.getAttribute('aria-label')
            && !e.getAttribute('aria-labelledby') && !e.labels.length).length);
        assert.equal(unnamed, 0, section + ' has unnamed settings');
      }
      await page.locator('[data-set=sources]').click();
      await expect(page.getByRole('checkbox', { name: 'Crossref：启用', exact: true })).toBeVisible();
      await expect(page.getByRole('textbox', { name: 'Crossref 联系邮箱', exact: true })).toBeVisible();
      await page.locator('[data-set=retrieval]').click();
      const field = page.getByRole('spinbutton', { name: '返回片段数', exact: true });
      const before = (await call('/api/status')).config.retrieval.top_k;
      for (const [width, theme] of [[1360, 'light'], [390, 'dark']]) {
        await page.setViewportSize({ width, height: 844 });
        await page.evaluate(t => { S.theme = t; applyTheme(); }, theme);
        await page.locator('label', { hasText: '返回片段数' }).click();
        await expect(field).toBeFocused();
        await field.fill('0');
        const failed = page.waitForResponse(r => r.url().endsWith('/api/config') && r.request().method() === 'PATCH');
        await page.locator('#btnSaveCfg').click();
        assert.equal((await failed).status(), 400);
        await expect(page.locator('#settingsSaveError')).toContainText('返回片段数必须在 1–50 之间');
        await expect(field).toHaveValue('0');
        await expect(field).toHaveAttribute('aria-invalid', 'true');
        await expect(field).toHaveAttribute('aria-describedby', /settingsSaveError/);
        assert.equal((await call('/api/status')).config.retrieval.top_k, before, 'Rejected config changed saved value');
        const visible = await page.locator('#settingsSaveError').evaluate(e => {
          const r = e.getBoundingClientRect(), pane = document.querySelector('#settingsPanes').getBoundingClientRect();
          return !!e.closest('dialog[open]') && r.y >= pane.y && r.bottom <= pane.bottom + 1;
        });
        assert(visible, 'Save error must be readable inside the active dialog');
        await page.screenshot({ path: path.join(output, `save-error-${width}-${theme}.png`) });
        await field.fill(String(before === 9 ? 8 : 9));
        const saved = page.waitForResponse(r => r.url().endsWith('/api/config') && r.request().method() === 'PATCH');
        await page.locator('#btnSaveCfg').click();
        assert.equal((await saved).status(), 200);
        await expect(page.locator('#settingsSaveStatus')).toHaveText('已保存');
        await expect(page.locator('#settingsSaveError')).toBeEmpty();
        await expect(field).not.toHaveAttribute('aria-invalid');
        assert.equal((await call('/api/status')).config.retrieval.top_k, before === 9 ? 8 : 9);
        await field.fill(String(before));
        await page.locator('#btnSaveCfg').click();
        await expect.poll(async () => (await call('/api/status')).config.retrieval.top_k).toBe(before);
        await expect(page.locator('#btnSaveCfg')).toBeEnabled();
      }
    });
    await check('empty-verification-guides-input-without-request', async () => {
      await page.locator('[data-view=verify]').first().click();
      let requests = 0;
      const observe = req => { if (req.method() === 'POST' && req.url().endsWith('/api/verify')) requests++; };
      page.on('request', observe);
      try {
        const input = page.getByRole('textbox', { name: '待核验论文标题或 DOI', exact: true });
        await input.fill('   ');
        await page.locator('#btnVerify').click();
        await expect(input).toBeFocused();
        await expect(input).toHaveAttribute('aria-invalid', 'true');
        await expect(page.locator('#verifyInputError')).toContainText('请输入论文标题或 DOI');
        assert.equal(requests, 0, 'Empty verification sent a request');
        await input.fill('验收文献 A');
        await expect(page.locator('#verifyInputError')).toBeEmpty();
        await expect(input).not.toHaveAttribute('aria-invalid');
        const response = page.waitForResponse(r => r.url().endsWith('/api/verify'));
        await input.press('Enter');
        assert.equal((await response).status(), 200);
        assert.equal(requests, 1, 'Corrected verification did not submit exactly once');
      } finally { page.off('request', observe); }
    });
    await check('narrow-short-actions-stay-readable', async () => {
      await page.setViewportSize({ width: 390, height: 844 });
      await page.evaluate(() => { S.sidebar = false; applySidebar(); });
      const oneLine = async selector => {
        const controls = page.locator(selector);
        assert(await controls.count(), 'Missing short action: ' + selector);
        for (const control of await controls.all()) {
          const lines = await control.evaluate(e => {
            const range = document.createRange(); range.selectNodeContents(e);
            return new Set([...range.getClientRects()].filter(r => r.width > 0).map(r => Math.round(r.y))).size;
          });
          assert.equal(lines, 1, 'Short action wraps: ' + await control.innerText());
        }
      };
      for (const theme of ['light', 'dark']) {
        await page.evaluate(t => { S.theme = t; applyTheme(); openView('library'); }, theme);
        await oneLine('.library-tools .btn, .tool-group-label');
        await page.screenshot({ path: path.join(output, `short-actions-library-${theme}.png`) });
        await page.evaluate(() => openView('verify'));
        await oneLine('#btnVerify');
        await page.screenshot({ path: path.join(output, `short-actions-verify-${theme}.png`) });
        await page.evaluate(() => openSettings('model'));
        await expect(page.locator('#pvArea')).toHaveAttribute('aria-busy','false');
        await expect(page.locator('#btnAddProvider')).toBeVisible();
        await oneLine('#btnAddProvider');
        await page.screenshot({ path: path.join(output, `short-actions-model-${theme}.png`) });
        await page.locator('[data-set=export]').click();
        await oneLine('#btnSaveCfg');
        await page.screenshot({ path: path.join(output, `short-actions-export-${theme}.png`) });
        await page.keyboard.press('Escape');
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Page overflows after reflow');
      }
    });
    await check('conversation-drafts-survive-navigation-and-stay-isolated', async () => {
      const a = await call('/api/conversations', 'POST', { title: '草稿 A' });
      const b = await call('/api/conversations', 'POST', { title: '草稿 B' });
      await page.evaluate(async id => { await loadSidebar(); openView('chat', { convId: id }); }, a.id);
      const input = page.locator('#chatInput');
      await expect(input).toBeVisible();
      await input.fill('尚未发送的草稿 A');
      await page.locator('.workspace-nav [data-view=library]').click();
      await page.locator(`[data-conv="${a.id}"]`).click();
      await expect(input).toHaveValue('尚未发送的草稿 A');
      await page.keyboard.press('Control+k');
      await expect(page.locator('#libSearch')).toBeFocused();
      await page.locator(`[data-conv="${b.id}"]`).click();
      await expect(input).toHaveValue('');
      await input.fill('尚未发送的草稿 B');
      await page.locator(`[data-conv="${a.id}"]`).click();
      await expect(input).toHaveValue('尚未发送的草稿 A');
      await page.evaluate(() => loadChat());
      await expect(input).toHaveValue('尚未发送的草稿 A');
      await page.screenshot({ path: path.join(output, 'draft-restored-desktop.png') });
      await input.fill('alphatopic');
      await page.locator('#btnSend').click();
      await expect(page.locator('.from-assistant')).toHaveCount(1);
      await page.locator(`[data-conv="${b.id}"]`).click();
      await expect(input).toHaveValue('尚未发送的草稿 B');
      await page.locator(`[data-conv="${a.id}"]`).click();
      await expect(input).toHaveValue('');
      const endpoint = base + '/api/conversations/' + a.id + '/messages';
      let release, intercepted;
      const started = new Promise(resolve => { intercepted = resolve; });
      const held = new Promise(resolve => { release = resolve; });
      let requests = 0;
      await page.route(endpoint, async route => {
        if (++requests === 1) {
          intercepted(); await held;
          await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'离线夹具：旧读取失败'})});
        } else await route.continue();
      });
      const oldLoad = page.evaluate(() => loadChat());
      try {
        await started;
        await page.evaluate(() => loadChat());
        await input.fill('新读取后的草稿不能被旧错误覆盖');
        release(); await oldLoad;
        await expect(input).toHaveValue('新读取后的草稿不能被旧错误覆盖');
      } finally { release(); await oldLoad; await page.unroute(endpoint); }
    });
    await check('provider-errors-retain-input-and-retry-in-dialog', async () => {
      await page.setViewportSize({ width: 390, height: 844 });
      await page.evaluate(() => { S.sidebar = false; applySidebar(); openSettings('model'); });
      await page.locator('#btnAddProvider').click();
      const add = page.locator('#addPvCard');
      await expect(add.getByRole('combobox', { name: '预设', exact: true })).toBeVisible();
      await expect(add.getByRole('combobox', { name: 'API 协议', exact: true })).toBeVisible();
      await page.locator('#addPvOk').click();
      await expect(add.locator('[data-pv-error]')).toContainText('显示名称不能为空');
      await expect(page.locator('#addPvName')).toHaveAttribute('aria-invalid', 'true');
      await page.locator('#addPvName').fill('交互模型夹具');
      await page.locator('#addPvUrl').fill('invalid-url');
      await page.locator('#addPvOk').click();
      await expect(add.locator('[data-pv-error]')).toContainText('API 地址需为完整');
      await expect(page.locator('#addPvName')).toHaveValue('交互模型夹具');
      await page.screenshot({ path: path.join(output, 'provider-add-error-narrow.png') });
      await page.locator('#addPvUrl').fill('http://127.0.0.1:9/v1');
      await page.locator('#addPvOk').click();
      await expect(add).toHaveCount(0);
      const provider = (await call('/api/providers')).items.find(p => p.name === '交互模型夹具');
      assert(provider && !provider.has_key, 'Fixture provider was not created without a key');
      const card = page.locator(`[data-pv="${provider.id}"]`);
      await expect(card.locator('[data-pv-status]')).toHaveText('已添加');
      await expect(card.getByRole('combobox', { name: 'API 协议', exact: true })).toBeVisible();
      await card.locator('[data-pv-name]').fill('修改后仍需保留');
      let saves = 0;
      const endpoint = base + '/api/providers/' + provider.id;
      await page.route(endpoint, async route => {
        saves++;
        await new Promise(resolve => setTimeout(resolve, 250));
        await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({detail:'离线夹具：保存暂不可用'}) });
      });
      try {
        await card.locator('[data-pv-save]').click();
        await expect(card.locator('[data-pv-save]')).toBeDisabled();
        await card.locator('[data-pv-save]').evaluate(e => e.click());
        await expect(card.locator('[data-pv-error]')).toContainText('保存暂不可用');
        await expect(card.locator('[data-pv-name]')).toHaveValue('修改后仍需保留');
        assert.equal(saves, 1, 'Repeated save sent another request');
        assert.equal((await call('/api/providers')).items.find(p => p.id === provider.id).name, '交互模型夹具');
        await page.screenshot({ path: path.join(output, 'provider-save-error-narrow.png') });
      } finally { await page.unroute(endpoint); }
      await card.locator('[data-pv-save]').click();
      await expect(card.locator('[data-pv-status]')).toHaveText('已保存');
      assert.equal((await call('/api/providers')).items.find(p => p.id === provider.id).name, '修改后仍需保留');
      const modelsEndpoint = endpoint + '/models';
      await page.route(modelsEndpoint, route => route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'离线夹具：模型目录写入失败'})}));
      try {
        await card.locator('[data-pv-name]').fill('分步保存后的名称');
        await card.locator('[data-pv-save]').click();
        await expect(card.locator('[data-pv-error]')).toContainText('提供商设置已保存，后续步骤未完成');
        assert.equal((await call('/api/providers')).items.find(p => p.id === provider.id).name, '分步保存后的名称');
        await expect(card.locator('[data-pv-name]')).toHaveValue('分步保存后的名称');
      } finally { await page.unroute(modelsEndpoint); }
      await card.locator('[data-pv-save]').click();
      await expect(card.locator('[data-pv-status]')).toHaveText('已保存');
      const testEndpoint = endpoint + '/test';
      await page.route(testEndpoint, route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ok:false,message:'离线夹具：连接被拒绝'}) }));
      try {
        await card.locator('[data-pv-test]').click();
        await expect(card.locator('[data-pv-error]')).toContainText('连接失败：离线夹具：连接被拒绝');
        await expect(card.locator('[data-pv-name]')).toHaveValue('分步保存后的名称');
        const visible = await card.locator('[data-pv-error]').evaluate(e => {
          const r=e.getBoundingClientRect(), pane=document.querySelector('#settingsPanes').getBoundingClientRect();
          return !!e.closest('dialog[open]') && r.y >= pane.y && r.bottom <= pane.bottom + 1;
        });
        assert(visible, 'Provider error is outside the visible dialog');
        await page.screenshot({ path: path.join(output, 'provider-test-error-narrow.png') });
      } finally { await page.unroute(testEndpoint); }
    });
    await check('narrow-source-focus-wraps-and-returns-to-trigger', async () => {
      const conversation = await call('/api/conversations', 'POST', { title:'来源键盘反例' });
      await page.evaluate(async id => { await loadSidebar(); openView('chat', {convId:id}); }, conversation.id);
      await expect(page.locator('#chatInput')).toBeVisible();
      await page.locator('#chatInput').fill('alphatopic'); await page.locator('#btnSend').click();
      await expect(page.locator('.from-assistant')).toHaveCount(1);
      await page.setViewportSize({width:390,height:844});
      const source=page.locator('.answer-sources [data-evidence-ref]').first();
      await source.focus(); await page.keyboard.press('Enter');
      await expect(page.locator('#inspSources')).toContainText('We did not use TriviaQA');
      await expect(page.locator('#btnCloseInspector')).toBeFocused();
      assert(await page.locator('.work').evaluate(e=>e.inert), 'Covered workspace remains interactive');
      for (const key of ['Shift+Tab', ...Array(10).fill('Tab')]) {
        await page.keyboard.press(key);
        assert(await page.evaluate(()=>document.querySelector('#inspector').contains(document.activeElement)), 'Focus escaped source overlay');
      }
      await page.keyboard.press('Control+,');
      await expect(page.locator('#settingsModal')).toBeVisible();
      await page.keyboard.press('Escape');
      await expect(page.locator('#btnInspector')).toHaveAttribute('aria-expanded','true');
      await page.keyboard.press('Escape'); await expect(source).toBeFocused();
      await source.click(); await expect(page.locator('#btnCloseInspector')).toBeFocused();
      await page.setViewportSize({width:1360,height:880});
      await expect(page.locator('#inspector')).not.toHaveAttribute('aria-modal');
      assert(!await page.locator('.work').evaluate(e=>e.inert), 'Desktop remains locked');
      await page.locator('#chatInput').focus(); await expect(page.locator('#chatInput')).toBeFocused();
      await page.setViewportSize({width:390,height:844});
      await expect(page.locator('#btnCloseInspector')).toBeFocused();
      await page.screenshot({path:path.join(output,'source-focus-narrow.png')});
      await page.locator('#btnCloseInspector').click(); await expect(source).toBeFocused();
    });
    await check('chat-receipts-collapse-normal-generation-and-success-but-preserve-manual-choice', async () => {
      const conversation = await call('/api/conversations', 'POST', {title:'记录展开反例'});
      await page.evaluate(async id=>{await loadSidebar();openView('chat',{convId:id});},conversation.id);
      await expect(page.locator('#chatInput')).toBeVisible();
      await page.locator('#chatInput').fill('alphatopic'); await page.locator('#btnSend').click();
      await expect(page.locator('.from-assistant')).toHaveCount(1);
      const panel=page.locator('#chatRunPanel');
      await expect(panel).not.toHaveAttribute('open');
      await expect(page.locator('#btnSend')).toBeEnabled();
      await page.locator('#chatInput').fill('alphatopic durablereloadmarker'); await page.locator('#btnSend').click();
      await expect(panel).not.toHaveAttribute('open');
      await page.locator('#chatRunPanel > summary').click();
      await expect(page.locator('.from-assistant')).toHaveCount(2);
      await expect(page.locator('#btnSend')).toBeEnabled();
      await expect(panel).toHaveAttribute('open','');
      await page.evaluate(()=>loadChat()); await expect(panel).toHaveAttribute('open','');
      await page.locator('#chatRunPanel > summary').click();
      await page.locator('#chatInput').fill('alphatopic durableretrymarker'); await page.locator('#btnSend').click();
      await expect(page.locator('#chatStream')).toContainText('回答失败');
      await expect(panel).toHaveAttribute('open','');
      await expect(page.locator('[data-chat-resume][data-retry=true]')).toBeVisible();
      await expect(panel).toContainText('重试可能再次计费');
      await page.screenshot({path:path.join(output,'receipt-failure-visible.png')});
    });
    await check('candidate-failure-can-retry-existing-actions', async () => {
      await page.locator('[data-view=recommend]').first().click();
      for (const [action, expected, width, theme] of [['ignore','ignored',1360,'light'],['reject','rejected',390,'dark']]) {
        await page.setViewportSize({width,height:844});
        await page.evaluate(t=>{S.sidebar=false;applySidebar();S.theme=t;applyTheme();},theme);
        const candidate=await call('/api/candidates','POST',{title:'候选恢复 '+action,url:'https://example.org/retry-'+action});
        await page.evaluate(()=>{S.poolFilter='open';return loadCandidates();});
        const button=page.locator(`[data-pool-${action}="${candidate.id}"]`);
        const row=page.locator('.pool-row').filter({has:button});
        const endpoint=base+'/api/candidates/'+candidate.id+'/status';let attempts=0;
        await page.route(endpoint,async route=>{
          if (++attempts===1) {
            await new Promise(resolve=>setTimeout(resolve,200));
            await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'离线夹具：候选操作失败'})});
          } else await route.continue();
        });
        try {
          await button.click();
          for (const b of await row.locator('.pool-actions button').all()) await expect(b).toBeDisabled();
          await button.evaluate(e=>e.click());
          await expect(row.locator('[role=alert]')).toContainText('候选操作失败');
          await expect(button).toBeEnabled();
          assert.equal(attempts,1,'Busy action submitted again');
          await page.screenshot({path:path.join(output,`candidate-retry-${theme}.png`)});
          await button.click();
          await expect.poll(async()=>(await call('/api/candidates?limit=100')).items.find(c=>c.id===candidate.id).status).toBe(expected);
          await expect(button).toHaveCount(0);
          assert.equal(attempts,2,'Restored button did not send exactly one retry');
        } finally { await page.unroute(endpoint); }
      }
    });
    await check('candidate-late-filter-response-and-error-are-ignored', async () => {
      await call('/api/candidates','POST',{title:'候选乱序回归',url:'https://example.org/late-filter'});
      await page.locator('[data-view=recommend]').first().click();
      for (const failure of [false,true]) {
        await page.evaluate(()=>{S.poolFilter='open';return loadCandidates();});
        let release,notify,reads=0;
        const held=new Promise(resolve=>{release=resolve;}),started=new Promise(resolve=>{notify=resolve;});
        const endpoint=base+'/api/candidates?limit=100';
        await page.route(endpoint,async route=>{
          if(++reads!==1)return route.continue();
          const response=failure?null:await route.fetch();notify();await held;
          if(failure)await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'离线夹具：旧筛选读取失败'})});
          else await route.fulfill({response});
        });
        try {
          await page.locator('[data-pool-filter=all]').click();await started;
          await page.locator('[data-view=library]').first().click();
          await page.locator('[data-view=recommend]').first().click();
          await expect(page.locator('[data-pool-filter=all].active')).toBeVisible();
          await page.locator('[data-pool-filter=library]').click();
          await expect(page.locator('[data-pool-filter=library].active')).toBeVisible();
          const response=page.waitForResponse(r=>r.url()===endpoint);release();await response;
          await page.waitForTimeout(100);
          assert.equal(await page.evaluate(()=>S.poolFilter),'library');
          await expect(page.locator('[data-pool-filter=library].active')).toBeVisible();
          await expect(page.locator('.pool-row')).toHaveCount(0);
          await expect(page.locator('#candidatePool')).not.toContainText('旧筛选读取失败');
          if(!failure)await page.screenshot({path:path.join(output,'candidate-late-response-ignored.png')});
        } finally { release();await page.unroute(endpoint); }
      }
    });
    await check('source-read-retry-back-and-late-error-use-current-source', async () => {
      const a=await call('/api/evidence','POST',{kind:'knowledge',title:'来源读取 A',content:'仅供本地回归的待审核原文 A。'});
      const b=await call('/api/evidence','POST',{kind:'knowledge',title:'来源读取 B',content:'仅供本地回归的待审核原文 B。'});
      await page.locator('[data-view=knowledge]').first().click();
      await page.setViewportSize({width:390,height:844});
      await page.evaluate(()=>{S.sidebar=false;applySidebar();S.theme='dark';applyTheme();});
      const linkA=page.locator(`#knowledgeList [data-evidence-ref="${a.id}"]`);
      const endpoint=base+'/api/evidence/'+a.id;let reads=0;
      await page.route(endpoint,route=> (++reads===1||reads===3)
        ?route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'离线夹具：来源暂不可读'})}):route.continue());
      try {
        await linkA.click();await expect(page.locator('#inspSources [role=alert]')).toContainText('来源暂不可读');
        await expect(page.locator('#btnRetryEvidence')).toBeVisible();
        await expect(page.locator('#btnSourceBack')).toBeVisible();
        await page.screenshot({path:path.join(output,'source-error-recovery-narrow.png')});
        await page.locator('#btnRetryEvidence').click();
        await expect(page.locator('#inspSources h3')).toHaveText('来源读取 A');
        assert.equal(await page.evaluate(()=>S.evidenceRef),a.id);
        assert.equal(reads,2,'Retry changed source or did not read again');
        await page.keyboard.press('Escape');await linkA.click();
        await expect(page.locator('#inspSources [role=alert]')).toContainText('来源暂不可读');
        await page.locator('#btnSourceBack').click();
        await expect(page.locator('#btnInspector')).toHaveAttribute('aria-expanded','false');
        await expect(linkA).toBeFocused();
        assert(!await page.locator('.work').evaluate(e=>e.inert));
      } finally { await page.unroute(endpoint); }
      let release,notify;
      const held=new Promise(resolve=>{release=resolve;}),started=new Promise(resolve=>{notify=resolve;});
      await page.route(endpoint,async route=>{notify();await held;await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'离线夹具：来源 A 旧错误'})});});
      try {
        await linkA.click();await started;await page.keyboard.press('Escape');
        await page.locator(`#knowledgeList [data-evidence-ref="${b.id}"]`).click();
        await expect(page.locator('#inspSources h3')).toHaveText('来源读取 B');
        const response=page.waitForResponse(r=>r.url()===endpoint);release();await response;await page.waitForTimeout(100);
        await expect(page.locator('#inspSources h3')).toHaveText('来源读取 B');
        await expect(page.locator('#inspSources')).not.toContainText('来源 A 旧错误');
        assert.equal(await page.evaluate(()=>S.evidenceRef),b.id);
      } finally { release();await page.unroute(endpoint); }
    });
    await check('new-knowledge-dialog-hands-focus-to-source-only-on-success', async () => {
      await page.locator('[data-view=knowledge]').first().click();
      await page.setViewportSize({width:390,height:844});
      await page.evaluate(()=>{S.sidebar=false;applySidebar();});
      const endpoint=base+'/api/evidence';let posts=0;
      await page.route(endpoint,route=>{
        if(route.request().method()!=='POST')return route.continue();
        posts++;
        return posts===1?route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'离线夹具：保存暂不可用'})}):route.continue();
      });
      try {
        await page.locator('#btnAddKnowledge').click();
        await page.locator('#researchTask [name=title]').fill('窄屏知识交接');
        await page.locator('#researchTask [name=content]').fill('仅作界面回归，不会自动可信。');
        await page.locator('#researchTask [type=submit]').click();
        await expect(page.locator('#researchTask .task-error')).toContainText('保存暂不可用');
        await expect(page.locator('#researchTask [name=title]')).toHaveValue('窄屏知识交接');
        assert(await page.evaluate(()=>!!document.activeElement.closest('#researchTask')),'Failure lost modal focus');
        await expect(page.locator('#btnInspector')).toHaveAttribute('aria-expanded','false');
        await page.locator('#researchTask [type=submit]').click();
        await expect(page.locator('#researchTask')).toHaveCount(0);
        await expect(page.locator('#inspSources h3')).toHaveText('窄屏知识交接');
        await expect(page.locator('#btnCloseInspector')).toBeFocused();
        assert(await page.locator('.work').evaluate(e=>e.inert));
        await page.screenshot({path:path.join(output,'new-knowledge-focus-narrow.png')});
        await page.keyboard.press('Escape');
        await page.locator('#btnAddKnowledge').click();
        await page.locator('#researchTask [name=title]').fill('取消不应创建');
        await page.locator('#researchTask [data-cancel]').first().click();
        await expect(page.locator('#researchTask')).toHaveCount(0);
        await expect(page.locator('#btnInspector')).toHaveAttribute('aria-expanded','false');
        assert.equal(posts,2,'Cancel created another source');
        await page.setViewportSize({width:1360,height:880});
        await page.locator('#btnAddKnowledge').click();
        await page.locator('#researchTask [name=title]').fill('桌面知识交接');
        await page.locator('#researchTask [name=content]').fill('桌面保持并排操作。');
        await page.locator('#researchTask [type=submit]').click();
        await expect(page.locator('#researchTask')).toHaveCount(0);
        await expect(page.locator('#inspSources h3')).toHaveText('桌面知识交接');
        await expect(page.locator('#btnAddKnowledge')).toBeFocused();
        assert(!await page.locator('.work').evaluate(e=>e.inert));
        await expect(page.locator('#inspector')).not.toHaveAttribute('aria-modal');
      } finally { await page.unroute(endpoint); }
    });
    await check('verification-keeps-current-target-and-rejects-stale-results', async () => {
      await page.locator('[data-view=verify]').click();
      const titleA = 'N10 核验旧对象 A', titleB = 'N10 核验当前对象 B';
      const held = await delayed(base + '/api/verify', r => r.postDataJSON().title === titleA);
      try {
        await page.locator('#verifyInput').fill(titleA); await page.locator('#btnVerify').click(); await held.ready;
        await page.locator('#verifyInput').fill(titleB); await page.locator('#btnVerify').click();
        await expect(page.locator('#wlReason')).toBeVisible();
        const late = page.waitForResponse(r => r.url() === base + '/api/verify' && r.request().postDataJSON().title === titleA);
        held.release(); await late; await page.waitForTimeout(50);
        await page.locator('#wlReason').fill('隔离回归：人工确认当前显示的对象。');
        const request = page.waitForRequest(r => r.url() === base + '/api/whitelist' && r.method() === 'POST');
        await page.locator('#btnWlConfirm').click();
        assert.equal((await request).postDataJSON().title, titleB, 'Confirmed the stale verification target');
        await expect(page.locator('#wlList')).toContainText(titleB);
        const titles = (await call('/api/whitelist')).items.map(x => x.title);
        assert(!titles.includes(titleA) && titles.includes(titleB));
        await expect(page.locator('#verifyTarget')).toHaveText(titleB);
        await page.screenshot({ path: path.join(output, 'p1-verification-bound-result.png') });
      } finally { await held.close(); }
      await page.locator('#verifyInput').fill('N10 输入更改前'); await page.locator('#btnVerify').click();
      await expect(page.locator('#btnWlConfirm')).toBeVisible();
      await page.locator('#verifyInput').fill('N10 输入更改后');
      await expect(page.locator('#btnWlConfirm')).toHaveCount(0);
      await expect(page.locator('#verifyResult')).toContainText('重新核验');
      const failed = await delayed(base + '/api/verify', r => r.postDataJSON().title === 'N10 迟到失败', '旧对象读取失败');
      try {
        await page.locator('#verifyInput').fill('N10 迟到失败'); await page.locator('#btnVerify').click(); await failed.ready;
        await page.locator('#verifyInput').fill('N10 最新成功'); await page.locator('#btnVerify').click();
        await expect(page.locator('#verifyTarget')).toHaveText('N10 最新成功');
        const late = page.waitForResponse(r => r.url() === base + '/api/verify' && r.status() === 503);
        failed.release(); await late; await page.waitForTimeout(50);
        await expect(page.locator('#verifyTarget')).toHaveText('N10 最新成功');
        await expect(page.locator('#verifyResult')).not.toContainText('旧对象读取失败');
      } finally { await failed.close(); }
      const confirming = await delayed(base + '/api/whitelist', r => r.method() === 'POST');
      try {
        await page.locator('#wlReason').fill('隔离回归：确认按钮等待时防止重复。');
        await page.locator('#btnWlConfirm').click(); await confirming.ready;
        await expect(page.locator('#btnWlConfirm')).toBeDisabled();
        await page.locator('#verifyInput').fill('N10 确认提交中改输入');
        confirming.release();
        await expect(page.locator('#wlList')).toContainText('N10 最新成功');
        await expect(page.locator('#btnWlConfirm')).toHaveCount(0);
        await expect(page.locator('#verifyResult')).toContainText('重新核验');
      } finally { await confirming.close(); }
      await page.locator('#verifyInput').fill('N10 确认提交失败对象');await page.locator('#btnVerify').click();
      await expect(page.locator('#wlReason')).toBeVisible();await page.locator('#wlReason').fill('隔离回归：旧对象的提交失败也需明确反馈。');
      const rejected = await delayed(base+'/api/whitelist',r=>r.method()==='POST','确认写入失败');
      try {
        await page.locator('#btnWlConfirm').click();await rejected.ready;
        await page.locator('#verifyInput').fill('N10 提交失败后当前输入');rejected.release();
        await expect(page.locator('#toasts')).toContainText('「N10 确认提交失败对象」的人工确认未完成');
        await expect(page.locator('#verifyResult')).toContainText('重新核验');
        assert(!(await call('/api/whitelist')).items.some(x=>x.title==='N10 确认提交失败对象'));
      } finally {await rejected.close();}
      await page.locator('#verifyInput').fill('N10 已确认但刷新失败');await page.locator('#btnVerify').click();
      await expect(page.locator('#wlReason')).toBeVisible();await page.locator('#wlReason').fill('隔离回归：区分确认成功和随后列表读取失败。');
      const listFailure = r=>r.request().method()==='GET'?r.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'确认后列表读取失败'})}):r.continue();
      await page.route(base+'/api/whitelist',listFailure);
      try {
        await page.locator('#btnWlConfirm').click();await expect(page.locator('#toasts')).toContainText('已确认「N10 已确认但刷新失败」，但结果刷新失败');
        await expect(page.locator('#btnWlConfirm')).toBeDisabled();await expect(page.locator('#btnWlConfirm')).toHaveText('已确认放行');
      } finally {await page.unroute(base+'/api/whitelist',listFailure);}
      assert((await call('/api/whitelist')).items.some(x=>x.title==='N10 已确认但刷新失败'));
      await page.screenshot({ path: path.join(output, 'p1-verification-current-target.png') });
    });
    await check('scope-editor-preserves-current-scope-and-snapshots-explicit-selection', async () => {
      const a = process.env.LITPRISM_ACCEPTANCE_SEED;
      const b = (await call('/api/acceptance/material', 'POST', { unanalysed: true })).paper_id;
      const c = (await call('/api/acceptance/material', 'POST', {})).paper_id;
      assert.equal(new Set([a,b,c]).size, 3);
      await page.evaluate(() => refreshLibrary());
      const original = { paper_ids: [a,b], mode: 'strict' };
      const id = (await call('/api/conversations', 'POST', { title: 'N10 多篇范围保留', scope: original })).id;
      const saved = async cid => { const s = (await call('/api/conversations/'+cid+'/messages')).conversation.scope; return typeof s === 'string' ? JSON.parse(s) : s; };
      const open = async () => { await page.evaluate(() => loadSidebar()); await page.locator(`[data-conv="${id}"]`).click(); await expect(page.locator('#scopeChip')).toBeVisible(); await page.locator('#scopeChip').click(); };
      const save = async () => { await page.locator('#researchTask [type=submit]').click(); await expect(page.locator('#researchTask')).toHaveCount(0); };
      await open(); await page.screenshot({path:path.join(output,'p1-scope-current-dialog.png')}); await save(); assert.deepEqual(await saved(id), original);
      await page.locator('[data-view=library]').first().click();
      await page.locator(`[data-select-paper="${b}"]`).check(); await page.locator(`[data-select-paper="${c}"]`).check();
      await open(); await save(); assert.deepEqual(await saved(id), original, 'Same count changed current paper IDs');
      await open(); await page.locator('#researchTask [name=scope]').selectOption('selected');
      // A background refresh must not replace the dialog's explicit selection or target conversation.
      const other = (await call('/api/conversations', 'POST', { title: 'N10 范围目标隔离' })).id;
      await page.evaluate(other => { S.selectedPapers.clear(); S.activeConv = other; }, other);
      await save(); assert.deepEqual(await saved(id), { paper_ids: [b,c], mode: 'strict' }); assert.equal(await saved(other), null);
      const group = (await call('/api/collections', 'POST', { name: 'N10 范围分组' })).id;
      for (const value of [{paper_ids:[a],mode:'strict'},{collection_id:group,mode:'strict'},{}]) {
        await call('/api/conversations/'+id,'PATCH',{scope:value});
        await open(); await page.locator('#researchTask').getByRole('button',{name:'取消',exact:true}).click();
        assert.deepEqual(await saved(id), Object.keys(value).length ? value : null);
        await open(); await save(); assert.deepEqual(await saved(id), Object.keys(value).length ? value : null);
      }
      await open(); await page.locator('#researchTask [name=scope]').selectOption(''); await save(); assert.equal(await saved(id), null);
      await page.screenshot({ path: path.join(output, 'p1-scope-retained.png') });
    });
    await check('sidebar-errors-keep-data-selection-and-distinguish-unread-from-empty', async () => {
      const seed = process.env.LITPRISM_ACCEPTANCE_SEED;
      await page.locator(`[data-select-paper="${seed}"]`).check();
      const initialCount = await page.locator('#paperList [data-paper]').count();
      const papersUrl = base + '/api/papers/catalog';
      const fail = r => r.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'N10 导航暂不可读'})});
      await page.route(papersUrl, fail);
      try {
        await page.locator('#convSearch').fill('N10 导航');
        await expect(page.locator('#sidebarReadError')).toContainText('文献读取失败');
        await expect(page.locator('#libList [data-paper]')).toHaveCount(initialCount);
        await expect(page.locator(`[data-select-paper="${seed}"]`)).toBeChecked();
        assert(await page.evaluate(seed => S.selectedPapers.has(seed), seed));
        await expect(page.locator('#btnPaperExport')).toBeEnabled();
        await page.screenshot({path:path.join(output,'p1-sidebar-error-retains-selection.png')});
        await page.locator('#btnRetrySidebar').click();
        await expect(page.locator('#btnRetrySidebar')).toBeEnabled();
        await expect(page.locator('#btnRetrySidebar')).toBeFocused();
      } finally { await page.unroute(papersUrl, fail); }
      await page.locator('#btnRetrySidebar').click(); await expect(page.locator('#sidebarReadError')).toBeHidden();
      await expect(page.locator('#sidebar [data-view=library]')).toBeFocused();
      await expect(page.locator(`[data-select-paper="${seed}"]`)).toBeChecked();
      await call('/api/conversations','POST',{title:'N10 导航会话'}); await page.evaluate(() => loadSidebar());
      const oldIds = await page.locator('#convList [data-conv]').evaluateAll(ns=>ns.map(n=>n.dataset.conv));
      const convUrl=base+'/api/conversations?*'; await page.route(convUrl, fail);
      try {
        await page.locator('#convSearch').fill('N10 导航会话');
        await expect(page.locator('#sidebarReadError')).toContainText('会话读取失败');
        assert.deepEqual(await page.locator('#convList [data-conv]').evaluateAll(ns=>ns.map(n=>n.dataset.conv)),oldIds);
      } finally { await page.unroute(convUrl, fail); }
      await page.locator('#btnRetrySidebar').click(); await expect(page.locator('#sidebarReadError')).toBeHidden();
      await expect(page.locator('#convList')).toContainText('N10 导航会话');
      const held = await delayed(base+'/api/conversations?q='+encodeURIComponent('N10'));
      try {
        await page.locator('#convSearch').fill('N10'); await held.ready;
        await page.locator('#convSearch').fill('N10 没有这个会话');
        await expect(page.locator('#convList [data-conv]')).toHaveCount(0);
        const late=page.waitForResponse(r=>r.url()===base+'/api/conversations?q='+encodeURIComponent('N10'));
        held.release();await late;await page.waitForTimeout(50);
        await expect(page.locator('#convList [data-conv]')).toHaveCount(0);
      } finally {await held.close();}
      const oldError = await delayed(base+'/api/conversations?q='+encodeURIComponent('N10 旧导航错误'),()=>true,'旧导航读取失败');
      try {
        await page.locator('#convSearch').fill('N10 旧导航错误');await oldError.ready;
        await page.locator('#convSearch').fill('N10 导航会话');await expect(page.locator('#convList')).toContainText('N10 导航会话');
        const late=page.waitForResponse(r=>r.url()===base+'/api/conversations?q='+encodeURIComponent('N10 旧导航错误'));
        oldError.release();await late;await page.waitForTimeout(50);
        await expect(page.locator('#sidebarReadError')).toBeHidden();
      } finally {await oldError.close();}
      await call('/api/skills/scaffold','POST',{id:'n10-navigation-read',name:'N10 导航技能'});await page.evaluate(()=>loadSidebar());
      const skillsCount=await page.locator('#skillCount').innerText();
      await page.route(base+'/api/skills',fail);
      try {
        await page.locator('#convSearch').fill('N10 导航');await expect(page.locator('#sidebarReadError')).toContainText('技能读取失败');
        await expect(page.locator('#skillCount')).toHaveText(skillsCount);
      } finally {await page.unroute(base+'/api/skills',fail);}
      await page.locator('#btnRetrySidebar').click();await expect(page.locator('#sidebarReadError')).toBeHidden();
      await page.route(papersUrl, fail);
      try {
        await page.reload(); await expect(page.locator('#sidebarReadError')).toContainText('文献读取失败');
        await expect(page.locator('#paperList')).toContainText('文献尚未读取');
        await expect(page.locator('#paperList')).not.toContainText('知识库还是空');
      } finally { await page.unroute(papersUrl, fail); }
      await page.locator('#btnRetrySidebar').click(); await expect(page.locator('#paperList [data-paper]')).toHaveCount(initialCount);
      const empty = r => r.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items:[],count:0,total:0,offset:0,has_more:false})});
      await page.route(papersUrl, empty);
      await page.route(base+'/api/papers?*', empty);
      try { await page.evaluate(() => loadSidebar()); await expect(page.locator('#paperList')).toContainText('知识库还是空'); await expect(page.locator('#sidebarReadError')).toBeHidden(); }
      finally { await page.unroute(papersUrl, empty); await page.unroute(base+'/api/papers?*', empty); }
    });
    await check('secondary-controls-have-associated-accessible-names', async () => {
      const seed = process.env.LITPRISM_ACCEPTANCE_SEED;
      const text = await call('/api/paper/'+seed+'/text');
      const title = 'N10 状态 <&> 名称';
      const d = await call('/api/deliverables','POST',{title,type:'notes',content:'原文描述 [P1 p.1]',citations:[{tag:'P1',paper_id:seed,chunk_id:text.pages[0].chunks[0].chunk_id}]});
      await page.locator('[data-view=deliverables]').click(); await expect(page.locator(`[data-dstat="${d.id}"]`)).toHaveAccessibleName(title+'的状态');
      await page.locator('[data-view=library]').first().click(); await page.locator(`[data-select-paper="${seed}"]`).check();
      await page.locator('#btnMovePapers').click(); await expect(page.locator('#researchTask [name=collection_id]')).toHaveAccessibleName('目标分组');
      await page.locator('#researchTask').getByRole('button',{name:'取消',exact:true}).click();
      await page.locator('#btnPaperExport').click(); await expect(page.locator('#researchTask [name=format]')).toHaveAccessibleName('导出格式');
      await page.locator('#researchTask').getByRole('button',{name:'取消',exact:true}).click();
      const group=(await call('/api/collections','POST',{name:'N10 名称分组'})).id;
      await page.locator('#btnCollection').click(); await page.locator(`[data-rename-group="${group}"]`).click();
      await expect(page.locator('#researchTask [name=name]')).toHaveAccessibleName('分组名称');
      await page.locator('#researchTask').getByRole('button',{name:'取消',exact:true}).click();
      await page.locator('[data-view=skills]').click(); await expect(page.locator('#skillCards')).not.toBeEmpty(); await page.locator('#btnSkillNew').click();
      await expect(page.locator('#nsId')).toHaveAccessibleName(/标识.*id/); await expect(page.locator('#nsName')).toHaveAccessibleName('显示名称'); await expect(page.locator('#nsDesc')).toHaveAccessibleName('用途说明');
      await page.locator('#nsCancel').click(); await page.locator('[data-view=verify]').click();
      await page.locator('#verifyInput').fill('N10 确认依据名称'); await page.locator('#btnVerify').click(); await expect(page.locator('#wlReason')).toHaveAccessibleName('人工确认依据');
      await page.setViewportSize({width:390,height:844}); await page.waitForTimeout(80);
      await page.screenshot({path:path.join(output,'p1-secondary-control-names-narrow.png')});
    });
    await check('viewport-theme-settings-matrix', async () => {
      const measurements = [];
      for (const width of [1360, 1000, 720, 390]) {
        await page.setViewportSize({ width, height: 760 });
        await page.evaluate(() => { S.sidebar = false; applySidebar(); });
        for (const theme of ['light', 'dark']) {
          await page.evaluate(t => { S.theme = t; applyTheme(); }, theme);
          await page.locator('#btnCollection').click();
          await expect(page.locator('#researchTask')).toBeVisible();
          const submit = await page.locator('#researchTask [type=submit]').boundingBox();
          assert(submit.x >= 0 && submit.x + submit.width <= width + 1, 'Task action outside viewport');
          await page.keyboard.press('Escape');
          await page.keyboard.press('Control+,');
          await expect(page.locator('#settingsModal')).toBeVisible();
          const m = await page.evaluate(() => {
            const e = document.querySelector('#settingsPanes'), r = e.getBoundingClientRect();
            return { x: r.x, width: r.width, scroll: e.scrollWidth, client: e.clientWidth };
          });
          measurements.push({ viewportWidth: width, theme, ...m });
          await page.screenshot({ path: path.join(output, `settings-${width}-${theme}.png`) });
          await page.keyboard.press('Escape');
          const overflow = await page.evaluate(() => {
            const e = document.querySelector('#workBody'); return e.scrollWidth - e.clientWidth;
          });
          measurements[measurements.length - 1].libraryOverflow = overflow;
          measurements[measurements.length - 1].libraryWidth = await page.locator('#workBody').evaluate(e => e.clientWidth);
        }
      }
      fs.writeFileSync(path.join(output, 'layout.json'), JSON.stringify(measurements, null, 2));
      assert(measurements.every(m => m.scroll <= m.client + 1 && m.width >= Math.min(300, m.viewportWidth * .7)
        && m.libraryOverflow <= 1 && m.libraryWidth >= m.viewportWidth * .7), 'Settings/library squeezed or overflows');
    });
    await check('narrow-navigation-and-theme-reopen', async () => {
      await page.setViewportSize({ width: 390, height: 760 });
      await page.reload();
      await expect(page.locator('#btnSidebar')).toHaveAttribute('aria-expanded', 'false');
      await page.locator('#btnSidebar').click();
      await expect(page.locator('#btnSidebar')).toHaveAttribute('aria-expanded', 'true');
      await page.locator('.workspace-nav [data-view=library]').click();
      await expect(page.locator('#btnSidebar')).toHaveAttribute('aria-expanded', 'false');
      await page.locator('#btnTheme').click();
      const theme = await page.locator('html').getAttribute('data-theme');
      await page.reload();
      await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
    });
    await check('secondary-text-contrast-in-both-themes', async () => {
      const ratios = await page.evaluate(() => {
        const luminance = hex => hex.trim().slice(1).match(/../g).map(v => parseInt(v, 16) / 255)
          .map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4)
          .reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
        const result = [];
        for (const theme of ['light', 'dark']) {
          document.documentElement.dataset.theme = theme;
          const style = getComputedStyle(document.documentElement);
          const text = luminance(style.getPropertyValue('--muted'));
          for (const token of ['--bg', '--panel', '--panel-2', '--panel-3']) {
            const bg = luminance(style.getPropertyValue(token));
            result.push({ theme, token, ratio: (Math.max(text, bg) + .05) / (Math.min(text, bg) + .05) });
          }
        }
        applyTheme();
        return result;
      });
      fs.writeFileSync(path.join(output, 'contrast.json'), JSON.stringify(ratios, null, 2));
      assert(ratios.every(r => r.ratio >= 4.5), 'Muted text contrast below 4.5:1');
    });
    await check('deliverable-late-list-cannot-resurrect-deleted-items', async () => {
      const seed = process.env.LITPRISM_ACCEPTANCE_SEED;
      const text = await call('/api/paper/'+seed+'/text');
      const item = await call('/api/deliverables','POST',{title:'N10 旧列表删除反例',type:'notes',content:'原文描述 [P1 p.1]',citations:[{tag:'P1',paper_id:seed,chunk_id:text.pages[0].chunks[0].chunk_id}]});
      const held = await delayed(base+'/api/deliverables', r=>r.method()==='GET');
      try {
        await page.locator('[data-view=deliverables]').click(); await held.ready;
        await page.locator('[data-view=library]').first().click();
        await page.locator('[data-view=deliverables]').click();
        await page.locator(`[data-ddelete="${item.id}"]`).click();
        await expect(page.locator(`[data-ddelete="${item.id}"]`)).toHaveCount(0);
        const response = page.waitForResponse(r=>r.url()===base+'/api/deliverables' && r.request().method()==='GET');
        held.release(); await response; await page.waitForTimeout(80);
        await expect(page.locator(`[data-ddelete="${item.id}"]`)).toHaveCount(0);
        assert(!(await page.evaluate(()=>S.deliverables)).some(x=>x.id===item.id));
        assert(!(await call('/api/deliverables')).items.some(x=>x.id===item.id));
      } finally {await held.close();}
      const failed = await delayed(base+'/api/deliverables',()=>true,'迟到的产出列表失败');
      try {
        await page.locator('[data-view=library]').first().click(); await page.locator('[data-view=deliverables]').click(); await failed.ready;
        await page.locator('[data-view=library]').first().click(); await page.locator('[data-view=deliverables]').click();
        await expect(page.locator('#delivList [data-dstat]').first()).toBeVisible();
        const response=page.waitForResponse(r=>r.url()===base+'/api/deliverables' && r.status()===503);
        failed.release(); await response; await page.waitForTimeout(80);
        await expect(page.locator('#toasts')).not.toContainText('迟到的产出列表失败');
      } finally {await failed.close();}
      await page.screenshot({path:path.join(output,'p2-deliverables-current.png')});
    });
    await check('context-and-history-dialog-stay-with-current-conversation', async () => {
      const a=(await call('/api/acceptance/compact-history','POST',{})).conversation_id;
      const b=(await call('/api/conversations','POST',{title:'N10 当前空会话'})).id;
      await page.evaluate(()=>loadSidebar());
      await page.locator(`[data-conv="${b}"]`).click(); await expect(page.locator('#tbTitle')).toHaveText('N10 当前空会话');
      await page.locator('#btnInspector').click(); await page.locator('[data-insp=context]').click();
      for (const failure of [null,'迟到的会话读取失败']) {
        const held=await delayed(base+'/api/context/'+a,()=>true,failure);
        try {
          await page.locator(`[data-conv="${a}"]`).click(); await held.ready;
          const current=page.waitForResponse(r=>r.url()===base+'/api/context/'+b);
          await page.locator(`[data-conv="${b}"]`).click(); await current;
          await expect(page.locator('#chatInput')).toHaveAttribute('data-conv-id',b);
          await page.waitForTimeout(100);
          await expect(page.locator('#inspContext .metric .v').first()).toHaveText('0');
          const response=page.waitForResponse(r=>r.url()===base+'/api/context/'+a);
          held.release(); await response; await page.waitForTimeout(80);
          await expect(page.locator('#inspContext .metric .v').first()).toHaveText('0');
          await expect(page.locator('#inspContext [data-restore-block]')).toHaveCount(0);
          await expect(page.locator('#inspContext')).not.toContainText('迟到的会话读取失败');
        } finally {await held.close();}
      }
      await page.locator(`[data-conv="${a}"]`).click(); await expect(page.locator('#inspContext [data-restore-block]')).toBeVisible();
      const held=await delayed(base+'/api/turns/'+a);
      try {
        await page.locator('#btnTurnReads').click(); await held.ready;
        await page.locator(`[data-conv="${b}"]`).click(); await expect(page.locator('#tbTitle')).toHaveText('N10 当前空会话');
        const response=page.waitForResponse(r=>r.url()===base+'/api/turns/'+a);
        held.release(); await response; await page.waitForTimeout(80);
        await expect(page.locator('#researchTask')).toHaveCount(0);
      } finally {await held.close();}
      await page.screenshot({path:path.join(output,'p2-context-current.png')});
    });
    await check('paper-late-response-and-conversation-rename-keep-location', async () => {
      const paper=(await call('/api/acceptance/material','POST',{unanalysed:true})).paper_id;
      const conv=(await call('/api/conversations','POST',{title:'N10 跨页当前会话'})).id;
      await page.evaluate(()=>refreshLibrary());
      for (const failure of [null,'迟到的旧论文失败']) {
        const held=await delayed(base+'/api/paper/'+paper,()=>true,failure);
        try {
          await page.locator(`#sidebar [data-paper="${paper}"]`).click(); await held.ready;
          await page.locator(`[data-conv="${conv}"]`).click(); await expect(page.locator('#tabbar')).toContainText('N10 跨页当前会话');
          const response=page.waitForResponse(r=>r.url()===base+'/api/paper/'+paper);
          held.release(); await response; await page.waitForTimeout(80);
          await expect(page.locator('#tabbar')).toContainText('N10 跨页当前会话');
          await expect(page.locator('#tabbar')).not.toContainText('未解读质量验收');
          await expect(page.locator('#toasts')).not.toContainText('迟到的旧论文失败');
        } finally {await held.close();}
      }
      const manage=async()=>{await page.locator(`[data-conv="${conv}"]`).hover(); await page.locator(`[data-conv-manage="${conv}"]`).click();};
      await manage(); await page.locator('#researchTask [name=title]').fill('N10 会话新名称');
      await page.locator('#researchTask [type=submit]').click(); await expect(page.locator('#researchTask')).toHaveCount(0);
      await expect(page.locator('#tbTitle')).toHaveText('N10 会话新名称'); await expect(page.locator('#tabbar')).toContainText('N10 会话新名称');
      await page.locator('[data-view=library]').first().click();
      await manage(); await page.locator('#researchTask [name=title]').fill('N10 后台改名');
      await page.locator('#researchTask [type=submit]').click(); await expect(page.locator('#researchTask')).toHaveCount(0);
      await expect(page.locator('#tbTitle')).toHaveText('知识库'); await expect(page.locator('#tabbar')).toContainText('知识库');
    });
    await check('review-navigation-discards-only-after-confirmation-and-ignores-late-work', async () => {
      const a=process.env.LITPRISM_ACCEPTANCE_SEED;
      const b=(await call('/api/acceptance/material','POST',{})).paper_id;
      await page.evaluate(()=>refreshLibrary());
      const open=async()=>{await page.locator(`#sidebar [data-paper="${b}"]`).click(); await page.locator('[data-semantic-claim="materialclaim"]').first().click(); await expect(page.locator('#semanticReview')).toBeVisible();};
      await open(); await page.locator(`#sidebar [data-paper="${a}"]`).click();
      await expect(page.locator('#semanticReview')).toHaveCount(0); assert.equal(await page.evaluate(()=>S.semanticRef),null);
      await open(); await page.locator('[data-view=library]').first().click(); await expect(page.locator('#semanticReview')).toHaveCount(0);
      const conv=(await call('/api/conversations','POST',{title:'N10 离开论文审核的会话'})).id; await page.evaluate(()=>loadSidebar());
      await open(); await page.locator(`[data-conv="${conv}"]`).click(); await expect(page.locator('#chatInput')).toBeVisible();
      await expect(page.locator('#semanticReview')).toHaveCount(0); assert.equal(await page.evaluate(()=>S.semanticRef),null);
      await open(); await page.locator('#semanticReview [name=note]').fill('N10 保留未提交的核对依据');
      const handlers=page.listeners('dialog'); page.removeAllListeners('dialog'); page.once('dialog',d=>d.dismiss());
      try {await page.locator(`#sidebar [data-paper="${a}"]`).click(); await expect(page.locator('#semanticReview [name=note]')).toHaveValue('N10 保留未提交的核对依据'); assert.equal(await page.evaluate(()=>S.activePaper),b);}
      finally {page.removeAllListeners('dialog'); handlers.forEach(h=>page.on('dialog',h));}
      const dismiss=async action=>{
        page.removeAllListeners('dialog'); page.once('dialog',d=>d.dismiss());
        try {await action(); await expect(page.locator('#semanticReview [name=note]')).toHaveValue('N10 保留未提交的核对依据'); assert.equal(await page.evaluate(()=>S.activePaper),b); assert.equal(await page.evaluate(()=>S.activeView),'paper');}
        finally {page.removeAllListeners('dialog'); handlers.forEach(h=>page.on('dialog',h));}
      };
      await page.locator('#semanticReview [name=note]').focus();
      await dismiss(()=>page.keyboard.press('Control+k'));
      await expect(page.locator('#semanticReview [name=note]')).toBeFocused();
      await dismiss(()=>page.locator('#btnSearch').click());
      assert(!await page.locator('#libSearch').evaluate(el=>document.activeElement===el));
      await page.locator(`#sidebar [data-paper="${a}"]`).click(); await expect(page.locator('#semanticReview')).toHaveCount(0);
      for (const failure of [null,'迟到的旧审核读取失败']) {
        await page.locator(`#sidebar [data-paper="${b}"]`).click();
        const held=await delayed(base+'/api/paper/'+b+'/semantic',r=>r.method()==='GET',failure);
        try {
          await page.locator('[data-semantic-claim="materialclaim"]').first().click(); await held.ready;
          await page.locator(`#sidebar [data-paper="${a}"]`).click(); await expect(page.locator('#paperDetail')).toContainText('验收文献 A');
          const response=page.waitForResponse(r=>r.url()===base+'/api/paper/'+b+'/semantic');
          held.release(); await response; await page.waitForTimeout(80);
          await expect(page.locator('#semanticReview')).toHaveCount(0);
          await expect(page.locator('#inspSources')).not.toContainText('迟到的旧审核读取失败');
        } finally {await held.close();}
      }
      await page.locator(`#sidebar [data-paper="${b}"]`).click();
      const oldText=await delayed(base+'/api/paper/'+b+'/text');
      try {
        // Return from the review to ordinary text, then open the same paper's review during that read.
        await page.locator('[data-semantic-claim="materialclaim"]').first().click(); await expect(page.locator('#semanticReview')).toBeVisible();
        await page.locator('#btnSemanticBack').click(); await oldText.ready;
        await page.locator('[data-semantic-claim="materialclaim"]').first().click(); await expect(page.locator('#semanticReview')).toBeVisible();
        const response=page.waitForResponse(r=>r.url()===base+'/api/paper/'+b+'/text');
        oldText.release(); await response; await page.waitForTimeout(80);
        await expect(page.locator('#semanticReview')).toBeVisible();
      } finally {await oldText.close();}
      await open(); await page.locator('#semanticReview [name=note]').fill('N10 提交的裁决只归属于原论文');
      const held=await delayed(base+'/api/paper/'+b+'/semantic/materialclaim/review',r=>r.method()==='POST');
      try {
        await page.locator('#semanticReview [value=confirm]').click(); await held.ready;
        await page.locator(`#sidebar [data-paper="${a}"]`).click(); await expect(page.locator('#paperDetail')).toContainText('验收文献 A');
        const response=page.waitForResponse(r=>r.url()===base+'/api/paper/'+b+'/semantic/materialclaim/review');
        held.release(); await response; await page.waitForTimeout(80);
        await expect(page.locator('#semanticReview')).toHaveCount(0); await expect(page.locator('#inspSources')).not.toContainText('正在读取陈述');
        const saved=await call('/api/paper/'+b+'/semantic'); assert(saved.reviews.some(r=>r.note==='N10 提交的裁决只归属于原论文'));
      } finally {await held.close();}
      await page.screenshot({path:path.join(output,'p2-review-current-paper.png')});
      await page.setViewportSize({width:390,height:844});
      await page.screenshot({path:path.join(output,'p2-review-current-paper-narrow.png')});
    });
    await check('deliverable-status-failure-rolls-back-and-retry-agrees-with-storage', async () => {
      const seed=process.env.LITPRISM_ACCEPTANCE_SEED, text=await call('/api/paper/'+seed+'/text');
      const item=await call('/api/deliverables','POST',{title:'N10 保存状态回退',type:'notes',content:'原文描述 [P1 p.1]',citations:[{tag:'P1',paper_id:seed,chunk_id:text.pages[0].chunks[0].chunk_id}]});
      await page.locator('[data-view=deliverables]').click();
      const endpoint=base+'/api/deliverables/'+item.id;
      const held=await delayed(endpoint,r=>r.method()==='PUT','状态写入失败');
      try {
        await page.locator(`[data-dstat="${item.id}"]`).selectOption('final'); await held.ready;
        await expect(page.locator(`[data-dstat="${item.id}"]`)).toBeDisabled();
        held.release(); await expect(page.locator(`[data-dfeedback="${item.id}"]`)).toContainText('状态写入失败');
        await expect(page.locator(`[data-dstat="${item.id}"]`)).toHaveValue('draft');
        assert.equal((await call('/api/deliverables/'+item.id)).status,'draft');
        await page.screenshot({path:path.join(output,'p2-status-save-failure.png')});
      } finally {await held.close();}
      await page.locator(`[data-dretry="${item.id}"]`).click();
      await expect(page.locator(`[data-dstat="${item.id}"]`)).toHaveValue('final');
      await expect(page.locator(`[data-dbadge="${item.id}"]`)).toHaveText('定稿');
      assert.equal((await call('/api/deliverables/'+item.id)).status,'final');
      const oldList=await delayed(base+'/api/deliverables',r=>r.method()==='GET');
      try {
        await page.locator('[data-view=library]').first().click();await page.locator('[data-view=deliverables]').click();await oldList.ready;
        await page.locator(`[data-dstat="${item.id}"]`).selectOption('exported');
        await expect(page.locator(`[data-dfeedback="${item.id}"]`)).toContainText('已保存');
        const response=page.waitForResponse(r=>r.url()===base+'/api/deliverables');oldList.release();await response;await page.waitForTimeout(80);
        await expect(page.locator(`[data-dstat="${item.id}"]`)).toHaveValue('exported');
      } finally {await oldList.close();}
      const uncertain=async route=>{if(route.request().method()!=='PUT')return route.continue();await route.fetch();await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'写入后响应丢失'})});};
      await page.route(endpoint,uncertain);
      try {
        await page.locator(`[data-dstat="${item.id}"]`).selectOption('draft');
        await expect(page.locator(`[data-dfeedback="${item.id}"]`)).toContainText('确认当前状态');
        await expect(page.locator(`[data-dstat="${item.id}"]`)).toHaveValue('draft');
        await expect(page.locator(`[data-dretry="${item.id}"]`)).not.toBeVisible();
        assert.equal((await call('/api/deliverables/'+item.id)).status,'draft');
      } finally {await page.unroute(endpoint,uncertain);}
    });
    await check('model-save-failure-and-refresh-failure-preserve-actual-selection', async () => {
      const provider=await call('/api/providers','POST',{name:'N10 回退模型',base_url:'http://127.0.0.1:9/v1',protocol:'openai'});
      await call('/api/providers/'+provider.id+'/models','PUT',{models:[{model_id:'ux-one'},{model_id:'ux-two'}]});
      await call('/api/providers/'+provider.id+'/active','POST',{model_id:'ux-one'});
      const conv=(await call('/api/conversations','POST',{title:'N10 模型持久值'})).id;
      await page.evaluate(()=>loadSidebar()); await page.locator(`[data-conv="${conv}"]`).click();
      const one=provider.id+'::ux-one', two=provider.id+'::ux-two';
      await expect(page.locator('#chatModel')).toHaveValue(one);
      const endpoint=base+'/api/providers/'+provider.id+'/active';
      const held=await delayed(endpoint,r=>r.method()==='POST','切换模型失败');
      try {
        await page.locator('#chatModel').selectOption(two); await held.ready;
        await expect(page.locator('#chatModel')).toBeDisabled(); await expect(page.locator('#btnSend')).toBeDisabled();
        await page.locator('[data-view=library]').first().click(); await page.locator(`[data-conv="${conv}"]`).click();
        await expect(page.locator('#chatModel')).toBeDisabled();
        held.release(); await expect(page.locator('#modelSaveFeedback')).toContainText('切换模型失败');
        await expect(page.locator('#chatModel')).toHaveValue(one); await expect(page.locator('#chatModel')).toBeEnabled();
        assert.equal((await call('/api/models')).active.model,'ux-one');
        await page.screenshot({path:path.join(output,'p2-model-save-failure.png')});
      } finally {await held.close();}
      const failedStatus=route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'模型已保存后的状态刷新失败'})});
      await page.route(base+'/api/status',failedStatus);
      try {
        await page.locator('#chatModel').selectOption(two);
        await expect(page.locator('#modelSaveFeedback')).toContainText('已切换');
        await expect(page.locator('#chatModel')).toHaveValue(two); await expect(page.locator('#chatModel')).toBeEnabled();
        assert.equal((await call('/api/models')).active.model,'ux-two');
        await expect(page.locator('#modelSaveFeedback')).toContainText('刷新');
      } finally {await page.unroute(base+'/api/status',failedStatus);}
    });
    await check('batch-move-first-middle-last-failures-report-and-retry-only-unfinished', async () => {
      const ids=[process.env.LITPRISM_ACCEPTANCE_SEED,(await call('/api/acceptance/material','POST',{unanalysed:true})).paper_id,(await call('/api/acceptance/material','POST',{})).paper_id];
      const group=(await call('/api/collections','POST',{name:'N10 部分移动目标'})).id;
      for (const index of [0,1,2]) {
        for (const id of ids) await call('/api/paper/'+id,'PATCH',{collection_id:null});
        await page.evaluate(async()=>{await refreshCollections();await refreshLibrary();});
        for (const id of ids) await page.locator(`[data-select-paper="${id}"]`).check();
        const requests=[]; let failed=false;
        const handler=route=>{if(route.request().method()!=='PATCH')return route.continue();const id=new URL(route.request().url()).pathname.split('/').at(-1);requests.push(id);if(id===ids[index]&&!failed){failed=true;return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'离线移动失败'})});}return route.continue();};
        await page.route(base+'/api/paper/*',handler);
        try {
          await page.locator('#btnMovePapers').click(); await page.locator('#researchTask [name=collection_id]').selectOption(group);
          await page.locator('#researchTask [type=submit]').click();
          await expect(page.locator('[data-move-result]')).toContainText('已移动 2 / 3');
          for (const [i,id] of ids.entries()) {
            const expected=i===index?null:group;
            assert.equal((await call('/api/paper/'+id)).paper.collection_id,expected);
            assert.equal((await page.evaluate(()=>S.papers)).find(p=>p.id===id).collection_id,expected);
          }
          if(index===1){await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(output,'p2-batch-partial-narrow.png')});await page.setViewportSize({width:1360,height:880});}
          await page.locator('#researchTask [type=submit]').click(); await expect(page.locator('#researchTask')).toHaveCount(0);
          for (const [i,id] of ids.entries()) {assert.equal(requests.filter(x=>x===id).length,i===index?2:1);assert.equal((await call('/api/paper/'+id)).paper.collection_id,group);}
        } finally {await page.unroute(base+'/api/paper/*',handler);}
      }
      for(const id of ids)await call('/api/paper/'+id,'PATCH',{collection_id:null});
      await page.evaluate(()=>refreshLibrary());
      let writes=0;
      const count=route=>{if(route.request().method()==='PATCH')writes++;return route.continue();};
      const failStatus=route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'移动后的列表刷新失败'})});
      await page.route(base+'/api/paper/*',count); await page.route(base+'/api/status',failStatus);
      try {
        await page.locator('#btnMovePapers').click();await page.locator('#researchTask [name=collection_id]').selectOption(group);await page.locator('#researchTask [type=submit]').click();
        await expect(page.locator('[data-move-result]')).toContainText('已移动 3 / 3');
        await expect(page.locator('[data-move-result]')).toContainText('刷新未完成');
        await page.unroute(base+'/api/status',failStatus);
        await page.locator('#researchTask [type=submit]').click();await expect(page.locator('#researchTask')).toHaveCount(0);
        assert.equal(writes,3,'Refreshing saved results must not repeat PATCH');
        for(const id of ids)assert.equal((await page.evaluate(()=>S.papers)).find(p=>p.id===id).collection_id,group);
      } finally {await page.unroute(base+'/api/status',failStatus);await page.unroute(base+'/api/paper/*',count);}
    });
    await check('batch-move-stop-preserves-completed-items-and-dialog-selection-snapshot', async () => {
      const ids=[process.env.LITPRISM_ACCEPTANCE_SEED,(await call('/api/acceptance/material','POST',{unanalysed:true})).paper_id,(await call('/api/acceptance/material','POST',{})).paper_id];
      const group=(await call('/api/collections','POST',{name:'N10 停止移动目标'})).id;
      for(const id of ids)await call('/api/paper/'+id,'PATCH',{collection_id:null});
      await page.evaluate(async()=>{await refreshCollections();await refreshLibrary();});
      for(const id of ids)await page.locator(`[data-select-paper="${id}"]`).check();
      await page.locator('#btnMovePapers').click(); await page.locator('#researchTask [name=collection_id]').selectOption(group);
      // A background refresh may alter the live selection while the dialog owns its original target set.
      await page.evaluate(()=>S.selectedPapers.clear());
      const held=await delayed(base+'/api/paper/'+ids[0],r=>r.method()==='PATCH');
      const writes=[];
      const record=r=>{if(r.method()==='PATCH'&&ids.some(id=>r.url()===base+'/api/paper/'+id))writes.push(r.url().split('/').at(-1));};
      page.on('request',record);
      try {
        await page.locator('#researchTask [type=submit]').click(); await held.ready;
        await expect(page.locator('#researchTask [type=submit]')).toBeDisabled();
        await page.locator('#researchTask footer [data-cancel]').click();
        held.release(); await expect(page.locator('[data-move-result]')).toContainText('已移动 1 / 3');
        await expect(page.locator('[data-move-result]')).toContainText('未处理 2');
        await expect(page.locator('[data-move-result]')).toContainText('不会撤销');
        for(const [i,id]of ids.entries())assert.equal((await call('/api/paper/'+id)).paper.collection_id,i?null:group);
        await page.screenshot({path:path.join(output,'p2-batch-stopped.png')});
        await page.locator('#researchTask [type=submit]').click(); await expect(page.locator('#researchTask')).toHaveCount(0);
        for(const id of ids){assert.equal(writes.filter(x=>x===id).length,1);assert.equal((await call('/api/paper/'+id)).paper.collection_id,group);}
        await page.locator(`[data-select-paper="${ids[0]}"]`).check();await page.locator('#btnMovePapers').click();
        await page.locator('#researchTask footer [data-cancel]').click();await expect(page.locator('#researchTask')).toHaveCount(0);assert.equal(writes.length,3);
      } finally {page.off('request',record);await held.close();}
    });
    await check('collection-selectors-show-full-paths-and-keep-real-ids', async () => {
      const a=(await call('/api/collections','POST',{name:'N10 路径父A'})).id;
      const b=(await call('/api/collections','POST',{name:'N10 路径父B'})).id;
      const x=(await call('/api/collections','POST',{name:'共同子组',parent_id:a})).id;
      const y=(await call('/api/collections','POST',{name:'共同子组',parent_id:b})).id;
      await page.evaluate(()=>refreshCollections());
      await expect(page.locator(`#collectionFilter option[value="${x}"]`)).toHaveText('N10 路径父A / 共同子组');
      await expect(page.locator(`#collectionFilter option[value="${y}"]`)).toHaveText('N10 路径父B / 共同子组');
      const seed=process.env.LITPRISM_ACCEPTANCE_SEED;await page.locator(`[data-select-paper="${seed}"]`).check();
      await page.locator('#btnMovePapers').click();await expect(page.locator(`#researchTask option[value="${x}"]`)).toHaveText('N10 路径父A / 共同子组');await page.keyboard.press('Escape');
      await page.locator('#btnNewConv').click();await expect(page.locator(`#researchTask option[value="g:${y}"]`)).toHaveText('分组：N10 路径父B / 共同子组');await page.keyboard.press('Escape');
      const conv=(await call('/api/conversations','POST',{title:'N10 分组路径范围',scope:{collection_id:x,mode:'strict'}})).id;
      await page.evaluate(()=>loadSidebar());await page.locator(`[data-conv="${conv}"]`).click();await page.locator('#scopeChip').click();
      await expect(page.locator(`#researchTask option[value="g:${x}"]`)).toHaveText('分组：N10 路径父A / 共同子组');await page.keyboard.press('Escape');
      await call('/api/collections/'+a,'PATCH',{parent_id:b});await page.evaluate(()=>refreshCollections());
      await expect(page.locator(`#collectionFilter option[value="${x}"]`)).toHaveText('N10 路径父B / N10 路径父A / 共同子组');
      await expect(page.locator('#scopeChip')).toContainText('N10 路径父B / N10 路径父A / 共同子组');
      await page.locator('[data-view=library]').first().click();await page.locator('#collectionFilter').selectOption(x);
      await call('/api/collections/'+x,'DELETE');await page.evaluate(()=>refreshCollections());
      await expect(page.locator('#collectionFilter')).toHaveValue('');await expect(page.locator(`#collectionFilter option[value="${x}"]`)).toHaveCount(0);
      const saved=(await call('/api/conversations/'+conv+'/messages')).conversation.scope;
      assert.equal((typeof saved==='string'?JSON.parse(saved):saved).collection_id,x,'Deleted group must not silently widen saved conversation scope');
    });
    await check('provider-read-failure-ends-loading-and-retries-without-stale-errors', async () => {
      const endpoint=base+'/api/providers';const fail=route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'提供商读取失败'})});
      await page.route(endpoint,fail);
      try {
        await page.locator('[data-view=settings]').click();await page.locator('[data-set=model]').click();
        await expect(page.locator('#pvArea [data-read-error]')).toContainText('提供商读取失败');await expect(page.locator('#pvArea .skeleton')).toHaveCount(0);
      } finally {await page.unroute(endpoint,fail);}
      await page.locator('#pvArea [data-read-retry]').click();await expect(page.locator('#pvArea .pv-card').first()).toBeVisible();
      await page.route(endpoint,fail);
      try {await page.locator('[data-set=general]').click();await page.locator('[data-set=model]').click();await expect(page.locator('#pvArea [data-read-error]')).toContainText('上次');await expect(page.locator('#pvArea .pv-card').first()).toBeVisible();await page.screenshot({path:path.join(output,'p2-provider-read-error.png')});}
      finally {await page.unroute(endpoint,fail);}
      const held=await delayed(endpoint,()=>true,'迟到的提供商错误');
      try {
        await page.locator('#pvArea [data-read-retry]').click();await held.ready;
        await expect(page.locator('#btnAddProvider')).toBeDisabled();
        await page.locator('[data-set=general]').click();const response=page.waitForResponse(r=>r.url()===endpoint&&r.status()===503);held.release();await response;await page.waitForTimeout(60);
        await expect(page.locator('#settingsPanes')).not.toContainText('迟到的提供商错误');
      } finally {await held.close();}
      await page.locator('[data-set=model]').click();await expect(page.locator('#pvArea .pv-card').first()).toBeVisible();await expect(page.locator('#pvArea [data-read-error]')).toHaveCount(0);
    });
    await check('model-directory-read-failure-is-not-unconfigured-and-keeps-known-selection', async () => {
      const provider=await call('/api/providers','POST',{name:'N10 读取目录',base_url:'http://127.0.0.1:9/v1',protocol:'openai'});
      await call('/api/providers/'+provider.id+'/models','PUT',{models:[{model_id:'read-one'},{model_id:'read-two'}]});await call('/api/providers/'+provider.id+'/active','POST',{model_id:'read-two'});
      const conv=(await call('/api/conversations','POST',{title:'N10 目录读取恢复'})).id;
      await page.reload();await expect(page.locator(`[data-conv="${conv}"]`)).toBeVisible();
      const endpoint=base+'/api/models', fail=route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'目录读取失败'})});
      await page.route(endpoint,fail);
      try {
        await page.locator(`[data-conv="${conv}"]`).click();await expect(page.locator('#modelReadError')).toContainText('目录读取失败');
        await expect(page.locator('#chatModel')).not.toContainText('未配置');await expect(page.locator('#chatModel')).toBeDisabled();await expect(page.locator('#btnSend')).toBeDisabled();
      } finally {await page.unroute(endpoint,fail);}
      await page.locator('#btnRetryModels').click();await expect(page.locator('#chatModel')).toHaveValue(provider.id+'::read-two');await expect(page.locator('#chatModel')).toBeEnabled();
      await page.route(endpoint,fail);
      try {await page.locator('[data-view=library]').first().click();await page.locator(`[data-conv="${conv}"]`).click();await expect(page.locator('#modelReadError')).toContainText('上次');await expect(page.locator('#chatModel')).toHaveValue(provider.id+'::read-two');await page.screenshot({path:path.join(output,'p2-model-directory-error.png')});}
      finally {await page.unroute(endpoint,fail);}
      await page.locator('#btnRetryModels').click();await expect(page.locator('#modelReadError')).not.toBeVisible();
    });
    await check('paper-source-read-failure-clears-skeleton-and-cannot-follow-navigation', async () => {
      const seed=process.env.LITPRISM_ACCEPTANCE_SEED, endpoint=base+'/api/paper/'+seed+'/text';
      const fail=route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'原文读取失败'})});
      await page.locator(`#sidebar [data-paper="${seed}"]`).click();await page.locator('#btnInspector').click();
      await page.route(endpoint,fail);
      try {await page.locator('[data-insp=sources]').click();await expect(page.locator('#inspSources [data-read-error]')).toContainText('原文读取失败');await expect(page.locator('#inspSources .skeleton')).toHaveCount(0);await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(output,'p2-source-read-error-narrow.png')});await page.setViewportSize({width:1360,height:880});}
      finally {await page.unroute(endpoint,fail);}
      await page.locator('#inspSources [data-read-retry]').click();await expect(page.locator('#inspSources .source-page')).toBeVisible();
      const held=await delayed(endpoint,()=>true,'迟到的原文错误');
      try {
        await page.locator('[data-insp=sources]').click();await held.ready;await page.locator('[data-view=library]').first().click();
        const response=page.waitForResponse(r=>r.url()===endpoint&&r.status()===503);held.release();await response;await page.waitForTimeout(60);
        await expect(page.locator('#inspSources')).not.toContainText('迟到的原文错误');
      } finally {await held.close();}
    });
    await check('six-list-read-failures-distinguish-first-failure-from-stale-content', async () => {
      await call('/api/memories','POST',{kind:'style_preference',value:'请使用简洁的表达。'});
      const trashed=(await call('/api/acceptance/material','POST',{unanalysed:true})).paper_id;await call('/api/paper/'+trashed,'DELETE');
      for(const [view,url,box]of [['knowledge','/api/evidence','#knowledgeList'],['memories','/api/memories','#memoryList'],['trash','/api/trash','#trashList'],['deliverables','/api/deliverables','#delivList'],['skills','/api/skills','#skillCards'],['verify','/api/whitelist','#wlList']]) {
        await page.reload();await expect(page.locator('#paperList [data-paper]').first()).toBeVisible();
        const endpoint=base+url, fail=route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:view+'列表读取失败'})});
        await page.route(endpoint,fail);
        try {await page.locator(`[data-view="${view}"]`).first().click();await expect(page.locator(box+' [data-read-error]')).toContainText(view+'列表读取失败');await expect(page.locator(box+' .skeleton')).toHaveCount(0);}
        finally {await page.unroute(endpoint,fail);}
        await page.locator(box+' [data-read-retry]').click();await expect(page.locator(box)).toHaveAttribute('aria-busy','false');await expect(page.locator(box+' [data-read-error]')).toHaveCount(0);
        const content=()=>page.locator(box).evaluate(el=>{const copy=el.cloneNode(true);copy.querySelectorAll('[data-read-error],[data-read-loading]').forEach(node=>node.remove());return copy.innerHTML;});
        const before=await content();assert(before.trim(),'Expected real list content');
        await page.route(endpoint,fail);
        try {await page.locator(`[data-view="${view}"]`).first().click();await expect(page.locator(box+' [data-read-error]')).toContainText('上次');assert.equal(await content(),before,'Last successful list content changed after failed read');if(view==='knowledge')await page.screenshot({path:path.join(output,'p2-list-stale-error.png')});}
        finally {await page.unroute(endpoint,fail);}
        await page.locator(box+' [data-read-retry]').click();await expect(page.locator(box)).toHaveAttribute('aria-busy','false');await expect(page.locator(box+' [data-read-error]')).toHaveCount(0);
      }
      await page.locator('[data-view=knowledge]').first().click();await expect(page.locator('#knowledgeList')).toHaveAttribute('aria-busy','false');
      const held=await delayed(base+'/api/evidence',()=>true,'迟到的知识列表错误');
      try {
        await page.locator('[data-view=knowledge]').first().click();await held.ready;
        await page.locator('[data-view=trash]').first().click();await page.locator('[data-view=knowledge]').first().click();
        await expect(page.locator('#knowledgeList')).toHaveAttribute('aria-busy','false');
        const response=page.waitForResponse(r=>r.url()===base+'/api/evidence'&&r.status()===503);held.release();await response;await page.waitForTimeout(60);
        await expect(page.locator('#knowledgeList [data-read-error]')).toHaveCount(0);
      } finally {await held.close();}
    });
    await check('memory-restore-and-skill-refresh-failures-recover-without-unhandled-promises', async () => {
      const memory=await call('/api/memories','POST',{kind:'style_preference',value:'回答请使用短段落并保留必要依据。'});
      await page.locator('[data-view=memories]').first().click();await page.locator(`[data-delete-memory="${memory.id}"]`).click();await expect(page.locator('#btnUndoMemory')).toBeVisible();
      const endpoint=base+'/api/memories/'+memory.id+'/restore', fail=route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'恢复偏好失败'})});
      await page.route(endpoint,fail);
      try {await page.locator('#btnUndoMemory').click();await expect(page.locator('[data-memory-action-error]')).toContainText('恢复偏好失败');await expect(page.locator('#btnUndoMemory')).toBeEnabled();}
      finally {await page.unroute(endpoint,fail);}
      await page.locator('#btnUndoMemory').click();await expect(page.locator(`[data-delete-memory="${memory.id}"]`)).toBeVisible();await expect(page.locator('#btnUndoMemory')).toHaveCount(0);
      await page.locator('[data-view=skills]').first().click();await expect(page.locator('#skillCards .skill-card').first()).toBeVisible();
      const failSkill=route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'刷新Skill失败'})});await page.route(base+'/api/skills',failSkill);
      try {await page.locator('#btnSkillRefresh').click();await expect(page.locator('#skillCards [data-read-error]')).toContainText('刷新Skill失败');await expect(page.locator('#btnSkillRefresh')).toBeEnabled();}
      finally {await page.unroute(base+'/api/skills',failSkill);}
      await page.locator('#btnSkillRefresh').click();await expect(page.locator('#skillCards [data-read-error]')).toHaveCount(0);
    });
    await check('deliverable-titles-and-actions-reflow-without-losing-existing-actions', async () => {
      const seed=process.env.LITPRISM_ACCEPTANCE_SEED, text=await call('/api/paper/'+seed+'/text');
      const title='Transformer 基本架构的直接原文与多来源核对笔记';
      const ids=[];
      for(const name of [title,'非常长的研究产出标题与来源核验记录'.repeat(8)]) ids.push((await call('/api/deliverables','POST',{title:name,type:'notes',content:'原文描述 [P1 p.1]',citations:[{tag:'P1',paper_id:seed,chunk_id:text.pages[0].chunks[0].chunk_id}]})).id);
      await page.locator('[data-view=deliverables]').click();await expect(page.locator('#delivList')).toHaveAttribute('aria-busy','false');
      const measurements=[];
      for(const theme of ['light','dark']) {
        await page.evaluate(theme=>{S.theme=theme;applyTheme();},theme);
        for(const width of [320,390,760,1360]) {
          await page.setViewportSize({width,height:880});await page.evaluate(()=>{S.sidebar=false;applySidebar();});
          for(const id of ids) {
            const row=page.locator(`[data-dstat="${id}"]`).locator('xpath=../..');
            const geometry=await row.evaluate(el=>{const main=el.querySelector('.list-main'),title=el.querySelector('.list-title'),actions=el.querySelector('[data-dstat]').parentElement;const r=e=>{const b=e.getBoundingClientRect();return {x:b.x,y:b.y,width:b.width,height:b.height,right:b.right,bottom:b.bottom};};return{row:r(el),main:r(main),title:r(title),actions:r(actions),controls:[...actions.children].map(r),scroll:document.documentElement.scrollWidth,viewport:innerWidth};});
            measurements.push({theme,width,id,...geometry});
            assert(geometry.scroll<=width+1,'Page overflows');
            if(width<=760){assert(geometry.main.width>=geometry.row.width-40,'Title remains squeezed by actions');assert(geometry.actions.y>=geometry.main.bottom-1,'Actions must follow content on narrow screens');}
            else {assert(geometry.actions.x>=geometry.main.right-1,'Desktop actions must remain alongside title');}
            assert(geometry.controls.every(c=>c.width>0&&c.x>=geometry.row.x&&c.right<=geometry.row.right+1),'An action exceeds its row');
            await expect(row.getByRole('button',{name:'导出',exact:true})).toBeVisible();await expect(row.getByRole('button',{name:'查看',exact:true})).toBeVisible();await expect(row.getByRole('button',{name:'编辑',exact:true})).toBeVisible();await expect(row.getByRole('button',{name:'删除',exact:true})).toBeVisible();
          }
          if(width===390||width===1360)await page.screenshot({path:path.join(output,`p2-deliverable-layout-${width}-${theme}.png`)});
        }
      }
      fs.writeFileSync(path.join(output,'deliverable-layout.json'),JSON.stringify(measurements,null,2));
      await page.locator(`[data-deliv="${ids[0]}"]`).click();await expect(page.locator('#researchTask')).toContainText('原文描述');await page.keyboard.press('Escape');
      await page.locator(`[data-dedit="${ids[0]}"]`).click();await expect(page.locator('#researchTask [name=title]')).toHaveValue(title);await page.keyboard.press('Escape');
    });
    await check('configuration-location-names-actual-action-and-never-guesses-path', async () => {
      const status=await call('/api/status');assert(status.config_path,'Backend must report the actual active config path');
      await page.locator('[data-view=settings]').click();await expect(page.locator('#btnOpenConfig')).toHaveAccessibleName('查看配置位置');
      await page.locator('#btnOpenConfig').click();await expect(page.locator('#configLocation')).toContainText(status.config_path);await expect(page.locator('#settingsModal')).toBeVisible();
      await expect(page.locator('#configLocation')).not.toContainText('已打开');
      for(const width of [390,1360]){await page.setViewportSize({width,height:880});assert(await page.locator('#configLocation').evaluate(el=>el.scrollWidth<=el.clientWidth+1),'Configuration path overflows');await page.screenshot({path:path.join(output,`p2-config-location-${width}.png`)});}
      const endpoint=base+'/api/status';
      const absent=route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({...status,config_path:null})});
      await page.route(endpoint,absent);
      try {await page.locator('#btnOpenConfig').click();await expect(page.locator('#configLocation')).toContainText('内置默认配置');await expect(page.locator('#configLocation')).not.toContainText('config.yaml');}
      finally {await page.unroute(endpoint,absent);}
      const missing=route=>{const data={...status};delete data.config_path;return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)});};
      await page.route(endpoint,missing);
      try {await page.locator('#btnOpenConfig').click();await expect(page.locator('#configLocation')).toContainText('服务未返回配置位置');await expect(page.locator('#configLocation')).not.toContainText('config.yaml');}
      finally {await page.unroute(endpoint,missing);}
      const failure=route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'配置位置读取失败'})});await page.route(endpoint,failure);
      try {await page.locator('#btnOpenConfig').click();await expect(page.locator('#configLocation')).toHaveAttribute('role','alert');await expect(page.locator('#configLocation')).toContainText('配置位置读取失败');await expect(page.locator('#btnOpenConfig')).toBeEnabled();await expect(page.locator('#configLocation')).not.toContainText('config.yaml');}
      finally {await page.unroute(endpoint,failure);}
      await page.locator('#btnOpenConfig').click();await expect(page.locator('#configLocation')).toContainText(status.config_path);
    });
    await check('preference-types-use-product-labels-and-skill-copy-keeps-execution-boundary', async () => {
      const records=[];
      for(const [kind,label,value]of [['style_preference','表达偏好','回答时优先使用简洁段落，必要处列出依据。'],['term_preference','术语偏好','术语首次出现时附上英文名称。'],['research_interest','研究方向','关注可追溯的学术证据检索。'],['misc','其他偏好','默认用中文进行说明。']]) {
        const result=await call('/api/memories','POST',{kind,value});assert(result.id,result.reason);records.push({id:result.id,kind,label});
      }
      const custom=await call('/api/memories','POST',{kind:'style_preference',key:'自定义展示名称',value:'回答结尾列出必要的下一步。'});assert(custom.id,custom.reason);
      await page.locator('[data-view=memories]').first().click();await expect(page.locator('#memoryList')).toHaveAttribute('aria-busy','false');
      for(const record of records)await expect(page.locator(`[data-edit-memory="${record.id}"]`).locator('..').locator('strong')).toHaveText(record.label);
      await expect(page.locator(`[data-edit-memory="${custom.id}"]`).locator('..').locator('strong')).toHaveText('自定义展示名称');
      await page.locator(`[data-edit-memory="${records[0].id}"]`).click();await expect(page.locator('#researchTask [name=kind]')).toHaveValue('style_preference');await expect(page.locator('#researchTask [name=kind] option:checked')).toHaveText('表达偏好');await page.keyboard.press('Escape');
      const go=async view=>{if(!await page.evaluate(()=>S.sidebar))await page.locator('#btnSidebar').click();await page.locator(`[data-view="${view}"]`).first().click();};
      for(const theme of ['light','dark'])for(const width of [390,1360]) {
        await page.setViewportSize({width,height:880});await page.evaluate(async({theme,width})=>{await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));S.theme=theme;applyTheme();S.sidebar=width>760;applySidebar();},{theme,width});
        await go('memories');await expect(page.locator('#memoryList')).toHaveAttribute('aria-busy','false');await page.screenshot({path:path.join(output,`p3-preferences-${width}-${theme}.png`)});
        await go('skills');await expect(page.locator('#skillCards')).toHaveAttribute('aria-busy','false');
        await expect(page.locator('.inline-note')).not.toContainText('一期');await expect(page.locator('.inline-note')).toContainText('声明式');await expect(page.locator('.inline-note')).toContainText('不会被加载或执行');
        assert(await page.locator('.inline-note > span').evaluate(el=>el.getBoundingClientRect().width>200),'Skill boundary sentence must wrap as one readable text block');
        await page.locator('.inline-note').scrollIntoViewIfNeeded();
        await page.screenshot({path:path.join(output,`p3-skills-${width}-${theme}.png`)});
      }
      const stored=(await call('/api/memories')).items;
      for(const record of records)assert.equal(stored.find(m=>m.id===record.id).kind,record.kind);
      assert.equal(stored.find(m=>m.id===custom.id).key,'自定义展示名称');
    });
    await check('g01-growing-library-pages-search-scope-and-older-conversations', async () => {
      await page.evaluate(()=>{S.theme='light';applyTheme();});
      const fixture = await call('/api/acceptance/library-capacity', 'POST', {});
      let scoped;
      try {
        await page.evaluate(async () => { await refreshCollections(); await refreshLibrary(); });
        const catalog = (await call('/api/papers/catalog')).items;
        await expect(page.locator('#libCount')).toHaveText(String(catalog.length));
        await expect(page.locator('#libList [data-paper]')).toHaveCount(20);
        await page.locator('#libSearch').fill('G01容量');
        await expect(page.locator('#libSub')).toHaveText('找到 301 篇匹配文献');
        await expect(page.locator('#paperList [data-paper]')).toHaveCount(50);
        await expect(page.locator('#libraryPageInfo')).toHaveText('1–50 / 301 篇');
        await page.locator('[data-select-paper]').first().check();
        await page.locator('#libraryNext').click();
        await expect(page.locator('#libraryPageInfo')).toHaveText('51–100 / 301 篇');
        await expect(page.locator('#btnPaperExport')).toBeDisabled();
        assert.equal(await page.evaluate(()=>S.selectedPapers.size),0);
        await page.evaluate(()=>loadSidebar());
        await expect(page.locator('#libraryPageInfo')).toHaveText('51–100 / 301 篇');
        for (let i=0;i<5;i++) {
          const before = await page.locator('#libraryPageInfo').textContent();
          await page.locator('#libraryNext').click();
          await expect(page.locator('#libraryPageInfo')).not.toHaveText(before);
        }
        await expect(page.locator('#libraryPageInfo')).toHaveText('301–301 / 301 篇');
        await expect(page.locator('#libraryNext')).toBeDisabled();
        await expect(page.locator(`#paperList [data-paper="${fixture.papers[0]}"]`)).toBeVisible();
        await expect(page.locator('#paperList')).toBeFocused();
        await page.screenshot({path:path.join(output,'g01-desktop-last-page.png')});
        await page.locator('#libSearch').fill('G01独有作者');
        await expect(page.locator('#paperList [data-paper]')).toHaveCount(1);
        await expect(page.locator('#libraryPager')).toBeHidden();
        await page.locator('[data-select-paper]').check();
        await page.locator('#libSearch').fill('');
        await page.locator('#collectionFilter').selectOption(fixture.parent);
        await expect(page.locator('#paperList [data-paper]')).toHaveCount(1);
        await expect(page.locator('#btnBatchDelete')).toBeDisabled();
        await expect(page.locator(`#paperList [data-paper="${fixture.papers[0]}"]`)).toBeVisible();
        scoped = (await call('/api/conversations','POST',{title:'G01范围可达性'})).id;
        await page.evaluate(id=>openView('chat',{convId:id}),scoped);
        await expect(page.locator('#scopeChip')).toBeVisible();
        await page.locator('#scopeChip').click();
        await page.locator('#researchTask [name=scope]').selectOption('p:'+fixture.papers[0]);
        await page.locator('#researchTask [type=submit]').click();
        await expect(page.locator('#researchTask')).toHaveCount(0);
        const saved = (await call('/api/conversations/'+scoped+'/messages')).conversation.scope;
        assert.deepEqual((typeof saved === 'string' ? JSON.parse(saved) : saved).paper_ids,[fixture.papers[0]]);
        await page.locator('#convSearch').fill('G01会话');
        await expect(page.locator('#convList [data-conv]')).toHaveCount(100);
        await expect(page.locator('#convCount')).toHaveText('101');
        await page.locator('#btnOlderConversations').click();
        await expect(page.locator('#convList [data-conv]')).toHaveCount(101);
        await expect(page.locator('#btnOlderConversations')).toHaveCount(0);
        await page.locator(`#convList [data-conv="${fixture.conversations[0]}"]`).scrollIntoViewIfNeeded();
        await expect(page.locator(`#convList [data-conv="${fixture.conversations[0]}"]`)).toBeVisible();
        await page.locator('#sidebar [data-view=library]').click();
        await page.locator('#collectionFilter').selectOption('');
        await page.locator('#libSearch').fill('G01容量');
        await expect(page.locator('#paperList [data-paper]')).toHaveCount(50);
        await page.setViewportSize({width:390,height:844});
        await page.evaluate(()=>{S.theme='dark';applyTheme();});
        await expect(page.locator('#libraryNext')).toBeVisible();
        assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
        await page.screenshot({path:path.join(output,'g01-narrow-dark-pagination.png')});
      } finally {
        if (scoped) await call('/api/conversations/'+scoped,'DELETE');
        await call('/api/acceptance/library-capacity','DELETE');
      }
    });
    await check('g01-library-late-reads-failure-and-retry-preserve-selection-boundaries', async () => {
      const fixture=await call('/api/acceptance/library-capacity','POST',{});
      try {
        await page.evaluate(()=>refreshLibrary());
        const url=base+'/api/papers?*';
        const held=await delayed(url,req=>new URL(req.url()).searchParams.get('q')==='G01容量');
        try {
          await page.locator('#libSearch').fill('G01容量');await held.ready;
          await page.locator('#libSearch').fill('G01独有作者');
          await expect(page.locator('#paperList [data-paper]')).toHaveCount(1);
          const late=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/papers'&&new URL(r.url()).searchParams.get('q')==='G01容量');
          held.release();await late;
          await expect(page.locator('#paperList [data-paper]')).toHaveCount(1);
          await expect(page.locator('#libSub')).toHaveText('找到 1 篇匹配文献');
        } finally {await held.close();}
        const fail=route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'G01列表暂不可读'})});
        await page.route(url,fail);
        try {
          await page.locator('[data-select-paper]').check();
          await page.evaluate(()=>loadLibrary());
          await expect(page.locator('#paperList [role=alert]')).toContainText('G01列表暂不可读');
          await expect(page.locator('[data-select-paper]')).toBeDisabled();
          await expect(page.locator('#btnPaperExport')).toBeDisabled();
        } finally {await page.unroute(url,fail);}
        await page.locator('#paperList [data-read-retry]').click();
        await expect(page.locator('#paperList [role=alert]')).toHaveCount(0);
        await expect(page.locator('[data-select-paper]')).toBeEnabled();
        await expect(page.locator('[data-select-paper]')).not.toBeChecked();
        await page.locator('#libSearch').fill('G01容量');
        await expect(page.locator('#libSub')).toHaveText('找到 301 篇匹配文献');
        await page.evaluate(()=>loadLibrary(300));
        await expect(page.locator('#libraryPageInfo')).toHaveText('301–301 / 301 篇');
        await call('/api/paper/'+fixture.papers[0],'DELETE');
        await page.evaluate(()=>loadLibrary(300));
        await expect(page.locator('#libraryPageInfo')).toHaveText('251–300 / 300 篇');
        await expect(page.locator('#paperList [data-paper]')).toHaveCount(50);
      } finally {await call('/api/acceptance/library-capacity','DELETE');}
    });
    await check('g02-reading-entry-materials-are-explicit-before-any-generation', async () => {
      const created=[];
      try {
        for (const [level,label] of Object.entries({metadata:'仅题录',abstract:'摘要',partial:'部分正文',fulltext:'已解析正文',unknown:'完整度未知'})) {
          const pid=(await call('/api/acceptance/reading','POST',{level})).paper_id;created.push(pid);
          await page.evaluate(()=>openView('library'));
          await page.evaluate(()=>refreshLibrary());
          await expect(page.locator(`#paperList [data-paper="${pid}"]`)).toBeVisible();
          await expect(page.locator(`#paperList [data-drag-paper="${pid}"]`)).toContainText('材料：'+label);
          await page.locator(`#paperList [data-paper="${pid}"]`).click();
          await expect(page.locator('#btnReanalyze')).toHaveText('解读论文');
          await expect(page.locator('#btnBudgetAnalyze')).toHaveCount(0);
          await expect(page.locator('[data-current-material]')).toContainText(label);
          await page.locator('#btnReanalyze').click();
          await expect(page.locator('#researchTask')).toContainText('本次材料：'+label);
          assert.equal((await call('/api/paper/'+pid+'/analysis-runs')).items.length,0);
          if (level==='metadata') {
            await expect(page.locator('#researchTask [type=submit]')).toBeDisabled();
            await expect(page.locator('#researchTask')).toContainText('可用文本过少');
          } else await expect(page.locator('#researchTask [type=submit]')).toBeEnabled();
          if(level==='partial')await page.screenshot({path:path.join(output,'g02-materials-desktop.png')});
          if(level==='unknown') {
            await page.setViewportSize({width:390,height:844});await page.evaluate(()=>{S.theme='dark';applyTheme();});
            const button=await page.locator('#researchTask [type=submit]').boundingBox();
            assert(button.x>=0&&button.x+button.width<=390&&button.y+button.height<=844);
            await page.screenshot({path:path.join(output,'g02-materials-narrow-dark.png')});
          }
          await page.locator('#researchTask [data-reading-text]').click();
          await expect(page.locator('#researchTask')).toHaveCount(0);
          await expect(page.locator('[data-ppane=text]')).toBeVisible();
        }
      } finally {for(const pid of created)await call('/api/paper/'+pid,'DELETE');}
    });
    await check('g02-default-reading-persists-unknown-outcomes-and-does-not-blindly-resubmit', async () => {
      const pid=(await call('/api/acceptance/reading','POST',{level:'abstract',retry:true,previous:true})).paper_id;
      const records=async()=> (await call('/api/paper/'+pid+'/analysis-runs')).items;
      try {
        await page.evaluate(id=>openView('paper',{paperId:id}),pid);
        await expect(page.locator('#paperDetail')).toContainText('PREVIOUS_READING_VISIBLE');
        await page.locator('#btnReanalyze').click();
        await expect(page.locator('#researchTaskTitle')).toHaveText('重新解读');
        await page.locator('#researchTask [name=with_concepts]').uncheck();
        await page.locator('#researchTask [name=semantic_review]').uncheck();
        await page.locator('#researchTask [type=submit]').click();
        await expect(page.locator('#researchTask')).toHaveCount(0);
        await expect(page.locator('#analysisRuns')).toContainText('运行失败');
        await expect(page.locator('#paperDetail')).toContainText('PREVIOUS_READING_VISIBLE');
        const failed=(await records())[0],rid=failed.id,before=failed.steps.length;
        assert(failed.retry_required);
        await page.reload();await expect(page.locator('#paperList [data-paper]').first()).toBeVisible();
        await page.evaluate(id=>openView('paper',{paperId:id}),pid);
        await expect(page.locator(`[data-resume-run="${rid}"]`)).toBeVisible();
        assert.equal((await records())[0].steps.length,before);
        await page.locator(`[data-resume-run="${rid}"]`).click();
        await expect(page.locator('#researchTask [name=retry_failed]')).not.toBeChecked();
        await page.locator('#researchTask [type=submit]').click();
        await expect(page.locator('#researchTask')).toHaveCount(0);
        await expect(page.locator('#analysisRuns')).toContainText('需确认重试');
        assert.equal((await records())[0].steps.length,before);
        await page.locator(`[data-resume-run="${rid}"]`).click();
        await page.locator('#researchTask [name=retry_failed]').check();
        await page.locator('#researchTask [type=submit]').click();
        await expect(page.locator('#researchTask')).toHaveCount(0);
        await expect(page.locator('#analysisRuns')).toContainText('仍有待审项');
        const done=(await records())[0];assert(done.steps.length>before&&!done.retry_required);
        assert.equal((await call('/api/paper/'+pid)).analysis._analysis_run_id,rid);
        await page.locator(`[data-resume-run="${rid}"]`).click();
        await page.locator('#researchTask [type=submit]').click();
        await expect(page.locator('#researchTask')).toHaveCount(0);
        assert.equal((await records())[0].steps.length,done.steps.length);
        const endpoint=base+'/api/paper/'+pid+'/analysis-runs';let starts=0;
        const lost=async route=>{if(route.request().method()!=='POST')return route.continue();starts++;await route.fetch();await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'G02回执丢失'})});};
        await page.route(endpoint,lost);
        try {
          await page.locator('#btnReanalyze').click();
          await page.locator('#researchTask [name=with_concepts]').uncheck();
          await page.locator('#researchTask [name=semantic_review]').uncheck();
          await page.locator('#researchTask [type=submit]').click();
          await expect(page.locator('#researchTask .task-error').last()).toContainText('请先查看运行记录');
          await expect(page.locator('#researchTask [type=submit]')).toBeHidden();
          await expect(page.locator('#researchTask [type=submit]')).toBeDisabled();
          await page.locator('#researchTask').evaluate(el=>el.querySelector('form').requestSubmit());
          assert.equal(starts,1);
          await page.locator('#researchTask').getByRole('button',{name:'查看运行记录',exact:true}).click();
          await expect(page.locator('#researchTask')).toHaveCount(0);
          await expect(page.locator('#analysisRuns [data-run-id]')).toHaveCount(2);
          assert.equal(starts,1);
        } finally {await page.unroute(endpoint,lost);}
      } finally {await call('/api/paper/'+pid,'DELETE');}
    });
    const openStreamFixture=async(mode='normal')=>{
      const fixture=await call('/api/acceptance/stream','POST',{mode});
      await page.evaluate(()=>loadSidebar());
      await page.locator(`[data-conv="${fixture.conversation_id}"]`).click();
      await page.locator('#chatInput').fill('streamingfixture 的原文是什么？');
      await page.locator('#btnSend').click();
      await expect(page.locator('#chatLiveDraft [data-live-text]')).toContainText('streamingfixture 提出了有依据的');
      return fixture;
    };
    const cleanStreamFixture=async fixture=>{
      await call('/api/acceptance/stream/'+fixture.key+'/release','POST',{});
      await call('/api/paper/'+fixture.paper_id,'DELETE');
      await call('/api/providers/'+fixture.provider_id,'DELETE');
    };
    await check('g05a-real-stream-before-final-reconnect-and-refresh-never-regenerate',async()=>{
      let fixture;
      try {
        fixture=await openStreamFixture();
        const cid=fixture.conversation_id;
        await expect(page.locator('#chatLiveDraft [data-live-thought] pre')).toHaveText('服务实际返回的思考片段。');
        await expect(page.locator('#chatRunPanel')).not.toHaveAttribute('open','');
        await expect(page.locator('#chatRunPanel')).not.toContainText('重试可能再次计费');
        await expect(page.locator('.from-assistant')).toHaveCount(0);
        let messages=(await call('/api/conversations/'+cid+'/messages')).items;
        assert.equal(messages.filter(m=>m.role==='assistant').length,0);
        await expect(page.locator('#chatLiveDraft [data-chat-review]')).toHaveCount(0);
        await page.locator('#chatLiveDraft summary').click();
        await page.screenshot({path:path.join(output,'g05a-live-desktop.png')});
        const rid=(await call('/api/conversations/'+cid+'/chat-runs')).items[0].id;
        await page.evaluate(({rid,cid})=>observeChatStream(rid,cid,true),{rid,cid});
        await expect(page.locator('#chatLiveDraft [data-live-text]')).toHaveText('streamingfixture 提出了有依据的');
        assert.equal((await call('/api/acceptance/stream/'+fixture.key)).qa_calls,1);
        await page.reload();await expect(page.locator('#paperList [data-paper]').first()).toBeVisible();
        await page.locator(`[data-conv="${cid}"]`).click();
        await expect(page.locator('#chatLiveDraft [data-live-text]')).toHaveText('streamingfixture 提出了有依据的');
        await page.setViewportSize({width:390,height:844});await page.evaluate(()=>{S.theme='dark';applyTheme();});
        assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
        await expect(page.locator('#chatLiveDraft [data-live-cancel]')).toBeEnabled();
        assert((await page.locator('#chatLiveDraft [data-live-cancel]').boundingBox()).height>=44);
        await page.screenshot({path:path.join(output,'g05a-live-narrow-dark.png')});
        await call('/api/acceptance/stream/'+fixture.key+'/release','POST',{});
        await expect(page.locator('.from-assistant')).toHaveCount(1);
        await expect(page.locator('#chatLiveDraft')).toHaveCount(0);
        await expect(page.locator('.from-assistant .answer-sources [data-evidence-ref]').first()).toBeVisible();
        messages=(await call('/api/conversations/'+cid+'/messages')).items;
        assert.equal(messages.filter(m=>m.role==='assistant').length,1);
        assert.equal(messages.find(m=>m.role==='assistant').status,'answered');
        assert.equal((await call('/api/acceptance/stream/'+fixture.key)).qa_calls,1);
      } finally {if(fixture)await cleanStreamFixture(fixture);}
    });
    await check('g05a-disconnect-and-cancel-keep-partial-text-untrusted-without-retry',async()=>{
      for(const mode of ['disconnect','cancel']) {
        let fixture;
        try {
          fixture=await openStreamFixture(mode);
          if(mode==='cancel')await page.locator('#chatLiveDraft [data-live-cancel]').click();
          else await call('/api/acceptance/stream/'+fixture.key+'/release','POST',{});
          await expect(page.locator('#chatLiveDraft [data-live-state]')).toContainText('生成已停止');
          await expect(page.locator('#chatLiveDraft [data-live-text]')).toHaveText('streamingfixture 提出了有依据的');
          await expect(page.locator('.from-assistant')).toHaveCount(0);
          const calls=await call('/api/acceptance/stream/'+fixture.key);
          assert.equal(calls.qa_calls,1);
          const record=(await call('/api/conversations/'+fixture.conversation_id+'/chat-runs')).items[0];
          assert.equal(record.status,mode==='cancel'?'cancelled':'failed');assert(record.retry_required);
          assert.equal((await call('/api/conversations/'+fixture.conversation_id+'/messages')).items.filter(m=>m.role==='assistant').length,0);
        } finally {if(fixture)await cleanStreamFixture(fixture);}
      }
    });
    await check('g05a-late-streams-stay-in-their-conversation-and-bad-citations-replace-preview',async()=>{
      let fixture,other;
      try {
        fixture=await openStreamFixture('bad_citation');
        other=(await call('/api/conversations','POST',{title:'流式隔离空会话'})).id;
        await page.evaluate(()=>loadSidebar());await page.locator(`[data-conv="${other}"]`).click();
        await expect(page.locator('#chatInput')).toBeVisible();
        await expect(page.locator('#chatLiveDraft')).toHaveCount(0);
        await call('/api/acceptance/stream/'+fixture.key+'/release','POST',{});
        await expect.poll(async()=> (await call('/api/conversations/'+fixture.conversation_id+'/chat-runs')).items[0].status).toBe('completed');
        await expect(page.locator('#chatLiveDraft')).toHaveCount(0);
        await expect(page.locator('.from-assistant')).toHaveCount(0);
        await page.locator(`[data-conv="${fixture.conversation_id}"]`).click();
        await expect(page.locator('.from-assistant')).toHaveCount(1);
        await expect(page.locator('#chatLiveDraft')).toHaveCount(0);
        const reply=(await call('/api/conversations/'+fixture.conversation_id+'/messages')).items.find(m=>m.role==='assistant');
        assert.notEqual(reply.status,'answered');
        assert.equal((await call('/api/acceptance/stream/'+fixture.key)).qa_calls,1);
      } finally {
        if(fixture)await cleanStreamFixture(fixture);
        if(other)await call('/api/conversations/'+other,'DELETE');
      }
    });
    results.push({ name: 'no-browser-errors-or-external-requests', passed: !errors.length && !external.length, errors, external });
    fs.writeFileSync(path.join(output, 'summary.json'), JSON.stringify({ results }, null, 2));
    for (const r of results) console.log(`${r.passed ? 'PASS' : 'FAIL'} ${r.name}${r.error ? ': ' + r.error : ''}`);
    if (results.some(r => !r.passed)) process.exitCode = 1;
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
