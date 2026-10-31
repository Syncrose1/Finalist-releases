#!/usr/bin/env bash
# Installs the Finalist Node on Linux or macOS (plan phase 4, 31 Oct 2026).
#
#   ./install.sh            asks before anything it changes
#   ./install.sh --yes      takes every default (for scripts)
#
# The node is plain Python (3.10+, standard library only) plus Claude Code, so it runs anywhere those
# do. Typed stations need nothing more. The voice is set up from the node's own screen (it downloads
# audio.cpp and its models); from source it also needs a Python with numpy, websockets and onnxruntime,
# which step 6 offers to make.
set -euo pipefail
NODE="$(cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" && pwd)"
YES=0; [[ "${1:-}" == "--yes" ]] && YES=1
OS="$(uname -s)"
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
no() { printf '  \033[33m•\033[0m %s\n' "$*"; }
ask() { [[ $YES == 1 ]] && return 0; read -r -p "  $1 [Y/n] " a; [[ -z "$a" || "$a" =~ ^[Yy] ]]; }

say "Finalist Node: installing from $NODE"

say "1. Python"
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then PY="$(command -v "$c")"; break; fi
done
if [[ -z "$PY" ]]; then
  no "Python 3.10 or newer isn't installed."
  [[ "$OS" == Darwin ]] && no "Install it from https://www.python.org/downloads/ (or: brew install python), then run this again." \
                        || no "Install it with your package manager (CachyOS/Arch: sudo pacman -S python), then run this again."
  exit 1
fi
ok "$("$PY" --version) at $PY"

say "2. Claude Code (Patient and Examiner Claude run on it, on your own subscription)"
if ! command -v claude >/dev/null; then
  no "Claude Code isn't installed."
  if ask "Install it now with Anthropic's installer (curl -fsSL https://claude.ai/install.sh | bash)?"; then
    curl -fsSL https://claude.ai/install.sh | bash
    export PATH="$HOME/.local/bin:$PATH"
  fi
fi
if command -v claude >/dev/null; then
  ok "$(claude --version 2>/dev/null | head -1)"
  no "If you've never used it on this machine, run  claude  once in a terminal and sign in."
else
  no "Without Claude Code the node serves the drill and the clock only. Install it, then run this again."
fi

say "3. Settings"
mkdir -p "$HOME/.config/finalist-node" "$HOME/.local/share/finalist-node"
ok "settings in ~/.config/finalist-node, your runs in ~/.local/share/finalist-node"
no "The first time it starts, the node prints a pairing code: enter it in Finalist's settings, signed in."

say "4. A way to start it"
chmod +x "$NODE/run.sh"
if [[ "$OS" == Darwin ]]; then
  L="$HOME/Desktop/Finalist Node.command"
  printf '#!/bin/bash\nexec "%s/run.sh"\n' "$NODE" > "$L"; chmod +x "$L"
  ok "Desktop: Finalist Node.command (double-click it; Ctrl+C in its window stops it)"
else
  mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications"
  ln -sf "$NODE/run.sh" "$HOME/.local/bin/finalist-node"
  cat > "$HOME/.local/share/applications/finalist-node.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=Finalist Node
Comment=Patient and Examiner Claude for Finalist, on this PC
Exec=$NODE/run.sh
Terminal=true
Categories=Education;
DESK
  ok "finalist-node in a terminal, or Finalist Node in your app menu (it opens a terminal; Ctrl+C stops it)"
  if [[ -d "$HOME/Documents/AI_Models/Launch_Scripts" ]]; then
    ln -sf "$NODE/run.sh" "$HOME/Documents/AI_Models/Launch_Scripts/Finalist Node.sh"; ok "and in Launch_Scripts"
  fi
fi

say "5. Reaching it from your other devices (optional)"
if command -v tailscale >/dev/null && tailscale status >/dev/null 2>&1; then
  if ask "Let your own devices on Tailscale reach this node (tailnet only, not the internet)?"; then
    if tailscale serve --bg --https=8443 http://127.0.0.1:47620 >/dev/null 2>&1; then
      DNS="$(tailscale status --json 2>/dev/null | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
      ok "at https://${DNS:-<this machine>.ts.net}:8443  (Finalist settings → Finalist Node → Node elsewhere?)"
    else
      no "Tailscale wouldn't let this user change it. Once: sudo tailscale set --operator=\$USER, then run this again."
    fi
  fi
else
  no "Tailscale isn't running here: this node is for this machine's browser (that's fine)."
fi

say "6. The voice"
VENV="$HOME/.local/share/finalist-node/voice-python"
if [[ -x "$VENV/bin/python" ]] && "$VENV/bin/python" -c "import numpy, websockets, onnxruntime" 2>/dev/null; then
  ok "the voice's Python is ready ($VENV)"
elif ask "Make a small Python environment for the voice (numpy, websockets, onnxruntime; ~60 MB)?"; then
  "$PY" -m venv "$VENV" && "$VENV/bin/pip" -q install numpy websockets onnxruntime && ok "made $VENV" || no "couldn't make it: stations stay typed"
else
  no "skipped: stations are typed until the voice has its Python"
fi
if [[ -x "$VENV/bin/python" ]]; then
  "$PY" - "$VENV/bin/python" <<'PYCFG'
import json, sys
from pathlib import Path
f = Path.home() / ".config/finalist-node/config.json"
d = json.loads(f.read_text()) if f.exists() else {}
d["voice_python"] = sys.argv[1]
f.parent.mkdir(parents=True, exist_ok=True)
f.write_text(json.dumps(d, indent=1))
PYCFG
fi
ok "then, on the node's screen, Set up the voice: it downloads the voice engine and models, and picks Ming
     on a graphics card that can run it, Pocket otherwise"

say "Done. Start it:"
if [[ "$OS" == Darwin ]]; then echo "  double-click Finalist Node.command on the Desktop"; else echo "  finalist-node"; fi
echo "  then open https://raahat.sh/finalist/account, sign in, and pair it with the code it prints."
