# Stops the running background instance WITHOUT removing the auto-start
# registration — it'll start again next time you log in. Use this if you
# need the port free temporarily (e.g. to run "python run.py" yourself in
# a visible window for debugging).
#   powershell -ExecutionPolicy Bypass -File stop_app.ps1

$taskName = "GreetingStudioAutoStart"
Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue

Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*run.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Write-Host "Stopped. It will start again automatically next time you log in."
Write-Host "To stop that too, run: uninstall_autostart.ps1"
