/**
 * 运行时缺口探针：起 sidecar → 载入真实页面 → 走遍所有视图与设置面板 →
 * 收集控制台报错、失败请求、未处理的 Promise 拒绝。
 *
 * 与 _shot.js 的区别：它出图，本脚本只出诊断，用于排查"点了没反应""某页报错"
 * 这类静态读代码看不出来的问题。
 *
 * 用法：env -u ELECTRON_RUN_AS_NODE ./node_modules/electron/dist/electron.exe tools/_probe.js
 * 输出：packaging/shots/probe.log
 */

const { app, BrowserWindow } = require('electron');
const { spawn } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');

const ROOT = path.resolve(__dirname, '..');
const OUT = path.join(ROOT, 'packaging', 'shots');
const LOG = path.join(OUT, 'probe.log');
const PY = process.env.LITPRISM_PY || 'C:/Users/18434/.workbuddy/binaries/python/envs/default/Scripts/python.exe';

fs.mkdirSync(OUT, { recursive: true });
fs.writeFileSync(LOG, '');
function log(...parts) {
  fs.appendFileSync(LOG, parts.map(p => (typeof p === 'string' ? p : JSON.stringify(p))).join(' ') + '\n');
}

if (!app) { log('需要以 Electron 运行'); process.exit(1); }
app.commandLine.appendSwitch('disable-gpu');
app.commandLine.appendSwitch('disable-gpu-compositing');
app.commandLine.appendSwitch('disable-gpu-sandbox');
app.commandLine.appendSwitch('no-sandbox');
app.disableHardwareAcceleration();

const sleep = ms => new Promise(r => setTimeout(r, ms));

function waitHandshake(child) {
  return new Promise((resolve, reject) => {
    let buf = '';
    const timer = setTimeout(() => reject(new Error('sidecar 启动超时')), 90000);
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', chunk => {
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

  const win = new BrowserWindow({
    width: 1280, height: 820, show: false,
    webPreferences: { preload: path.join(ROOT, 'desktop-electron', 'preload.js') },
  });

  const consoleIssues = [];
  const httpIssues = [];
  const loadIssues = [];

  win.webContents.on('console-message', (_e, level, message, line, source) => {
    // level: 0 verbose 1 info 2 warning 3 error
    if (level >= 2) {
      consoleIssues.push({ level: level === 3 ? 'error' : 'warn', message, source: String(source || '').split('/').pop() + ':' + line });
    }
  });
  win.webContents.on('did-fail-load', (_e, code, desc, url) => {
    if (code !== -3) loadIssues.push({ code, desc, url });
  });
  win.webContents.session.webRequest.onCompleted({ urls: ['*://127.0.0.1:*/*'] }, d => {
    if (d.statusCode >= 400) httpIssues.push({ status: d.statusCode, method: d.method, url: d.url.replace(/^http:\/\/127\.0\.0\.1:\d+/, '') });
  });

  await win.loadURL(`http://127.0.0.1:${port}/#t=${encodeURIComponent(token)}`);
  await sleep(1800);

  const run = js => win.webContents.executeJavaScript(js);

  // 页面级未捕获异常
  await run(`window.__probeErrors = [];
    window.addEventListener('error', e => window.__probeErrors.push('error: ' + (e.message || e.type)));
    window.addEventListener('unhandledrejection', e => window.__probeErrors.push('rejection: ' + (e.reason && e.reason.message || e.reason)));
    true`);

  const conv = await run(`(async () => (await api('/api/conversations')).items?.[0]?.id || null)()`);
  const paper = await run(`(async () => (await api('/api/papers')).items?.[0]?.id || null)()`);
  log('会话', conv, '| 论文', paper);

  // 1) 走遍所有主视图
  const views = ['chat', 'library', 'knowledge', 'deliverables', 'recommend', 'memories', 'trash', 'skills', 'verify'];
  for (const v of views) {
    const before = consoleIssues.length + httpIssues.length;
    await run(`openView(${JSON.stringify(v)})`);
    await sleep(1100);
    // 记录导航后实际停在哪个视图：openView 失败时页面会静默留在原处
    const landed = await run(`(() => {
      const pane = [...document.querySelectorAll('[data-pane]')].find(p => p.offsetParent !== null);
      const crumb = (document.querySelector('.loc-chip') || {}).textContent || '(无面包屑)';
      return ((pane && pane.dataset.pane) || '(无可见 pane)') + ' | 面包屑: ' + crumb.trim();
    })()`);
    log('视图', v, '→ 实际', landed, '新增问题', consoleIssues.length + httpIssues.length - before);
  }

  // 2) 设置各面板
  const panes = ['general', 'model', 'sources', 'verify', 'retrieval', 'export', 'backup', 'about'];
  for (const p of panes) {
    const before = consoleIssues.length + httpIssues.length;
    await run(`openSettings(${JSON.stringify(p)})`);
    await sleep(800);
    log('设置', p, '新增问题', consoleIssues.length + httpIssues.length - before);
  }
  await run(`closeSettings()`);
  await sleep(300);

  // 3) 论文页三个标签
  if (paper) {
    await run(`openView('paper', {paperId: ${JSON.stringify(paper)}})`);
    await sleep(1600);
    for (const tab of ['text', 'concepts', 'analysis']) {
      const before = consoleIssues.length + httpIssues.length;
      await run(`document.querySelector('[data-ptab="${tab}"]')?.click()`);
      await sleep(900);
      log('论文标签', tab, '新增问题', consoleIssues.length + httpIssues.length - before);
    }
  }

  // 4) 右栏三个标签
  const beforeIns = consoleIssues.length + httpIssues.length;
  await run(`toggleInspector(true)`);
  await sleep(900);
  for (const t of ['context', 'graph', 'sources']) {
    await run(`document.querySelector('[data-insp="${t}"]')?.click()`);
    await sleep(700);
  }
  log('右栏三标签 新增问题', consoleIssues.length + httpIssues.length - beforeIns);

  // 5) 四套主题
  for (const id of ['paper', 'paper-dark', 'minimal', 'glass']) {
    const before = consoleIssues.length + httpIssues.length;
    await run(`setTheme(${JSON.stringify(id)})`);
    await sleep(500);
    log('主题', id, '新增问题', consoleIssues.length + httpIssues.length - before);
  }
  await run(`setTheme('paper')`);

  const pageErrors = await run(`window.__probeErrors || []`);

  // 去重汇总
  const dedupe = arr => {
    const seen = new Map();
    for (const it of arr) {
      const k = JSON.stringify(it);
      seen.set(k, (seen.get(k) || 0) + 1);
    }
    return [...seen.entries()].map(([k, n]) => ({ ...JSON.parse(k), count: n }));
  };

  log('');
  log('================ 汇总 ================');
  log('控制台 warn/error:', consoleIssues.length);
  for (const it of dedupe(consoleIssues)) log('  [' + it.level + ']', it.message, '·', it.source, it.count > 1 ? '(x' + it.count + ')' : '');
  log('HTTP 4xx/5xx:', httpIssues.length);
  for (const it of dedupe(httpIssues)) log('  ' + it.status, it.method, it.url, it.count > 1 ? '(x' + it.count + ')' : '');
  log('加载失败:', loadIssues.length);
  for (const it of dedupe(loadIssues)) log('  ', it.code, it.desc, it.url);
  log('页面未捕获异常:', pageErrors.length);
  for (const e of pageErrors) log('  ', e);

  child.kill();
  app.quit();
}).catch(e => {
  log('探针失败：', e && e.message ? e.message : String(e));
  app.quit();
});
