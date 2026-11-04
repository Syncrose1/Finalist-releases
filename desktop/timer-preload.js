// the timer window's bridge to the app
const { contextBridge, ipcRenderer } = require('electron')
contextBridge.exposeInMainWorld('finalist', {
  state: (block) => ipcRenderer.invoke('timer_state', block),
  act: (block, op, replace) => ipcRenderer.invoke('timer_act', block, op, replace),
  open: (block) => ipcRenderer.invoke('timer_open', block),
  onBye: (fn) => ipcRenderer.on('bye', fn),
})
