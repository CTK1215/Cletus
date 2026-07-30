const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('cletus', {
  version: '0.3.0',
  close: () => ipcRenderer.send('cletus:close')
});
