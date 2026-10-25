'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { resolveWindowState: fit, readWindowState, writeWindowState, createWindowState } = require('../window-state');
const { FakeWindow, FakeScreen, display } = require('./fakes.cjs');

const saved = (bounds = { x: 100, y: 80, width: 1100, height: 700 }, maximized = false) => ({ version: 1, bounds, maximized });
function inside(bounds, area) {
  assert(bounds.x >= area.x && bounds.y >= area.y);
  assert(bounds.x + bounds.width <= area.x + area.width);
  assert(bounds.y + bounds.height <= area.y + area.height);
}

test('missing cache centers default size in work area, excluding taskbar', () => {
  const d = display(1, 0, 40, 1920, 1040);
  const result = fit(null, [d], 1);
  assert.deepEqual(result.options, { x: 280, y: 120, width: 1360, height: 880, minWidth: 1000, minHeight: 660 });
});
test('retains valid normal bounds and maximized flag', () => {
  const state = saved(undefined, true);
  assert.deepEqual(fit(state, [display()], 1).state, state);
});
test('negative monitor origins remain valid; DPI is already in DIP', () => {
  const left = { ...display(2, -1600, -300, 1600, 900), scaleFactor: 2 };
  const state = saved({ x: -1500, y: -250, width: 1100, height: 700 });
  assert.deepEqual(fit(state, [display(), left], 1).state, state);
});
test('monitor with greatest overlap wins rather than enumeration order', () => {
  const state = saved({ x: 1800, y: 100, width: 1400, height: 800 });
  const result = fit(state, [display(), display(2, 1920)], 1);
  assert.equal(result.state.bounds.x, 1920);
});
test('removed monitor falls back to primary and fits smaller workspace', () => {
  const result = fit(saved({ x: 3500, y: 100, width: 1600, height: 1000 }), [display(1, 0, 30, 1280, 690)], 1);
  assert.deepEqual(result.state.bounds, { x: 0, y: 30, width: 1280, height: 690 });
});
test('tiny work area lowers minimum size instead of forcing window off screen', () => {
  const d = display(1, 0, 0, 800, 560);
  assert.deepEqual(fit(null, [d], 1).options, { x: 0, y: 0, width: 800, height: 560, minWidth: 800, minHeight: 560 });
});
test('partially offscreen window is fully clamped with title bar reachable', () => {
  const d = display(1, 0, 40);
  const result = fit(saved({ x: -100, y: -700, width: 1200, height: 800 }), [d], 1);
  inside(result.state.bounds, d.workArea);
});
test('corrupt schema and invalid numeric geometry use defaults', () => {
  for (const invalid of [null, [], 'bad', { version: 2 }, saved({}, true), saved(undefined, 'yes'),
    saved({ x: Infinity, y: 0, width: 1000, height: 700 }), saved({ x: 0, y: 0, width: -1, height: 700 }),
    saved({ x: 0, y: 0, width: '1200', height: 700 }), saved({ x: 0, y: .5, width: 1200, height: 700 })]) {
    assert.deepEqual(fit(invalid, [display()], 1), fit(null, [display()], 1));
  }
});
test('undersized and huge numeric geometry fit within monitor bounds', () => {
  const d = display();
  for (const b of [{ x: 0, y: 0, width: 1, height: 1 }, { x: 9999999, y: -9999999, width: 9999999, height: 9999999 }]) {
    const result = fit(saved(b), [d], 1); inside(result.state.bounds, d.workArea);
    assert(result.options.width >= 1000 && result.options.height >= 660);
  }
});
test('geometry invariant across fixed multi-monitor/resolution/DPI matrix', () => {
  const displays = [display(), display(2, -1280, 80, 1280, 700), display(3, 0, -900, 900, 700)];
  for (const x of [-10000, -1200, 0, 1700, 9000]) for (const y of [-900, 0, 9000]) {
    const result = fit(saved({ x, y, width: 1300, height: 900 }), displays, 1);
    assert(displays.some(d => { const b = result.state.bounds, a = d.workArea;
      return b.x >= a.x && b.y >= a.y && b.x + b.width <= a.x + a.width && b.y + b.height <= a.y + a.height; }));
  }
});

function fixture(t, state = saved(), displays = [display()], ready = true) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'litprism-window-test-'));
  const file = path.join(root, 'window-state.json');
  if (state) fs.writeFileSync(file, JSON.stringify(state));
  const screen = new FakeScreen(displays);
  let pending; const timers = {
    setTimeout(fn) { pending = fn; return 1; }, clearTimeout() { pending = undefined; },
    tick() { const fn = pending; pending = undefined; if (fn) fn(); },
  };
  const logs = [];
  const controller = createWindowState({ statePath: file, screen, timers, log: msg => logs.push(msg) });
  const win = new FakeWindow(controller.options); controller.attach(win);
  if (ready) win.emit('ready-to-show');
  t.after(() => { controller.dispose(); fs.rmSync(root, { recursive: true, force: true }); });
  return { root, file, screen, controller, win, timers, logs, read: () => JSON.parse(fs.readFileSync(file, 'utf8')) };
}
test('move/resize debounce coalesces events and close flushes latest normal geometry', t => {
  const f = fixture(t);
  f.win.bounds.x = 250; f.win.emit('move'); f.win.bounds.width = 1300; f.win.emit('resize');
  assert.equal(f.read().bounds.x, 100);
  f.win.emit('close'); assert.equal(f.read().bounds.x, 250); assert.equal(f.read().bounds.width, 1300);
});
test('timer persists state without waiting for window close', t => {
  const f = fixture(t); f.win.bounds.y = 120; f.win.emit('move'); f.timers.tick();
  assert.equal(f.read().bounds.y, 120);
});
test('maximized restart retains normal dimensions and unmaximize persists preference', t => {
  const f = fixture(t, saved(undefined, true)); f.win.emit('ready-to-show');
  assert(f.win.isMaximized()); f.timers.tick(); assert.deepEqual(f.read(), saved(undefined, true));
  f.win.unmaximize(); f.win.emit('close'); assert.deepEqual(f.read(), saved());
});
test('closing minimized window preserves prior maximized preference and normal bounds', t => {
  const f = fixture(t, saved(undefined, true)); f.win.emit('ready-to-show');
  f.win.minimized = true; f.win.maximized = false; f.win.emit('resize');
  f.win.emit('close'); assert.deepEqual(f.read(), saved(undefined, true));
});
test('temporary fullscreen and minimized geometry are never persisted', t => {
  const f = fixture(t); f.win.fullscreen = true; f.win.bounds = { x: 0, y: 0, width: 1920, height: 1080 };
  f.win.emit('resize'); f.win.emit('close'); assert.deepEqual(f.read(), saved());
});
test('live monitor removal relocates normal window and preserves maximization', t => {
  const f = fixture(t, saved({ x: 2100, y: 100, width: 1200, height: 800 }, true), [display(), display(2, 1920)]);
  f.win.emit('ready-to-show');
  f.screen.displays = [display(1, 0, 0, 1280, 720)]; f.screen.emit('display-removed');
  inside(f.win.bounds, f.screen.displays[0].workArea); assert(f.win.isMaximized());
  assert.deepEqual(f.win.minimum, [1000, 660]); f.timers.tick(); assert(f.read().maximized);
});
test('work area and scaling changes update minimum size without multiplying DPI', t => {
  const f = fixture(t); f.screen.displays = [{ ...display(1, 0, 40, 900, 600), scaleFactor: 2 }];
  f.screen.emit('display-metrics-changed', {}, f.screen.displays[0], ['scaleFactor', 'workArea']);
  assert.deepEqual(f.win.minimum, [900, 600]); inside(f.win.bounds, f.screen.displays[0].workArea);
});
test('minimized unplug defers onscreen relocation until restore without showing window', t => {
  const f = fixture(t, saved({ x: 2100, y: 0, width: 1200, height: 800 }), [display(), display(2, 1920)]);
  f.win.minimized = true; f.screen.displays = [display()]; f.screen.emit('display-removed');
  assert.equal(f.win.bounds.x, 2100); assert(!f.win.calls.includes('show'));
  f.win.restore(); inside(f.win.bounds, f.screen.displays[0].workArea);
});
test('display query failure leaves last cache intact and later events recover', t => {
  const f = fixture(t); f.screen.displays = []; f.win.emit('move'); f.timers.tick();
  assert.deepEqual(f.read(), saved()); assert(f.logs.length);
  f.screen.displays = [display(1, 0, 0, 800, 600)]; f.screen.emit('display-added');
  inside(f.win.bounds, f.screen.displays[0].workArea);
});
test('closed window removes screen/window listeners and cancels pending writes', t => {
  const f = fixture(t); f.win.emit('move'); f.win.destroyed = true; f.win.emit('closed');
  assert.equal(f.screen.eventNames().length, 0); f.timers.tick(); assert.deepEqual(f.read(), saved());
});
test('bad JSON, oversized cache and missing file recover without throwing', t => {
  const f = fixture(t);
  fs.writeFileSync(f.file, '{broken'); assert.equal(readWindowState(f.file), null);
  fs.writeFileSync(f.file, ' '.repeat(16385)); assert.equal(readWindowState(f.file), null);
  assert.equal(readWindowState(path.join(f.root, 'missing.json')), null);
});
test('atomic save persists only allowlisted geometry; no leftover temporary file', t => {
  const f = fixture(t);
  assert(writeWindowState(f.file, { ...saved(), token: 'synthetic-secret', url: 'https://example.org', minimized: true }));
  assert.deepEqual(f.read(), saved()); assert.deepEqual(fs.readdirSync(f.root), ['window-state.json']);
});
test('failed atomic replacement preserves previous file and cleans temporary file', t => {
  const f = fixture(t); const logs = [];
  const io = { ...fs, renameSync() { throw new Error('simulated EPERM'); } };
  assert.equal(writeWindowState(f.file, saved(undefined, true), io, msg => logs.push(msg)), false);
  assert.deepEqual(f.read(), saved()); assert.deepEqual(fs.readdirSync(f.root), ['window-state.json']);
  assert.equal(logs.length, 1);
});
test('cache directory permission failure remains nonfatal', t => {
  const f = fixture(t);
  const io = { ...fs, mkdirSync() { throw new Error('simulated EACCES'); } };
  assert.equal(writeWindowState(f.file, saved(undefined, true), io), false); assert.deepEqual(f.read(), saved());
});
test('partial temporary write failure preserves original cache and cleans owned temporary file', t => {
  const f = fixture(t);
  const io = { ...fs, writeFileSync(fd) { fs.writeFileSync(fd, '{partial'); throw new Error('simulated ENOSPC'); } };
  assert.equal(writeWindowState(f.file, saved(undefined, true), io), false);
  assert.deepEqual(f.read(), saved()); assert.deepEqual(fs.readdirSync(f.root), ['window-state.json']);
});
test('fullscreen monitor change defers actual relocation until leaving fullscreen', t => {
  const f = fixture(t, saved({ x: 2100, y: 0, width: 1200, height: 800 }), [display(), display(2, 1920)]);
  f.win.fullscreen = true; f.screen.displays = [display()]; f.screen.emit('display-removed');
  assert.equal(f.win.bounds.x, 2100); assert(!f.win.calls.includes('setBounds'));
  f.win.fullscreen = false; f.win.emit('leave-full-screen'); inside(f.win.bounds, f.screen.displays[0].workArea);
});
test('startup resize events cannot erase saved maximization before ready-to-show', t => {
  const f = fixture(t, saved(undefined, true), [display()], false);
  f.win.emit('resize'); f.timers.tick(); f.win.emit('ready-to-show');
  assert(f.win.isMaximized(), 'Initial native resize erased saved maximization');
});
test('display change before ready never prematurely maximizes or shows the hidden window', t => {
  const f = fixture(t, saved({ x: 2100, y: 0, width: 1200, height: 800 }, true), [display(), display(2, 1920)], false);
  f.screen.displays = [display()]; f.screen.emit('display-removed');
  inside(f.win.bounds, f.screen.displays[0].workArea);
  assert(!f.win.calls.includes('maximize') && !f.win.calls.includes('show'));
  f.win.emit('ready-to-show'); assert(f.win.isMaximized());
});
