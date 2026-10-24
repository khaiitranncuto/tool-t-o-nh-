param([switch]$CleanupOnly)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Stop-OldToolServer {
    # Cursor may have both a Task and debugpy instance alive. Keep draining
    # verified tool listeners until the port remains empty.
    for ($round = 0; $round -lt 20; $round++) {
        $listeners = @(Get-NetTCPConnection -LocalPort 5050 -State Listen -ErrorAction SilentlyContinue)
        if ($listeners.Count -eq 0) {
            Start-Sleep -Milliseconds 400
            if (-not (Get-NetTCPConnection -LocalPort 5050 -State Listen -ErrorAction SilentlyContinue)) { return }
            continue
        }
        try {
            $state = Invoke-RestMethod "http://127.0.0.1:5050/api/state" -TimeoutSec 2
            if ($null -eq $state.accounts -or $null -eq $state.jobs -or $null -eq $state.logs) { throw "API mismatch" }
        } catch {
            throw "Port 5050 is occupied by another application. Stop it before running TOOL V1 RING."
        }
        try {
            $shutdown = Invoke-RestMethod "http://127.0.0.1:5050/api/system/shutdown" -Method Post -ContentType "application/json" -Body "{}" -TimeoutSec 45
            Write-Host "[CLEANUP] Closed $($shutdown.closed) tool browser profile(s)."
        } catch {
            Write-Warning "The old server did not complete browser cleanup: $($_.Exception.Message)"
        }
        foreach ($listener in $listeners) {
            $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
            if ($null -eq $processInfo -or $processInfo.Name -ne "python.exe" -or $processInfo.CommandLine -notmatch "(app|runner)\.py") {
                throw "Port 5050 belongs to an unrecognized process; it was not stopped."
            }
            Write-Host "[RESTART] Stopping old TOOL V1 RING server (PID $($listener.OwningProcess))..."
            [System.Diagnostics.Process]::GetProcessById($listener.OwningProcess).Kill()
        }
        Start-Sleep -Milliseconds 400
    }
    throw "Could not obtain exclusive use of port 5050. Stop other Cursor Run sessions."
}

Stop-OldToolServer
if ($CleanupOnly) { exit 0 }
Write-Host "[RUNNING] TOOL V1 RING on http://127.0.0.1:5050"
& python app.py
exit $LASTEXITCODE
