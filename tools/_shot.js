/**
 * 视觉与交互验证：起 sidecar → 载入真实页面 → 驱动界面 → 截图 + 断言。
 *
 * 只用于开发期核对渲染与交互，不参与打包。
 * 用法：env -u ELECTRON_RUN_AS_NODE ./node_modules/electron/dist/electron.exe tools/_shot.js
 *
 * 两个必须注意的点：
 * 1. 用**源码**起 sidecar —— 打包 exe 里是构建时的静态文件，看不到当前改动；
 * 2. 进度写文件而不是 console —— Windows 下 Electron（GUI 子系统）spawn 控制台
 *    子进程后，父进程的 console 输出会丢失（与 main.js 的 trace 同因）。
 */

const { app, BrowserWindow } = require('electron');
const { spawn } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');

const ROOT = path.resolve(__dirname, '..');
const OUT = path.join(ROOT, 'packaging', 'shots');
const LOG = path.join(OUT, 'shot.log');
const PY = process.env.LITPRISM_PY || 'C:/Users/18434/.workbuddy/binaries/python/envs/default/Scripts/python.exe';

fs.mkdirSync(OUT, { recursive: true });
fs.writeFileSync(LOG, '');
function log(...parts) {
  fs.appendFileSync(LOG, parts.map(p => (typeof p === 'string' ? p : JSON.stringify(p))).join(' ') + '\n');
}

if (!app) {
  log('需要以 Electron 运行（清掉 ELECTRON_RUN_AS_NODE）');
  process.exit(1);
}
// 与 desktop-electron/main.js 一致：本机 GPU 进程会以 0xC0000005 崩溃
app.commandLine.appendSwitch('disable-gpu');
app.commandLine.appendSwitch('disable-gpu-compositing');
app.commandLine.appendSwitch('disable-gpu-sandbox');
app.commandLine.appendSwitch('no-sandbox');
app.disableHardwareAcceleration();

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

function waitHandshake(child) {
  return new Promise((resolve, reject) => {
    let buf = '';
    const timer = setTimeout(() => reject(new Error('sidecar 启动超时')), 90000);
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk) => {
      buf += chunk;
      const line = buf.split(/\r?\n/).find(l => l.startsWith('LITPRISM_READY '));
      if (!line) return;
      clearTimeout(timer);
      resolve(JSON.parse(line.slice('LITPRISM_READY '.length)));
    });
    child.on('error', reject);
  });
}

app.whenReady().then(async () => {
  const child = spawn(PY, ['-m', 'litprism.sidecar', '--port', '0'], {
    stdio: ['ignore', 'pipe', 'ignore'], windowsHide: true,
    env: { ...process.env, PYTHONPATH: path.join(ROOT, 'src') },
  });
  const { port, token } = await waitHandshake(child);
  log('sidecar ready on', port);

  const win = new BrowserWindow({
    width: 1280, height: 820, show: false,
    webPreferences: { preload: path.join(ROOT, 'desktop-electron', 'preload.js') },
  });
  await win.loadURL(`http://127.0.0.1:${port}/#t=${encodeURIComponent(token)}`);
  await sleep(1500);

  const run = (js) => win.webContents.executeJavaScript(js);
  const shot = async (name) => {
    // 关掉 GPU 后合成器不会主动重绘，capturePage 可能拿到切换前的旧帧
    win.webContents.invalidate();
    await sleep(400);
    fs.writeFileSync(path.join(OUT, name + '.png'), (await win.webContents.capturePage()).toPNG());
    log('已截图', name);
  };

  const conv = await run(`(async () => (await api('/api/conversations')).items?.[0]?.id || null)()`);
  const paper = await run(`(async () => (await api('/api/papers')).items?.[0]?.id || null)()`);
  log('会话', conv, '| 论文', paper);

  // 1) 对话空状态
  await run(`openView('chat', {convId: ${JSON.stringify(conv)}})`);
  await sleep(1800);
  await shot('chat-empty');

  // 1b) 文献范围：默认全部 → 改成单篇，胶囊应随之变化
  const scopeBefore = await run(`(document.querySelector('.scope-chip span')||{}).textContent || '(无胶囊)'`);
  await run(`(async () => {
    await api('/api/conversations/' + ${JSON.stringify(conv)},
      { method: 'PATCH', body: { scope: { paper_ids: [${JSON.stringify(paper)}] } } });
    await loadChat();
  })()`);
  await sleep(900);
  const scopeAfter = await run(`(document.querySelector('.scope-chip span')||{}).textContent || '(无胶囊)'`);
  log('范围胶囊:', scopeBefore, '→', scopeAfter);
  await shot('chat-scope');

  // 2) 论文页：位置条应显示论文标题
  await run(`openView('paper', {paperId: ${JSON.stringify(paper)}})`);
  await sleep(1800);
  log('论文页位置条:', await run(`(document.querySelector('.loc-chip')||{}).textContent||''`));

  // 2a) 量状态条与 summary 标记的真实几何：三角默认画在内容盒外，
  //     父级 padding 不足时会压到边框上
  const geom = await run(`(() => {
    const pick = (sel) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const b = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      return {x: Math.round(b.x), w: Math.round(b.width), pl: cs.paddingLeft, ml: cs.marginLeft,
              display: cs.display, list: cs.listStylePosition};
    };
    const box = document.querySelector('.analysis-status');
    const bb = box && box.getBoundingClientRect();
    // 其他 details.card 是否同样贴边
    const cards = [...document.querySelectorAll('details.card')].map(el => {
      const b = el.getBoundingClientRect();
      const s = el.querySelector('summary');
      const cs = s ? getComputedStyle(s) : null;
      const r = document.createRange(); if (s) r.selectNodeContents(s);
      return {text: (s?.textContent || '').trim().slice(0, 12),
              pad: getComputedStyle(el).paddingLeft,
              sumPad: cs?.paddingLeft, sumDisplay: cs?.display,
              markerRoom: s ? Math.round(r.getBoundingClientRect().x - b.x) : null};
    });
    return {
      box: pick('.analysis-status'),
      summary: pick('.analysis-status[data-level="block"] > .status-more > summary'),
      // 三角在内容盒左侧，用首行文字的起点减去盒左边界估算其占用
      cards,
      textStart: (() => {
        const s = document.querySelector('.status-more > summary');
        if (!s) return null;
        const r = document.createRange(); r.selectNodeContents(s);
        return Math.round(r.getBoundingClientRect().x - bb.x);
      })(),
    };
  })()`);
  log('状态条几何:', geom);
  await shot('paper-view');

  // 2b) 只有提示、没有阻断时的状态条形态（真实数据难凑，直接替换 DOM 验证）
  const noticeOnly = await run(`(() => {
    const block = document.querySelector('.analysis-status');
    if (!block) return 'no-block';
    block.outerHTML = analysisStatusBlock([
      {level: 'notice', text: '当前使用离线占位模型（mock），结果仅演示流程，不构成真实学术解读。'},
      {level: 'notice', text: '术语提取未完成：provider 超时'},
    ]);
    return 'replaced';
  })()`);
  log('提示态替换:', noticeOnly);
  await shot('paper-notice-only');

  // 3) N05 补录：加一条 → 面板与召回应更新
  const added = await run(`(async () => {
    const sem = await api('/api/paper/' + ${JSON.stringify(paper)} + '/semantic');
    // 必须按**分析所属材料**取原文：二次导入后当前材料可能已是另一版
    const mid = sem.material_id ? '?material_id=' + encodeURIComponent(sem.material_id) : '';
    const d = await api('/api/paper/' + ${JSON.stringify(paper)} + '/text' + mid);
    const c = d.pages.flatMap(p => p.chunks)[0];
    const quote = (c.text || '').split(/[。.!?]/)[0].slice(0, 60);
    const r = await api('/api/paper/' + ${JSON.stringify(paper)} + '/semantic/claims',
      { method: 'POST', body: { field: 'key_conclusions', text: '验证补录用的一条陈述。', chunk_id: c.chunk_id, quote } });
    await loadManualClaims(${JSON.stringify(paper)});
    await loadQuality(${JSON.stringify(paper)});
    return { id: r.claim.id, recall: r.recall, panel: (document.querySelector('#manualCount')||{}).textContent || '' };
  })()`);
  log('补录结果:', added);
  await run(`['#qualityPanel','#manualPanel'].forEach(id => { const el = document.querySelector(id); if (el) el.open = true; })`);
  await sleep(400);
  await shot('paper-claim');

  // 验证用的补录不能留在主库里 —— 看完立刻删掉
  const cleaned = await run(`(async () => {
    await api('/api/paper/' + ${JSON.stringify(paper)} + '/semantic/claims/' + ${JSON.stringify('')}.concat(${JSON.stringify('')}) + ${JSON.stringify(added.id)}, { method: 'DELETE' });
    const d = await api('/api/paper/' + ${JSON.stringify(paper)} + '/semantic');
    return { remaining: (d.manual || []).length };
  })()`);
  log('清理后剩余补录:', cleaned);

  // 4) 切回对话：应正常切换
  await run(`openView('chat', {convId: ${JSON.stringify(conv)}})`);
  await sleep(1200);
  log('切回后状态:', await run(`({view:S.activeView, pane:(document.querySelector('.pane.active')||{}).dataset})`));
  await shot('chat-returned');

  // 5) 点左侧导航：位置条更新，且没有关闭按钮
  await run(`enterView('library')`);
  await sleep(900);
  log('点知识库后:', await run(`({view:S.activeView, chip:(document.querySelector('.loc-chip')||{}).textContent||'', 关闭按钮:!!document.querySelector('[data-close]')})`));

  // 6) 深色主题下的状态条：新组件自带配色，必须两种主题都看
  await run(`localStorage.setItem('litprism-theme','dark'); location.reload()`);
  await sleep(2200);
  await run(`openView('paper', {paperId: ${JSON.stringify(paper)}})`);
  await sleep(1600);
  await shot('paper-view-dark');
  await run(`localStorage.setItem('litprism-theme','light'); location.reload()`);
  await sleep(2200);

  // 7) 其余页面逐个过一遍（只截图，不改动）
  const views = ['library', 'verify', 'recommend', 'deliverables', 'knowledge',
                 'memories', 'trash', 'skills', 'settings'];
  for (const v of views) {
    await run(`enterView('${v}')`);
    await sleep(800);
    await shot('view-' + v);
  }

  // 8) 右栏：证据详情（来源面板）—— 这是对话与解读共用的侧栏
  // 设置页会留下模态框，先关掉，否则挡住右栏
  // 设置页是 #settingsModal 覆盖层（不是 <dialog>），必须用它的关闭函数
  await run(`typeof closeSettings === 'function' ? closeSettings() : $('#settingsModal')?.classList.remove('open')`);
  await sleep(500);
  const evidence = await run(`(async () => {
    const d = await api('/api/paper/' + ${JSON.stringify(paper)} + '/text');
    const c = d.pages.flatMap(p => p.chunks)[0];
    openView('paper', {paperId: ${JSON.stringify(paper)}});
    await new Promise(r => setTimeout(r, 600));
    S.evidenceRef = c.chunk_id;
    toggleInspector(true);
    refreshInspector();
    return c.chunk_id;
  })()`);
  log('右栏证据:', evidence);
  await sleep(1400);
  await shot('inspector-evidence');

  child.kill();
  app.quit();
}).catch((e) => {
  log('失败：', e && e.message ? e.message : String(e));
  app.quit();
});
