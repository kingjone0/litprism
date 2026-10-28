/** 空态按钮的端到端检查：空库 → 说明旁必须有可点按钮，且点了能触发原动作。 */
const { chromium } = require('playwright/test');
const assert = require('node:assert/strict');

(async () => {
  const base = process.env.BASE_URL;
  if (!base) throw new Error('BASE_URL required');
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const page = await browser.newPage({ viewport: { width: 1360, height: 880 } });
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));

  await page.goto(base, { waitUntil: 'networkidle' });
  await page.locator('[data-view="library"]').click();

  const importCta = page.locator('[data-empty-action="import"]');
  await importCta.waitFor({ state: 'visible', timeout: 15000 });
  assert.equal(await importCta.textContent(), '导入文献');
  // 右上角导入按钮的真实行为是唤起文件选择框：CTA 必须真触发它
  const [chooser] = await Promise.all([page.waitForEvent('filechooser', { timeout: 10000 }), importCta.click()]);
  assert(chooser, '空态导入按钮没有触发文件选择');
  console.log('OK library empty state -> file chooser');

  // 对比页空态：没有勾选时给"去知识库勾选"，点了必须真的回到知识库
  await page.evaluate(() => window.openView('compare'));
  await page.waitForTimeout(300);
  const gotoLib = page.locator('[data-empty-action="view:library"]').first();
  await gotoLib.waitFor({ state: 'visible', timeout: 10000 });
  await gotoLib.click();
  await page.waitForTimeout(300);
  // 回到知识库的标志：知识库空态（导入按钮）重新可见
  await page.locator('[data-empty-action="import"]').waitFor({ state: 'visible', timeout: 10000 });
  console.log('OK compare empty state -> navigate to library');

  assert.deepEqual(errors, [], `page errors: ${errors.join('; ')}`);
  await browser.close();
  console.log('DONE');
})().catch(err => { console.error('FAIL', err.message); process.exit(1); });
