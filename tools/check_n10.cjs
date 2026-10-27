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
