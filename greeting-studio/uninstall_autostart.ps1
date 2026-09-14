# Removes the auto-start scheduled task and stops any running background
# instance. Run this if you want to go back to starting the app manually.
#   powershell -ExecutionPolicy Bypass -File uninstall_autostart.ps1

$taskName = "GreetingStudioAutoStart"

Write-Host "Stopping the task (if running)..."
Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue

Write-Host "Removing the scheduled task..."
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

# Belt-and-suspenders: make sure no leftover python.exe running this app survives
# (Stop-ScheduledTask should already kill the whole process tree, but just in case).
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*run.py*" } |
    ForEach-Object {
        Write-Host "Stopping leftover process (PID $($_.ProcessId))..."
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

Write-Host "Done. The app will no longer start automatically on login."
Write-Host "Run it manually with: python run.py"
