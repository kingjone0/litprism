/**
 * 文献棱镜 LitPrism · Electron 外壳
 *
 * 与 Tauri 版做同一件事，但刻意走最简单的路径：
 *   起 Python sidecar → 读 stdout 握手行 → 窗口 loadURL(它自己的页面)
 *
 * 为什么这么设计：Python 侧本来就用 FastAPI 提供整套 UI 和 API（`run.py serve`），
 * 窗口直接加载那个地址就是**同源**页面，不需要 CORS、不需要注入脚本、
 * 不需要把前端打进壳里 —— 少一层就少一类坑。
 *
 * token 走 URL 的 hash（`#t=…`）：hash 不会发给服务器，也不进 Referer。
 */

const electron = require('electron');
const { app, BrowserWindow, dialog, ipcMain, shell } = electron;
const { spawn } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');

// 环境变量 ELECTRON_RUN_AS_NODE=1 会让 Electron 以**纯 Node** 身份启动，
// 此时 require('electron') 拿不到 app，下一行就会以
// 「Cannot read properties of undefined (reading 'commandLine')」崩掉 ——
// 打包后的应用无控制台，看起来就是「双击没反应」。
// 这是从某些 IDE/终端启动时继承下来的变量，不是打包问题。这里给出可读的原因。
if (!app) {
  console.error(
    'Electron 以 Node 模式启动（ELECTRON_RUN_AS_NODE 被设置）。\n' +
    '请清掉该变量后再启动，例如：env -u ELECTRON_RUN_AS_NODE LitPrism.exe\n' +
    '或直接从资源管理器双击。'
  );
  process.exit(1);
}

const HANDSHAKE = 'LITPRISM_READY ';
const STARTUP_TIMEOUT_MS = 120000;

// 本机实测：Chromium 的 GPU 进程反复以 0xC0000005（访问违例）崩溃，
// 最终 FATAL「GPU process isn't usable」→ 窗口全白。Tauri/WebView2 同样症状，
// 因为底子是同一个 Chromium。这里彻底关掉 GPU 与 GPU 沙箱。
// 这个应用是纯文本界面，不需要 GPU。
app.commandLine.appendSwitch('disable-gpu');
app.commandLine.appendSwitch('disable-gpu-compositing');
app.commandLine.appendSwitch('disable-gpu-sandbox');
app.commandLine.appendSwitch('no-sandbox');
app.disableHardwareAcceleration();

// 用自己的 userData 目录，避免和本机其它 Electron 程序共用（默认是 %APPDATA%\Electron）。
// 若设置了 LITPRISM_HOME（sidecar 的可写根也认这个变量），就跟着它走 ——
// 这样同一台机器上跑两份代码时，窗口缓存与知识库都不会互相踩。
const userDataRoot = process.env.LITPRISM_HOME || path.join(app.getPath('appData'), 'LitPrism');
app.setPath('userData', userDataRoot);

let child = null;
let sidecarInfo = null;
let win = null;

// 打包后没有控制台可看；启动过程写一行日志，排障时把路径打到 TEMP。
const TRACE_LOG = path.join(process.env.TEMP || process.env.TMP || '.', 'litprism-shell.log');
function trace(message) {
  try { fs.appendFileSync(TRACE_LOG, `${new Date().toISOString()} ${message}\n`); } catch { /* 日志失败不影响启动 */ }
}

/** 找 sidecar：环境变量 > 打包后的 resources > 开发目录 */
function locateSidecar() {
  const exeName = process.platform === 'win32' ? 'litprism-sidecar.exe' : 'litprism-sidecar';
  const candidates = [];
  if (process.env.LITPRISM_SIDECAR) candidates.push(process.env.LITPRISM_SIDECAR);
  candidates.push(path.join(process.resourcesPath || '', exeName));
  candidates.push(path.join(__dirname, 'binaries', exeName));
  candidates.push(path.join(__dirname, '..', 'packaging', 'dist', exeName));
  for (const c of candidates) {
    if (c && fs.existsSync(c)) return c;
  }
  throw new Error(
    `找不到后台服务 ${exeName}。请先运行 \`python packaging/build.py\`，` +
      `或用环境变量 LITPRISM_SIDECAR 指定。已尝试：\n${candidates.join('\n')}`
  );
}

/** 启动 sidecar 并等握手行（stdout 只认那一行，其余丢弃） */
function startSidecar() {
  return new Promise((resolve, reject) => {
    const exe = locateSidecar();
    trace('sidecar path: ' + exe);
    // Windows 下 .bat 不能直接 spawn（会 EINVAL），必须经 cmd
    const isBatch = /\.(bat|cmd)$/i.test(exe);
    const cmd = isBatch && process.platform === 'win32' ? 'cmd.exe' : exe;
    const args = isBatch && process.platform === 'win32'
      ? ['/d', '/s', '/c', exe, '--port', '0']
      : ['--port', '0'];

    trace('spawn: ' + cmd + ' ' + args.join(' '));
    child = spawn(cmd, args, {
      stdio: ['ignore', 'pipe', 'ignore'],
      windowsHide: true,
    });

    const timer = setTimeout(() => {
      reject(new Error(`后台服务启动超时（${STARTUP_TIMEOUT_MS / 1000}s）`));
    }, STARTUP_TIMEOUT_MS);

    child.on('error', (err) => {
      trace('spawn error: ' + err.message);
      clearTimeout(timer);
      reject(new Error(`启动后台服务失败（${exe}）：${err.message}`));
    });

    child.on('exit', (code) => {
      trace('sidecar exited: ' + code);
      clearTimeout(timer);
      reject(new Error(`后台服务在握手前退出（code=${code}）`));
    });

    let buf = '';
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk) => {
      buf += chunk;
      for (const line of buf.split(/\r?\n/)) {
        if (!line.startsWith(HANDSHAKE)) continue;
        try {
          const info = JSON.parse(line.slice(HANDSHAKE.length));
          sidecarInfo = info;
          clearTimeout(timer);
          resolve(info);
        } catch (e) {
          clearTimeout(timer);
          reject(new Error(`握手信息解析失败：${e.message}\n原始内容：${line}`));
        }
        return;
      }
      // 只保留最后一段（可能被切断的半行）
      buf = buf.slice(-4096);
    });
  });
}

/** 回收 sidecar。
 *
 * **顺序很关键**：PyInstaller onefile 是「启动器进程 + 真进程」两段结构，
 * 启动器只是等真进程结束。若先杀启动器，再按它 `/T` 枚举子进程时父进程已经没了，
 * 真进程就成了孤儿继续占着端口（实测踩过：应用退了，sidecar 还活着）。
 * 所以先用握手行里的真实 pid 杀，再收拾启动器。
 */
function stopSidecar() {
  if (!child && !sidecarInfo) return;
  const launcherPid = child && child.pid;
  const realPid = sidecarInfo && sidecarInfo.pid;

  if (process.platform === 'win32') {
    for (const pid of [realPid, launcherPid]) {
      if (!pid) continue;
      try {
        spawn('taskkill', ['/PID', String(pid), '/T', '/F'], {
          windowsHide: true,
          stdio: 'ignore',
        });
      } catch {
        /* ignore */
      }
    }
  } else if (child) {
    try {
      child.kill('SIGTERM');
    } catch {
      /* ignore */
    }
  }

  child = null;
  sidecarInfo = null;
}

/**
 * 自证探针（仅测试用，LITPRISM_SELFTEST=1 时启用）。
 *
 * 为什么需要它：桌面窗口的截图会被其它窗口遮挡，靠"拍屏幕"判断渲染状态不可靠，
 * 之前已经因此误判过好几轮。改成让页面**自己**把 DOM 摘要与截图写到磁盘，
 * 不依赖窗口层级，也不依赖外部工具。
 */
async function selfTest() {
  await new Promise((r) => setTimeout(r, 2500));
  try {
    const dom = await win.webContents.executeJavaScript(
      `JSON.stringify({
         title: document.title,
         chars: document.body ? document.body.innerText.length : -1,
         head: document.body ? document.body.innerText.slice(0, 300) : '',
         env: window.__LITPRISM__ ? 'set' : 'none',
         scripts: [...document.scripts].map(s => s.src || 'inline'),
       })`
    );
    fs.writeFileSync(path.join(__dirname, '..', '_selftest.json'), dom, 'utf8');
    const img = await win.webContents.capturePage();
    fs.writeFileSync(path.join(__dirname, '..', '_selftest.png'), img.toPNG());

    // 界面验收：进到论文解读 → 点开第一条待核对陈述 → 检查右栏原文是否高亮了对应句子。
    // 只验某个视图时可设 LITPRISM_SELFTEST_SKIP_CLAIM=1 跳过这段（省时间，避免挂在语义接口上）。
    const nav = process.env.LITPRISM_SELFTEST_SKIP_CLAIM ? {found: false}
      : await win.webContents.executeJavaScript(
      `(async () => {
         const sleep = ms => new Promise(r => setTimeout(r, ms));
         const paper = document.querySelector('[data-paper]');
         if (paper) { paper.click(); await sleep(2500); }
         let link = document.querySelector('[data-semantic-claim]');
         if (!link) {
           const tab = [...document.querySelectorAll('button')]
             .find(b => (b.textContent || '').trim() === '解读');
           if (tab) { tab.click(); await sleep(2500); link = document.querySelector('[data-semantic-claim]'); }
         }
         if (!link) return {found: false, body: document.body.innerText.slice(0, 300)};
         link.click(); await sleep(2500);
         const box = document.querySelector('#inspSources');
         return {found: true,
                 marks: document.querySelectorAll('mark.ev-hit').length,
                 sources: document.querySelectorAll('.ev-source').length,
                 text: box ? box.innerText.slice(0, 500) : ''};
       })()`
    );
    if (!process.env.LITPRISM_SELFTEST_SKIP_CLAIM) {
      fs.writeFileSync(path.join(__dirname, '..', '_selftest_inspector.json'),
        JSON.stringify(nav, null, 2), 'utf8');
      const shot = await win.webContents.capturePage();
      fs.writeFileSync(path.join(__dirname, '..', '_selftest_inspector.png'), shot.toPNG());
    }

    // 可选：切到指定视图并截图（用于新界面的可视验证）
    const view = process.env.LITPRISM_SELFTEST_VIEW;
    if (view) {
      const shot = await win.webContents.executeJavaScript(
        `(async () => {
           const sleep = ms => new Promise(r => setTimeout(r, ms));
           const nav = document.querySelector('[data-view="${view}"]');
           if (nav) { nav.click(); await sleep(2000); }
           const pane = document.querySelector('[data-pane="${view}"]');
           return {clicked: !!nav, text: pane ? pane.innerText.slice(0, 1500) : ''};
         })()`
      );
      fs.writeFileSync(path.join(__dirname, '..', '_selftest_view.json'),
        JSON.stringify(shot, null, 2), 'utf8');
      const viewShot = await win.webContents.capturePage();
      fs.writeFileSync(path.join(__dirname, '..', '_selftest_view.png'), viewShot.toPNG());
    }
  } catch (e) {
    fs.writeFileSync(path.join(__dirname, '..', '_selftest.err'), String(e && e.stack), 'utf8');
  }
}

async function createWindow() {
  // 叠层控件与页面顶栏同色，避免出现一条突兀的浅/深色带
  const TITLEBAR_COLORS = {
    light: { color: '#ffffff', symbol: '#55504a' },
    dark: { color: '#1c1b19', symbol: '#d9d4cc' },
  };

  win = new BrowserWindow({
    width: 1360,
    height: 880,
    minWidth: 1000,
    minHeight: 660,
    title: '文献棱镜 LitPrism',
    backgroundColor: '#fcfcfa',
    autoHideMenuBar: true,
    show: false,
    // 页面里已经有自绘的工具栏；再叠一条系统标题栏就是两层边框。
    // 用 hidden + overlay：去掉系统标题栏，只把最小化/最大化/关闭叠在右上角。
    titleBarStyle: 'hidden',
    titleBarOverlay: { color: TITLEBAR_COLORS.light.color, symbolColor: TITLEBAR_COLORS.light.symbol, height: 44 },
    webPreferences: {
      // 页面由本地后台提供，不需要任何 Node 能力 —— 只加一个主题通道
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    },
  });

  ipcMain.on('ui:titlebar-theme', (_event, theme) => {
    if (!win || win.isDestroyed()) return;
    const palette = TITLEBAR_COLORS[theme] || TITLEBAR_COLORS.light;
    try {
      win.setTitleBarOverlay({ color: palette.color, symbolColor: palette.symbol, height: 44 });
      win.setBackgroundColor(theme === 'dark' ? '#171614' : '#fcfcfa');
    } catch {
      // 老版本 Electron 不支持动态更换叠层颜色：保持初始配色即可
    }
  });

  win.once('ready-to-show', () => win.show());

  // 外链一律交给系统浏览器，窗口永远只待在本机后台
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });

  try {
    const { port, token } = await startSidecar();
    trace('handshake ok: port=' + port);
    await win.loadURL(`http://127.0.0.1:${port}/#t=${encodeURIComponent(token)}`);
    trace('window loaded');
    if (process.env.LITPRISM_SELFTEST) await selfTest();
  } catch (err) {
    trace('startup failed: ' + err.message);
    await win.loadURL(
      'data:text/html;charset=utf-8,' +
        encodeURIComponent(
          `<body style="margin:0;background:#0b1120;color:#f87171;font:14px/1.7 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;padding:32px">
             <h3 style="margin:0 0 12px">后台服务启动失败</h3>
             <pre style="white-space:pre-wrap;color:#cbd5e1">${String(err.message)}</pre>
           </body>`
        )
    );
    win.show();
  }
}

// 单实例：两个实例会各起一个 sidecar，抢同一个 SQLite 知识库
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (win) {
      if (win.isMinimized()) win.restore();
      win.show();
      win.focus();
    }
  });

  app.whenReady().then(createWindow);

  app.on('window-all-closed', () => {
    stopSidecar();
    app.quit();
  });

  app.on('before-quit', stopSidecar);
  process.on('exit', stopSidecar);
}
