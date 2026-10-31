# Finalist Node

The Finalist Node runs **Patient and Examiner Claude** for [Finalist](https://raahat.sh/finalist)'s OSCE stations on your own
computer, through your own [Claude Code](https://claude.com/claude-code) (so the stations cost nothing beyond your Claude plan).
Finalist's cards, clock and recall drills work without it; the node adds the stations you run with Claude.

## Get it

1. **Claude Code**, signed in. Install it once, then run `claude` and sign in:
   - macOS / Linux: `curl -fsSL https://claude.ai/install.sh | bash`
   - Windows (PowerShell): `irm https://claude.ai/install.ps1 | iex`
2. **The node**:
   - **Linux or Mac:** in a terminal, run
     ```
     curl -fsSL https://raw.githubusercontent.com/Syncrose1/Finalist-releases/main/install.sh | sh
     ```
     It downloads the right build, adds **Finalist Node** to your apps (Linux) or Desktop (Mac), and starts it.
     Run it again to update.
   - **Windows:** from [Releases](../../releases/latest), download `finalist-node-windows-x64.exe`. It's one file:
     keep it wherever you like and double-click it.
   - Or download any build from [Releases](../../releases/latest) yourself (on Linux and Mac, `chmod +x` it first:
     a downloaded file isn't allowed to run until it's marked).

## Start it

Open it. A small window shows its log, and the node's own screen opens in your browser. The first time, that screen shows
a pairing code: in Finalist, signed in, go to **Settings → Practise with Claude** and enter it. The node is then yours.

It runs while that window is open. To stop it, use **Stop the node** on its screen, close the window, or press Ctrl+C.
It never installs itself as a background service.

### The first time on a Mac or Windows

These builds aren't signed, so your computer asks once whether to trust them:

- **Windows**: "Windows protected your PC" → **More info** → **Run anyway**.
- **macOS**: double-clicking says it can't be opened. Instead, in Terminal:
  ```
  chmod +x ~/Downloads/finalist-node-macos-*
  xattr -d com.apple.quarantine ~/Downloads/finalist-node-macos-*
  ~/Downloads/finalist-node-macos-arm64     # or -intel
  ```
  (Or: System Settings → Privacy & Security → **Open Anyway**, after the first try.)
- **Linux**: the installer above does it all; by hand, `chmod +x finalist-node-linux-x64 && ./finalist-node-linux-x64`

## From your other devices

The node answers only this computer's browser unless you use [Tailscale](https://tailscale.com) (free for personal use). With
Tailscale on both devices, run this once on the node's computer:

```
tailscale serve --bg --https=8443 http://127.0.0.1:47620
```

Then, in Finalist on your other device, open **Node elsewhere?** and enter the address the node's screen shows (it ends in
`.ts.net:8443`). The node's screen also has **Reach it from anywhere** (for a hospital PC) and **Share it with a friend**.

## Voice

Spoken stations need a local voice, which is only set up on some machines for now; elsewhere stations are typed.

## Running from source

Python 3.10 or newer, no packages needed: `cd node && python3 -m finalist_node`. `node/install.sh` (Linux, macOS) and
`node/install.ps1` (Windows) set up a launcher.

## What it can see

Every request needs a Finalist sign-in, which the node checks with Finalist's sign-in service. Your runs and reviews stay on
your computer and in your own Finalist account. The code is all here.
