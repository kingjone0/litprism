/** 会话二：sidecar token 模式的浏览器实测（Electron 生产形态：#t= hash 传 token）。 */
const { chromium, expect } = require('playwright/test');
const fs = require('node:fs');
const BASE = 'http://127.0.0.1:8766';
const TOKEN = fs.readFileSync('E:/antigravity/_selftest.fulltest-20261120/token.env', 'utf-8')
  .match(/TOKEN=(\S+)/)[1];
(async () => {
  const results = [];
  const record = (name, ok, detail = '') => { results.push({ name, ok, detail }); console.log(`${ok ? 'PASS' : 'FAIL'} ${name} ${detail}`); };
  const browser = await chromium.launch({ channel: 'chrome', headless: true, args: ['--disable-gpu', '--no-sandbox'] });
  const page = await browser.newPage({ viewport: { width: 1360, height: 880 } });
  page.setDefaultTimeout(15000);
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  try {
    // 生产形态：hash 传 token，页面加载后 fetch 自动带 X-LitPrism-Token
    await page.goto(`${BASE}/#t=${encodeURIComponent(TOKEN)}`);
    await page.waitForTimeout(4000);  // boot 完成（无 token 模式下裸 fetch 会 401，须带 header 断言）
    const authed = await page.evaluate(async () => {
      const token = window.__LITPRISM__?.token;
      const r = await fetch('/api/status', { headers: token ? { 'X-LitPrism-Token': token } : {} });
      return { status: r.status, tokenPresent: !!token };
    });
    record('hash token 解析且 API 放行', authed.status === 200 && authed.tokenPresent,
      `tokenPresent=${authed.tokenPresent} /api/status=${authed.status}`);
    await expect(page.locator('#paperList')).toBeVisible({ timeout: 20000 });
    await page.screenshot({ path: 'E:/antigravity/_selftest.fulltest-20261120/token-session.png' });

    // 无 token 的新页面：API 必须拒绝
    const page2 = await browser.newPage();
    const noToken = await page2.goto(`${BASE}/`).then(async () =>
      page2.evaluate(async () => (await fetch('/api/status')).status));
    record('无 hash token 的新页面 API 401', noToken === 401, `status=${noToken}`);
    await page2.close();
    record('无未捕获页面错误', errors.length === 0, JSON.stringify(errors));
  } catch (e) {
    record('会话二异常', false, e.message);
  } finally {
    fs.writeFileSync('E:/antigravity/_selftest.fulltest-20261120/token-session-report.json',
      JSON.stringify(results, null, 2), 'utf-8');
    await browser.close();
  }
  process.exitCode = results.some(r => !r.ok) ? 1 : 0;
})();
