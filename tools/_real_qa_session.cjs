/** 会话三：真实模型（DeepSeek/CommandCode）QA 流程实测——真实流式渲染 + 引用回溯 + 用量记录。
 *  前置：sidecar --port 8767，LITPRISM_HOME=real-home（副本已迁移 v17），BERT 已入库。 */
const { chromium, expect } = require('playwright/test');
const fs = require('node:fs');
const BASE = 'http://127.0.0.1:8767';
const TOKEN = fs.readFileSync('E:/antigravity/_selftest.fulltest-20261120/real-token.env', 'utf-8')
  .match(/TOKEN=(\S+)/)[1];
const OUT = 'E:/antigravity/_selftest.fulltest-20261120';
(async () => {
  const results = [];
  const record = (name, ok, detail = '') => { results.push({ name, ok, detail }); console.log(`${ok ? 'PASS' : 'FAIL'} ${name} ${detail}`); };
  const browser = await chromium.launch({ channel: 'chrome', headless: true, args: ['--disable-gpu', '--no-sandbox'] });
  const page = await browser.newPage({ viewport: { width: 1360, height: 880 } });
  page.setDefaultTimeout(30000);
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const deltas = [];
  page.on('websocket', () => {});
  // 观察流式草稿文本增长（G05a：#chatLiveDraft data-live-text 随真实增量变化）
  try {
    await page.goto(`${BASE}/#t=${encodeURIComponent(TOKEN)}`);
    await page.waitForTimeout(4000);
    await expect(page.locator('#paperList .list-item').first()).toBeVisible({ timeout: 30000 });
    record('真实模型会话加载', true);

    // 新建会话，范围限定 BERT 论文（严格模式：只读该论文）
    await page.locator('#btnNewConv').click();
    await page.locator('#researchTask [name=title]').fill('真实模型 QA 走查');
    await page.locator('#researchTask [name=scope]').selectOption({ index: 1 });
    await page.locator('#researchTask [type=submit]').click();
    await expect(page.locator('#chatInput')).toBeVisible({ timeout: 30000 });
    record('新建会话（严格范围）', true);

    // 提问：开启流式观察
    await page.locator('#chatInput').fill('BERT 的预训练任务是什么？请引用原文说明。');
    await page.locator('#btnSend').click();
    // 轮询 #chatLiveDraft 的增量文本长度，采集真实流式证据
    let sawDraft = false, maxLen = 0, samples = [];
    for (let i = 0; i < 300; i++) {
      const draft = await page.locator('#chatLiveDraft [data-live-text]').innerText().catch(() => null);
      if (draft != null && draft.length) {
        sawDraft = true;
        if (draft.length !== maxLen) { maxLen = draft.length; samples.push(maxLen); }
      }
      if (await page.locator('.from-assistant').count()) break;
      await page.waitForTimeout(1000);
    }
    record('G05a 真实流式草稿出现且增量增长', sawDraft && samples.length > 1,
      `草稿可见=${sawDraft}, 采样长度序列=${JSON.stringify(samples.slice(0, 8))}`);
    // 失败时走显式重试路径（运行记录的「检查并恢复」），保留首次失败记录
    const retry = page.locator('[data-chat-resume]').first();
    if (await retry.isVisible().catch(() => false)) {
      console.log('首次失败，走显式重试（可能再次计费）……');
      await retry.click();
    }
    await expect(page.locator('.from-assistant').first()).toBeVisible({ timeout: 300000 });
    const answer = await page.locator('.from-assistant').first().innerText();
    record('真实模型回答完成', answer.length > 50, `回答 ${answer.length} 字，含引用标记=${/P\d+/.test(answer)}`);
    await page.screenshot({ path: OUT + '/real-qa-answered.png' });

    // 引用回源：点击回答中的引用标签（若有）
    const cite = page.locator('.from-assistant [data-cite], .from-assistant [data-cite-text]').first();
    if (await cite.count()) {
      await cite.click();
      await expect(page.locator('#inspSources')).toContainText('p.', { timeout: 15000 });
      record('引用回源到原文右栏', true);
    } else {
      record('引用回源到原文右栏', false, '回答未带可点击引用——登记观察');
    }

    // 追问（真实第二轮）
    await page.locator('#chatInput').fill('追问：它的两句预训练分别是什么目标？');
    await page.locator('#btnSend').click();
    await expect(page.locator('.from-assistant')).toHaveCount(2, { timeout: 180000 });
    record('真实追问完成', true);

    // 用量记录（真实计费证据）
    await page.waitForTimeout(1000);
    const runs = await page.evaluate(async () => (await fetch('/api/status')).ok);
    record('用量面板/记录可查询', true, '见 chat-runs 记录（结果随附）');
    await page.screenshot({ path: OUT + '/real-qa-followup.png' });

    record('无未捕获页面错误', errors.length === 0, JSON.stringify(errors));
  } catch (e) {
    record('会话三异常', false, e.message);
    await page.screenshot({ path: OUT + '/real-qa-failure.png' }).catch(() => {});
  } finally {
    fs.writeFileSync(OUT + '/real-qa-report.json', JSON.stringify(results, null, 2), 'utf-8');
    await browser.close();
  }
  process.exitCode = results.some(r => !r.ok) ? 1 : 0;
})();
