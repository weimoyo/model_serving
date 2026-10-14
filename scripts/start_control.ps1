param(
    [switch]$InitializeOnly,
    [string]$BindHost = '127.0.0.1',
    [ValidateRange(1, 65535)]
    [int]$Port = 8080
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

$state = Join-Path $projectRoot '.state'
$tokenFile = Join-Path $state 'control.token'
New-Item -ItemType Directory -Force -Path $state | Out-Null

if (-not (Test-Path -LiteralPath $tokenFile)) {
    $bytes = [byte[]]::new(32)
    [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $token = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
    [IO.File]::WriteAllText($tokenFile, $token, [Text.UTF8Encoding]::new($false))
    icacls.exe $tokenFile /inheritance:r | Out-Null
    icacls.exe $tokenFile /grant:r "$($env:USERDOMAIN)\$($env:USERNAME):(R,W)" `
        'NT AUTHORITY\SYSTEM:(F)' 'BUILTIN\Administrators:(F)' | Out-Null
}

if ($InitializeOnly) {
    Write-Host 'Gateway API Key 文件已初始化。'
    exit 0
}

$env:MODEL_SERVING_STATE_DIR = $state
$env:MODEL_SERVING_API_KEY_FILE = $tokenFile
$env:MODEL_SERVING_API_KEY = [IO.File]::ReadAllText($tokenFile).Trim()
$nodeSecretDir = Join-Path $state 'node-secrets'
New-Item -ItemType Directory -Force -Path $nodeSecretDir | Out-Null
icacls.exe $nodeSecretDir /inheritance:r | Out-Null
icacls.exe $nodeSecretDir /grant:r "$($env:USERDOMAIN)\$($env:USERNAME):(OI)(CI)(F)" `
    'NT AUTHORITY\SYSTEM:(OI)(CI)(F)' 'BUILTIN\Administrators:(OI)(CI)(F)' | Out-Null
$env:MODEL_SERVING_NODE_SECRET_DIR = $nodeSecretDir

# Keep the gateway SSH identity selection across control-plane restarts without
# storing private-key material in an environment file or source-controlled config.
$gatewayKeyPointer = Join-Path $state 'gateway_key.path'
if ([string]::IsNullOrWhiteSpace($env:MODEL_SERVING_SSH_KEY) -and (Test-Path -LiteralPath $gatewayKeyPointer)) {
    $configuredKey = [IO.File]::ReadAllText($gatewayKeyPointer).Trim()
    if (-not [IO.Path]::IsPathRooted($configuredKey)) {
        $configuredKey = Join-Path $state $configuredKey
    }
    if (-not (Test-Path -LiteralPath $configuredKey)) {
        throw "Gateway SSH key 不存在：$configuredKey"
    }
    $env:MODEL_SERVING_SSH_KEY = (Resolve-Path -LiteralPath $configuredKey).Path
}

Get-ChildItem -LiteralPath $state -Filter '*_AGENT_KEY.token' -File | ForEach-Object {
    $environmentName = $_.BaseName
    $value = [IO.File]::ReadAllText($_.FullName).Trim()
    if (-not [string]::IsNullOrWhiteSpace($value)) {
        [Environment]::SetEnvironmentVariable($environmentName, $value, 'Process')
    }
}

if ($BindHost -notin @('127.0.0.1', '::1', 'localhost')) {
    Write-Warning "控制面将监听 $BindHost。节点自注册应通过 HTTPS 反向代理访问；不要把 HTTP 管理接口直接暴露到公网。"
}

& (Join-Path $projectRoot '.venv\Scripts\python.exe') -m uvicorn app:app --host $BindHost --port $Port
exit $LASTEXITCODE
