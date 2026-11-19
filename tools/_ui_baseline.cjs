/** 视觉基线/对比截图：12 视图关键组合 × 明暗 × 宽窄。
 *  用法：NODE_PATH=... node tools/_ui_baseline.cjs [--tag baseline|after]
 *  输出：_selftest.uipolish-20261121/<tag>/*.png */
const { chromium } = require('playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const BASE = process.env.LITPRISM_FULLTEST_URL || 'http://127.0.0.1:8765';
const TAG = process.argv.includes('--after') ? 'after' : 'baseline';
const OUT = path.join('E:/antigravity/_selftest.uipolish-20261121', TAG);
(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ channel: 'chrome', headless: true,
    args: ['--disable-gpu', '--disable-gpu-compositing', '--no-sandbox'] });
  const page = await browser.newPage({ viewport: { width: 1360, height: 880 } });
  page.setDefaultTimeout(20000);
  const shot = async name => page.screenshot({ path: path.join(OUT, name + '.png') });
  await page.goto(BASE + '/');
  await page.waitForTimeout(1500);
  const hasPaper = await page.locator('#paperList .list-item').count() > 0;

  // 宽屏 × 明暗：库/论文详情/原文/对话/设置
  for (const theme of ['paper', 'paper-dark']) {
    await page.evaluate(t => setTheme(t), theme);
    await page.waitForTimeout(300);
    await page.goto(BASE + '/'); await page.waitForTimeout(800);
    await shot(`library-${theme}-1360`);
    if (hasPaper) {
      await page.locator('#paperList .list-item').first().click();
      await page.waitForTimeout(1000);
      await shot(`paper-${theme}-1360`);
      await page.locator('#paperDetail').getByText('原文', { exact: true }).click();
      await page.waitForTimeout(500);
      await shot(`paper-text-${theme}-1360`);
    }
    await page.locator('[data-view="chat"]').first().click().catch(() => {});
    await page.waitForTimeout(800);
    await shot(`chat-${theme}-1360`);
    await page.evaluate(() => { const b = document.querySelector('[data-view="settings"], .side-item button'); });
    await page.evaluate(() => openSettings('general'));
    await page.waitForTimeout(500);
    await shot(`settings-${theme}-1360`);
    await page.keyboard.press('Escape');
  }

  // 窄屏 390 × 明暗：库与论文详情
  await page.setViewportSize({ width: 390, height: 844 });
  for (const theme of ['paper', 'paper-dark']) {
    await page.evaluate(t => setTheme(t), theme);
    await page.goto(BASE + '/'); await page.waitForTimeout(800);
    await shot(`library-${theme}-390`);
    if (hasPaper) {
      await page.locator('#paperList .list-item').first().click();
      await page.waitForTimeout(1000);
      await shot(`paper-${theme}-390`);
    }
  }
  await browser.close();
  console.log('baseline shots written to', OUT);
})();
