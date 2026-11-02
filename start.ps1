$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    py -3 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "创建 Python 环境失败" }
}
& .venv\Scripts\python.exe -m pip install -r requirements.lock
if ($LASTEXITCODE -ne 0) { throw "安装依赖失败" }
if (-not (Test-Path ".env")) { Copy-Item .env.example .env }
& .venv\Scripts\python.exe run.py
