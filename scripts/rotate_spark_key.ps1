param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9_.-]+@[A-Za-z0-9_.:-]+$')]
    [string]$Spark,

    [ValidatePattern('^[A-Za-z_][A-Za-z0-9_]*_AGENT_KEY$')]
    [string]$EnvironmentName = 'SPARK01_AGENT_KEY'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$state = Join-Path $projectRoot '.state'
$tokenFile = Join-Path $state "$EnvironmentName.token"
New-Item -ItemType Directory -Force -Path $state | Out-Null

$bytes = [byte[]]::new(32)
[Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
$token = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
[IO.File]::WriteAllText($tokenFile, $token, [Text.UTF8Encoding]::new($false))
icacls.exe $tokenFile /inheritance:r | Out-Null
icacls.exe $tokenFile /grant:r "$($env:USERDOMAIN)\$($env:USERNAME):(R,W)" `
    'NT AUTHORITY\SYSTEM:(F)' 'BUILTIN\Administrators:(F)' | Out-Null

scp -- $tokenFile "${Spark}:/tmp/model-serving-agent-key"
if ($LASTEXITCODE -ne 0) { throw '上传临时 Agent Key 失败' }

$rotate = @'
set -e
sudo sed -i '/^NODE_AGENT_API_KEY=/d' /etc/model-serving-agent.env
printf 'NODE_AGENT_API_KEY=' | sudo tee -a /etc/model-serving-agent.env >/dev/null
cat /tmp/model-serving-agent-key | sudo tee -a /etc/model-serving-agent.env >/dev/null
printf '\n' | sudo tee -a /etc/model-serving-agent.env >/dev/null
sudo chmod 600 /etc/model-serving-agent.env
rm -f /tmp/model-serving-agent-key
sudo systemctl restart model-serving-agent
sleep 2
sudo systemctl is-active model-serving-agent
curl -fsS http://127.0.0.1:9010/healthz
'@

ssh -t -- $Spark $rotate
if ($LASTEXITCODE -ne 0) { throw '轮换 Spark Agent Key 失败' }
Write-Host 'Spark Agent Key 已轮换并保存到本机受限文件。'
