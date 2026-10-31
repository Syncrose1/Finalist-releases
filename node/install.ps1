# Installs the Finalist Node on Windows (plan phase 4, 31 Oct 2026). Untested on real Windows yet.
#   Right-click > Run with PowerShell, or:  powershell -ExecutionPolicy Bypass -File install.ps1
# The node is plain Python (3.10+) plus Claude Code. Stations are typed on Windows (no voice yet).
$ErrorActionPreference = 'Stop'
$Node = Split-Path -Parent $MyInvocation.MyCommand.Path
function Say($t) { Write-Host "`n$t" -ForegroundColor White }
function Ok($t) { Write-Host "  + $t" -ForegroundColor Green }
function No($t) { Write-Host "  . $t" -ForegroundColor Yellow }

Say "Finalist Node: installing from $Node"

Say "1. Python"
$py = $null
foreach ($c in @('py -3', 'python')) {
  try { $v = & cmd /c "$c -c ""import sys; print(sys.version_info >= (3, 10))""" 2>$null; if ($v -eq 'True') { $py = $c; break } } catch {}
}
if (-not $py) { No "Python 3.10 or newer isn't installed. Get it from https://www.python.org/downloads/ (tick 'Add to PATH'), then run this again."; exit 1 }
Ok "$(& cmd /c "$py --version")"

Say "2. Claude Code"
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
  No "Claude Code isn't installed."
  $a = Read-Host "  Install it now with Anthropic's installer (irm https://claude.ai/install.ps1 | iex)? [Y/n]"
  if ($a -eq '' -or $a -match '^[Yy]') { Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression }
}
if (Get-Command claude -ErrorAction SilentlyContinue) { Ok "Claude Code found"; No "If you've never used it here, run  claude  once and sign in." }
else { No "Without Claude Code the node serves the drill and the clock only." }

Say "3. Settings"
New-Item -ItemType Directory -Force -Path "$HOME\.config\finalist-node", "$HOME\.local\share\finalist-node" | Out-Null
Ok "settings in $HOME\.config\finalist-node"
No "The first time it starts, it prints a pairing code: enter it in Finalist's settings, signed in."

Say "4. A way to start it"
$lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Finalist Node.lnk'
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = Join-Path $Node 'run.cmd'; $s.WorkingDirectory = $Node; $s.Save()
Ok "Desktop: Finalist Node (Ctrl+C in its window stops it)"

Say "5. Reaching it from your other devices (optional)"
if (Get-Command tailscale -ErrorAction SilentlyContinue) {
  $a = Read-Host "  Let your own devices on Tailscale reach this node (tailnet only)? [Y/n]"
  if ($a -eq '' -or $a -match '^[Yy]') { tailscale serve --bg --https=8443 http://127.0.0.1:47620 | Out-Null; Ok "on your tailnet at port 8443" }
} else { No "Tailscale isn't installed: this node is for this machine's browser (that's fine)." }

Say "Done. Start it with the Finalist Node shortcut on the Desktop,"
Write-Host "  then open https://raahat.sh/finalist/account, sign in, and pair it with the code it prints."
Read-Host "`nPress Enter to close"
