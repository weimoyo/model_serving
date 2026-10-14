param([switch]$Reset)

$root = Split-Path -Parent $PSScriptRoot
$runDir = Join-Path $root '.demo-live'
$pidFile = Join-Path $runDir 'pids.json'
if (Test-Path -LiteralPath $pidFile) {
    $saved = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
    foreach ($processId in @($saved.backend, $saved.agent, $saved.gateway)) {
        if (-not $processId) { continue }
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$processId" -ErrorAction SilentlyContinue
        if ($process -and $process.CommandLine -match 'uvicorn' -and $process.ExecutablePath -like '*python.exe') {
            Stop-Process -Id $processId
            Write-Host "Stopped demo process $processId"
        }
    }
}
if ($Reset -and (Test-Path -LiteralPath $runDir)) {
    $resolved = (Resolve-Path -LiteralPath $runDir).Path
    if ((Split-Path -Parent $resolved) -eq $root -and (Split-Path -Leaf $resolved) -eq '.demo-live') {
        Remove-Item -LiteralPath $resolved -Recurse -Force
        Write-Host 'Removed demo state.'
    }
}
