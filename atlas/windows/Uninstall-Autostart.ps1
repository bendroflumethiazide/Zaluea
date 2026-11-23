# Stops ATLAS from starting at login by removing the shortcut Install-Autostart.ps1 added.
#   powershell -ExecutionPolicy Bypass -File windows\Uninstall-Autostart.ps1
$link = Join-Path ([Environment]::GetFolderPath('Startup')) 'ATLAS research stack.lnk'
if (Test-Path $link) { Remove-Item $link; Write-Host "Removed: $link" } else { Write-Host 'ATLAS was not set to start at login.' }
