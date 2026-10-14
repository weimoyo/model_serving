param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9_.-]+@[A-Za-z0-9_.:-]+$')]
    [string]$Spark,

    [ValidateSet('https://huggingface.co', 'https://hf-mirror.com')]
    [string]$HfEndpoint = 'https://huggingface.co',

    [switch]$BuildRuntime
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$agent = Join-Path $projectRoot 'node_agent.py'
$service = Join-Path $projectRoot 'deploy\model-serving-agent.service'

scp -- $agent "${Spark}:/tmp/model-serving-node-agent.py"
if ($LASTEXITCODE -ne 0) { throw '上传 node_agent.py 失败' }
scp -- $service "${Spark}:/tmp/model-serving-agent.service"
if ($LASTEXITCODE -ne 0) { throw '上传 systemd unit 失败' }
if ($BuildRuntime) {
    scp -r -- (Join-Path $projectRoot 'runtime\llm-vllm-ssh') "${Spark}:/tmp/"
    if ($LASTEXITCODE -ne 0) { throw '上传 vLLM runtime 构建目录失败' }
}

$install = @"
set -e
sudo install -m 0644 /tmp/model-serving-node-agent.py /opt/model-serving/node_agent.py
sudo install -m 0644 /tmp/model-serving-agent.service /etc/systemd/system/model-serving-agent.service
sudo install -d -m 0755 /srv/model-serving/models /srv/model-serving/workspaces
sudo /opt/model-serving/.venv/bin/pip install 'huggingface-hub>=0.27,<2'
sudo sed -i '/^HF_ENDPOINT=/d' /etc/model-serving-agent.env
sudo sed -i '/^HF_HUB_DISABLE_XET=/d' /etc/model-serving-agent.env
printf '%s\n' 'HF_ENDPOINT=$HfEndpoint' | sudo tee -a /etc/model-serving-agent.env >/dev/null
printf '%s\n' 'HF_HUB_DISABLE_XET=1' | sudo tee -a /etc/model-serving-agent.env >/dev/null
sudo systemctl daemon-reload
sudo systemctl restart model-serving-agent
sleep 2
sudo systemctl is-active model-serving-agent
curl -fsS http://127.0.0.1:9010/healthz
"@

ssh -t -- $Spark $install
if ($LASTEXITCODE -ne 0) { throw 'Spark Agent 更新或健康检查失败' }

if ($BuildRuntime) {
    $build = @'
set -e
cd /tmp/llm-vllm-ssh
sudo docker build -t model-serving/vllm-ssh:spark .
sudo docker image inspect model-serving/vllm-ssh:spark --format '{{.Id}}'
'@
    ssh -t -- $Spark $build
    if ($LASTEXITCODE -ne 0) { throw 'Spark vLLM runtime 构建失败' }
}

Write-Host 'Spark Node Agent 已更新；请回到本机控制面执行节点 probe。'
