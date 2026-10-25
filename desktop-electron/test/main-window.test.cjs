'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { createRequire } = require('node:module');
const { FakeWindow, FakeScreen, display } = require('./fakes.cjs');

const entry = path.resolve(__dirname, '../main.js');
const entryRequire = createRequire(entry);

async function boot(root, saved, displays = [display()], closeBeforeHandshake = false) {
  if (saved) fs.writeFileSync(path.join(root, 'window-state.json'), JSON.stringify(saved));
  const app = new EventEmitter();
  let ready = false;
  app.commandLine = { appendSwitch() {} };
  app.disableHardwareAcceleration = () => {};
  app.getPath = () => root;
  app.setPath = (name, value) => { if (name === 'userData') app.userData = value; };
  app.requestSingleInstanceLock = () => true;
  app.quit = () => {};
  app.whenReady = () => Promise.resolve().then(() => { ready = true; });
  const screen = new FakeScreen(displays);
  const originalDisplays = screen.getAllDisplays.bind(screen);
  screen.getAllDisplays = () => { assert(ready, 'screen accessed before app ready'); return originalDisplays(); };
  const windows = [], spawns = [], ipcMain = new EventEmitter();
  class Window extends FakeWindow { constructor(opts) { super(opts); windows.push(this); } }
  const localProcess = new EventEmitter();
  localProcess.env = { LITPRISM_HOME: root, LITPRISM_SIDECAR: path.join(root, 'sidecar.exe'), TEMP: root };
  localProcess.platform = 'win32';
  localProcess.exit = () => { throw new Error('Unexpected process.exit'); };
  // Fake only Electron/process launch. Main entry, state module and disk I/O are real.
  vm.runInNewContext(fs.readFileSync(entry, 'utf8'), {
    require: name => {
      if (name === 'electron') return { app, BrowserWindow: Window, screen, ipcMain, shell: { openExternal() {} } };
      if (name === 'node:child_process') return { spawn: (...args) => {
        spawns.push(args);
        const child = new EventEmitter(); child.pid = 12345;
        child.stdout = new EventEmitter(); child.stdout.setEncoding = () => {};
        queueMicrotask(() => {
          if (closeBeforeHandshake) { windows[0].emit('close'); windows[0].destroyed = true; windows[0].emit('closed'); }
          child.stdout.emit('data', 'LITPRISM_READY {"port":8899,"token":"synthetic","pid":12346}\n');
        });
        return child;
      } };
      if (name === 'node:fs') return { ...fs, existsSync: p => p.endsWith('sidecar.exe') || fs.existsSync(p) };
      return entryRequire(name);
    },
    __dirname: path.dirname(entry), process: localProcess, console, setTimeout, clearTimeout,
  }, { filename: entry });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(windows.length, 1);
  return { win: windows[0], app, screen, spawns };
}

test('main restores geometry/maximized before show and persists normal bounds on close', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'litprism-shell-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const saved = { version: 1, bounds: { x: 70, y: 50, width: 1110, height: 710 }, maximized: true };
  const { win, app, screen, spawns } = await boot(root, saved);
  for (const [key, value] of Object.entries(saved.bounds)) assert.equal(win.options[key], value);
  assert(win.calls.indexOf('maximize') < win.calls.indexOf('show'));
  assert.equal(win.options.webPreferences.nodeIntegration, false);
  assert.equal(win.options.webPreferences.contextIsolation, true);
  assert.equal(app.userData, root);
  assert.equal(spawns.length, 1, 'No real processes are launched by this test');
  win.emit('close'); win.destroyed = true; win.emit('closed');
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(root, 'window-state.json'))), saved);
  assert.equal(screen.listenerCount('display-removed'), 0);
});

test('main fits small display and separate data roots keep separate window caches', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'litprism-shell-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const a = path.join(root, 'a'), b = path.join(root, 'b'); fs.mkdirSync(a); fs.mkdirSync(b);
  const saved = { version: 1, bounds: { x: 3000, y: 400, width: 1600, height: 900 }, maximized: false };
  const first = await boot(a, saved, [display(1, 0, 0, 800, 600)]);
  assert.equal(first.win.options.width, 800); assert.equal(first.win.options.minWidth, 800);
  assert.equal(first.win.options.height, 600); assert.equal(first.win.options.minHeight, 600);
  assert.equal(first.win.options.x, 0); assert.equal(first.win.options.y, 0);
  first.win.emit('close'); first.win.destroyed = true; first.win.emit('closed');
  const second = await boot(b, null);
  assert.equal(second.win.options.width, 1360);
  second.win.emit('close'); second.win.destroyed = true; second.win.emit('closed');
  assert.notDeepEqual(JSON.parse(fs.readFileSync(path.join(a, 'window-state.json'))),
    JSON.parse(fs.readFileSync(path.join(b, 'window-state.json'))));
});

test('closing during startup never loads or shows a destroyed window', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'litprism-shell-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const { win, app, screen } = await boot(root, null, [display()], true);
  assert.equal(win.url, undefined); assert(!win.calls.includes('show'));
  assert.equal(screen.listenerCount('display-removed'), 0);
  app.emit('second-instance'); assert(!win.calls.includes('focus'));
});
