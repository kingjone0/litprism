/** 四套主题的口径检查：只有一个 data-theme，且明暗/皮肤确实生效。 */
const { chromium } = require('playwright/test');
const assert = require('node:assert/strict');

const EXPECT = {
  paper: { dark: false },
  'paper-dark': { dark: true },
  minimal: { dark: true },
  glass: { dark: false },
};
const lum = rgb => {
  const [r, g, b] = rgb.match(/\d+/g).slice(0, 3).map(Number);
  return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
};

(async () => {
  const base = process.env.BASE_URL;
  const out = process.env.SHOT_DIR;
  if (!base) throw new Error('BASE_URL required');
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 820 } });
  await page.goto(base, { waitUntil: 'networkidle' });

  for (const [id, want] of Object.entries(EXPECT)) {
    await page.evaluate(theme => window.setTheme(theme), id);
    await page.waitForTimeout(150);
    const attr = await page.locator('html').getAttribute('data-theme');
    assert.equal(attr, id, `data-theme 应为 ${id}，实际 ${attr}`);
    const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    const fg = await page.evaluate(() => getComputedStyle(document.body).color);
    const bgL = lum(bg), fgL = lum(fg);
    if (want.dark) {
      assert(bgL < 0.25, `${id} 背景偏亮：${bg} (L=${bgL.toFixed(2)})`);
      assert(fgL > bgL, `${id} 文字比背景还暗：${fg} on ${bg}`);
    } else {
      assert(bgL > 0.6, `${id} 背景偏暗：${bg} (L=${bgL.toFixed(2)})`);
      assert(fgL < bgL, `${id} 文字比背景还亮：${fg} on ${bg}`);
    }
    const skin = await page.evaluate(() => document.documentElement.dataset.skin);
    assert.equal(skin, undefined, '不应再有 data-skin');
    if (out) await page.screenshot({ path: `${out}/theme-${id}.png` });
    console.log(`OK ${id} bg=${bg} fg=${fg}`);
  }
  await browser.close();
  console.log('DONE');
})().catch(e => { console.error('FAIL', e.message); process.exit(1); });
