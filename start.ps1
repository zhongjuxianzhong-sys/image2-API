# 启动脚本：启动原生桌面客户端。提供商地址与 API Key 在客户端内填写。
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

# 优先使用项目虚拟环境，其次使用 PATH 中的 Python，最后回退到 py 启动器。
$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (Test-Path -LiteralPath $venvPython) {
  $python = $venvPython
  $pythonArgs = @()
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
  $python = "python"
  $pythonArgs = @()
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
  $python = "py"
  $pythonArgs = @("-3")
} else {
  throw "未找到 Python。请先安装 Python 3.10+，或在本目录创建 .venv 后重试。"
}

# 需要走本机代理时，启动前先设置 $env:HTTP_PROXY / $env:HTTPS_PROXY。
Write-Host "正在启动 GPT 生图工坊桌面客户端..." -ForegroundColor Cyan
& $python @pythonArgs "client.py"