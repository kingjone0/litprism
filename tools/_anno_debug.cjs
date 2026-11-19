/** 标注保存后 tab 计数刷新的间歇性失败最小复现。 */
const { chromium } = require('playwright/test');
const BASE = 'http://127.0.0.1:8765';
(async () => {
  const browser = await chromium.launch({ channel: 'chrome', headless: true,
    args: ['--disable-gpu', '--no-sandbox'] });
  const page = await browser.newPage({ viewport: { width: 1360, height: 880 } });
  page.setDefaultTimeout(15000);
  page.on('dialog', async d => { try { await d.accept('复现批注 ' + Date.now()); } catch (_) {} });
  page.on('response', r => {
    if (r.url().includes('/annotations')) console.log('NET', r.status(), r.request().method(), r.url().slice(BASE.length));
  });
  page.on('pageerror', e => console.log('PAGEERROR', e.message));
  await page.goto(BASE + '/');
  await page.locator('#paperList .list-item', { hasText: 'Attention' }).click();
  await page.locator('#paperDetail').getByText(/标注 \(\d+\)/).click();
  const countBefore = await page.locator('#paperDetail').getByText(/标注 \(\d+\)/).innerText();
  console.log('tab before:', countBefore);
  await page.locator('#paperDetail').getByText('原文', { exact: true }).click();
  await page.locator('[data-ppane="text"]').getByText('Provided proper attribution', { exact: false }).first().scrollIntoViewIfNeeded();
  const box = await page.locator('[data-ppane="text"]').getByText('Provided proper attribution', { exact: false }).first().boundingBox();
  for (let i = 0; i < 3; i++) {
    await page.mouse.move(box.x + 10, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width - 20, box.y + box.height / 2, { steps: 8 });
    await page.mouse.up();
    await page.waitForTimeout(400);
    if (await page.getByRole('button', { name: '标注这段' }).isVisible().catch(() => false)) break;
  }
  await page.getByRole('button', { name: '标注这段' }).click();
  await page.waitForTimeout(2500);
  const countAfter = await page.locator('#paperDetail').getByText(/标注 \(\d+\)/).innerText();
  console.log('tab after (2.5s):', countAfter);
  await page.waitForTimeout(3000);
  const countLate = await page.locator('#paperDetail').getByText(/标注 \(\d+\)/).innerText();
  console.log('tab after (5.5s):', countLate);
  await browser.close();
})();
