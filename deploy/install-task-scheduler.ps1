<#
.SYNOPSIS
    Registers the UniFi poller as a Windows Scheduled Task on ai-pc.

.DESCRIPTION
    Creates a task that runs one poll every N minutes and exits. Nothing stays
    resident between polls, so a wedged process cannot cause a silent gap; if a
    single run hangs, Task Scheduler kills it at the execution time limit and
    the next run still happens on schedule.

    Credentials come from the env file, not from this script or the task
    definition, so nothing secret lands in the task XML.

.EXAMPLE
    # From an elevated PowerShell prompt, in the repo root:
    .\deploy\install-task-scheduler.ps1 -EnvFile C:\ProgramData\unifi-monitor\env

.EXAMPLE
    # Run as the logged-in user instead of SYSTEM, every 10 minutes:
    .\deploy\install-task-scheduler.ps1 -IntervalMinutes 10 -RunAsCurrentUser
#>

[CmdletBinding()]
param(
    [string]$TaskName        = "UniFi Network Monitor",
    [string]$ProjectRoot     = "",
    [string]$EnvFile         = "C:\ProgramData\unifi-monitor\env",
    [string]$PythonExe       = "",
    [int]   $IntervalMinutes = 5,
    [switch]$RunAsCurrentUser
)

$ErrorActionPreference = "Stop"

# Resolved here, not as a param default: under `powershell -File` the parameter
# defaults are bound before the script scope exists, so $PSScriptRoot is still
# empty there and Split-Path fails on it. In the body it is populated.
if (-not $ProjectRoot) {
    $scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
    if (-not $scriptDir) { throw "Cannot determine the script location; pass -ProjectRoot explicitly." }
    $ProjectRoot = Split-Path -Parent $scriptDir
}

if (-not $PythonExe) {
    $found = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $found) { throw "python.exe not found on PATH; pass -PythonExe explicitly." }
    $PythonExe = $found.Source
}
if (-not (Test-Path (Join-Path $ProjectRoot "unifi_monitor\cli.py"))) {
    throw "unifi_monitor package not found under $ProjectRoot"
}
if (-not (Test-Path $EnvFile)) {
    Write-Warning "Env file $EnvFile does not exist yet. Copy unifi_monitor\.env.example there and fill it in before the first run."
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $EnvFile) | Out-Null
}

# Restrict the env file: it holds the controller password. The account the task
# actually runs as has to keep read access, or every poll fails to authenticate
# with credentials it cannot see.
if (Test-Path $EnvFile) {
    $acl = Get-Acl $EnvFile
    $acl.SetAccessRuleProtection($true, $false)
    $acl.Access | ForEach-Object { $acl.RemoveAccessRule($_) | Out-Null }
    foreach ($who in @("BUILTIN\Administrators", "NT AUTHORITY\SYSTEM")) {
        $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
            $who, "FullControl", "Allow")))
    }
    if ($RunAsCurrentUser) {
        $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
            "$env:USERDOMAIN\$env:USERNAME", "Modify", "Allow")))
    }
    try {
        Set-Acl -Path $EnvFile -AclObject $acl -ErrorAction Stop
        Write-Host "Locked down $EnvFile"
    } catch {
        # Set-Acl wants SeSecurityPrivilege, which an unelevated shell lacks.
        # icacls achieves the same DACL for a file the caller owns.
        $grants = @("/grant:r", "BUILTIN\Administrators:F", "/grant:r", "NT AUTHORITY\SYSTEM:F")
        if ($RunAsCurrentUser) { $grants += @("/grant:r", "${env:USERNAME}:M") }
        & icacls $EnvFile /inheritance:r @grants | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Locked down $EnvFile (via icacls)"
        } else {
            Write-Warning "Could not restrict $EnvFile - it holds the controller password. Fix its permissions by hand."
        }
    }
}

$action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "-m unifi_monitor.cli poll" `
    -WorkingDirectory $ProjectRoot

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes ([Math]::Max(2, $IntervalMinutes - 1))) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

if ($RunAsCurrentUser) {
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType S4U -RunLevel Limited
} else {
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" `
        -LogonType ServiceAccount -RunLevel Highest
}

# The task itself carries only the pointer to the env file, never the secrets.
# Machine scope needs elevation; fall back to the user's own environment, which
# is the right scope anyway for a task running as that user.
$env:UNIFI_MONITOR_ENV = $EnvFile
$scope = if ($RunAsCurrentUser) { "User" } else { "Machine" }
try {
    [Environment]::SetEnvironmentVariable("UNIFI_MONITOR_ENV", $EnvFile, $scope)
} catch {
    [Environment]::SetEnvironmentVariable("UNIFI_MONITOR_ENV", $EnvFile, "User")
    Write-Warning "Could not set UNIFI_MONITOR_ENV at $scope scope; set it for the current user instead."
}

$description = "Polls the UniFi controller and records issues to SQLite. No LLM in the path."
$registered = $false
try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force `
        -Description $description -ErrorAction Stop | Out-Null
    $registered = $true
} catch {
    # S4U ("run whether or not the user is logged on") needs elevation, and so
    # does registering as SYSTEM. Interactive works from an ordinary shell.
    if ($RunAsCurrentUser) {
        Write-Warning "S4U registration was denied ($($_.Exception.Message.Trim())). Retrying with an Interactive principal."
        $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
            -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
            -Settings $settings -Principal $principal -Force `
            -Description $description -ErrorAction Stop | Out-Null
        $registered = $true
        Write-Warning "Registered with LogonType=Interactive: polls run only while $env:USERNAME is logged on. Re-run this script from an elevated prompt for an S4U task that polls regardless."
    } else {
        throw
    }
}

# Never claim success on a task that is not actually there.
if (-not $registered -or -not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    throw "Task '$TaskName' was not registered."
}

Write-Host ""
Write-Host "Registered '$TaskName': every $IntervalMinutes minute(s), running:"
Write-Host "  $PythonExe -m unifi_monitor.cli poll   (cwd $ProjectRoot)"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Fill in $EnvFile (see unifi_monitor\.env.example)"
Write-Host "  2. Verify the controller is reachable:"
Write-Host "       python -m unifi_monitor.cli check"
Write-Host "  3. Force a run and watch it:"
Write-Host "       Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "       python -m unifi_monitor.cli status"
Write-Host ""
Write-Host "To remove:  Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
