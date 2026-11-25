# Turns the background app back on after stop_app.ps1 (or just restarts it).
#   powershell -ExecutionPolicy Bypass -File start_app.ps1

$taskName = "GreetingStudioAutoStart"
. (Join-Path $PSScriptRoot "app_process.ps1")

if (-not (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
    Write-Host "Auto-start isn't installed. Run install_autostart.ps1 (as Administrator) first." -ForegroundColor Yellow
    exit 1
}
Enable-ScheduledTask -TaskName $taskName | Out-Null
Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Stop-GreetingStudio
Start-ScheduledTask -TaskName $taskName

Write-Host "Starting... checking in 45 seconds."
Start-Sleep -Seconds 45
if (Test-GreetingStudio) {
    Write-Host "Running - open http://127.0.0.1:8321" -ForegroundColor Green
} else {
    Write-Host "Not answering yet. Give it another minute, then check app_error.log." -ForegroundColor Yellow
}
