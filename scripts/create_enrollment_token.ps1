param(
    [ValidateRange(5, 1440)]
    [int]$TtlMinutes = 60,
    [ValidateRange(1, 20)]
    [int]$Uses = 1,
    [string]$ControlUrl = 'http://127.0.0.1:8080',
    [string]$OutFile
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$tokenFile = Join-Path $projectRoot '.state\control.token'
if (-not (Test-Path -LiteralPath $tokenFile)) {
    throw '控制面 API Key 不存在；请先运行 scripts\start_control.ps1 -InitializeOnly。'
}

$apiKey = [IO.File]::ReadAllText($tokenFile).Trim()
$headers = @{ Authorization = "Bearer $apiKey" }
$body = @{ ttl_minutes = $TtlMinutes; uses = $Uses } | ConvertTo-Json
$record = Invoke-RestMethod -Method Post -Uri "$($ControlUrl.TrimEnd('/'))/api/enrollment-tokens" `
    -Headers $headers -ContentType 'application/json' -Body $body

if ($OutFile) {
    $resolved = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutFile)
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($resolved)) | Out-Null
    [IO.File]::WriteAllText($resolved, $record.token, [Text.UTF8Encoding]::new($false))
    icacls.exe $resolved /inheritance:r | Out-Null
    icacls.exe $resolved /grant:r "$($env:USERDOMAIN)\$($env:USERNAME):(R,W)" `
        'NT AUTHORITY\SYSTEM:(F)' 'BUILTIN\Administrators:(F)' | Out-Null
    Write-Host "Enrollment token 已写入：$resolved"
} else {
    Write-Host 'Enrollment token（仅显示本次，请通过安全渠道交给算力提供者）：'
    Write-Output $record.token
}
Write-Host "Token ID: $($record.id)"
Write-Host "Expires:  $($record.expires_at)"
Write-Host "Uses:     $($record.uses_remaining)"
