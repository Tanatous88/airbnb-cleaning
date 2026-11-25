# Pauses the background app: stops it and switches off the auto-start and
# watchdog until you run start_app.ps1. Use this when you need the port free
# (e.g. to run "python run.py" yourself in a visible window for debugging).
#   powershell -ExecutionPolicy Bypass -File stop_app.ps1

$taskName = "GreetingStudioAutoStart"
. (Join-Path $PSScriptRoot "app_process.ps1")

Disable-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue | Out-Null
Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Stop-GreetingStudio

Write-Host "Stopped, and auto-start is paused. Run start_app.ps1 to turn it back on."
