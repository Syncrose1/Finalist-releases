#!/bin/sh
# Installs the Finalist Node on Linux or a Mac, then starts it:
#   curl -fsSL https://raw.githubusercontent.com/Syncrose1/Finalist-releases/main/install.sh | sh
# A downloaded file isn't allowed to run until it's marked executable, which nobody should have to know
# (31 Oct 2026: double-clicking the Linux download did nothing). This fetches the latest build for this
# computer into ~/.local/bin, marks it, adds "Finalist Node" to the app menu (Linux) or the Desktop
# (Mac), and starts it: its screen opens in the browser. Run it again to update.
set -eu
REPO="https://github.com/Syncrose1/Finalist-releases/releases/latest/download"
case "$(uname -s)-$(uname -m)" in
  Linux-x86_64)          ASSET=finalist-node-linux-x64 ;;
  Darwin-arm64)          ASSET=finalist-node-macos-arm64 ;;
  Darwin-x86_64)         ASSET=finalist-node-macos-intel ;;
  *) echo "There's no Finalist Node build for $(uname -s) $(uname -m) yet. It also runs from source: see the README."; exit 1 ;;
esac
BIN="$HOME/.local/bin"; EXE="$BIN/finalist-node"
mkdir -p "$BIN"
echo "Downloading the Finalist Node ($ASSET)..."
curl -fL --progress-bar -o "$EXE.part" "$REPO/$ASSET"
mv "$EXE.part" "$EXE"
chmod +x "$EXE"
[ "$(uname -s)" = Darwin ] && xattr -d com.apple.quarantine "$EXE" 2>/dev/null || true

if [ "$(uname -s)" = Linux ]; then
  ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"; APPS="$HOME/.local/share/applications"
  mkdir -p "$ICONS" "$APPS"
  cat > "$ICONS/finalist-node.svg" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#1c1812"/><text x="32" y="45" text-anchor="middle" font-family="Georgia,serif" font-size="36" fill="#f2ecdd">F</text></svg>
SVG
  cat > "$APPS/finalist-node.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Finalist Node
Comment=Patient and Examiner Claude for Finalist, on this computer
Exec=$EXE
Icon=finalist-node
Terminal=false
Categories=Education;
EOF
  command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
  echo "Installed: \"Finalist Node\" is in your apps."
else
  cat > "$HOME/Desktop/Finalist Node.command" <<EOF
#!/bin/sh
exec "$EXE"
EOF
  chmod +x "$HOME/Desktop/Finalist Node.command"
  echo "Installed: \"Finalist Node\" is on your Desktop."
fi

if curl -s -o /dev/null http://127.0.0.1:47620/v1/hello; then
  echo "A Finalist Node is already running: stop it from its screen, then start the new one from your apps."
else
  echo "Starting it now: its screen opens in your browser."
  nohup "$EXE" >/dev/null 2>&1 &
fi
