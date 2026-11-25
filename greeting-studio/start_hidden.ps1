# Runs the app with no visible window, logging to app.log / app_error.log,
# and keeps it alive: restarts it if it exits, and also if it stops answering
# (still running but hung) for 3 checks in a row. This is what the scheduled
# task (installed by install_autostart.ps1) launches.
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot
. (Join-Path $PSScriptRoot "app_process.ps1")

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$logPath = Join-Path $PSScriptRoot "app.log"
$errLogPath = Join-Path $PSScriptRoot "app_error.log"

function Write-Log($text) {
    Add-Content -Path $logPath -Value "===== $text at $(Get-Date -Format o) ====="
}

while ($true) {
    # Clear out anything left over from a previous run still holding the port.
    Stop-GreetingStudio
    Add-Content -Path $logPath -Value ""
    Write-Log "Starting"
    # cmd.exe's >> appends; PowerShell's own -RedirectStandardOutput would
    # truncate the log on every restart.
    $cmdArgs = "/c `"`"$python`" run.py >> `"$logPath`" 2>> `"$errLogPath`"`""
    $proc = Start-Process -FilePath "cmd.exe" -ArgumentList $cmdArgs `
        -WorkingDirectory $PSScriptRoot -NoNewWindow -PassThru

    Start-Sleep -Seconds 45   # give it time to start up
    $fails = 0
    while (-not $proc.HasExited) {
        if (Test-GreetingStudio) {
            $fails = 0
        } else {
            $fails++
        }
        if ($fails -ge 3) {
            Write-Log "Not responding for 3 checks - restarting"
            Stop-GreetingStudio
            & taskkill.exe /PID $proc.Id /T /F 2>&1 | Out-Null
            break
        }
        Start-Sleep -Seconds 60
    }
    Write-Log "Exited - restarting in 5s"
    Start-Sleep -Seconds 5
}
