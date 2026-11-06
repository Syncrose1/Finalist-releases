#!/bin/bash
# Finalist Desktop's installer (5 Nov 2026), on a Mac or Linux. Pulls the latest release itself: nothing to download
# first. Run it and choose with the arrow keys:
#   curl -fsSL https://raw.githubusercontent.com/Syncrose1/Finalist-releases/main/desktop/install/install.sh | bash
# Or say what to do (also how it runs with no terminal to ask in):
#   ... | bash -s -- install        install or update
#   ... | bash -s -- uninstall      remove the app, keep your data (datasets, runs, voice, sign-in)
#   ... | bash -s -- purge          remove the app and all its data
# Installing puts the app where apps go, clears the "unsigned app" block, and opens it. No password needed.
# FINALIST_NO_OPEN=1 installs without opening; FINALIST_PREFIX=<dir> installs somewhere else (for testing).
set -euo pipefail

REPO="Syncrose1/Finalist-releases"
REL="https://github.com/$REPO/releases/latest/download"
RAW="https://raw.githubusercontent.com/$REPO/main"
SELF="curl -fsSL $RAW/desktop/install/install.sh | bash"
OS="$(uname -s)"
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
B=$'\033[1m'; D=$'\033[2m'; G=$'\033[38;5;35m'; Y=$'\033[38;5;214m'; R=$'\033[0m'
say() { printf '%s%s%s\n' "$B" "$*" "$R"; }
tmp="$(mktemp -d)"; mnt=""
cleanup() { { printf '\033[?25h' >/dev/tty; } 2>/dev/null || true; if [ -n "$mnt" ]; then hdiutil detach "$mnt" -quiet 2>/dev/null || true; fi; rm -rf "$tmp"; }
trap cleanup EXIT
trap 'exit 130' INT

# where things live: the app, and the data (the node's: node/finalist_node/config.py, the same on every system)
mac_dirs() { printf '%s\n' "${FINALIST_PREFIX:-/Applications}" "$HOME/Applications"; }
linux_dir() { printf '%s\n' "${FINALIST_PREFIX:-$HOME/.local/opt/finalist}"; }
data_dirs() {
  printf '%s\n' "$HOME/.config/finalist-node" "$HOME/.local/share/finalist-node"
  if [ "$OS" = Darwin ]; then printf '%s\n' "$HOME/Library/Application Support/Finalist"; else printf '%s\n' "${XDG_CONFIG_HOME:-$HOME/.config}/Finalist"; fi
}
installed_version() {
  if [ "$OS" = Darwin ]; then
    while IFS= read -r d; do [ -d "$d/Finalist.app" ] && { defaults read "$d/Finalist.app/Contents/Info" CFBundleShortVersionString 2>/dev/null || echo "?"; return; }; done < <(mac_dirs)
  else
    d="$(linux_dir)"; [ -e "$d/Finalist.AppImage" ] && { cat "$d/VERSION" 2>/dev/null || echo "?"; return; }
  fi
  return 0
}
latest_version() { curl -fsSL --max-time 5 "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null | sed -n 's/.*"tag_name": *"v\{0,1\}\([^"]*\)".*/\1/p' | head -n 1; }

do_uninstall() {   # $1 = purge to remove the data too
  local found=""
  if [ "$OS" = Darwin ]; then
    osascript -e 'tell application "Finalist" to quit' >/dev/null 2>&1 || true
    while IFS= read -r d; do [ -d "$d/Finalist.app" ] && { rm -rf "$d/Finalist.app"; say "Removed $d/Finalist.app"; found=1; }; done < <(mac_dirs)
  else
    d="$(linux_dir)"
    [ -e "$d/Finalist.AppImage" ] && { rm -rf "$d"; say "Removed $d"; found=1; }
    [ -e "$apps/finalist.desktop" ] && { rm -f "$apps/finalist.desktop"; say "Removed it from the app launcher"; }
  fi
  [ -n "$found" ] || say "Finalist Desktop wasn't installed here, so there was no app to remove."
  if [ "${1:-}" = purge ]; then
    while IFS= read -r p; do [ -e "$p" ] && { rm -rf "$p"; say "Removed $p"; }; done < <(data_dirs)
    say "Finalist Desktop and all its data are gone."
  else
    echo "Your data is kept (datasets, runs, voice, sign-in), ready if you install again."
  fi
}

do_install() {
  local app dest asset
  case "$OS" in
    Darwin)
      # Apple silicon, even from a Terminal running under Rosetta (which reports x86_64)
      if [ "$(sysctl -in hw.optional.arm64 2>/dev/null || echo 0)" = 1 ]; then asset=Finalist-macos-arm64.dmg; else asset=Finalist-macos-intel.dmg; fi
      say "Downloading Finalist for this Mac ($asset)"
      curl -fL --progress-bar -o "$tmp/Finalist.dmg" "$REL/$asset"
      mnt="$(hdiutil attach -nobrowse -readonly -noautoopen "$tmp/Finalist.dmg" | awk -F'\t' '/\/Volumes\// {print $NF}' | tail -n 1)"
      [ -d "$mnt/Finalist.app" ] || { echo "The download didn't contain Finalist.app." >&2; exit 1; }
      dest="${FINALIST_PREFIX:-/Applications}"
      [ -w "$dest" ] || { dest="$HOME/Applications"; mkdir -p "$dest"; }
      osascript -e 'tell application "Finalist" to quit' >/dev/null 2>&1 || true
      sleep 1
      say "Installing into $dest"
      rm -rf "$dest/Finalist.app"
      ditto "$mnt/Finalist.app" "$dest/Finalist.app"
      # the builds aren't signed by Apple: without this, the first open is refused until "Open Anyway"
      xattr -dr com.apple.quarantine "$dest/Finalist.app" 2>/dev/null || true
      app="$dest/Finalist.app"
      [ -n "${FINALIST_NO_OPEN:-}" ] || open "$app"
      ;;
    Linux)
      [ "$(uname -m)" = x86_64 ] || { echo "Finalist Desktop for Linux is built for x86_64 only." >&2; exit 1; }
      dest="$(linux_dir)"; mkdir -p "$dest"
      say "Downloading Finalist for Linux"
      curl -fL --progress-bar -o "$tmp/Finalist.AppImage" "$REL/Finalist-linux-x64.AppImage"
      chmod 755 "$tmp/Finalist.AppImage"
      mv -f "$tmp/Finalist.AppImage" "$dest/Finalist.AppImage"   # a running copy keeps its old file
      app="$dest/Finalist.AppImage"
      latest_version > "$dest/VERSION" || true
      # in the app launcher, with its icon
      curl -fsSL -o "$dest/icon.png" "$RAW/desktop/icon.png" || true
      # Unpack once, here, and run the app directly. AppImage's own "extract and run" (its fallback without FUSE 2)
      # left the renderers unable to create shared memory ("No such process"), so the window stayed white
      # (CachyOS laptop, 6 Nov 2026); the unpacked binary run directly is clean, and needs no FUSE at all.
      say "Unpacking"
      rm -rf "$dest/app" "$tmp/squashfs-root"
      (cd "$tmp" && "$dest/Finalist.AppImage" --appimage-extract >/dev/null) || { echo "Couldn't unpack the AppImage." >&2; exit 1; }
      mv "$tmp/squashfs-root" "$dest/app"
      run="$dest/app/finalist"
      mkdir -p "$apps"
      # APPIMAGE tells the app it's the packaged build. --no-sandbox must be on the COMMAND LINE: Chromium starts its
      # zygote before main.js runs, so appendSwitch('no-sandbox') there comes too late for the renderers, which then
      # can't create shared memory inside the sandbox ("No such process") and the window stays white (CachyOS, 6 Nov).
      printf '[Desktop Entry]\nType=Application\nName=Finalist\nComment=Finals revision, with its node\nExec=env APPIMAGE="%s" "%s" --no-sandbox %%U\nIcon=%s\nCategories=Education;\nStartupWMClass=finalist\nTerminal=false\n' "$app" "$run" "$dest/icon.png" > "$apps/finalist.desktop"
      if command -v update-desktop-database >/dev/null; then update-desktop-database "$apps" >/dev/null 2>&1 || true; fi
      export APPIMAGE="$app"; app="$run"
      [ -n "${FINALIST_NO_OPEN:-}" ] || (nohup "$app" --no-sandbox >/dev/null 2>&1 &)
      ;;
    *) echo "On Windows, run this in PowerShell instead:  irm $RAW/desktop/install/install.ps1 | iex" >&2; exit 1 ;;
  esac
  say "${G}Finalist is installed:${R}${B} $app"
  echo "Run the same line again any time to update it or uninstall it."
  if ! command -v claude >/dev/null 2>&1; then
    echo "For Claude stations, also install Claude Code once and sign in:"
    echo "  curl -fsSL https://claude.ai/install.sh | bash   then run: claude"
  fi
}

# ── the menu: up/down (or j/k, or a number) to move, Enter to choose, Esc or q to leave ──
CHOICE=0
menu() {   # menu <question> <option>...: sets CHOICE to the chosen index (the last option on Esc)
  local q="$1"; shift; local opts=("$@") n=$# i key rest w=0 o
  for o in "${opts[@]}"; do [ ${#o} -gt $w ] && w=${#o}; done
  CHOICE=0
  printf '\033[?25l' >/dev/tty
  draw() {
    printf '  %s%s%s\n' "$B" "$q" "$R" >/dev/tty
    for i in "${!opts[@]}"; do
      if [ "$i" = "$CHOICE" ]; then printf '    %s%-*s  [•]  ←%s\n' "$G$B" "$w" "${opts[$i]}" "$R" >/dev/tty
      else printf '    %-*s  %s[ ]%s\n' "$w" "${opts[$i]}" "$D" "$R" >/dev/tty; fi
    done
    printf '%s    ↑ ↓ to move · Enter to choose · Esc to leave%s\n' "$D" "$R" >/dev/tty
  }
  draw
  while :; do
    IFS= read -rsn1 key </dev/tty || key=q
    case "$key" in
      $'\x1b') rest=""; IFS= read -rsn2 -t 0.05 rest </dev/tty || true
               case "$rest" in '[A') CHOICE=$(( (CHOICE + n - 1) % n ));; '[B') CHOICE=$(( (CHOICE + 1) % n ));; '') CHOICE=$((n - 1)); break;; esac ;;
      k) CHOICE=$(( (CHOICE + n - 1) % n ));;
      j) CHOICE=$(( (CHOICE + 1) % n ));;
      [1-9]) [ "$key" -le "$n" ] && CHOICE=$((key - 1));;
      q) CHOICE=$((n - 1)); break;;
      '') break;;
    esac
    printf '\033[%dA\033[J' $((n + 2)) >/dev/tty
    draw
  done
  printf '\033[?25h' >/dev/tty
  echo >/dev/tty
}

# the name, big, so the terminal doesn't look empty (and plain on a terminal too narrow for it)
banner() {
  local cols shades=(42 41 36 35 30 29) i=0 line
  cols="$( { tput cols </dev/tty; } 2>/dev/null || echo 80)"
  echo >/dev/tty
  if [ "$cols" -ge 61 ]; then
    while IFS= read -r line; do printf '  \033[38;5;%sm%s%s\n' "${shades[$i]}" "$line" "$R" >/dev/tty; i=$((i + 1)); done <<'ART'
███████╗██╗███╗   ██╗ █████╗ ██╗     ██╗███████╗████████╗
██╔════╝██║████╗  ██║██╔══██╗██║     ██║██╔════╝╚══██╔══╝
█████╗  ██║██╔██╗ ██║███████║██║     ██║███████╗   ██║
██╔══╝  ██║██║╚██╗██║██╔══██║██║     ██║╚════██║   ██║
██║     ██║██║ ╚████║██║  ██║███████╗██║███████║   ██║
╚═╝     ╚═╝╚═╝  ╚═══╝╚═╝  ╚═╝╚══════╝╚═╝╚══════╝   ╚═╝
ART
    printf '  %*s%s%s%s\n' 17 '' "$D" "By Syncra (Raahat Shah)" "$R" >/dev/tty
  else
    printf '  %sF I N A L I S T%s\n  %sBy Syncra (Raahat Shah)%s\n' "$G$B" "$R" "$D" "$R" >/dev/tty
  fi
}

interactive() {
  local have latest state
  have="$(installed_version)"; latest="$(latest_version || true)"
  if [ -z "$have" ]; then state="Not installed on this computer${latest:+ · latest is v$latest}"
  elif [ -n "$latest" ] && [ "$have" = "$latest" ]; then state="Installed: v$have, up to date"
  else state="Installed: v$have${latest:+ · v$latest is out}"; fi
  banner
  printf '\n  %sFinalist Desktop%s · %s%s%s\n\n' "$B" "$R" "$D" "$state" "$R" >/dev/tty
  while :; do
    menu "What would you like to do?" \
      "Install or update Finalist Desktop" \
      "Uninstall Finalist Desktop but keep my data" \
      "Uninstall Finalist Desktop completely" \
      "Exit"
    case "$CHOICE" in
      0) do_install; return;;
      1) do_uninstall; return;;
      2) printf '  %sThis also deletes your node’s datasets, runs, voice and sign-in on this computer.%s\n\n' "$Y" "$R" >/dev/tty
         menu "Remove everything?" "Yes, remove Finalist Desktop and all its data" "No, go back"
         if [ "$CHOICE" = 0 ]; then do_uninstall purge; return; fi ;;
      *) echo "Nothing changed."; return;;
    esac
  done
}

case "${1:-}" in
  install|update) do_install ;;
  uninstall) if [ "${2:-}" = --purge ]; then do_uninstall purge; else do_uninstall; fi ;;
  purge) do_uninstall purge ;;
  "") if { : </dev/tty; } 2>/dev/null; then interactive; else do_install; fi ;;   # no terminal to ask in: install
  *) echo "Use: install, uninstall or purge (or nothing, to choose from a menu)" >&2; exit 2 ;;
esac
