# 容器级离线校验：复刻 plugin_daemon 环境初始化（--network none）
# 用法: .\container-offline-check.ps1 -Pkg <离线包.difypkg> -Modules dify_plugin,bs4 [-Image langgenius/dify-plugin-daemon:0.6.10-local]
param(
    [Parameter(Mandatory=$true)][string]$Pkg,
    [Parameter(Mandatory=$true)][string]$Modules,
    [string]$Image = "langgenius/dify-plugin-daemon:0.6.10-local",
    [string]$Work = (Join-Path $env:TEMP "dify-offline-e2e")
)
$ErrorActionPreference = "Continue"   # docker stderr progress lines must not abort the script

# 1) extract package
Remove-Item $Work -Recurse -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $Work | Out-Null
python -m zipfile -e $Pkg $Work | Out-Null
$mods = @($Modules -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })

# 2) check.py imports the requested modules inside the venv
$pyBody = @"
import sys
ok = True
for m in sys.argv[1:]:
    try:
        __import__(m)
        print("IMPORT-OK", m)
    except Exception as e:
        ok = False
        print("IMPORT-FAIL", m, type(e).__name__, e)
sys.exit(0 if ok else 1)
"@
[System.IO.File]::WriteAllText((Join-Path $Work "check.py"), $pyBody)

$shText = ("#!/bin/sh" + "`n" +
  "set -e" + "`n" +
  "cd /p" + "`n" +
  "rm -rf /p/.venv" + "`n" +
  "uv venv .venv --python 3.12 >/dev/null" + "`n" +
  "uv pip install --python /p/.venv/bin/python --no-index --find-links=/p/wheels -r /p/requirements.txt" + "`n" +
  "/p/.venv/bin/python /lab/check.py $($mods -join ' ')" + "`n" +
  "echo CONTAINER-OFFLINE-PASS" + "`n")
[System.IO.File]::WriteAllText((Join-Path $Work "check.sh"), $shText)

# 3) run fully offline in the target daemon image
docker run --rm --network none -v "${Work}:/p" -v "${Work}:/lab" --entrypoint sh $Image /lab/check.sh
if ($LASTEXITCODE -ne 0) { Write-Error "container offline check FAILED (exit $LASTEXITCODE)" }
Write-Output "e2e container-offline PASS for $Pkg"
