/** 与 check_workflows.py 的真实本地 API 配合，无可见窗口、无真实模型。 */
const { chromium, expect } = require('playwright/test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

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
    await expect(page.locator('#comparisonResult')).toContainText('一致点');
    await expect(page.locator('#comparisonResult')).toContainText('结论与证据映射');
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
    await expect(page.locator('#paperDetail')).toContainText('1 项待核对');
    await expect(page.locator('#paperDetail')).toContainText('用量未知');
    await expect(page.locator('#paperDetail [data-ppane=analysis] > .card').last()).not.toContainText('待核查陈述');
    await page.locator('[data-semantic-claim]').first().click();
    await expect(page.locator('#inspSources')).toContainText('陈述与整体差异核对');
    const expectedSourceIds = await page.evaluate(async () => {
      const d = await api(semanticEndpoint(S.semanticRef));
      const claim = d.report.items.find(c => c.id === S.semanticRef.claimId);
      return d.sources.filter(s => claim.candidate_tags.includes(s.tag)).map(s => s.chunk_id).sort();
    });
    assert(expectedSourceIds.length >= 1 && expectedSourceIds.length <= 3);
    const displayedSourceIds = await page.locator('#inspSources [data-evidence-ref]')
      .evaluateAll(nodes => nodes.map(n => n.dataset.evidenceRef).sort());
    assert.deepEqual(displayedSourceIds, expectedSourceIds);
    await page.locator('#semanticReview [name=reviewed_text]').fill('semantichumanmarker 人工修正的知识陈述。');
    await page.locator('#semanticReview [name=note]').fill('离线验收：已查对原文、数字与适用条件。');
    await page.locator('#semanticReview [value=confirm]').click();
    await expect(page.locator('#semanticReview [value=confirm]')).toBeDisabled();
    await expect(page.locator('#inspSources')).toContainText('人工审核历史');
    if (process.env.LITPRISM_UI_OUTPUT) {
      fs.mkdirSync(process.env.LITPRISM_UI_OUTPUT, {recursive:true});
      await page.screenshot({path:path.join(process.env.LITPRISM_UI_OUTPUT, 'semantic-review-light.png')});
      await page.evaluate(() => document.documentElement.dataset.theme = 'dark');
      await page.setViewportSize({width:850,height:880});
      await page.screenshot({path:path.join(process.env.LITPRISM_UI_OUTPUT, 'semantic-review-dark-narrow.png')});
      await page.setViewportSize({width:1360,height:880});
      await page.evaluate(() => document.documentElement.dataset.theme = 'light');
    }
    await page.locator('#semanticReview [name=note]').fill('离线验收撤销');
    await page.locator('#semanticReview [value=revoke]').click();
    await expect(page.locator('#semanticReview [value=confirm]')).toBeEnabled();
    await page.keyboard.press('Escape');
    await page.locator('[data-ptab=concepts]').click();
    await expect(page.locator('[data-ppane=concepts]')).toContainText('alphatopic');

    // Skip machine review: persisted candidates can still be reviewed and revoked.
    await page.locator('#btnBudgetAnalyze').click();
    await page.locator('#researchTask [name=semantic_review]').uncheck();
    await page.locator('#researchTask [name=with_concepts]').uncheck();
    await page.locator('#researchTask [type=submit]').click();
    await expect(page.locator('#researchTask')).toHaveCount(0);
    await expect(page.locator('#paperDetail')).toContainText('候选');
    await page.locator('[data-semantic-claim]').first().click();
    await expect(page.locator('#inspSources')).toContainText('未经机器核查');
    await page.locator('#semanticReview [name=reviewed_text]').fill('candidatehumanmarker 人工核对后的知识。');
    await page.locator('#semanticReview [name=note]').fill('离线验收：直接人工查对候选原文。');
    await page.locator('#semanticReview [value=confirm]').click();
    await expect(page.locator('#semanticReview [value=confirm]')).toHaveText('已人工确认入库');
    await page.locator('#semanticReview [name=note]').fill('候选入库后撤销验收');
    await page.locator('#semanticReview [value=revoke]').click();
    await expect(page.locator('#semanticReview [value=confirm]')).toBeEnabled();
    await page.keyboard.press('Escape');

    // 新建会话范围、问答、原文证据与每轮请求快照。
    await page.locator('#btnNewConv').click();
    await page.locator('#researchTask [name=scope]').selectOption('p:' + process.env.LITPRISM_ACCEPTANCE_SEED);
    await page.locator('#researchTask [type=submit]').click();
    await expect(page.locator('#chatInput')).toBeVisible();
    await expect(page.locator('#scopeChip')).toContainText('仅限：');
    await page.locator('#scopeChip').click();
    await expect(page.locator('#researchTask [name=mode]')).toHaveCount(0);
    await page.locator('#researchTask [name=scope]').selectOption('');
    await page.locator('#researchTask [type=submit]').click();
    await expect(page.locator('#scopeChip')).toContainText('全库知识与联网');
    await expect(page.locator('#chatWebSearch')).toHaveCount(0);
    const defaultRequest = page.waitForRequest(r => r.method()==='POST' && r.url().endsWith('/chat-runs'));
    await page.locator('#chatInput').fill('alphatopic');
    await page.locator('#btnSend').click();
    assert.equal((await defaultRequest).postDataJSON().web_search,true);
    await expect(page.locator('.from-assistant')).toHaveCount(1);
    await expect(page.locator('.from-assistant')).toContainText('offline:acceptance-fixture');
    await expect(page.locator('.chat-reasoning')).toHaveCount(1);
    await page.locator('.chat-reasoning summary').click();
    await expect(page.locator('.chat-reasoning pre')).toHaveText('<script>thinking_ui_marker</script>');
    assert.equal(await page.locator('.chat-reasoning script').count(), 0);
    await expect(page.locator('.request-budget')).toHaveCount(0);
    await expect(page.locator('.from-assistant')).not.toContainText('等待设置');
    const storedDiagnostics = await page.evaluate(async () => {
      const d = await api('/api/conversations/'+S.activeConv+'/messages');
      return d.items.filter(m=>m.role==='assistant').at(-1).request_settings;
    });
    assert.equal(storedDiagnostics.max_tokens,384000);
    assert.equal(storedDiagnostics.timeout,300);
    await expect(page.locator('.chat-bubble').last()).not.toContainText('thinking_ui_marker');

    await page.locator('.answer-sources [data-evidence-ref]').first().click();
    await expect(page.locator('#inspSources')).toContainText('论文原文');
    await expect(page.locator('#inspSources')).toContainText('自动核验通过');
    await expect(page.locator('#inspSources')).toContainText('用于离线验收的原文描述');
    await page.locator('#btnSourceBack').click();
    await expect(page.locator('#inspSources')).toContainText('引用片段');
    await page.locator('[data-insp=context]').click();
    await page.locator('#btnTurnReads').click();
    await expect(page.locator('#researchTask')).toContainText('request');
    await page.locator('#researchTask [data-cancel]').first().click();
    await page.keyboard.press('Escape');

    // 知识快照：保存候选，在右侧核对并人工确认，再通过回复链接回看。
    const auditConv = await page.evaluate(() => S.activeConv);
    await page.locator('#chatInput').fill('alphatopic refusalcitationmarker');
    await page.locator('#btnSend').click();
    await expect(page.locator('.from-assistant')).toHaveCount(2);
    await expect(page.locator('.from-assistant').last()).toContainText('证据不足');
    await page.locator('.from-assistant').last().locator('.answer-sources [data-evidence-ref]').first().click();
    await expect(page.locator('#inspSources')).toContainText('论文原文');
    await page.reload();
    await page.locator(`[data-conv="${auditConv}"]`).click();
    await expect(page.locator('.from-assistant').last().locator('.answer-sources [data-evidence-ref]')).toHaveCount(1);
    await page.locator('#chatInput').fill('alphatopic invalidcitationmarker');
    await page.locator('#btnSend').click();
    await expect(page.locator('.from-assistant')).toHaveCount(3);
    await expect(page.locator('.from-assistant').last()).toContainText('已阻止展示结论');
    await expect(page.locator('.chat-bubble').last()).not.toContainText('BLOCKED_UI_DRAFT');
    await expect(page.locator('.from-assistant').last().locator('.answer-sources')).toHaveCount(0);
    await page.reload();
    await page.locator(`[data-conv="${auditConv}"]`).click();
    await expect(page.locator('.from-assistant').last()).toContainText('已阻止展示结论');
    // A blocked draft is available only in the audit sidebar; corrected text has to bind real sources.
    await page.locator('.from-assistant').last().locator('[data-chat-review]').click();
    await expect(page.locator('#inspSources')).toContainText('BLOCKED_UI_DRAFT');
    await expect(page.locator('.chat-bubble').last()).not.toContainText('BLOCKED_UI_DRAFT');
    await page.locator('#chatReviewForm [name=note]').fill('已核对实际原文，修正虚构编号。');
    // P99 is unknown, so the actual delivered source is under the other-source disclosure.
    await page.locator('#chatReviewForm summary').filter({hasText:'本轮其他原文'}).click();
    await page.locator('#chatReviewForm .ev-source summary').first().click();
    await page.locator('#chatReviewForm [name=source_tags]').first().check();
    await page.locator('#chatReviewForm [value=confirm]').click();
    await expect(page.locator('#chatReviewForm [role=alert]')).toContainText('引用仍有问题');
    await expect(page.locator('#chatReviewForm [name=note]')).toHaveValue('已核对实际原文，修正虚构编号。');
    await page.locator('#chatReviewForm [name=reviewed_text]').fill('经人工核对的原文说明。[P1]');
    if (process.env.LITPRISM_UI_OUTPUT) {
      await page.screenshot({path:path.join(process.env.LITPRISM_UI_OUTPUT, 'chat-review-light.png')});
      await page.evaluate(() => document.documentElement.dataset.theme = 'dark');
      await page.setViewportSize({width:850,height:880});
      await page.screenshot({path:path.join(process.env.LITPRISM_UI_OUTPUT, 'chat-review-dark-narrow.png')});
      await page.setViewportSize({width:1360,height:880});
      await page.evaluate(() => document.documentElement.dataset.theme = 'light');
    }
    await page.locator('#chatReviewForm [value=confirm]').click();
    await expect(page.locator('#chatReviewForm [value=confirm]')).toHaveText('已确认入库');
    await page.reload();
    await page.locator(`[data-conv="${auditConv}"]`).click();
    await page.locator('.from-assistant').last().locator('[data-chat-review]').click();
    await expect(page.locator('#chatReviewForm [name=reviewed_text]')).toHaveValue('经人工核对的原文说明。[P1]');
    await page.locator('#chatReviewForm [name=note]').fill('撤销本次离线验收知识。');
    await page.locator('#chatReviewForm [value=revoke]').click();
    await expect(page.locator('#inspSources')).toContainText('已撤销');
    await page.locator('#btnChatReviewBack').click();
    await page.locator('#chatInput').fill('alphatopic attributionmarker');
    await page.locator('#btnSend').click();
    await expect(page.locator('.from-assistant')).toHaveCount(4);
    await expect(page.locator('.from-assistant').last()).toContainText('待核对');
    await expect(page.locator('.chat-bubble').last()).not.toContainText('使用 TriviaQA 训练');
    await page.locator('.from-assistant').last().locator('.answer-sources [data-evidence-ref]').click();
    await expect(page.locator('#inspSources')).toContainText('did not use TriviaQA');
    await page.reload();
    await page.locator(`[data-conv="${auditConv}"]`).click();
    await expect(page.locator('.from-assistant').last()).toContainText('待核对');
    await expect(page.locator('.chat-bubble').last()).not.toContainText('使用 TriviaQA 训练');
    await expect(page.locator('.from-assistant').last().locator('.answer-sources [data-evidence-ref]')).toHaveCount(1);
    if (process.env.LITPRISM_UI_OUTPUT) {
      await page.locator('.from-assistant').last().scrollIntoViewIfNeeded();
      await page.locator('.from-assistant').last().locator('.answer-sources [data-evidence-ref]').click();
      await expect(page.locator('#inspSources')).toContainText('did not use TriviaQA');
      await expect(page.locator('#toasts .toast')).toHaveCount(0);
      await page.screenshot({path:path.join(process.env.LITPRISM_UI_OUTPUT, 'chat-guard-light.png')});
      await page.evaluate(() => document.documentElement.dataset.theme = 'dark');
      await page.setViewportSize({width:850,height:880});
      await page.screenshot({path:path.join(process.env.LITPRISM_UI_OUTPUT, 'chat-guard-dark-narrow.png')});
      await page.setViewportSize({width:1360,height:880});
      await page.evaluate(() => document.documentElement.dataset.theme = 'light');
    }
    // Numeric/condition mismatch uses the same held draft and stable sources.
    await page.keyboard.press('Escape');
    await page.locator('#chatInput').fill('alphatopic conditionalmarker');
    await page.locator('#btnSend').click();
    await expect(page.locator('.from-assistant')).toHaveCount(5);
    await expect(page.locator('.from-assistant').last()).toContainText('年份未对应');
    await expect(page.locator('.chat-bubble').last()).not.toContainText('2020年领导人得67%');
    await page.locator('.from-assistant').last().locator('.answer-sources [data-evidence-ref]').click();
    await expect(page.locator('#inspSources')).toContainText('2024 leaders');
    await page.reload();
    await page.locator(`[data-conv="${auditConv}"]`).click();
    await expect(page.locator('.from-assistant').last()).toContainText('年份未对应');
    await expect(page.locator('.from-assistant').last().locator('.answer-sources [data-evidence-ref]')).toHaveCount(1);
    await page.locator('#chatInput').fill('alphatopic tagcitationmarker');
    await page.locator('#btnSend').click();
    await expect(page.locator('.from-assistant')).toHaveCount(6);
    await expect(page.locator('.from-assistant').last().locator('.answer-sources')).toContainText('p.1');
    await page.locator('.from-assistant').last().locator('.answer-sources [data-evidence-ref]').click();
    await expect(page.locator('#inspSources')).toContainText('第 1 页');
    await page.reload();
    await page.locator(`[data-conv="${auditConv}"]`).click();
    await expect(page.locator('.from-assistant').last()).toContainText('稳定片段编号');
    await expect(page.locator('.from-assistant').last().locator('.answer-sources')).toContainText('p.1');
    // Claim/quote inspection is not a semantic approval and must preserve form edits.
    await page.locator('#chatInput').fill('alphatopic claimmappingmarker');
    await page.locator('#btnSend').click();
    await expect(page.locator('.from-assistant')).toHaveCount(7);
    await page.reload();
    await page.locator(`[data-conv="${auditConv}"]`).click();
    await page.locator('.from-assistant').last().locator('[data-chat-review]').click();
    await page.locator('.qa-claim-map > summary').click();
    await expect(page.locator('.qa-claim-map')).toContainText('claim_subject_marker');
    await expect(page.locator('.qa-claim-map script')).toHaveCount(0);
    await expect(page.locator('.qa-claim-map blockquote')).toHaveText('We did not use TriviaQA data for this model.');
    await expect(page.locator('.qa-claim-map')).toContainText('原句可定位不等于支持结论');
    await page.locator('#qaWholeReplyReview > summary').click();
    await page.locator('#chatReviewForm [name=note]').fill('source navigation preserves this note');
    await page.locator('[data-qa-map-source]').first().click();
    await expect(page.locator('#chatReviewForm .ev-source summary').first()).toBeFocused();
    await expect(page.locator('#chatReviewForm [name=note]')).toHaveValue('source navigation preserves this note');
    await expect(page.locator('#chatReviewForm [name=source_tags]:checked')).toHaveCount(0);
    if (process.env.LITPRISM_UI_OUTPUT) {
      await page.screenshot({path:path.join(process.env.LITPRISM_UI_OUTPUT, 'claim-map-light.png')});
      await page.evaluate(() => document.documentElement.dataset.theme = 'dark');
      await page.setViewportSize({width:850,height:880});
      // Theme switches animate for 120ms; inspect the settled colors, not the transient frame.
      await expect.poll(() => page.locator('.qa-claim-map .btn').first().evaluate(el => {
        const style = getComputedStyle(el);
        const light = c => {
          const channels = c.match(/[\d.]+/g).slice(0,3).map(Number).map(x => x/255);
          return channels.map(x => x <= .04045 ? x/12.92 : ((x+.055)/1.055)**2.4)
            .reduce((sum,x,i) => sum+x*[.2126,.7152,.0722][i],0);
        };
        const a = light(style.color), b = light(style.backgroundColor);
        return (Math.max(a,b)+.05)/(Math.min(a,b)+.05);
      })).toBeGreaterThanOrEqual(4.5);
      assert.equal(await page.locator('.qa-claim-map').evaluate(el => el.scrollWidth > el.clientWidth), false);
      await page.screenshot({path:path.join(process.env.LITPRISM_UI_OUTPUT, 'claim-map-dark-narrow.png'), animations:'disabled'});
      await page.setViewportSize({width:1360,height:880});
      await page.evaluate(() => document.documentElement.dataset.theme = 'light');
    }
    // 本轮外部证据可引用；确认后在同一重点会话重新检索，撤销上游则失效。
    await page.locator('#chatInput').fill('alphatopic mixedwebmarker');
    await page.locator('#btnSend').click();
    await expect(page.locator('.from-assistant')).toHaveCount(8);
    const webAnswer = page.locator('.from-assistant').last();
    await expect(webAnswer.locator('.chat-bubble')).toContainText('本轮搜索摘要');
    await expect(webAnswer.locator('.answer-sources').first()).toContainText('搜索摘要（未审核）');
    const webTag = (await webAnswer.locator('.answer-sources a').first().innerText()).match(/\[(P\d+)\]/)[1];
    await webAnswer.locator('[data-chat-review]').click();
    await page.locator(`#chatReviewForm input[value="${webTag}"]`).check();
    await page.locator('#chatReviewForm [name=reviewed_text]').fill(`alphatopic mixedcorrectedmarker 已核对的修订知识。[${webTag}]`);
    await page.locator('#chatReviewForm [name=note]').fill('仅确认当前知识所需的摘要依据，保留网页未审核状态。');
    await page.locator('#chatReviewForm [value=confirm]').click();
    await expect(page.locator('#chatReviewForm [value=confirm]')).toHaveText('已确认入库');
    await page.reload();
    await page.locator(`[data-conv="${auditConv}"]`).click();
    await page.locator('#chatInput').fill('alphatopic mixedcorrectedmarker');
    await page.locator('#btnSend').click();
    await expect(page.locator('.from-assistant')).toHaveCount(9);
    await expect(page.locator('.from-assistant').last().locator('.chat-bubble')).toContainText('新确认的知识');
    await expect(page.locator('.from-assistant').last().locator('.answer-sources').first()).toContainText('人工知识');
    await page.locator('.from-assistant').last().locator('.answer-sources a').first().click();
    await expect(page.locator('#inspSources')).toContainText('知识依据与当前状态');
    await page.locator('#inspSources details a[data-evidence-ref]').filter({hasText:'混合取证外部快照'}).click();
    await expect(page.locator('#inspSources')).toContainText('待人工审核');
    await expect(page.locator('#inspSources')).toContainText('尚未人工确认');
    await expect(page.locator('#evidenceReview [value=confirm]')).toBeEnabled();
    await expect(page.locator('#evidenceReview [value=revoke]')).toBeDisabled();
    if (process.env.LITPRISM_UI_OUTPUT) {
      await expect(page.locator('#inspSources')).toContainText('搜索摘要获取时间');
      await page.screenshot({path:path.join(process.env.LITPRISM_UI_OUTPUT,'mixed-evidence-light.png')});
      await page.evaluate(() => document.documentElement.dataset.theme='dark');
      await page.setViewportSize({width:850,height:880});
      await page.screenshot({path:path.join(process.env.LITPRISM_UI_OUTPUT,'mixed-evidence-dark-narrow.png'),animations:'disabled'});
      await page.setViewportSize({width:1360,height:880});
      await page.evaluate(() => document.documentElement.dataset.theme='light');
    }
    await page.locator('#evidenceReview [name=note]').fill('撤回上游依据，检验派生知识停止引用。');
    await page.locator('#evidenceReview [value=reject]').click();
    await expect(page.locator('#inspSources')).toContainText('已拒绝');
    await page.reload();
    await page.locator(`[data-conv="${auditConv}"]`).click();
    await expect(page.locator('.from-assistant').last()).toContainText('来源校验未通过');
    await expect(page.locator('.from-assistant').last().locator('.answer-sources')).toContainText('依据失效（仅回溯）');
    await page.locator('[data-view=knowledge]').first().click();
    await expect(page.locator('#knowledgeList')).toContainText('依据失效');
    await page.locator('#btnAddKnowledge').click();
    await page.locator('#researchTask [name=title]').fill('reviewmarker 审核来源');
    await page.locator('#researchTask [name=content]').fill('reviewmarker 知识快照的测试内容。');
    await page.locator('#researchTask [type=submit]').click();
    await expect(page.locator('#researchTask')).toHaveCount(0);
    await expect(page.locator('#inspSources')).toContainText('待人工审核');
    await page.locator('#evidenceReview [name=note]').fill('测试人员已核对原文与适用范围');
    await page.locator('#evidenceReview [value=confirm]').click();
    await expect(page.locator('#inspSources')).toContainText('测试人员已核对原文与适用范围');
    await expect(page.locator('#knowledgeList')).toContainText('人工确认');
    await page.locator('#btnNewConv').click();
    await page.locator('#researchTask [name=title]').fill('知识条目验收');
    await page.locator('#researchTask [type=submit]').click();
    await page.locator('#chatInput').fill('reviewmarker');
    await page.locator('#btnSend').click();
    await expect(page.locator('.from-assistant .answer-sources')).toContainText('reviewmarker 审核来源');
    const evidenceRef = await page.locator('.answer-sources [data-evidence-ref]').first().getAttribute('data-evidence-ref');
    await page.reload();
    await expect(page.locator('#paperList')).toBeVisible();
    await page.locator('[data-conv]').filter({ hasText: '知识条目验收' }).click();
    await expect(page.locator('.chat-reasoning')).toHaveCount(1);
    await expect(page.locator('.request-budget')).toHaveCount(0);
    await page.locator(`.answer-sources [data-evidence-ref="${evidenceRef}"]`).click();
    await expect(page.locator('#inspSources')).toContainText('知识条目');
    const output = process.env.LITPRISM_UI_OUTPUT;
    if (output) {
      fs.mkdirSync(output, { recursive: true });
      await expect(page.locator('#toasts .toast')).toHaveCount(0);
      await page.screenshot({ path: path.join(output, 'evidence-sidebar-light.png') });
    }
    await page.locator('#evidenceReview [name=note]').fill('测试撤销');
    await page.locator('#evidenceReview [value=revoke]').click();
    await expect(page.locator('#inspSources')).toContainText('已撤销');
    await page.keyboard.press('Escape');

    // 偏好创建、编辑、删除与恢复。
    await page.locator('[data-view=knowledge]').first().click();
    await page.locator('#btnAddKnowledge').click();
    await page.locator('#researchTask [name=kind]').selectOption('web');
    await page.locator('#researchTask [name=title]').fill('网页来源 <b>原文片段</b>');
    await page.locator('#researchTask [name=url]').fill('https://example.org/evidence-source');
    await page.locator('#researchTask [name=content]').fill('这是用于验收的网页摘要快照，不是论文全文。');
    await page.locator('#researchTask [type=submit]').click();
    await expect(page.locator('#inspSources')).toContainText('网页摘要快照');
    await expect(page.locator('#inspSources h3')).toHaveText('网页来源 <b>原文片段</b>');
    await expect(page.locator('#inspSources h3 b')).toHaveCount(0);
    await expect(page.locator('#inspSources a[target=_blank]')).toHaveAttribute('href', 'https://example.org/evidence-source');
    await expect(page.locator('#inspSources')).toContainText('待人工审核');
    await page.keyboard.press('Escape');

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
    await page.locator('[data-run-skill]').click();
    await page.locator('#researchTask [name=topic]').fill('alphatopic delaymarker');
    await page.locator('#researchTask [type=submit]').click();
    await expect(page.locator('#researchTask .task-progress')).toBeVisible();
    await page.locator('#researchTask .task-progress button').click();
    await expect(page.locator('#researchTask .task-error')).toContainText('取消');
    await page.locator('#researchTask [data-cancel]').first().click();

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

    // 手工任务输出上限已移除；其他设置仍可保存。
    await page.locator('[data-view=settings]').first().click();
    await page.locator('#settingsNav [data-set=retrieval]').click();
    await expect(page.locator('[data-cfg="context.output_reserve_tokens"]')).toHaveCount(0);
    await expect(page.locator('[data-cfg="llm.max_tokens"]')).toHaveCount(0);
    await expect(page.locator('[data-cfg="context.window_tokens"]')).toHaveCount(0);
    await page.locator('[data-cfg="retrieval.top_k"]').fill('8');
    const [savedConfig] = await Promise.all([
      page.waitForResponse(r => r.url().endsWith('/api/config') && r.request().method() === 'PATCH'),
      page.locator('#btnSaveCfg').click(),
    ]);
    assert.equal(savedConfig.status(), 200);
    await page.locator('#btnCloseSettings').click();

    // 阶段预算已取消；持久记录保留实际调用和待审状态，刷新后仍能精确恢复。
    await page.locator('#paperList [data-paper]').first().click();
    await expect(page.locator('#btnBudgetAnalyze')).toBeVisible();
    await page.locator('#btnBudgetAnalyze').click();
    await page.locator('#researchTask [name=with_concepts]').uncheck();
    await page.locator('#researchTask [name=semantic_review]').check();
    await page.locator('#researchTask [type=submit]').click();
    await expect(page.locator('#researchTask')).toHaveCount(0);
    await expect(page.locator('#analysisRuns')).toContainText('仍有待审项');
    await expect(page.locator('#analysisRuns')).toContainText('原文提取 1 次');
    const budgetRun = await page.locator('#analysisRuns [data-run-id]').first().getAttribute('data-run-id');
    await page.reload();
    await page.locator('#paperList [data-paper]').first().click();
    await expect(page.locator('#analysisRuns')).toContainText('仍有待审项');
    await page.locator(`[data-resume-run="${budgetRun}"]`).click();
    await expect(page.locator('#researchTask')).toContainText('已用调用');
    await expect(page.locator('#researchTask [name=semantic_review]')).toHaveCount(0);
    await page.locator('#researchTask [type=submit]').click();
    await expect(page.locator('#researchTask')).toHaveCount(0);
    await expect(page.locator('#analysisRuns')).toContainText('仍有待审项');
    await expect(page.locator('#analysisRuns')).toContainText('原文提取 1 次');
    await expect(page.locator('#analysisRuns')).toContainText('逐项复核 1 次');
    if (output) {
      await expect(page.locator('#toasts .toast')).toHaveCount(0);
      await page.screenshot({ path: path.join(output, 'analysis-run-light.png') });
    }

    // 新工作流在深色主题与桌面最小窗口内保持可操作。
    await page.setViewportSize({ width: 1000, height: 660 });
    await page.locator('#btnTheme').click();
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
    await page.locator(`[data-resume-run="${budgetRun}"]`).click();
    await expect(page.locator('#researchTask')).toBeVisible();
    if (output) await page.screenshot({ path: path.join(output, 'analysis-run-dark-narrow.png') });
    const budgetSubmit = await page.locator('#researchTask [type=submit]').boundingBox();
    assert(budgetSubmit.x >= 0 && budgetSubmit.y >= 0 && budgetSubmit.x + budgetSubmit.width <= 1000 && budgetSubmit.y + budgetSubmit.height <= 660);
    await page.locator('#researchTask [data-cancel]').first().click();
    await page.locator('.workspace-nav [data-view=library]').click();
    // 批量导出只对可见勾选开放，刷新后不继承已回收文献的选择。
    await page.locator('[data-select-paper]').first().check();
    await page.locator('#btnPaperExport').click();
    await expect(page.locator('#researchTask')).toBeVisible();
    const submit = await page.locator('#researchTask [type=submit]').boundingBox();
    assert(submit.x >= 0 && submit.y >= 0 && submit.x + submit.width <= 1000 && submit.y + submit.height <= 660);
    await page.locator('#researchTask [name=format]').selectOption('bibtex');
    const [bib] = await Promise.all([page.waitForEvent('download'), page.locator('#researchTask [type=submit]').click()]);
    assert.equal(bib.suggestedFilename(), 'litprism.bib');
    await expect(page.locator('#researchTask')).toHaveCount(0);
    if (output) {
      await page.locator('[data-conv]').filter({ hasText: '知识条目验收' }).click();
      await page.locator('.answer-sources [data-evidence-ref]').first().click();
      await expect(page.locator('#inspSources')).toContainText('已撤销');
      await expect(page.locator('#toasts .toast')).toHaveCount(0);
      await page.screenshot({ path: path.join(output, 'evidence-sidebar-dark-narrow.png') });
    }
    await page.keyboard.press('Escape');
    await page.locator('[data-conv]').filter({ hasText: '知识条目验收' }).click();
    await page.locator('#chatInput').fill('alphatopic delaymarker');
    await page.locator('#btnSend').click();
    await expect(page.locator('.task-progress')).toBeVisible();
    await page.locator('.task-progress button').click();
    await expect(page.locator('.task-progress')).toHaveCount(0);
    await expect(page.locator('#chatStream')).toContainText('任务已取消');
    await expect(page.locator('#btnSend')).toBeEnabled();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    // 模型能力只读展示与重新载入；无密钥、无服务探测、无模型调用。
    const capacityProvider = await page.evaluate(async () => {
      const p = await api('/api/providers', {method:'POST', body:{name:'Capacity fixture',base_url:'https://api.commandcode.ai/provider/v1'}});
      await api(`/api/providers/${p.id}/models`, {method:'PUT', body:{models:[{model_id:'deepseek/deepseek-v4.1-flash'}]}});
      return p.id;
    });
    await page.locator('[data-view=settings]').first().click();
    await page.locator('#settingsNav [data-set=model]').click();
    await page.locator(`[data-pv-edit="${capacityProvider}"]`).click();
    const card = page.locator(`[data-pv="${capacityProvider}"]`);
    await expect(card.locator('[data-model-window]')).toHaveText('1,000,000 token');
    await expect(card.locator('[data-model-max-output]')).toHaveText('384,000 token');
    await expect(card.locator('[data-pm-window], [data-pm-max-output], [data-pm-budget]')).toHaveCount(0);
    await card.locator('[data-pv-save]').click();
    await expect(card.locator('[data-pv-status]')).toHaveText('已保存');
    await page.locator(`[data-pv-edit="${capacityProvider}"]`).click();
    await expect(card.locator('[data-model-max-output]')).toHaveText('384,000 token');
    if (output) {
      await page.setViewportSize({width:1360,height:880});
      await page.evaluate(() => document.documentElement.dataset.theme = 'light');
      await page.screenshot({path:path.join(output, 'model-capacity-light.png')});
      await page.setViewportSize({width:850,height:880});
      await page.evaluate(() => document.documentElement.dataset.theme = 'dark');
      await page.screenshot({path:path.join(output, 'model-capacity-dark-narrow.png')});
    }
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    // N05/N06/N12: real storage, historical citations and read-only analysis history.
    await page.locator('#btnCloseSettings').click();
    await page.setViewportSize({width:1360,height:880});
    const materialFixture = await page.evaluate(async () => {
      const result = await api('/api/acceptance/material', {method:'POST', body:{}});
      await openView('paper', {paperId:result.paper_id});
      return result;
    });
    await expect(page.locator('#paperDetail')).toContainText('originalmarker');
    await page.locator('#qualityPanel summary').click();
    await expect(page.locator('#qualityTable')).toBeVisible();
    await expect(page.locator('#qualityTable tbody tr')).toHaveCount(7);
    await expect(page.locator('#qualityTable')).toContainText('未提取');
    await expect(page.locator('#qualityTable')).toContainText('未审核');
    await expect(page.locator('#qualitySummary')).toContainText('尚未人工审核');
    await page.locator('#qualityPanel summary').click();
    await page.locator('#manualPanel summary').click();
    await page.locator('#btnAddClaim').click();
    await expect(page.locator('#claimSource')).toContainText('originalmarker');
    await page.locator('#claimQuote').fill('训练使用八块 GPU。');
    await page.locator('#claimText').fill('人工补录：训练使用八块 GPU。');
    await page.locator('#claimField').selectOption('methodology');
    await page.locator('#btnSaveClaim').click();
    await expect(page.locator('#manualList')).toContainText('人工补录：训练使用八块 GPU。');
    await page.locator('#qualityPanel summary').click();
    await expect(page.locator('#qualityTable')).toContainText('漏提占比');
    await expect(page.locator('#qualityTable')).toContainText('机器未提取，已有人工补录');
    await expect(page.locator('#qualityDefs')).toContainText('不是真实召回率');
    if (output) {
      await expect(page.locator('#toasts .toast')).toHaveCount(0);
      await page.screenshot({path:path.join(output,'quality-missing-fields.png')});
    }
    await page.evaluate(async pid => {
      await api('/api/acceptance/material', {method:'POST',body:{paper_id:pid,replace:true}});
      await loadPaperDetail();
    }, materialFixture.paper_id);
    await expect(page.locator('#paperDetail')).toContainText('这份分析基于旧材料');
    await page.locator('[data-ptab=text]').click();
    await expect(page.locator('#paperTextBody')).toContainText('replacementmarker');
    await expect(page.locator('#paperTextBody')).not.toContainText('originalmarker');
    await page.locator('#paperMaterialSelect').selectOption(materialFixture.materials[0].id);
    await expect(page.locator('#paperTextBody')).toContainText('originalmarker');
    await page.evaluate(ref => showEvidence(ref), materialFixture.chunks[0].id);
    await expect(page.locator('#inspSources')).toContainText('历史材料');
    await expect(page.locator('#inspSources')).toContainText('originalmarker');
    await page.keyboard.press('Escape');
    await page.evaluate(async pid => {
      await api('/api/acceptance/material', {method:'POST',body:{paper_id:pid}});
      await loadPaperDetail();
    }, materialFixture.paper_id);
    const history = await page.evaluate(pid => api(`/api/paper/${pid}/materials`), materialFixture.paper_id);
    await page.locator('#analysisVersionSelect').selectOption(history.analyses[0].id);
    await expect(page.locator('#paperDetail')).toContainText('历史分析');
    await expect(page.locator('#paperDetail')).toContainText('修改或补录请返回最新版本');
    await expect(page.locator('#paperDetail')).toContainText('originalmarker');
    await expect(page.locator('#btnAddClaim')).toHaveCount(0);
    await page.locator('[data-ptab=text]').click();
    await expect(page.locator('#paperTextBody')).toContainText('originalmarker');
    if (output) {
      await page.evaluate(() => document.documentElement.dataset.theme = 'light');
      await page.screenshot({path:path.join(output, 'material-history-light.png')});
      await page.setViewportSize({width:850,height:880});
      await page.evaluate(() => document.documentElement.dataset.theme = 'dark');
      await page.screenshot({path:path.join(output, 'material-history-dark-narrow.png')});
    }
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    // N08: download, inspect, recover to a new isolated directory, switch/cancel.
    await page.setViewportSize({width:1360,height:880});
    // Durable QA: reload while waiting, recover one receipt, explicitly retry one failure.
    const beforeDurableConv = await page.evaluate(() => S.activeConv);
    await page.locator('#btnNewConv').click();
    await page.locator('#researchTask [name=title]').fill('持久对话验收');
    await page.locator('#researchTask [name=scope]').selectOption('p:' + process.env.LITPRISM_ACCEPTANCE_SEED);
    await page.locator('#researchTask [type=submit]').click();
    await expect(page.locator('#researchTask')).toHaveCount(0);
    await expect.poll(() => page.evaluate(() => S.activeConv)).not.toBe(beforeDurableConv);
    await expect(page.locator('.chat-welcome')).toBeVisible();
    const durableConv = await page.evaluate(() => S.activeConv);
    await page.locator('#chatInput').fill('alphatopic durablereloadmarker');
    await page.locator('#btnSend').click();
    await expect(page.locator('.task-progress')).toHaveCount(1);
    await page.reload();
    await page.locator(`[data-conv="${durableConv}"]`).click();
    await expect(page.locator('#chatRunPanel')).toContainText('durablereloadmarker');
    await expect.poll(async () => page.evaluate(async cid => (await api('/api/conversations/'+cid+'/chat-runs')).items[0].status, durableConv)).toBe('completed');
    await expect(page.locator('#chatRunPanel > summary')).toContainText('结果已保存');
    if (!await page.locator('#chatRunPanel').evaluate(el => el.open)) await page.locator('#chatRunPanel > summary').click();
    await page.locator('[data-chat-result]').first().click();
    await expect(page.locator('.from-assistant')).toHaveCount(1);
    assert.equal(await page.evaluate(async cid => (await api('/api/conversations/'+cid+'/chat-runs')).items.length, durableConv), 1);
    await page.locator('#chatInput').fill('alphatopic durableretrymarker');
    await page.locator('#btnSend').click();
    await expect(page.locator('[data-chat-resume][data-retry=true]')).toHaveText('重新请求（可能再次计费）');
    await page.reload();
    await page.locator(`[data-conv="${durableConv}"]`).click();
    await expect(page.locator('#chatRunPanel')).toContainText('用量未知');
    if (process.env.LITPRISM_UI_OUTPUT) {
      await page.screenshot({path:path.join(process.env.LITPRISM_UI_OUTPUT, 'chat-runs-light.png')});
      await page.evaluate(() => document.documentElement.dataset.theme = 'dark');
      await page.setViewportSize({width:850,height:880});
      await page.screenshot({path:path.join(process.env.LITPRISM_UI_OUTPUT, 'chat-runs-dark-narrow.png')});
      await page.setViewportSize({width:1360,height:880});
      await page.evaluate(() => document.documentElement.dataset.theme = 'light');
    }
    await page.locator('[data-chat-resume][data-retry=true]').click();
    await expect(page.locator('.from-assistant')).toHaveCount(2);
    const retried = await page.evaluate(async cid => (await api('/api/conversations/'+cid+'/chat-runs')).items[0], durableConv);
    assert.equal(retried.status, 'completed');
    assert.deepEqual(retried.calls.filter(c => c.task === 'qa').map(c => c.status), ['failed','succeeded']);
    assert.equal(await page.locator('.from-user').count(), 2);
    await page.locator('[data-view=settings]').first().click();
    await page.locator('#settingsNav [data-set=backup]').click();
    await expect(page.locator('.backup-settings')).toContainText('密钥不随备份迁移');
    await page.locator('#backupOriginals').check();
    const [backupDownload] = await Promise.all([
      page.waitForEvent('download'), page.locator('#backupExport').click(),
    ]);
    assert.equal(backupDownload.suggestedFilename(), 'litprism-backup.zip');
    await expect(page.locator('#backupStatus')).toContainText('备份已下载');
    await page.locator('#backupFile').setInputFiles(await backupDownload.path());
    await expect(page.locator('#backupPreview')).toContainText('校验通过');
    if (output) {
      await page.evaluate(() => document.documentElement.dataset.theme = 'light');
      await page.locator('#settingsPanes').evaluate(el => { el.scrollTop = 0; });
      await page.screenshot({path:path.join(output, 'backup-preview-light.png')});
    }
    const backupStatus = await page.evaluate(() => api('/api/backups/status'));
    const recoveredPath = path.join(path.dirname(backupStatus.data_dir), 'ui-restored');
    // An existing directory must fail without losing the validated preview.
    await page.locator('#backupTarget').fill(backupStatus.data_dir);
    await page.locator('#backupRestore').click();
    await expect(page.locator('#backupError')).toContainText('目标目录已存在');
    await page.locator('#backupTarget').fill(recoveredPath);
    await page.locator('#backupRestore').click();
    await expect(page.locator('#backupRestored')).toContainText('已校验并保存');
    await expect(page.locator('#backupRestore')).toBeDisabled();
    assert.equal((await page.evaluate(() => api('/api/backups/status'))).requires_restart, false);
    if (output) {
      await page.setViewportSize({width:850,height:880});
      await page.evaluate(() => document.documentElement.dataset.theme = 'dark');
      await page.locator('#backupActivate').scrollIntoViewIfNeeded();
      await page.screenshot({path:path.join(output, 'backup-switch-dark-narrow.png')});
    }
    await page.locator('#backupActivate').click();
    await expect(page.locator('#backupStatus')).toContainText('切换待重启生效');
    const blockedMutation = await page.evaluate(async () => {
      try { await api('/api/conversations', {method:'POST',body:{title:'must be blocked'}}); return false; }
      catch (e) { return e.message.includes('重启或取消切换'); }
    });
    assert.equal(blockedMutation, true);
    await page.locator('#backupCancel').click();
    await expect(page.locator('.backup-settings')).toBeVisible();
    await expect(page.locator('#backupActivate')).toBeVisible();
    await expect(page.locator('#backupCancel')).toBeHidden();
    assert.equal((await page.evaluate(() => api('/api/backups/status'))).requires_restart, false);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    // Empty chat selection is unrestricted; source records remain separate from paper quotes.
    await page.locator('#btnCloseSettings').click();
    await page.setViewportSize({width:1360,height:880});
    await page.evaluate(async () => {
      const c = await api('/api/conversations', {method:'POST', body:{title:'空选择与程序记录',scope:{paper_ids:[]}}});
      await loadSidebar(); await openView('chat', {convId:c.id});
    });
    await expect(page.locator('#scopeChip')).toContainText('全库');
    await page.locator('#chatInput').fill('alphatopic metadatamarker');
    await page.locator('#btnSend').click();
    await expect(page.locator('.from-assistant')).toHaveCount(1);
    await expect(page.locator('.from-assistant .chat-bubble')).toContainText('获取时间');
    await expect(page.locator('.from-assistant .chat-bubble')).not.toContainText('已隔离生成草稿');
    await page.locator('[data-chat-review]').click();
    await page.locator('.qa-claim-map > summary').click();
    await expect(page.locator('.qa-claim-map')).toContainText('程序记录 · 非论文原句');
    await expect(page.locator('.qa-claim-map blockquote')).toContainText('获取时间：');
    await expect(page.locator('.qa-claim-map .task-error')).toHaveCount(0);
    const emptyReceipt = await page.evaluate(async () => (await api('/api/conversations/'+S.activeConv+'/chat-runs')).items[0]);
    assert.equal(emptyReceipt.status, 'completed');
    assert(emptyReceipt.calls.some(c => c.task === 'web_search' && c.status === 'succeeded'));
    if (output) await page.screenshot({path:path.join(output,'unrestricted-metadata-sidebar.png')});
    await page.keyboard.press('Escape');
    const unanalysed = await page.evaluate(async () => {
      const p = await api('/api/acceptance/material', {method:'POST',body:{unanalysed:true}});
      await openView('paper', {paperId:p.paper_id}); return p;
    });
    await page.locator('#qualityPanel summary').click();
    await expect(page.locator('#qualityTable')).toBeVisible();
    await expect(page.locator('#qualityTable tbody tr')).toHaveCount(7);
    await expect(page.locator('#qualitySummary')).toContainText('尚未解读');
    if (output) await page.screenshot({path:path.join(output,'quality-not-analyzed.png')});
    await page.evaluate(async pid => {
      await api('/api/acceptance/anchor-trial', {method:'POST',body:{}});
      const c = await api('/api/conversations', {method:'POST',body:{title:'锚点隔离试验',scope:{paper_ids:[pid]}}});
      await loadSidebar(); await openView('chat',{convId:c.id});
    }, unanalysed.paper_id);
    await page.locator('#chatInput').fill('originalmarker anchortrialmarker');
    await page.locator('#btnSend').click();
    await expect(page.locator('.from-assistant')).toHaveCount(1);
    await expect(page.locator('.from-assistant .chat-bubble')).toContainText('隔离试验');
    await page.locator('[data-chat-review]').click();
    await page.locator('.qa-claim-map > summary').click();
    await expect(page.locator('.qa-claim-map')).toContainText('试验锚点 · 程序回填原文');
    await expect(page.locator('.qa-answer-binding')).toHaveText('已对应回答原文');
    await expect(page.locator('.qa-claim-map blockquote')).toContainText('originalmarker');
    await expect(page.locator('.qa-claim-map blockquote')).not.toContainText('训练使用八块');
    await expect(page.locator('.qa-claim-map .task-error')).toHaveCount(0);
    if (output) await page.screenshot({path:path.join(output,'anchor-trial-sidebar.png')});
    await page.locator('.qa-claim-map [data-qa-map-source]').first().click();
    await expect(page.locator('#inspSources')).toContainText('originalmarker');
    await page.evaluate(async pid => {
      const c = await api('/api/conversations', {method:'POST',body:{title:'未绑定记录验收',scope:{paper_ids:[pid]}}});
      await loadSidebar(); await openView('chat',{convId:c.id});
    }, unanalysed.paper_id);
    await page.locator('#chatInput').fill('originalmarker unboundmappingmarker');
    await page.locator('#btnSend').click();
    await expect(page.locator('.from-assistant')).toHaveCount(1);
    await page.locator('[data-chat-review]').click();
    await expect(page.locator('.qa-answer-binding')).toContainText('尚未对应回答原文');
    await expect(page.locator('.qa-claim-map')).toContainText('模型另外概括的句子');
    if (output) await page.screenshot({path:path.join(output,'answer-unbound-sidebar.png')});
    const unitConv = await page.evaluate(async pid => {
      const c = await api('/api/conversations', {method:'POST',body:{title:'单元独立审核验收',scope:{paper_ids:[pid]}}});
      await loadSidebar(); await openView('chat',{convId:c.id}); return c.id;
    }, unanalysed.paper_id);
    await page.locator('#chatInput').fill('originalmarker unitreviewmarker');
    await page.locator('#btnSend').click();
    await expect(page.locator('.from-assistant')).toHaveCount(1);
    const unitParent = await page.locator('[data-chat-review]').getAttribute('data-chat-review');
    await expect(page.locator('.chat-answer-unit')).toHaveCount(0);
    await expect(page.locator('[data-chat-unit-review]')).toHaveCount(0);
    await page.locator('[data-chat-review]').click();
    await expect(page.locator('.qa-unit-list')).toBeVisible();
    await expect(page.locator('#qaWholeReplyReview')).not.toHaveAttribute('open', '');
    await expect(page.locator('[data-qa-unit]')).toHaveCount(2);
    await page.locator('[data-qa-unit]').first().click();
    await expect(page.locator('#chatUnitReviewForm [data-unit-anchor]')).toHaveCount(2);
    if (output) {
      const selectionDebug = await page.evaluate(async rid => {
        const p = await api('/api/chat-reviews/'+rid);
        const u = await api('/api/chat-reviews/'+rid+'/units/'+p.units[0].id);
        const c = await api('/api/chat-reviews/'+rid+'/units/'+p.units[0].id+'/sources/P1');
        return {unit:u,context:c};
      }, unitParent);
      fs.writeFileSync(path.join(output,'unit-selection-debug.json'),JSON.stringify(selectionDebug,null,2));
    }
    await expect(page.locator('#chatUnitReviewForm [data-unit-anchor]:checked')).toHaveCount(1);
    await expect(page.locator('#chatUnitReviewForm [data-unit-anchor]:checked')).toBeVisible();
    await expect(page.locator('#qaUnitSource')).toHaveValue('P1');
    await page.locator('.qa-unit-supplement > summary').click();
    await page.locator('#chatUnitReviewForm [data-unit-anchor]').nth(1).check();
    await page.locator('#chatUnitReviewForm [name=reviewed_text]').fill('错误引用 [P99]');
    await page.locator('#chatUnitReviewForm [name=note]').fill('离线控制：核对原文并补选后句。');
    await page.locator('#chatUnitReviewForm [value=confirm]').click();
    await expect(page.locator('#chatUnitReviewForm [role=alert]')).toContainText('引用');
    await expect(page.locator('#chatUnitReviewForm [name=reviewed_text]')).toHaveValue('错误引用 [P99]');
    await expect(page.locator('#chatUnitReviewForm [data-unit-anchor]:checked')).toHaveCount(2);
    await page.locator('#chatUnitReviewForm [name=reviewed_text]').fill('unitknowledgefollowmarker originalmarker，并使用八块GPU。[P1]');
    await page.locator('#chatUnitReviewForm [value=confirm]').click();
    await expect(page.locator('#inspSources')).toContainText('状态：确认入库');
    await expect(page.locator('.from-assistant')).toContainText('已处理 1/2 条');
    await expect(page.locator('.from-assistant .chat-bubble')).toContainText('UNREVIEWED_UI_UNIT_MARKER');
    await expect(page.locator('.qa-unit-row')).toHaveCount(2);
    await expect(page.locator('[data-qa-unit]').nth(1)).toContainText('未审核');
    const firstUnit = await page.evaluate(async rid => {
      const d = await api('/api/chat-reviews/'+rid);
      assertEqual(d.review_mode, 'units');
      assertEqual(d.units[1].decision, 'unreviewed');
      return d.units[0];
      function assertEqual(a,b) { if(a !== b) throw new Error('Unexpected unit state: '+a); }
    }, unitParent);
    const evidenceState = await page.evaluate(async ({rid,uid}) => api('/api/chat-reviews/'+rid+'/units/'+uid), {rid:unitParent,uid:firstUnit.id});
    assert.equal(evidenceState.original_evidence.length, 1);
    assert.equal(evidenceState.reviews[0].evidence_snapshot.length, 2);
    if (output) {
      await page.evaluate(() => {document.documentElement.dataset.theme='light';document.querySelector('#inspSources').scrollTop=0;});
      await page.screenshot({path:path.join(output,'unit-review-light.png'),animations:'disabled'});
      await page.evaluate(() => document.documentElement.dataset.theme='dark');
      await page.setViewportSize({width:850,height:880});
      await page.screenshot({path:path.join(output,'unit-review-dark-narrow.png'),animations:'disabled'});
      await page.setViewportSize({width:1360,height:880});
      await page.evaluate(() => document.documentElement.dataset.theme='light');
    }
    await page.locator('#chatInput').fill('unitknowledgefollowmarker');
    await page.locator('#btnSend').click();
    await expect(page.locator('.from-assistant')).toHaveCount(2);
    await expect(page.locator('.from-assistant').last()).toContainText('已重新检索单元人工知识');
    const retrievalProof = await page.evaluate(async ({cid,sid}) => {
      const messages = await api('/api/conversations/'+cid+'/messages');
      const knowledge = await api('/api/evidence/'+sid);
      const delivered = messages.items.filter(m => m.role==='assistant').at(-1).retrieval.delivered_chunk_ids;
      return {hit:knowledge.chunks.some(c => delivered.includes(c.id)),texts:knowledge.chunks.map(c=>c.text)};
    }, {cid:unitConv,sid:firstUnit.knowledge_id});
    assert.equal(retrievalProof.hit,true);
    assert.equal(retrievalProof.texts.some(s=>s.includes('UNREVIEWED_UI_UNIT_MARKER')),false);
    await page.locator(`[data-chat-review="${unitParent}"]`).click();
    await page.locator('[data-qa-unit]').first().click();
    await expect(page.locator('#chatUnitReviewForm [data-unit-anchor]')).toHaveCount(2);
    await page.locator('#chatUnitReviewForm [name=reviewed_text]').fill('unitknowledgeupdatedmarker 经重新核对。[P1]');
    await page.locator('#chatUnitReviewForm [name=note]').fill('离线控制：修订知识版本。');
    let releaseRevision;
    const revisionGate = new Promise(resolve => {releaseRevision=resolve;});
    const revisionPattern = '**/api/chat-reviews/*/units/*/review';
    const revisionRoute = async route => {
      if (route.request().postDataJSON().action === 'revise') await revisionGate;
      await route.continue();
    };
    await page.route(revisionPattern, revisionRoute);
    await page.locator('#chatUnitReviewForm [value=revise]').click();
    try {
      await expect(page.locator('#chatUnitReviewForm [name=reviewed_text]')).toBeDisabled();
      await expect(page.locator('#chatUnitReviewForm [name=note]')).toBeDisabled();
    } finally {releaseRevision();}
    await expect(page.locator('#inspSources')).toContainText('状态：修订入库');
    await expect(page.locator('.from-assistant').last()).toContainText('来源校验未通过');
    await expect(page.locator('.from-assistant').last()).not.toContainText('已重新检索单元人工知识');
    await page.unroute(revisionPattern, revisionRoute);
    await expect(page.locator('#chatUnitReviewForm [name=reviewed_text]')).toHaveValue('unitknowledgeupdatedmarker 经重新核对。[P1]');
    const priorKnowledge = await page.evaluate(sid => api('/api/evidence/'+sid), firstUnit.knowledge_id);
    assert.equal(priorKnowledge.status,'revoked');
    await page.locator('#chatUnitReviewForm [name=note]').fill('离线控制：撤销本单元知识。');
    await page.locator('#chatUnitReviewForm [value=revoke]').click();
    await expect(page.locator('#inspSources')).toContainText('状态：撤销');
    await page.locator('#btnUnitReviewBack').click();
    await expect(page.locator('[data-qa-unit]').first()).toContainText('已撤销');
    await expect(page.locator('[data-qa-unit]').nth(1)).toContainText('未审核');
    await expect(page.locator('#chatReviewForm [value=confirm]')).toBeDisabled();
    await page.locator('[data-qa-unit]').nth(1).click();
    await page.locator('#chatUnitReviewForm [name=note]').fill('离线控制：拒绝未核实的旁句。');
    await page.locator('#chatUnitReviewForm [value=reject]').click();
    await expect(page.locator('#inspSources')).toContainText('状态：拒绝');
    await expect(page.locator('.qa-review-progress')).toContainText('全部 2 条已处理');
    // Checkpoint bookkeeping remains auditable but is never a user chat bubble.
    const compactedUi = await page.evaluate(async () => {
      const result = await api('/api/acceptance/compact-history',{method:'POST',body:{}});
      S.activeConv = result.conversation_id;
      await loadChat();
      return api('/api/conversations/'+S.activeConv+'/messages');
    });
    assert.equal(compactedUi.items.filter(m=>m.kind==='compact-checkpoint').length,1);
    await expect(page.locator('.from-user')).toHaveCount(1);
    await expect(page.locator('.from-assistant')).toHaveCount(1);
    await expect(page.locator('.from-user')).toContainText('LONG_ORIGINAL_VISIBLE');
    await expect(page.locator('.from-assistant')).toContainText('LONG_REPLY_VISIBLE');
    await expect(page.locator('#chatStream')).not.toContainText('<compacted-summary>');
    assert.deepEqual(errors, []);
    console.log('Real API workflow acceptance passed: research tasks, durable analysis budgets/resume, reply source links, sidebar review/revoke, persistent provenance, web snapshots, cancellation, themes and narrow layout.');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
