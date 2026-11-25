# Shared helpers, dot-sourced by the start/stop/install scripts:
#   . (Join-Path $PSScriptRoot "app_process.ps1")

$AppPort = 8321
$AppDir = $PSScriptRoot

function Stop-GreetingStudio {
    # uvicorn runs the real server in a CHILD python process. Killing only the
    # parent (the old behaviour) could leave that child alive and still holding
    # port 8321, so the next start failed with "address already in use".
    # So: kill this app's processes together with their whole tree, then free
    # the port from whatever still holds it. Only python processes started
    # from THIS folder are touched — never some other project's run.py.
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*run.py*" -and $_.CommandLine -like "*$AppDir*" } |
        ForEach-Object { & taskkill.exe /PID $_.ProcessId /T /F 2>&1 | Out-Null }

    Get-NetTCPConnection -LocalPort $AppPort -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object { & taskkill.exe /PID $_.OwningProcess /T /F 2>&1 | Out-Null }
}

function Test-GreetingStudio {
    # True if the app answers on its port within 20 seconds.
    try {
        Invoke-WebRequest -Uri "http://127.0.0.1:$AppPort/api/settings" -UseBasicParsing `
            -TimeoutSec 20 | Out-Null
        return $true
    } catch {
        return $false
    }
}
