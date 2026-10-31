# Finalist Node

The Finalist Node runs **Patient and Examiner Claude** for [Finalist](https://raahat.sh/finalist)'s OSCE stations on your own
computer, through your own [Claude Code](https://claude.com/claude-code) (so the stations cost nothing beyond your Claude plan).
Finalist's cards, clock and recall drills work without it; the node adds the stations you run with Claude.

## Get it

1. **Claude Code**, signed in. Install it once, then run `claude` and sign in:
   - macOS / Linux: `curl -fsSL https://claude.ai/install.sh | bash`
   - Windows (PowerShell): `irm https://claude.ai/install.ps1 | iex`
2. **The node**: from [Releases](../../releases/latest), download the one for your computer. Nothing is installed:
   it's one file you keep wherever you like (a USB stick works too).

   | Computer | Download | Then |
   |---|---|---|
   | Windows | `finalist-node-windows-x64.exe` | double-click it |
   | Mac with Apple silicon (M1 or later) | `finalist-node-macos-arm64.zip` | double-click to extract, then double-click **Finalist Node** |
   | Older Intel Mac | `finalist-node-macos-intel.zip` | the same |
   | Linux | `finalist-node-linux-x64.tar.gz` | extract it, then double-click **Finalist Node** |

   (The Linux and Mac builds come in an archive because a downloaded file isn't allowed to run until it's marked
   executable; the archive keeps that mark.)

## Start it

Open **Finalist Node**. Its own screen opens in your browser (on Windows and Mac a small window also shows its log). The first time, that screen shows
a pairing code: in Finalist, signed in, go to **Settings → Practise with Claude** and enter it. The node is then yours.

To stop it, press **Stop the node** on its screen (or close its window, where there is one). Closing the browser tab
doesn't stop it.
It never installs itself as a background service.

### The first time on a Mac or Windows

These builds aren't signed, so your computer asks once whether to trust them:

- **Windows**: "Windows protected your PC" → **More info** → **Run anyway**.
- **macOS**: the first double-click says it can't be opened. Go to System Settings → Privacy & Security and press
  **Open Anyway** (once). Or in Terminal: `xattr -d com.apple.quarantine ~/Downloads/"Finalist Node"`.
- **Linux**: nothing to confirm. If your file manager opens it as text instead of running it, allow it to run
  executables (or run `./"Finalist Node"` in a terminal from its folder).

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
