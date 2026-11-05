# Finalist Desktop

**Finalist Desktop** is [Finalist](https://raahat.sh/finalist) as an app on your computer, with a small helper inside it
we call a node. The node runs **Patient and Examiner Claude** for the OSCE stations through your own
[Claude Code](https://claude.com/claude-code) (so stations cost nothing beyond your Claude plan), the voice, and the
image and ECG datasets. Timers float over your screen while you work.

Leave it open, and your other devices (a hospital PC, a borrowed laptop, your phone) can use all of it too: open
Finalist there and sign in. They use this computer's power over the internet, with nothing to install on them.

## Get it

Paste one line into a terminal. It fetches the latest release for your computer, installs it, gets it past the
"unsigned app" warning and opens it. Run the same line again any time to **update** or **uninstall** (keeping your
data, or removing everything): it asks with a little menu.

- **Mac or Linux** (Terminal):
  ```
  curl -fsSL https://raw.githubusercontent.com/Syncrose1/Finalist-releases/main/desktop/install/install.sh | bash
  ```
- **Windows** (PowerShell):
  ```
  irm https://raw.githubusercontent.com/Syncrose1/Finalist-releases/main/desktop/install/install.ps1 | iex
  ```

No password or administrator needed. To skip the menu, say what to do: `... | bash -s -- install`, `uninstall`
or `purge` on a Mac or Linux; on Windows, `& ([scriptblock]::Create((irm <the address above>))) uninstall`.

### Or download it yourself

From [Releases](../../releases/latest), download the one for your computer:

| Computer | Download | Then |
|---|---|---|
| Windows | `Finalist-windows-x64-setup.exe` | double-click it: it installs for you only and opens |
| Mac with Apple silicon (M1 or later) | `Finalist-macos-arm64.dmg` | open it and drag **Finalist** into Applications |
| Older Intel Mac | `Finalist-macos-intel.dmg` | the same |
| Linux | `Finalist-linux-x64.AppImage` | mark it as executable (right-click → Properties → Permissions → allow it to run as a program), then double-click it |

Open **Finalist** and sign in. That's all: this computer now works for your account, here and from anywhere.

For Claude stations, install **Claude Code** on this computer once and sign in (run `claude`):

- macOS / Linux: `curl -fsSL https://claude.ai/install.sh | bash`
- Windows (PowerShell): `irm https://claude.ai/install.ps1 | iex`

### The first time on a Mac or Windows

These builds aren't signed, so your computer asks once whether to trust them:

- **Windows**: "Windows protected your PC" → **More info** → **Run anyway**.
- **macOS**: the first open says it can't be opened. Go to System Settings → Privacy & Security and press
  **Open Anyway** (once). Or in Terminal: `xattr -dr com.apple.quarantine /Applications/Finalist.app`.
- **Linux**: mark the AppImage as executable first (above). In a terminal that's
  `chmod +x Finalist-linux-x64.AppImage`.

## Using it from elsewhere

Keep Finalist open on this computer (it can sit minimised). Your other devices find it by themselves: sign in to
Finalist there. Close the app, or let the computer sleep, and they lose it until it's back.

How: the node gets its own secure web address through a Cloudflare tunnel (free, no account), and Finalist keeps
that address on your account so your devices, and a friend you share it with, can look it up. The address alone
opens nothing: every request still needs a Finalist sign-in.

**Share it with a friend** from the node's screen in the app: two hours of Claude stations for their Finalist
account, with their own runs. They just sign in to Finalist with that email.

## Voice

Spoken stations need a local voice, set up from the node's screen; until then stations are typed.

## Running from source

The node: Python 3.10 or newer, no packages needed: `cd node && python3 -m finalist_node`.
The app: `cd desktop && npm install && npm start`.

## What it can see

Every request needs a Finalist sign-in, which the node checks with Finalist's sign-in service. Your runs and reviews stay on
your computer and in your own Finalist account. The code is all here.
