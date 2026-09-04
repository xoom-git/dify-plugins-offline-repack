# 启动 dify-offline-repack Web（默认 http://127.0.0.1:8010）
param([int]$Port = 8010, [string]$Host0 = "0.0.0.0")
$py = Join-Path $PSScriptRoot ".venv-dev\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Error "dev venv 不存在，请先: py -3.10 -m venv .venv-dev && .venv-dev\Scripts\pip install -r dify-offline-web\requirements.txt"; exit 1 }
Push-Location (Join-Path $PSScriptRoot "dify-offline-web")
& $py -m uvicorn app:app --host $Host0 --port $Port
Pop-Location
