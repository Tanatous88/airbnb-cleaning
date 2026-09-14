# Registers a Windows Scheduled Task that starts Guest Greeting & Review
# Studio automatically when you log in — no more manually opening
# PowerShell and running "python run.py" every time.
#
# Run this ONCE, from an ADMINISTRATOR PowerShell (right-click PowerShell ->
# "Run as administrator") — registering a scheduled task requires it:
#   powershell -ExecutionPolicy Bypass -File install_autostart.ps1
#
# The app runs hidden in the background from then on (every login), and
# restarts itself automatically if it ever crashes. Output goes to
# app.log / app_error.log in this folder — check those instead of a
# console window if something looks wrong.
#
# To remove it later, run uninstall_autostart.ps1.

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "This needs to run as Administrator." -ForegroundColor Yellow
    Write-Host "Right-click PowerShell -> 'Run as administrator', then re-run this script."
    exit 1
}

$taskName = "GreetingStudioAutoStart"
$scriptPath = Join-Path $PSScriptRoot "start_hidden.ps1"

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$scriptPath`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -DontStopOnIdleEnd -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
        -Description "Runs Guest Greeting & Review Studio in the background on login." `
        -ErrorAction Stop | Out-Null
} catch {
    Write-Host "Failed to register the scheduled task: $_" -ForegroundColor Red
    exit 1
}

Write-Host "Installed. Starting it now (instead of waiting for the next login)..."
Start-ScheduledTask -TaskName $taskName

Start-Sleep -Seconds 3
Write-Host "Done. Check http://127.0.0.1:8321 in a browser in a few seconds."
Write-Host "Logs: $PSScriptRoot\app.log and $PSScriptRoot\app_error.log"

