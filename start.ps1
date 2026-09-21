# 启动脚本：启动原生桌面客户端。提供商地址与 API Key 在客户端内填写。
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

# 需要走本机代理时，启动前先设置 $env:HTTP_PROXY / $env:HTTPS_PROXY。
Write-Host "正在启动 Image2 生图工坊桌面客户端..." -ForegroundColor Cyan
python "client.py"
