# Starts the ATLAS research stack automatically when you log in to Windows (minimized, no browser window).
# It only adds a shortcut to your own Startup folder: no admin rights, no services, nothing else changes.
# Undo with Uninstall-Autostart.ps1.
#   powershell -ExecutionPolicy Bypass -File windows\Install-Autostart.ps1
$ErrorActionPreference = 'Stop'
$launcher = Join-Path $PSScriptRoot 'Start-ATLAS.cmd'
if (-not (Test-Path $launcher)) { throw "Start-ATLAS.cmd not found next to this script ($launcher)" }
$link = Join-Path ([Environment]::GetFolderPath('Startup')) 'ATLAS research stack.lnk'
$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut($link)
$s.TargetPath = $launcher
$s.Arguments = '--login'
$s.WorkingDirectory = Split-Path $PSScriptRoot -Parent
$s.WindowStyle = 7          # minimized
$s.Description = 'ATLAS research stack: scanner, shadow trading, dashboard (no keys, no real orders)'
$s.Save()
Write-Host "Added: $link"
Write-Host 'ATLAS will start minimized each time you log in. Open the dashboard at http://127.0.0.1:8770'
