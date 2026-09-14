# Runs the app in a loop (auto-restarts if it crashes), with no visible
# window, logging output to app.log / app_error.log. This is what the
# scheduled task (installed by install_autostart.ps1) actually launches.
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$logPath = Join-Path $PSScriptRoot "app.log"
$errLogPath = Join-Path $PSScriptRoot "app_error.log"

while ($true) {
    Add-Content -Path $logPath -Value "`n===== Starting at $(Get-Date -Format o) ====="
    # Use cmd.exe's >> for real append — PowerShell's own
    # -RedirectStandardOutput truncates the file on every restart instead.
    $cmdArgs = "/c `"`"$python`" run.py >> `"$logPath`" 2>> `"$errLogPath`"`""
    Start-Process -FilePath "cmd.exe" -ArgumentList $cmdArgs -WorkingDirectory $PSScriptRoot `
        -NoNewWindow -Wait
    Add-Content -Path $logPath -Value "===== Exited at $(Get-Date -Format o) — restarting in 5s ====="
    Start-Sleep -Seconds 5
}
