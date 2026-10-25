'use strict';

// Electron screen and BrowserWindow both use DIP coordinates. Never scale twice.
const fs = require('node:fs');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const DEFAULT_SIZE = { width: 1360, height: 880 };
const MIN_SIZE = { width: 1000, height: 660 };
const DISPLAY_EVENTS = ['display-added', 'display-removed', 'display-metrics-changed'];

function validBounds(b) {
  return b && ['x', 'y', 'width', 'height'].every(k => Number.isInteger(b[k]) && Math.abs(b[k]) <= 2147483647)
    && b.width > 0 && b.height > 0;
}

function validState(s) {
  return s && s.version === 1 && validBounds(s.bounds) && typeof s.maximized === 'boolean';
}

function cleanBounds(b) { return { x: b.x, y: b.y, width: b.width, height: b.height }; }
function overlap(a, b) {
  return Math.max(0, Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x))
    * Math.max(0, Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y));
}

/** Fit the whole normal window to one current work area (taskbar excluded). */
function resolveWindowState(saved, displays, primaryId) {
  const available = displays.filter(d => validBounds(d.workArea));
  if (!available.length) throw new Error('No usable display work area');
  const valid = validState(saved);
  const primary = available.find(d => d.id === primaryId) || available[0];
  const target = valid ? available.reduce((best, d) =>
    overlap(saved.bounds, d.workArea) > overlap(saved.bounds, best.workArea) ? d : best, primary) : primary;
  const area = target.workArea;
  const requested = valid ? saved.bounds : DEFAULT_SIZE;
  const minWidth = Math.min(MIN_SIZE.width, area.width), minHeight = Math.min(MIN_SIZE.height, area.height);
  const width = Math.max(minWidth, Math.min(requested.width, area.width));
  const height = Math.max(minHeight, Math.min(requested.height, area.height));
  const sameDisplay = valid && overlap(saved.bounds, area) > 0;
  const x = sameDisplay ? Math.max(area.x, Math.min(saved.bounds.x, area.x + area.width - width))
    : area.x + Math.floor((area.width - width) / 2);
  const y = sameDisplay ? Math.max(area.y, Math.min(saved.bounds.y, area.y + area.height - height))
    : area.y + Math.floor((area.height - height) / 2);
  return { state: { version: 1, bounds: { x, y, width, height }, maximized: valid ? saved.maximized : false },
    options: { x, y, width, height, minWidth, minHeight } };
}

function readWindowState(file, io = fs, log = () => {}) {
  try {
    if (io.statSync(file).size > 16384) throw new Error('Window cache too large');
    const state = JSON.parse(io.readFileSync(file, 'utf8'));
    return validState(state) ? { version: 1, bounds: cleanBounds(state.bounds), maximized: state.maximized } : null;
  } catch (error) {
    if (error.code !== 'ENOENT') log('window state: cache unreadable, using defaults');
    return null;
  }
}

function writeWindowState(file, state, io = fs, log = () => {}) {
  if (!validState(state)) return false;
  const temporary = file + '.' + randomUUID() + '.tmp';
  let created = false, descriptor;
  try {
    io.mkdirSync(path.dirname(file), { recursive: true });
    // Projected allowlist only: no URL/token, renderer state or arbitrary cache fields.
    descriptor = io.openSync(temporary, 'wx', 0o600);
    created = true;
    io.writeFileSync(descriptor, JSON.stringify({ version: 1, bounds: cleanBounds(state.bounds), maximized: state.maximized }), 'utf8');
    io.fsyncSync(descriptor);
    io.closeSync(descriptor); descriptor = undefined;
    io.renameSync(temporary, file);
    return true;
  } catch {
    log('window state: save failed; previous cache retained');
    return false;
  } finally {
    if (descriptor !== undefined) { try { io.closeSync(descriptor); } catch { /* Best effort on disk failure. */ } }
    if (created) { try { io.unlinkSync(temporary); } catch { /* Already renamed, or best effort cleanup. */ } }
  }
}

function createWindowState({ statePath, screen, io = fs, log = () => {}, timers = { setTimeout, clearTimeout } }) {
  function topology() {
    try {
      const displays = screen.getAllDisplays();
      const primary = screen.getPrimaryDisplay();
      if (!displays.some(d => validBounds(d.workArea))) throw new Error('No display');
      return { displays, primaryId: primary?.id };
    } catch { log('window state: display query failed; keeping current bounds'); return null; }
  }
  const initialScreens = topology();
  const initial = initialScreens ? resolveWindowState(readWindowState(statePath, io, log), initialScreens.displays, initialScreens.primaryId)
    : { state: { version: 1, bounds: { x: 0, y: 0, ...DEFAULT_SIZE }, maximized: false },
      options: { ...DEFAULT_SIZE, minWidth: MIN_SIZE.width, minHeight: MIN_SIZE.height } };
  let state = initial.state, win, timer, applying = false, disposed = false, restored = false;
  const listeners = [];
  function listen(emitter, name, fn) { emitter.on(name, fn); listeners.push([emitter, name, fn]); }
  function capture() {
    if (!win || win.isDestroyed() || !restored || applying || win.isMinimized() || win.isFullScreen()) return;
    const bounds = win.getNormalBounds();
    if (validBounds(bounds)) state = { version: 1, bounds: cleanBounds(bounds), maximized: win.isMaximized() };
  }
  function flush() {
    if (disposed) return;
    timers.clearTimeout(timer); timer = undefined;
    capture();
    const screens = topology();
    if (!screens) return; // A transient display failure must not replace a good cache.
    state = resolveWindowState(state, screens.displays, screens.primaryId).state;
    writeWindowState(statePath, state, io, log);
  }
  function schedule() {
    if (disposed || applying || !restored) return;
    capture();
    timers.clearTimeout(timer);
    timer = timers.setTimeout(flush, 300);
  }
  function repair() {
    if (!win || win.isDestroyed() || applying || disposed) return;
    const screens = topology();
    if (!screens) return;
    capture();
    const fit = resolveWindowState(state, screens.displays, screens.primaryId);
    state = fit.state;
    // Do not pop a minimized window or exit a user's temporary fullscreen mode.
    if (win.isMinimized() || win.isFullScreen()) { schedule(); return; }
    applying = true;
    try {
      const changed = JSON.stringify(cleanBounds(win.getNormalBounds())) !== JSON.stringify(state.bounds);
      const maximized = state.maximized;
      if (changed && win.isMaximized()) win.unmaximize();
      win.setMinimumSize(fit.options.minWidth, fit.options.minHeight);
      if (changed) win.setBounds(state.bounds);
      if (restored && maximized && !win.isMaximized()) win.maximize();
    } finally { applying = false; }
    schedule();
  }
  function dispose() {
    disposed = true;
    timers.clearTimeout(timer);
    for (const [emitter, name, fn] of listeners) emitter.removeListener(name, fn);
  }
  function attach(window) {
    if (win) throw new Error('Window state already attached');
    win = window;
    // Apply the startup preference at first rendered frame, before main.js calls show.
    const ready = () => {
      win.removeListener('ready-to-show', ready);
      restored = true;
      applying = true;
      try { if (state.maximized) win.maximize(); } finally { applying = false; }
      repair();
    };
    listen(win, 'ready-to-show', ready);
    for (const event of ['move', 'resize', 'maximize', 'unmaximize']) listen(win, event, schedule);
    for (const event of ['restore', 'leave-full-screen']) listen(win, event, repair);
    for (const event of DISPLAY_EVENTS) listen(screen, event, repair);
    listen(win, 'close', flush);
    listen(win, 'closed', dispose);
  }
  return { options: initial.options, attach, flush, dispose };
}

module.exports = { resolveWindowState, readWindowState, writeWindowState, createWindowState };
