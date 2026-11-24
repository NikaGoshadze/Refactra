<#
.SYNOPSIS
Run the Refactra worker on the Docker host and restart it if it stops unexpectedly.

.DESCRIPTION
Passes all arguments to `python -m refactra.worker`. A clean stop (exit code 0) ends the
loop. Any other exit restarts the worker after a delay that doubles up to 60 seconds and
resets after five minutes of uptime. Press Ctrl+C to stop.

.EXAMPLE
.\scripts\run-worker.ps1 --repository-workflow --candidate-source $source --candidate-artifacts $artifacts
#>
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$WorkerArgs)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$project = Join-Path $root 'services/backend'
$envFile = Join-Path $root '.env'
$delay = 5

while ($true) {
    $started = Get-Date
    & uv run --project $project --env-file $envFile python -m refactra.worker @WorkerArgs
    $code = $LASTEXITCODE
    if ($code -eq 0) {
        Write-Host 'Worker stopped cleanly.'
        break
    }
    if (((Get-Date) - $started).TotalMinutes -ge 5) { $delay = 5 }
    Write-Warning "Worker exited with code $code. Restarting in $delay seconds (Ctrl+C to stop)."
    Start-Sleep -Seconds $delay
    $delay = [Math]::Min($delay * 2, 60)
}
