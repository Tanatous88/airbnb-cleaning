# Registers a Windows Scheduled Task that keeps Guest Greeting & Review Studio
# running in the background — no more manually opening PowerShell and running
# "python run.py".
#
# Run from an ADMINISTRATOR PowerShell (right-click PowerShell -> "Run as
# administrator") — registering a scheduled task requires it. Safe to re-run:
#   powershell -ExecutionPolicy Bypass -File install_autostart.ps1
#
# It starts the app when you log in as THIS Windows user, and checks every
# 10 minutes that it's still running (restarting it if not). The app also
# restarts itself if it crashes or hangs. It can only run while this user is
# logged in — the Airbnb browser windows need a desktop — so after a reboot,
# log in as this user once.
#
# Output goes to app.log / app_error.log in this folder. To pause it, run
# stop_app.ps1 (and start_app.ps1 to resume); to remove it, run
# uninstall_autostart.ps1.

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "This needs to run as Administrator." -ForegroundColor Yellow
    Write-Host "Right-click PowerShell -> 'Run as administrator', then re-run this script."
    exit 1
}

$taskName = "GreetingStudioAutoStart"
$scriptPath = Join-Path $PSScriptRoot "start_hidden.ps1"
$user = "$env:USERDOMAIN\$env:USERNAME"

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$scriptPath`""

# 1) at this user's login, and 2) every 10 minutes as a watchdog. While the
# app is running the watchdog does nothing (MultipleInstances IgnoreNew); if
# it has died, the next check starts it again.
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $user
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes 10) -RepetitionDuration (New-TimeSpan -Days 3650)

$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew -StartWhenAvailable `
    -DontStopOnIdleEnd -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($atLogon, $watchdog) `
        -Principal $principal -Settings $settings `
        -Description "Keeps Guest Greeting & Review Studio running in the background." `
        -ErrorAction Stop | Out-Null
} catch {
    Write-Host "Failed to register the scheduled task: $_" -ForegroundColor Red
    exit 1
}

Write-Host "Installed for $user. Starting it now..."
. (Join-Path $PSScriptRoot "app_process.ps1")
Stop-GreetingStudio
Start-ScheduledTask -TaskName $taskName

Start-Sleep -Seconds 45
if (Test-GreetingStudio) {
    Write-Host "Running - open http://127.0.0.1:8321" -ForegroundColor Green
} else {
    Write-Host "Not answering yet. Give it another minute, then check app_error.log if it still isn't up." -ForegroundColor Yellow
}
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State
