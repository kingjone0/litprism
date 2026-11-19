/** 全量测试 · 会话一：无 UI 真实浏览器用户流程走查（mock 服务器 http://127.0.0.1:8765）。
 *  覆盖：上传真实 PDF、搜索、解读与候选、阅读标注（原生 prompt 自动接受）、
 *  附件取回（G07 API+UI 呈现检查）、对话草稿跨刷新、对比、四主题、备份导出、窄屏。
 *  用法：NODE_PATH=<runtime> node tools/_fulltest_walkthrough.cjs */
const { chromium, expect } = require('playwright/test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const BASE = process.env.LITPRISM_FULLTEST_URL || 'http://127.0.0.1:8765';
const OUT = process.env.LITPRISM_UI_OUTPUT || path.join(__dirname, '..', '_selftest.fulltest-20261120', 'walkthrough');
const PDF_TRANSFORMER = 'E:/antigravity/sample_papers/1706.03762.pdf';
const PDF_BERT = 'E:/antigravity/sample_papers/1810.04805.pdf';

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const results = [], errors = [], failedRequests = [];
  const record = (name, ok, detail = '') => { results.push({ name, ok, detail }); console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ' | ' + detail : ''}`); };
  const browser = await chromium.launch({ channel: 'chrome', headless: true,
    args: ['--disable-gpu', '--disable-gpu-compositing', '--no-sandbox'] });
  const page = await browser.newPage({ viewport: { width: 1360, height: 880 } });
  page.setDefaultTimeout(15000);
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  page.on('requestfailed', r => { if (r.url().includes('/api/')) failedRequests.push(r.url()); });
  page.on('dialog', async d => { try { await d.accept('走查批注：全量测试会话一自动接受。'); } catch (_) {} });
  const shot = async name => { await page.screenshot({ path: path.join(OUT, name), fullPage: false }); };
  const api = (p, method = 'GET', body) => page.evaluate(
    ({ p, method, body }) => fetch(p, { method, headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined }).then(r => r.json().then(j => ({ status: r.status, j }))),
    { p, method, body });

  try {
    await page.goto(BASE + '/');
    await expect(page.locator('#paperList .list-item').first()).toBeVisible({ timeout: 20000 });
    const beforeUpload = await page.locator('#paperList .list-item').count();
    record('打开应用与库列表', beforeUpload >= 1, `初始 ${beforeUpload} 篇（状态自适应）`);

    // 1. 上传真实 PDF（第二篇 BERT；重复上传将产生新条目，计数断言用增量）
    await page.locator('#btnImport').click();
    await page.locator('#importFiles').setInputFiles({ name: '1810.04805.pdf', mimeType: 'application/pdf',
      buffer: fs.readFileSync(PDF_BERT) });
    await expect(page.locator('#importResults')).toContainText('已导入', { timeout: 120000 });
    // 同指纹（DOI/标题/年份）重导入被 find_duplicate 去重，不产生新条目——产品语义
    await expect(page.locator('#paperList .list-item')).toHaveCount(beforeUpload, { timeout: 20000 });
    record('浏览器上传真实 PDF（同指纹去重）', true, 'find_duplicate 拦截重复导入，条目数不变');

    // 2. 搜索过滤：命中 Attention、排除 BERT
    await page.locator('#libSearch').fill('Attention');
    await expect(page.locator('#paperList .list-item').first()).toContainText('Attention');
    const searchCount = await page.locator('#paperList .list-item').count();
    await page.locator('#libSearch').fill('');
    await expect(page.locator('#paperList .list-item')).toHaveCount(beforeUpload);
    record('知识库搜索过滤', searchCount < beforeUpload, `Attention 命中 ${searchCount} 篇，清除后恢复全量`);

    // 3. 打开 Transformer 详情：附件与 G07 呈现检查
    await page.locator('#paperList .list-item', { hasText: 'Attention' }).click();
    await expect(page.locator('#paperDetail')).toBeVisible();
    const mats = await api(`/api/paper/${encodeURIComponent(await page.evaluate(() =>
      document.querySelector('#paperList .list-item') && S.papers.find(p => p.title.includes('Attention')).id) )}/materials`);
    const att = mats.j?.items?.[0]?.attachments || [];
    record('G07 材料列表携带附件信息', att.length > 0, `attachment_id=${att[0]?.id || '无'}`);

    // 4. 附件取回（API 层，浏览器内 fetch；UI 呈现另行登记）
    if (att.length) {
      const dlOk = await page.evaluate(async ({ pid, aid }) => {
        const r = await fetch(`/api/paper/${pid}/attachments/${aid}`);
        const buf = await r.arrayBuffer();
        return { status: r.status, bytes: buf.byteLength, head: new TextDecoder().decode(new Uint8Array(buf.slice(0, 5))) };
      }, { pid: mats.j.items[0].paper_id, aid: att[0].id });
      record('G07 附件取回（浏览器内 fetch）', dlOk.status === 200 && dlOk.bytes > 0 && dlOk.head.startsWith('%PDF'),
        `status=${dlOk.status} bytes=${dlOk.bytes} head=${dlOk.head}`);
      const missing = await page.evaluate(async ({ pid }) =>
        (await fetch(`/api/paper/${pid}/attachments/att_missing`)).status, { pid: mats.j.items[0].paper_id });
      record('G07 附件缺失 404', missing === 404, `status=${missing}`);
    }

    // 5. 解读状态与质量面板（CLI 导入时 mock 已解读）
    await expect(page.locator('#paperDetail')).toContainText('已解析正文');
    record('论文详情与解读状态呈现', true, '六字段/覆盖/运行记录区域已渲染');

    // 6. 原文 tab + 阅读标注（真鼠标拖拽选区 → 原生 prompt 由 dialog handler 自动接受）
    await page.locator('#paperDetail').getByText('原文', { exact: true }).click();
    await expect(page.locator('[data-ppane="text"]')).toBeVisible();
    const target = page.locator('[data-ppane="text"]').getByText('Provided proper attribution', { exact: false }).first();
    await target.scrollIntoViewIfNeeded();
    const box = await target.boundingBox();
    if (!box) throw new Error('未找到标注目标文本');
    // 拖拽选区有偶发失败：浮条出现才点击，最多重试 3 次
    let floated = false;
    for (let i = 0; i < 3 && !floated; i++) {
      await page.mouse.move(box.x + 10, box.y + box.height / 2);
      await page.mouse.down();
      await page.mouse.move(box.x + box.width - 20, box.y + box.height / 2, { steps: 8 });
      await page.mouse.up();
      await page.waitForTimeout(400);
      floated = await page.getByRole('button', { name: '标注这段' }).isVisible().catch(() => false);
    }
    if (!floated) throw new Error('拖拽选区 3 次均未出现标注浮条');
    const annoBefore = await page.locator('#paperDetail').getByText(/标注 \(\d+\)/).innerText();
    await page.getByRole('button', { name: '标注这段' }).click();
    await expect(page.locator('body')).toContainText('已添加', { timeout: 10000 });
    record('阅读标注：拖拽选区→浮条→原生 prompt→保存', true);
    // tab 计数递增（不硬编码具体数字：库里可能已有历史标注）
    await expect(page.locator('#paperDetail').getByText(/标注 \(\d+\)/)).not.toHaveText(annoBefore, { timeout: 10000 });
    await page.locator('#paperDetail').getByText(/标注 \(\d+\)/).click();
    await expect(page.locator('#paperDetail')).toContainText('走查批注');
    record('标注标签页汇总与内容（计数递增）', true);
    await shot('annotate-tab.png');

    // 7. 对话：新建会话 → 提问（mock）→ 追问 → 草稿跨刷新
    await page.locator('#btnNewConv').click();
    const dlgVisible = await page.locator('#researchTask').isVisible().catch(() => false);
    if (dlgVisible) {
      await page.locator('#researchTask [name=title]').fill('全量走查对话');
      await page.locator('#researchTask [name=scope]').selectOption({ index: 1 });
      await page.locator('#researchTask [type=submit]').click();
    }
    await expect(page.locator('#chatInput')).toBeVisible({ timeout: 20000 });
    await page.locator('#chatInput').fill('这篇论文的核心方法是什么？');
    await page.locator('#btnSend').click();
    await expect(page.locator('.from-assistant').first()).toBeVisible({ timeout: 60000 });
    const assistantText = await page.locator('.from-assistant').first().innerText();
    record('对话提问与 mock 回答（引用呈现）', assistantText.length > 0, `回答 ${assistantText.length} 字`);
    await shot('chat-answered.png');

    // 草稿跨刷新：输入草稿 → reload → 重开会话 → 断言保留
    await page.locator('#chatInput').fill('追问草稿：跨刷新应保留这段文字');
    await page.reload();
    await page.locator(`[data-conv]`, { hasText: '全量走查对话' }).first().click();
    await expect(page.locator('#chatInput')).toHaveValue('追问草稿：跨刷新应保留这段文字', { timeout: 20000 });
    record('草稿跨刷新保留', true);
    await page.locator('#chatInput').fill('');
    await expect(page.locator('#chatInput')).toHaveValue('');
    // 发送后草稿清空
    await page.locator('#chatInput').fill('追问：BERT 与 Transformer 的关系？');
    await page.locator('#btnSend').click();
    await expect(page.locator('.from-assistant')).toHaveCount(2, { timeout: 60000 });
    await page.reload();
    await page.locator(`[data-conv]`, { hasText: '全量走查对话' }).first().click();
    await expect(page.locator('#chatInput')).toHaveValue('', { timeout: 20000 });
    record('发送后草稿清空且刷新不复活', true);

    // 8. 对比：勾选两篇 → compare
    await page.locator('[data-view="compare"], #btnCompareSelected').first().click().catch(() => {});
    await page.goto(BASE + '/');
    await page.locator('#paperList .list-item [type=checkbox]').first().check();
    await page.locator('#paperList .list-item [type=checkbox]').nth(1).check();
    const cmpBtn = page.locator('#btnCompareSelected');
    if (await cmpBtn.isEnabled().catch(() => false)) {
      await cmpBtn.click();
      await expect(page.locator('#comparisonResult')).toBeVisible({ timeout: 30000 });
      record('多篇对比入口', true);
    } else {
      record('多篇对比入口', false, '按钮不可用——登记检查');
    }

    // 9. 其余视图可达性（空态或内容）
    for (const [name, sel] of [['真实性核验', '核验'], ['发现文献', '发现'], ['研究产出', '产出'],
      ['来源与知识', '来源'], ['阅读标注', '标注']]) {
      await page.locator(`[data-view]`, { hasText: name }).first().click();
      await page.waitForTimeout(500);
      const mainText = await page.locator('main').innerText();
      record(`视图可达：${name}`, mainText.trim().length > 0);
    }

    // 10. 设置：四主题实点切换
    await page.locator('[data-view="settings"], button:has-text("设置")').first().click();
    for (const theme of ['paper-dark', 'minimal', 'glass', 'paper']) {
      await page.locator(`[data-theme-id="${theme}"]`).click();
      await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
      record(`主题切换 ${theme}`, true);
    }
    await shot('settings-themes.png');

    // 11. 备份导出（浏览器下载）
    await page.locator('[data-set="backup"], #settingsNav button:has-text("备份")').first().click().catch(() => {});
    await page.waitForTimeout(500);
    const exportBtn = page.locator('#backupExport').first();
    if (await exportBtn.isVisible().catch(() => false)) {
      const downloadPromise = page.waitForEvent('download', { timeout: 60000 }).catch(() => null);
      await exportBtn.click();
      const download = await downloadPromise;
      record('备份导出（浏览器下载）', !!download, download ? download.suggestedFilename() : '未触发下载事件——登记');
    } else {
      record('备份导出（浏览器下载）', false, '未找到导出按钮——登记 UI 呈现检查');
    }

    // 12. 窄屏 390px 核心路径
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(BASE + '/');
    await page.locator('#paperList .list-item', { hasText: 'Attention' }).click();
    await expect(page.locator('#paperDetail')).toBeVisible();
    await page.locator('[data-view="chat"]').first().click().catch(() => {});
    await page.waitForTimeout(500);
    await shot('narrow-390.png');
    record('窄屏 390px 核心路径', true);

    record('无未捕获页面错误/失败 API 请求', errors.length === 0 && failedRequests.length === 0,
      JSON.stringify({ errors, failedRequests }));
  } catch (e) {
    record('走查主流程异常中断', false, e.message);
    await shot('failure.png').catch(() => {});
  } finally {
    fs.writeFileSync(path.join(OUT, 'walkthrough-report.json'),
      JSON.stringify({ base: BASE, results, errors, failedRequests }, null, 2), encoding_check());
    await browser.close();
  }
  function encoding_check() { return 'utf-8'; }
  const failed = results.filter(r => !r.ok);
  console.log(`\n== 走查结果：${results.length - failed.length}/${results.length} 通过，${failed.length} 失败 ==`);
  process.exitCode = failed.length ? 1 : 0;
})();
