// The web panel (11 Nov 2026): Ctrl+T, "gastrectomy", read, Ctrl+W, back to Finalist. A web search chosen in the
// spotlight opens here, in a WebContentsView laid over the main window, not a new top-level window, so Hyprland's rules
// never tile it: the page draws the dimmed backdrop, the rounded frame and its slim bar (back, forward, reload, address,
// find, Open in browser, close) and tells us where the frame's body is; the view sits exactly there. Clicking the dim,
// Esc (unless the page is using it), Ctrl+W, or the close button closes it; the page then puts focus back where it was.
//
// Security: the view's webContents is sandboxed, contextIsolation on, nodeIntegration off and NO preload; it lives in
// its own session partition (persist:finalist-web), so no cookie or storage is shared with Finalist or the node.
// Permission requests (camera, mic, notifications, location...) are refused; only fullscreen and copy-to-clipboard
// are allowed. Popups open in the same panel; non-web links (mailto: aside) are refused. Downloads go to ~/Downloads,
// with a note on the page.

const { WebContentsView, session, shell, app } = require('electron')
const path = require('path')
const fs = require('fs')

const SEARCH = 'https://www.google.com/search?q='   // Google (11 Nov 2026, Raahat: its AI overview helps); DuckDuckGo: 'https://duckduckgo.com/?q='
const PARTITION = 'persist:finalist-web'
const RADIUS = 14                              // matches the frame's rounded corners in the page (CSS px)
const ALLOWED = new Set(['fullscreen', 'clipboard-sanitized-write'])

const isWeb = (u) => { try { return ['http:', 'https:'].includes(new URL(u).protocol) } catch { return false } }
/** what the address field was given, as a URL: a URL-ish string is visited, anything else is searched */
function toUrl(input) {
  const s = String(input || '').trim()
  if (!s) return null
  if (/^https?:\/\//i.test(s)) return s
  if (!/\s/.test(s) && /^[\w-]+(\.[\w-]+)+(:\d+)?(\/.*)?$/.test(s)) return 'https://' + s
  return SEARCH + encodeURIComponent(s)
}
function uniquePath(dir, name) {
  const ext = path.extname(name), stem = path.basename(name, ext) || 'download'
  let p = path.join(dir, stem + ext)
  for (let n = 1; fs.existsSync(p); n++) p = path.join(dir, `${stem} (${n})${ext}`)
  return p
}

let sessionReady = false
function webSession(send) {
  const ses = session.fromPartition(PARTITION)
  // a plain Chrome user agent, without Electron's and this app's tokens: Google serves its full results page, AI
  // overview included, to Chrome, and sometimes a reduced one to an unknown embedder
  ses.setUserAgent(ses.getUserAgent().replace(/\s?(Electron|finalist|Finalist|FinalistDesktop)\/[^\s]+/g, ''))
  if (sessionReady) return ses
  sessionReady = true
  ses.setPermissionRequestHandler((_wc, perm, cb) => cb(ALLOWED.has(perm)))
  ses.setPermissionCheckHandler((_wc, perm) => ALLOWED.has(perm))
  ses.on('will-download', (_e, item) => {
    const p = uniquePath(app.getPath('downloads'), item.getFilename())
    item.setSavePath(p)
    send('web_note', `Downloading ${path.basename(p)} to Downloads`)
    item.once('done', (_e2, state) => send('web_note', state === 'completed' ? `Saved ${path.basename(p)} to Downloads` : `The download of ${path.basename(p)} didn't finish`))
  })
  return ses
}

/** the panel for one main window; returns its controls for main.js (the key handler and IPC) */
function webPanel(getMain) {
  let view = null, fullscreen = false, rect = null, lastFind = ''
  const main = () => { const m = getMain(); return m && !m.isDestroyed() ? m : null }
  const send = (ch, v) => main()?.webContents.send(ch, v)
  const state = () => {
    if (!view) return
    const wc = view.webContents, h = wc.navigationHistory
    send('web_state', { url: wc.getURL(), title: wc.getTitle(), back: h.canGoBack(), forward: h.canGoForward(), loading: wc.isLoading() })
  }
  const place = () => {
    const m = main()
    if (!view || !m) return
    if (fullscreen) { const b = m.getContentBounds(); view.setBounds({ x: 0, y: 0, width: b.width, height: b.height }); view.setBorderRadius(0); return }
    if (!rect) return
    const z = m.webContents.getZoomFactor()
    view.setBounds({ x: Math.round(rect.x * z), y: Math.round(rect.y * z), width: Math.round(rect.width * z), height: Math.round(rect.height * z) })
    view.setBorderRadius(Math.round(RADIUS * z))
  }

  function close(tell = true) {
    const m = main()
    if (view) {
      const v = view; view = null; rect = null; fullscreen = false
      if (m) m.contentView.removeChildView(v)
      v.webContents.close()
    }
    if (m && tell) { m.webContents.focus(); send('web_closed') }
  }

  function open(input) {
    const url = toUrl(input), m = main()
    if (!url || !m) return
    if (view) { view.webContents.loadURL(url); view.webContents.focus(); return }
    view = new WebContentsView({ webPreferences: { session: webSession(send), contextIsolation: true, nodeIntegration: false, sandbox: true, webSecurity: true } })
    const v = view, wc = v.webContents
    v.setBackgroundColor('#ffffff')
    // popups: into this panel; nothing ever opens a new window
    wc.setWindowOpenHandler(({ url: u }) => { if (isWeb(u)) wc.loadURL(u); else if (u.startsWith('mailto:')) shell.openExternal(u); return { action: 'deny' } })
    wc.on('will-navigate', (e, u) => { if (!isWeb(u)) { e.preventDefault(); if (u.startsWith('mailto:')) shell.openExternal(u) } })
    for (const ev of ['did-navigate', 'did-navigate-in-page', 'page-title-updated', 'did-start-loading', 'did-stop-loading']) wc.on(ev, state)
    wc.on('found-in-page', (_e, r) => send('web_find', { active: r.activeMatchOrdinal, matches: r.matches }))
    wc.on('enter-html-full-screen', () => { fullscreen = true; place() })
    wc.on('leave-html-full-screen', () => { fullscreen = false; place() })
    wc.on('render-process-gone', () => { if (view === v) close() })
    wc.on('before-input-event', (e, i) => { if (keys(i)) e.preventDefault() })
    wc.loadURL(url)
    state()
    // shown once the page says where the frame's body is (web_bounds)
  }

  /** the panel's own keys; true when taken (the page never sees them) */
  function keys(i) {
    if (i.type !== 'keyDown' || !view) return false
    const ctl = (i.control || i.meta) && !i.alt, k = (i.key || '').toLowerCase(), wc = view.webContents
    if (ctl && k === 'w') { close(); return true }
    if (ctl && k === 't') { close(); send('spotlight'); return true }   // focus goes back first, so the spotlight returns there too
    if (ctl && k === 'l') { main()?.webContents.focus(); send('web_focus', 'address'); return true }
    if (ctl && k === 'f') { main()?.webContents.focus(); send('web_focus', 'find'); return true }
    if ((ctl && k === 'r') || i.key === 'F5') { wc.reload(); return true }
    if (i.alt && !i.control && i.key === 'ArrowLeft') { if (wc.navigationHistory.canGoBack()) wc.navigationHistory.goBack(); return true }
    if (i.alt && !i.control && i.key === 'ArrowRight') { if (wc.navigationHistory.canGoForward()) wc.navigationHistory.goForward(); return true }
    if (i.key === 'Escape' && !i.control && !i.alt && !i.shift && !i.meta && !fullscreen) {
      // Esc belongs to the page while it's typing (and in a frame, which may be); otherwise it closes the panel.
      // The page still gets this Esc either way; a field is left, as everywhere in Finalist, so a second Esc closes.
      const v = view
      v.webContents.executeJavaScriptInIsolatedWorld(999, [{ code: `(() => { const a = document.activeElement; if (!a) return false
        const t = a.tagName, typing = a.isContentEditable || t === 'TEXTAREA' || t === 'SELECT' || t === 'IFRAME' || (t === 'INPUT' && !['checkbox','radio','range','button','submit','reset'].includes(a.type))
        if (typing) a.blur(); return typing })()` }])
        .then((typing) => { if (!typing && view === v) close() })
        .catch(() => { if (view === v) close() })
    }
    return false
  }

  return {
    isOpen: () => !!view,
    close,
    open,
    bounds(r) {
      const first = !rect
      rect = r && r.width > 0 && r.height > 0 ? r : null
      const m = main()
      if (!view || !m || !rect) return
      place()
      if (first) { m.contentView.addChildView(view); view.webContents.focus() }
    },
    act(op, arg) {
      if (!view) return
      const wc = view.webContents, h = wc.navigationHistory
      if (op === 'back' && h.canGoBack()) h.goBack()
      else if (op === 'forward' && h.canGoForward()) h.goForward()
      else if (op === 'reload') wc.reload()
      else if (op === 'stop') wc.stop()
      else if (op === 'go') { const u = toUrl(arg); if (u) wc.loadURL(u); wc.focus() }
      else if (op === 'focus') wc.focus()
      else if (op === 'external') { const u = wc.getURL(); if (isWeb(u)) shell.openExternal(u); close() }
      else if (op === 'find') {
        const { text, back } = arg || {}
        if (!text) { wc.stopFindInPage('clearSelection'); lastFind = ''; send('web_find', { active: 0, matches: 0 }); return }
        wc.findInPage(text, { forward: !back, findNext: text !== lastFind }); lastFind = text
      } else if (op === 'stopfind') { wc.stopFindInPage('keepSelection'); lastFind = ''; wc.focus() }
    },
    refit: place,
  }
}

module.exports = { webPanel, toUrl, SEARCH, PARTITION }
