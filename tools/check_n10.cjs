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
    await check('chat-receipts-collapse-only-automatic-success', async () => {
      const conversation = await call('/api/conversations', 'POST', {title:'记录展开反例'});
      await page.evaluate(async id=>{await loadSidebar();openView('chat',{convId:id});},conversation.id);
      await expect(page.locator('#chatInput')).toBeVisible();
      await page.locator('#chatInput').fill('alphatopic'); await page.locator('#btnSend').click();
      await expect(page.locator('.from-assistant')).toHaveCount(1);
      const panel=page.locator('#chatRunPanel');
      await expect(panel).not.toHaveAttribute('open');
      await expect(page.locator('#btnSend')).toBeEnabled();
      await page.locator('#chatInput').fill('alphatopic durablereloadmarker'); await page.locator('#btnSend').click();
      await expect(panel).toHaveAttribute('open','');
      await page.locator('#chatRunPanel > summary').click();
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
    results.push({ name: 'no-browser-errors-or-external-requests', passed: !errors.length && !external.length, errors, external });
    fs.writeFileSync(path.join(output, 'summary.json'), JSON.stringify({ results }, null, 2));
    for (const r of results) console.log(`${r.passed ? 'PASS' : 'FAIL'} ${r.name}${r.error ? ': ' + r.error : ''}`);
    if (results.some(r => !r.passed)) process.exitCode = 1;
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
