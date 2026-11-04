// The main window's bridge (4 Nov 2026): the page tells the app what the one timer is doing, so the floating
// timer works for everyone (it used to read the Revision System, which is one person's); the overlay's
// buttons come back here as operations the page's timer carries out.
const { contextBridge, ipcRenderer } = require('electron')
contextBridge.exposeInMainWorld('finalistDesktop', {
  timer: (state) => ipcRenderer.send('page_timer', state),
  onTimerOp: (fn) => ipcRenderer.on('timer_op', (_e, op) => fn(op)),
})
