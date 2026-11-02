# Stops and removes the BandBridge task.
if (Get-ScheduledTask -TaskName BandBridge -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName BandBridge -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName BandBridge -Confirm:$false
    Write-Host "Task BandBridge removed."
} else {
    Write-Host "Task BandBridge is not installed."
}
