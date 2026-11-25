# Removes the auto-start scheduled task and stops the background app. Run
# this if you want to go back to starting the app manually.
#   powershell -ExecutionPolicy Bypass -File uninstall_autostart.ps1

$taskName = "GreetingStudioAutoStart"
. (Join-Path $PSScriptRoot "app_process.ps1")

Write-Host "Stopping the task (if running)..."
Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue

Write-Host "Removing the scheduled task..."
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

Stop-GreetingStudio

Write-Host "Done. The app will no longer start automatically."
Write-Host "Run it manually with: python run.py"
