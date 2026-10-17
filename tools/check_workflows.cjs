/** 与 check_workflows.py 的真实本地 API 配合，无可见窗口、无真实模型。 */
const { chromium, expect } = require('playwright/test');
const assert = require('node:assert/strict');

(async () => {
  const base = process.env.LITPRISM_ACCEPTANCE_URL;
  if (!base) throw new Error('请通过 tools/check_workflows.py 启动');
  const browser = await chromium.launch({ channel: 'chrome', headless: true, args: ['--disable-gpu', '--disable-gpu-compositing', '--no-sandbox'] });
  try {
    const page = await browser.newPage({ viewport: { width: 1360, height: 880 } });
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    page.on('dialog', dialog => dialog.accept());
    await page.route('**/*', route => route.request().url().startsWith(base) ? route.continue() : route.abort());
    await page.goto(base + '/#t=acceptance-only');
    await expect(page.locator('#paperList .list-item')).toHaveCount(1);

    // 首次导入必须因未核验而失败，人工确认后重新上传。
    const file = { name: 'acceptance.md', mimeType: 'text/markdown', buffer: Buffer.from('# Acceptance Study\n\nalphatopic 用于离线验收的原文描述。 '.repeat(25)) };
    await page.locator('#importFiles').setInputFiles(file);
    await expect(page.locator('[data-import-verify]')).toBeVisible();
    await expect(page.locator('#paperList .list-item')).toHaveCount(1);
    await page.locator('[data-import-verify]').click();
    await expect(page.locator('#wlReason')).toBeVisible();
    await page.locator('#wlReason').fill('离线验收：测试人员已经查阅原文。');
    await page.locator('#btnWlConfirm').click();
    await expect(page.locator('#verifyResult')).toContainText('manual_confirmed');
    await page.locator('.workspace-nav [data-view=library]').click();
    await page.locator('#importFiles').setInputFiles(file);
    await expect(page.locator('#paperList .list-item')).toHaveCount(2);

    await page.locator('#btnCollection').click();
    await page.locator('#researchTask [name=name]').fill('验收分组');
    await page.locator('#researchTask [type=submit]').click();
    await expect(page.locator('#researchTask')).toHaveCount(0);
    await page.locator('[data-select-paper]').nth(0).check();
    await page.locator('[data-select-paper]').nth(1).check();
    await page.locator('#btnMovePapers').click();
    const group = await page.locator('#researchTask [name=collection_id] option').nth(1).getAttribute('value');
    await page.locator('#researchTask [name=collection_id]').selectOption(group);
    await page.locator('#researchTask [type=submit]').click();
    await expect(page.locator('#researchTask')).toHaveCount(0);
    await page.locator('#collectionFilter').selectOption(group);
    await expect(page.locator('#paperList .list-item')).toHaveCount(2);

    // 对比 → 保存 → 预览 → 下载归档，均走真实守卫。
    await page.locator('#btnCompareSelected').click();
    await expect(page.locator('#btnSaveComparison')).toBeVisible();
    await page.locator('#btnSaveComparison').click();
    await page.locator('[data-view=deliverables]').first().click();
    await expect(page.locator('[data-deliv]')).toHaveCount(1);
    await page.locator('[data-deliv]').first().click();
    await expect(page.locator('#researchTask')).toContainText('引用来源');
    await page.locator('#researchTask [data-cancel]').first().click();
    const [archive] = await Promise.all([page.waitForEvent('download'), page.locator('#btnExportAll').click()]);
    assert.equal(archive.suggestedFilename(), 'litprism-deliverables.zip');

    await page.locator('.workspace-nav [data-view=library]').click();
    await page.locator('#paperList [data-paper]').first().click();
    await page.locator('#btnReanalyze').click();
    await expect(page.locator('#paperDetail')).toContainText('offline:acceptance-fixture');

    // 新建会话范围、问答、原文证据与每轮请求快照。
    await page.locator('#btnNewConv').click();
    await page.locator('#researchTask [name=scope]').selectOption('p:' + process.env.LITPRISM_ACCEPTANCE_SEED);
    await page.locator('#researchTask [type=submit]').click();
    await expect(page.locator('#chatInput')).toBeVisible();
    await page.locator('#chatInput').fill('alphatopic');
    await page.locator('#btnSend').click();
    await expect(page.locator('.from-assistant')).toHaveCount(1);
    await expect(page.locator('.from-assistant')).toContainText('offline:acceptance-fixture');
    await page.locator('#btnInspector').click();
    await expect(page.locator('#inspSources')).toContainText('引用片段');
    await page.locator('[data-insp=context]').click();
    await page.locator('#btnTurnReads').click();
    await expect(page.locator('#researchTask')).toContainText('request');
    await page.locator('#researchTask [data-cancel]').first().click();
    await page.keyboard.press('Escape');

    // 偏好创建、编辑、删除与恢复。
    await page.locator('[data-view=memories]').first().click();
    await page.locator('#btnAddMemory').click();
    await page.locator('#researchTask [name=value]').fill('请使用简短中文。');
    await page.locator('#researchTask [type=submit]').click();
    await expect(page.locator('#memoryList')).toContainText('请使用简短中文。');
    await page.locator('[data-edit-memory]').click();
    await page.locator('#researchTask [name=value]').fill('请使用详细中文。');
    await page.locator('#researchTask [type=submit]').click();
    await page.locator('[data-delete-memory]').click();
    await page.locator('#btnUndoMemory').click();
    await expect(page.locator('#memoryList')).toContainText('请使用详细中文。');

    // Skill 创建 → 参数输入 → 生成 → 引用校验。
    await page.locator('[data-view=skills]').first().click();
    await page.locator('#btnSkillNew').click();
    await page.locator('#nsId').fill('acceptance-skill');
    await page.locator('#nsName').fill('离线验收技能');
    await page.locator('#nsCreate').click();
    await expect(page.locator('[data-run-skill]')).toHaveCount(1);
    await page.locator('[data-run-skill]').click();
    await page.locator('#researchTask [name=topic]').fill('alphatopic');
    await page.locator('#researchTask [type=submit]').click();
    await expect(page.locator('#skillRunResult')).toContainText('用于离线验收');

    await page.locator('[data-view=recommend]').first().click();
    await page.locator('#recommendTopic').fill('alphatopic');
    await page.locator('#recommendForm button').click();
    await expect(page.locator('#recommendResults')).toContainText('候选');

    // 文献软删除后可以恢复原文。
    await page.locator('.workspace-nav [data-view=library]').click();
    await page.locator('#paperList [data-paper]').first().click();
    await page.locator('#btnDelete').click();
    await expect(page.locator('#paperList .list-item')).toHaveCount(1);
    await expect(page.locator('#btnCompareSelected')).toBeDisabled();
    await expect(page.locator('#libSummary')).toContainText('1');
    await page.locator('[data-view=trash]').first().click();
    await expect(page.locator('[data-restore-paper]')).toHaveCount(1);
    await page.locator('[data-restore-paper]').click();
    await expect(page.locator('[data-restore-paper]')).toHaveCount(0);
    await page.locator('.workspace-nav [data-view=library]').click();
    await expect(page.locator('#paperList .list-item')).toHaveCount(2);

    // 空分组不误报为空库；删除正在筛选的分组后返回全库。
    await page.locator('#btnCollection').click();
    await page.locator('#researchTask [name=name]').fill('空分组');
    await page.locator('#researchTask [type=submit]').click();
    await expect(page.locator('#researchTask')).toHaveCount(0);
    const emptyGroup = await page.locator('#collectionFilter option', { hasText: '空分组' }).getAttribute('value');
    await page.locator('#collectionFilter').selectOption(emptyGroup);
    await expect(page.locator('#paperList')).toContainText('此分组暂无文献');
    await page.locator('#btnCollection').click();
    await page.locator(`[data-delete-group="${emptyGroup}"]`).click();
    await expect(page.locator('#researchTask')).toHaveCount(0);
    await expect(page.locator('#collectionFilter')).toHaveValue('');
    await expect(page.locator('#paperList .list-item')).toHaveCount(2);

    // 新工作流在深色主题与桌面最小窗口内保持可操作。
    await page.setViewportSize({ width: 1000, height: 660 });
    await page.locator('#btnTheme').click();
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
    await page.locator('#btnPaperExport').click();
    await expect(page.locator('#researchTask')).toBeVisible();
    const submit = await page.locator('#researchTask [type=submit]').boundingBox();
    assert(submit.x >= 0 && submit.y >= 0 && submit.x + submit.width <= 1000 && submit.y + submit.height <= 660);
    await page.locator('#researchTask [name=format]').selectOption('bibtex');
    const [bib] = await Promise.all([page.waitForEvent('download'), page.locator('#researchTask [type=submit]').click()]);
    assert.equal(bib.suggestedFilename(), 'litprism.bib');
    await expect(page.locator('#researchTask')).toHaveCount(0);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.deepEqual(errors, []);
    console.log('Real API workflow acceptance passed: import guard + confirmation, groups, comparison, export, analysis, scoped chat, snapshots, preferences, skills, recommendations, recycle bin.');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
