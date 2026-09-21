# 一键打包为 Windows 单文件程序。
# 产物：dist\Image2Studio.exe + dist\使用说明.txt
# 提供商地址与 API Key 只由使用者在网页界面填写：exe 内不含任何 key 与中转站地址，
# 源码目录的 .env 既不会被打包，也不会被复制到 dist。
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

# 优先使用项目虚拟环境；没有时优先使用 PATH 中的 Python，再回退到 py 启动器。
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

# 确保依赖已安装
& $python @pythonArgs -m pip install --disable-pip-version-check -r requirements.txt
& $python @pythonArgs -m pip install --disable-pip-version-check pyinstaller

# 根据客户端品牌标记生成多尺寸 Windows 图标。
& $python @pythonArgs "make_icon.py"
if ($LASTEXITCODE -ne 0) {
  throw "生成程序图标失败，退出代码：$LASTEXITCODE"
}

Write-Host "开始打包..." -ForegroundColor Cyan
& $python @pythonArgs -m PyInstaller --noconfirm --clean Image2Studio.spec
if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller 打包失败，退出代码：$LASTEXITCODE"
}

# 随程序分发使用说明。
Copy-Item -LiteralPath (Join-Path $Root "使用说明.txt") -Destination (Join-Path $Root "dist\使用说明.txt") -Force

# 防呆：dist 目录里不该出现任何 .env 配置文件。
$staleEnv = Join-Path $Root "dist\.env"
if (Test-Path -LiteralPath $staleEnv) {
  Remove-Item -LiteralPath $staleEnv -Force
  Write-Host "已删除 dist\.env（打包不携带任何 key）。" -ForegroundColor Yellow
}

# 旧版本曾用明文文件保存 Base URL；新版本统一使用加密凭据文件。
$staleSettings = Join-Path $Root "dist\client-settings.json"
if (Test-Path -LiteralPath $staleSettings) {
  Remove-Item -LiteralPath $staleSettings -Force
  Write-Host "已删除 dist\client-settings.json（旧版明文配置）。" -ForegroundColor Yellow
}

Write-Host "打包完成：$Root\dist\Image2Studio.exe" -ForegroundColor Green
Get-ChildItem -LiteralPath (Join-Path $Root "dist") -Force |
  Where-Object { -not $_.PSIsContainer } |
  ForEach-Object { Write-Host ("  {0}  {1:N0} 字节" -f $_.Name, $_.Length) }
Write-Host "分发 dist 目录内容即可；使用者双击 exe 后直接进入原生桌面客户端，关闭窗口即可退出。" -ForegroundColor Yellow
