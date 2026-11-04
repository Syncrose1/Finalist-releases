// Finalist Desktop (4 Nov 2026), on Electron: Chromium scrolls like the browser (a Tauri/WebKitGTK build
// ran at 60 fps with harsh wheel steps on a 120 Hz screen; Raahat: 'night and day', Electron kept). The Finalist site is the window, served through the node (http://127.0.0.1:47620/finalist)
// so the site, the node and its screen share one origin; the node is kept running while the app is open
// and goes with it; timers float over the screen from start until stop or done (pause keeps them), placed
// on Hyprland by the same window rules (titles "Finalist timer N: …").

const { app, BrowserWindow, ipcMain, shell, screen } = require('electron')
const { spawn } = require('child_process')
const path = require('path')
const fs = require('fs')
const os = require('os')

const NODE = 'http://127.0.0.1:47620'
const BASE = process.env.FINALIST_BASE || NODE
const TIMER_W = 320, TIMER_H = 78
const SLOTS = ['qqm', 'passmed', 'anki', 'workout', 'osce', 'akt', 'psa', 'fy1']

// Chromium on Wayland, at the display's own refresh rate
app.commandLine.appendSwitch('ozone-platform-hint', 'auto')
// an AppImage can't use Chromium's setuid sandbox, and Ubuntu 24.04's AppArmor blocks the namespace one, so
// it wouldn't start at all; the window only ever loads Finalist itself through the node
if (process.env.APPIMAGE) app.commandLine.appendSwitch('no-sandbox')
app.commandLine.appendSwitch('enable-features', 'WaylandWindowDecorations')
app.setName('Finalist')

let main = null, node = null, timers = {}, subject = ''
const timerWins = new Map()

async function get(url, ms = 1500) {
  const c = new AbortController(); const t = setTimeout(() => c.abort(), ms)
  try { const r = await fetch(url, { signal: c.signal }); return r.ok ? await r.json() : null } catch { return null } finally { clearTimeout(t) }
}
const nodeUp = async () => !!(await get(`${NODE}/v1/hello`))

function startNode() {
  const env = { ...process.env, FINALIST_PARENT_PID: String(process.pid) }
  const opts = { env, stdio: 'ignore', detached: false, windowsHide: true }
  // packaged: the node built beside the app (resources/node, PyInstaller onedir); from source: ~/Finalist/node
  const bin = process.platform === 'win32' ? 'finalist-node.exe' : 'finalist-node'
  for (const p of [path.join(process.resourcesPath || '', 'node', bin), path.join(path.dirname(process.execPath), bin), path.join(process.resourcesPath || '', bin)]) {
    if (p && fs.existsSync(p)) return spawn(p, ['--no-window'], opts)
  }
  const src = process.env.FINALIST_NODE_DIR || path.join(os.homedir(), 'Finalist/node')
  if (fs.existsSync(path.join(src, 'finalist_node'))) return spawn(process.platform === 'win32' ? 'python' : 'python3', ['-u', '-m', 'finalist_node', '--no-window'], { ...opts, cwd: src })
  return null
}

function createMain() {
  const startPath = process.env.FINALIST_PATH || (process.argv.includes('--revise') ? '/finalist/revise' : '/finalist')
  main = new BrowserWindow({
    width: 1320, height: 880, minWidth: 340, minHeight: 420, title: 'Finalist', backgroundColor: '#f2ecdd',
    autoHideMenuBar: true, show: true, icon: path.join(__dirname, 'icon.png'),
    webPreferences: { contextIsolation: true, sandbox: true, preload: path.join(__dirname, 'main-preload.js') },
  })
  main.setMenuBarVisibility(false)
  // the site's user agent says it's the desktop app (the sidebar's Finalist Node shows for it)
  main.webContents.setUserAgent(main.webContents.getUserAgent() + ' FinalistDesktop/0.1')
  main.loadFile(path.join(__dirname, 'splash.html'))
  // the site and the node stay here; anything else opens in the browser
  const here = (u) => { try { const x = new URL(u); return x.protocol === 'file:' || (x.hostname === '127.0.0.1' && x.port === '47620') || (process.env.FINALIST_BASE && x.hostname === 'localhost') } catch { return false } }
  main.webContents.setWindowOpenHandler(({ url }) => { if (!here(url)) shell.openExternal(url); return { action: here(url) ? 'allow' : 'deny' } })
  main.webContents.on('will-navigate', (e, url) => { if (!here(url)) { e.preventDefault(); shell.openExternal(url) } })
  // keep a node running while the app is open; show Finalist once it answers
  let shown = false
  const watch = async () => {
    const up = await nodeUp()
    if (!up && (!node || node.exitCode !== null || node.signalCode !== null)) node = startNode()      // a node killed outright has no exit code, only a signal
    if (up && !shown && main && !main.isDestroyed()) { shown = true; main.loadURL(`${BASE}${startPath}`) }
    setTimeout(watch, shown ? 3000 : 400)
  }
  watch()
  main.webContents.on('did-finish-load', tellPage)
  // testing: run a snippet in the page once it's up (FINALIST_PROBE_JS)
  if (process.env.FINALIST_PROBE_JS) main.webContents.on('did-finish-load', () => { if (main.webContents.getURL().startsWith('http')) setTimeout(() => main.webContents.executeJavaScript(process.env.FINALIST_PROBE_JS).catch(() => {}), 4000) })
  main.on('closed', () => { main = null })
}

// ---- floating timers ----------------------------------------------------------------------------------
// If a timer's window dies (closed under us, its renderer crashed, or it never showed, as when the shell is
// restarted), it's brought back. Three failures inside a minute and the overlay gives up: the page is told,
// and draws the full timer itself, as on the website; after a quiet minute it tries again (4 Nov 2026).
let overlayOk = true
const fails = []
function tellPage() {
  if (!main || main.isDestroyed()) return
  const js = `window.__finalistOverlay = ${overlayOk && Object.values(timers).some((v) => v.shown)}; window.dispatchEvent(new CustomEvent('finalist-overlay', { detail: window.__finalistOverlay }))`
  main.webContents.executeJavaScript(js).catch(() => {})
}
function overlayFailed(b, why) {
  const now = Date.now()
  fails.push(now); while (fails.length && now - fails[0] > 60_000) fails.shift()
  console.error(`[timer] ${b}: ${why} (${fails.length} in the last minute)`)
  if (fails.length >= 3 && overlayOk) { overlayOk = false; for (const [k, w] of timerWins) { w.__closing = true; if (!w.isDestroyed()) w.close(); timerWins.delete(k) } }
  tellPage()
}
function syncTimers() {
  if (!overlayOk) {
    if (!fails.length || Date.now() - fails[fails.length - 1] > 60_000) { overlayOk = true; fails.length = 0 } else return
  }
  const want = Object.entries(timers).filter(([, v]) => v.shown).map(([k]) => k)
  // a timer leaving fades out first, then hides, then closes (no frame of a dark box mid-animation)
  for (const [b, w] of timerWins) if (!want.includes(b)) {
    timerWins.delete(b); w.__closing = true
    if (!w.isDestroyed()) w.webContents.send('bye')
    setTimeout(() => { if (!w.isDestroyed()) { w.hide(); w.close() } }, 240)
  }
  for (const b of want) {
    if (timerWins.has(b)) continue
    const slot = Math.max(0, SLOTS.indexOf(b)), { width } = screen.getPrimaryDisplay().workAreaSize
    const w = new BrowserWindow({
      width: TIMER_W, height: TIMER_H, x: width - TIMER_W - 24, y: 64 + slot * (TIMER_H + 12),
      frame: false, transparent: true, resizable: false, alwaysOnTop: true, skipTaskbar: true, focusable: true,
      show: false, hasShadow: false, title: `Finalist timer ${slot}: ${timers[b].name}`,
      webPreferences: { preload: path.join(__dirname, 'timer-preload.js'), contextIsolation: true, sandbox: false },
    })
    w.setVisibleOnAllWorkspaces(true)
    w.on('page-title-updated', (e) => e.preventDefault())     // keep the title the window rules match
    w.on('close', (e) => { if (!w.__closing) e.preventDefault() })   // never closed by hand
    w.on('closed', () => { if (!w.__closing && timerWins.get(b) === w) { timerWins.delete(b); overlayFailed(b, 'its window went') } })
    w.webContents.on('render-process-gone', (_e, d) => { if (!w.__closing) { w.__closing = true; if (!w.isDestroyed()) w.destroy(); timerWins.delete(b); overlayFailed(b, `it crashed (${d.reason})`) } })
    if (process.env.FINALIST_DEBUG) w.webContents.on('console-message', (e) => console.error('[timer]', e.message, e.lineNumber))
    w.loadFile(path.join(__dirname, 'timer.html'), { query: { b } })
    let shown = false
    w.once('ready-to-show', () => { shown = true; w.showInactive(); tellPage() })
    setTimeout(() => { if (!shown && !w.isDestroyed() && timerWins.get(b) === w) { w.__closing = true; w.destroy(); timerWins.delete(b); overlayFailed(b, 'it never showed') } }, 8000)
    timerWins.set(b, w)
  }
}
// the page's timer, as the page reports it (any account): one timer, shown while it's timing an activity
let got = 0, emptySince = 0
ipcMain.on('page_timer', (_e, st) => {
  got = Date.now()
  const l = st && st.locked
  if (l) {
    emptySince = 0
    timers = { [l.activity]: { ms: l.ms, running: l.running, shown: true, name: l.name, page: l.page, plan_min: l.plan_min || null } }
    subject = ''
  } else {
    // a page loading reports nothing for a moment: only take the timer away if it stays gone
    if (!emptySince) emptySince = Date.now()
    if (Date.now() - emptySince < 2500) { setTimeout(() => { if (emptySince && Date.now() - emptySince >= 2500) { timers = {}; syncTimers(); tellPage() } }, 2600); return }
    timers = {}
  }
  syncTimers(); tellPage()
})
ipcMain.handle('timer_state', (_e, block) => {
  const t = timers[block]
  return { timer: t ? { ...t, ms: t.ms + (t.running ? Date.now() - got : 0) } : null, subject }
})
ipcMain.handle('timer_act', async (_e, block, op) => {
  // the overlay's buttons: carried out by the page's timer (pause, resume, stop)
  if (main && !main.isDestroyed()) main.webContents.send('timer_op', op === 'start' ? 'resume' : op === 'stop' ? 'unlock' : op)
  await new Promise((r) => setTimeout(r, 350))
  const t = timers[block]
  return { timer: t ? { ...t, ms: t.ms + (t.running ? Date.now() - got : 0) } : null, conflict: null }
})
// clicking a timer takes you back to the page it was started from
ipcMain.handle('timer_open', (_e, block) => {
  const page = timers[block]?.page
  if (!main || main.isDestroyed()) createMain()
  if (page && main) main.loadURL(`${BASE}${page}`)
  if (main) { main.show(); main.focus() }
})

// one Finalist at a time: opening it again (or Super+Y's --revise) brings the open one forward
const solo = app.requestSingleInstanceLock()
if (!solo) app.quit()
app.on('second-instance', (_e, argv) => {
  if (!main || main.isDestroyed()) return createMain()
  if (argv.includes('--revise')) main.loadURL(`${BASE}/finalist/revise`)
  if (main.isMinimized()) main.restore()
  main.show(); main.focus()
})
app.whenReady().then(() => { if (solo) createMain() })
app.on('window-all-closed', () => app.quit())
app.on('before-quit', () => { for (const w of timerWins.values()) w.__closing = true; if (node && node.exitCode === null) node.kill() })
