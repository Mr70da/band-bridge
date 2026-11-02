# Registers the BandBridge task: starts tray.py at logon of the current user, no elevation.
$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$action = New-ScheduledTaskAction -Execute "$repo\.venv\Scripts\pythonw.exe" -Argument "`"$repo\tray.py`"" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited  # Bluetooth and the tray need the desktop session
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName BandBridge -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Task BandBridge registered for ${user}: runs `"$repo\.venv\Scripts\pythonw.exe`" tray.py at logon."
Write-Host "Start it now:  Start-ScheduledTask -TaskName BandBridge   (stop 'python -m bridge.api' first)"
Write-Host "Remove it:     powershell -ExecutionPolicy Bypass -File `"$repo\uninstall_startup.ps1`""
