// The main window's bridge (4 Nov 2026): the page tells the app what the one timer is doing, so the floating
// timer works for everyone (it used to read the Revision System, which is one person's); the overlay's
// buttons come back here as operations the page's timer carries out.
const { contextBridge, ipcRenderer } = require('electron')
contextBridge.exposeInMainWorld('finalistDesktop', {
  timer: (state) => ipcRenderer.send('page_timer', state),
  onTimerOp: (fn) => ipcRenderer.on('timer_op', (_e, op) => fn(op)),
  // the spotlight (Ctrl+T, taken by the app) and the web panel (web-panel.js)
  onSpotlight: (fn) => { const f = () => fn(); ipcRenderer.on('spotlight', f); return () => ipcRenderer.removeListener('spotlight', f) },
  web: {
    open: (query) => ipcRenderer.send('web_open', String(query)),
    bounds: (r) => ipcRenderer.send('web_bounds', { x: r.x, y: r.y, width: r.width, height: r.height }),
    close: () => ipcRenderer.send('web_close'),
    act: (op, arg) => ipcRenderer.send('web_act', String(op), arg),
    on: (fn) => {
      const chs = ['web_state', 'web_closed', 'web_note', 'web_focus', 'web_find'], fs = chs.map((ch) => (_e, v) => fn(ch, v))
      chs.forEach((ch, n) => ipcRenderer.on(ch, fs[n]))
      return () => chs.forEach((ch, n) => ipcRenderer.removeListener(ch, fs[n]))
    },
  },
})
