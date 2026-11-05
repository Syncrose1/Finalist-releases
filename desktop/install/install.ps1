# Finalist Desktop's installer (5 Nov 2026), on Windows. Pulls the latest release itself: nothing to download first.
# In PowerShell, run it and choose with the arrow keys:
#   irm https://raw.githubusercontent.com/Syncrose1/Finalist-releases/main/desktop/install/install.ps1 | iex
# Or say what to do: & ([scriptblock]::Create((irm <that address>))) install | uninstall | purge
# Installing clears the "downloaded from the internet" mark that sets off SmartScreen and installs for you only (no
# administrator needed); the app opens when it's done. Uninstall keeps your node's data; purge removes it too.
param([string]$Action = '')
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is many times slower with its progress bar
$Repo = 'Syncrose1/Finalist-releases'
$Uninstaller = Join-Path $env:LOCALAPPDATA 'Programs\Finalist\Uninstall Finalist.exe'
# the node's data (node/finalist_node/config.py puts it under your home on every system) and the app's own
$DataDirs = @((Join-Path $HOME '.config\finalist-node'), (Join-Path $HOME '.local\share\finalist-node'), (Join-Path $env:APPDATA 'Finalist'))

function Get-InstalledVersion {
  $keys = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*'
  $k = Get-ItemProperty $keys -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -like 'Finalist*' } | Select-Object -First 1
  if ($k) { if ($k.DisplayVersion) { $k.DisplayVersion } else { '?' } } elseif (Test-Path $Uninstaller) { '?' } else { $null }
}
function Get-LatestVersion {
  try { ((Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest" -TimeoutSec 5).tag_name) -replace '^v', '' } catch { $null }
}

function Install-Finalist {
  $url = "https://github.com/$Repo/releases/latest/download/Finalist-windows-x64-setup.exe"
  $exe = Join-Path $env:TEMP 'Finalist-setup.exe'
  Write-Host 'Downloading Finalist for Windows' -ForegroundColor White
  Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
  Unblock-File $exe
  Get-Process -Name 'Finalist' -ErrorAction SilentlyContinue | Stop-Process -Force
  Write-Host 'Installing' -ForegroundColor White
  Start-Process -FilePath $exe -Wait
  Remove-Item $exe -ErrorAction SilentlyContinue
  Write-Host 'Finalist is installed.' -ForegroundColor Green
  Write-Host 'Run the same line again any time to update it or uninstall it.'
  if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Host 'For Claude stations, also install Claude Code once and sign in:'
    Write-Host '  irm https://claude.ai/install.ps1 | iex   then run: claude'
  }
}

function Uninstall-Finalist([switch]$Purge) {
  Get-Process -Name 'Finalist' -ErrorAction SilentlyContinue | Stop-Process -Force
  if (Test-Path $Uninstaller) { Start-Process -FilePath $Uninstaller -ArgumentList '/S' -Wait; Write-Host 'Removed Finalist Desktop.' -ForegroundColor White }
  else { Write-Host "Finalist Desktop wasn't installed here, so there was no app to remove." }
  if ($Purge) {
    foreach ($d in $DataDirs) { if (Test-Path $d) { Remove-Item -Recurse -Force $d; Write-Host "Removed $d" } }
    Write-Host 'Finalist Desktop and all its data are gone.' -ForegroundColor White
  } else { Write-Host 'Your data is kept (datasets, runs, voice, sign-in), ready if you install again.' }
}

# the menu: up/down (or j/k, or a number) to move, Enter to choose, Esc or q to leave; returns the chosen index
function Show-Menu([string]$Question, [string[]]$Options) {
  $w = ($Options | Measure-Object -Property Length -Maximum).Maximum
  $i = 0; $n = $Options.Count
  $cursor = [Console]::CursorVisible; [Console]::CursorVisible = $false
  try {
    while ($true) {
      Write-Host ('  ' + $Question) -ForegroundColor White
      for ($j = 0; $j -lt $n; $j++) {
        $label = '    ' + $Options[$j].PadRight($w) + '  '
        if ($j -eq $i) { Write-Host ($label + '[*]  <-') -ForegroundColor Green } else { Write-Host ($label + '[ ]') -ForegroundColor Gray }
      }
      Write-Host '    Up/Down to move - Enter to choose - Esc to leave' -ForegroundColor DarkGray
      $k = [Console]::ReadKey($true)
      switch ($k.Key) {
        'UpArrow' { $i = ($i + $n - 1) % $n } 'K' { $i = ($i + $n - 1) % $n }
        'DownArrow' { $i = ($i + 1) % $n } 'J' { $i = ($i + 1) % $n }
        'Enter' { Write-Host ''; return $i }
        'Escape' { Write-Host ''; return ($n - 1) } 'Q' { Write-Host ''; return ($n - 1) }
        default { if ($k.KeyChar -match '[1-9]' -and [int]"$($k.KeyChar)" -le $n) { $i = [int]"$($k.KeyChar)" - 1 } }
      }
      # back up over the menu and draw it again in place
      $top = [Math]::Max(0, [Console]::CursorTop - ($n + 2))
      [Console]::SetCursorPosition(0, $top)
      for ($j = 0; $j -lt $n + 2; $j++) { Write-Host (' ' * ([Console]::WindowWidth - 1)) }
      [Console]::SetCursorPosition(0, $top)
    }
  } finally { [Console]::CursorVisible = $cursor }
}

# the name, big, so the terminal doesn't look empty (and plain on a window too narrow for it)
function Show-Banner {
  $art = @(
    '███████╗██╗███╗   ██╗ █████╗ ██╗     ██╗███████╗████████╗',
    '██╔════╝██║████╗  ██║██╔══██╗██║     ██║██╔════╝╚══██╔══╝',
    '█████╗  ██║██╔██╗ ██║███████║██║     ██║███████╗   ██║',
    '██╔══╝  ██║██║╚██╗██║██╔══██║██║     ██║╚════██║   ██║',
    '██║     ██║██║ ╚████║██║  ██║███████╗██║███████║   ██║',
    '╚═╝     ╚═╝╚═╝  ╚═══╝╚═╝  ╚═╝╚══════╝╚═╝╚══════╝   ╚═╝'
  )
  $shades = 'Green', 'Green', 'DarkGreen', 'DarkGreen', 'DarkCyan', 'DarkCyan'
  Write-Host ''
  if ([Console]::WindowWidth -ge 61) {
    for ($j = 0; $j -lt $art.Count; $j++) { Write-Host ('  ' + $art[$j]) -ForegroundColor $shades[$j] }
    Write-Host ('  ' + (' ' * 17) + 'By Syncra (Raahat Shah)') -ForegroundColor DarkGray
  } else { Write-Host '  F I N A L I S T' -ForegroundColor Green; Write-Host '  By Syncra (Raahat Shah)' -ForegroundColor DarkGray }
}

function Start-Interactive {
  $have = Get-InstalledVersion; $latest = Get-LatestVersion
  if (-not $have) { $state = 'Not installed on this computer' + $(if ($latest) { " - latest is v$latest" } else { '' }) }
  elseif ($latest -and $have -eq $latest) { $state = "Installed: v$have, up to date" }
  else { $state = "Installed: v$have" + $(if ($latest) { " - v$latest is out" } else { '' }) }
  Show-Banner; Write-Host ''
  Write-Host '  Finalist Desktop' -ForegroundColor White -NoNewline; Write-Host " - $state" -ForegroundColor DarkGray; Write-Host ''
  while ($true) {
    $c = Show-Menu 'What would you like to do?' @('Install or update Finalist Desktop', 'Uninstall Finalist Desktop but keep my data', 'Uninstall Finalist Desktop completely', 'Exit')
    switch ($c) {
      0 { Install-Finalist; return }
      1 { Uninstall-Finalist; return }
      2 {
        Write-Host "  This also deletes your node's datasets, runs, voice and sign-in on this computer." -ForegroundColor Yellow; Write-Host ''
        if ((Show-Menu 'Remove everything?' @('Yes, remove Finalist Desktop and all its data', 'No, go back')) -eq 0) { Uninstall-Finalist -Purge; return }
      }
      default { Write-Host 'Nothing changed.'; return }
    }
  }
}

switch ($Action) {
  { $_ -in 'install', 'update' } { Install-Finalist }
  'uninstall' { Uninstall-Finalist }
  'purge' { Uninstall-Finalist -Purge }
  '' { if ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected) { Start-Interactive } else { Install-Finalist } }
  default { Write-Host 'Use: install, uninstall or purge (or nothing, to choose from a menu)' -ForegroundColor Red }
}
