/** 无窗口的前端交互验收。需安装 playwright；截图目录由 LITPRISM_UI_OUTPUT 指定。 */
const { chromium, expect } = require('playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({
    channel: 'chrome', headless: true,
    args: ['--disable-gpu', '--disable-gpu-compositing', '--no-sandbox'],
  });
  try {
    const page = await browser.newPage({ viewport: { width: 1360, height: 820 } });
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    let sends = 0;
    const papers = [
      { id: 'p_demo1', title: '示例文献：注意力机制研究', authors: ['示例作者'], year: 2025, verify_level: 'unchecked' },
      { id: 'p_demo2', title: '示例文献：模型训练方法与实验分析', authors: ['示例作者'], year: 2024, verify_level: 'likely' },
    ];
    const sources = [{ kind: 'component', component: { type: 'citation-list', items: [
      { chunk_id: 'c_demo', paper_id: 'p_demo1', paper_title: papers[0].title, page: 3, section: '方法', text: '这是用于界面验收的示例原文片段。' },
    ] } }];
    await page.route('http://litprism.test/**', async route => {
      const url = new URL(route.request().url());
      if (!url.pathname.startsWith('/api/')) {
        const name = url.pathname === '/' ? 'index.html' : path.basename(url.pathname);
        const file = path.join(__dirname, '../src/litprism/web/static', name);
        const contentType = name.endsWith('.css') ? 'text/css' : name.endsWith('.js') ? 'application/javascript' : 'text/html';
        if (fs.existsSync(file)) return route.fulfill({ body: fs.readFileSync(file), contentType });
        return route.fulfill({ status: 404 });
      }
      let data = { items: [] };
      if (url.pathname === '/api/status') data = { stats: { papers: 2, analyses: 0 }, config: { app: { version: '1.0.0' } } };
      if (url.pathname === '/api/papers') data = { items: papers };
      if (url.pathname === '/api/conversations') data = route.request().method() === 'POST'
        ? { id: 'conv_demo' } : { items: [{ id: 'conv_demo', title: '示例研究对话', message_count: sends * 2 }] };
      if (url.pathname.endsWith('/messages')) data = { conversation: { title: '示例研究对话' }, items: [], sources: [] };
      if (url.pathname === '/api/paper/p_demo1/text') data = { pages: [{ page: 3, chunks: [{ section: '方法', text: '用于界面验收的原文。' }] }] };
      if (url.pathname === '/api/paper/p_demo1') data = { paper: papers[0], concepts: [] };
      if (url.pathname.startsWith('/api/chat/')) {
        sends++;
        await new Promise(r => setTimeout(r, 100));
        data = { user: { role: 'user', content: '示例问题' }, assistant: { role: 'assistant', content: '这是界面验收回复。[P1 p.3]', citation_check: { passed: false, issues: ['示例校验提示'] } }, sources };
      }
      return route.fulfill({ json: data });
    });
    await page.goto('http://litprism.test/');
    await expect(page.locator('#paperList .list-item')).toHaveCount(2);
    await expect(page.locator('#inspector')).toBeHidden();
    await expect(page.locator('.dots')).toHaveCount(0);
    await page.locator('#btnSearch').click();
    await expect(page.locator('#libSearch')).toBeFocused();
    await page.locator('#libSearch').fill('训练');
    await expect(page.locator('#paperList .list-item')).toHaveCount(1);
    await page.locator('#libSearch').fill('');

    const output = process.env.LITPRISM_UI_OUTPUT;
    if (output) {
      fs.mkdirSync(output, { recursive: true });
      await page.screenshot({ path: path.join(output, 'workbench-library.png') });
    }
    await page.locator('#btnNewConv').click();
    await expect(page.locator('#chatInput')).toBeVisible();
    await page.locator('.starter').first().click();
    assert((await page.locator('#chatInput').inputValue()).length > 0);
    await page.locator('#chatInput').fill('示例研究问题');
    await page.locator('#chatInput').press('Control+Enter');
    await page.locator('#chatInput').press('Control+Enter');
    await expect(page.locator('.from-assistant')).toHaveCount(1);
    assert.equal(sends, 1, '同一问题不应重复提交');
    await expect(page.locator('.lui-degraded')).toContainText('示例校验提示');
    await page.locator('#btnInspector').click();
    await expect(page.locator('#inspSources')).toContainText('p.3');
    await page.locator('#inspSources [data-cite]').click();
    await expect(page.locator('#inspSources [data-cite-text]')).toBeVisible();
    await page.locator('#btnTheme').click();
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
    if (output) await page.screenshot({ path: path.join(output, 'workbench-chat-dark.png') });
    await page.setViewportSize({ width: 1000, height: 660 });
    await expect(page.locator('#inspector')).toBeVisible();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    const input = await page.locator('#chatInput').boundingBox();
    assert(input.y + input.height <= 660, '输入区必须留在窗口内');
    await page.keyboard.press('Escape');
    await expect(page.locator('#inspector')).toBeHidden();
    await page.keyboard.press('Control+b');
    await expect(page.locator('#sidebar')).toBeHidden();
    await page.keyboard.press('Control+b');
    await expect(page.locator('#sidebar')).toBeVisible();
    await page.keyboard.press('Control+Alt+b');
    await expect(page.locator('#inspector')).toBeVisible();
    assert.deepEqual(errors, [], '页面运行不应有 JS 异常');
    console.log('Workbench checks passed: library, search, chat, citations, theme, shortcuts, narrow layout.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
