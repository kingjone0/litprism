// 渲染页与外壳之间唯一的桥：只暴露"告诉主进程当前是浅色还是深色"。
// 页面本身仍然加载自本地 sidecar，不需要任何 Node 能力。
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('litShell', {
  platform: process.platform,
  setTitleBarTheme: theme => ipcRenderer.send('ui:titlebar-theme', theme === 'dark' ? 'dark' : 'light'),
});
