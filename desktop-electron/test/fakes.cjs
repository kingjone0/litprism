'use strict';
const { EventEmitter } = require('node:events');

class FakeWindow extends EventEmitter {
  constructor(options) {
    super(); this.options = options;
    this.bounds = { x: options.x ?? 0, y: options.y ?? 0, width: options.width, height: options.height };
    this.maximized = false; this.minimized = false; this.fullscreen = false; this.destroyed = false;
    this.calls = [];
    this.webContents = { setWindowOpenHandler() {} };
  }
  isDestroyed() { return this.destroyed; }
  isMaximized() { return this.maximized; }
  isMinimized() { return this.minimized; }
  isFullScreen() { return this.fullscreen; }
  getNormalBounds() { return { ...this.bounds }; }
  setMinimumSize(width, height) { this.minimum = [width, height]; }
  setBounds(bounds) { this.bounds = { ...bounds }; this.calls.push('setBounds'); this.emit('resize'); this.emit('move'); }
  maximize() { this.maximized = true; this.calls.push('maximize'); this.emit('maximize'); this.emit('resize'); }
  unmaximize() { this.maximized = false; this.calls.push('unmaximize'); this.emit('unmaximize'); this.emit('resize'); }
  restore() { this.minimized = false; this.emit('restore'); }
  show() { this.calls.push('show'); }
  focus() { this.calls.push('focus'); }
  setTitleBarOverlay() {}
  setBackgroundColor() {}
  async loadURL(url) { this.url = url; this.emit('ready-to-show'); }
}

class FakeScreen extends EventEmitter {
  constructor(displays, primaryId = displays[0].id) { super(); this.displays = displays; this.primaryId = primaryId; }
  getAllDisplays() { return this.displays; }
  getPrimaryDisplay() { return this.displays.find(d => d.id === this.primaryId) || this.displays[0]; }
}

function display(id = 1, x = 0, y = 0, width = 1920, height = 1040) {
  return { id, workArea: { x, y, width, height }, scaleFactor: 1 };
}

module.exports = { FakeWindow, FakeScreen, display };
