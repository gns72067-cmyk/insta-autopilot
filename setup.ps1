# insta-autopilot 설치 (Windows)
#   powershell -ExecutionPolicy Bypass -File setup.ps1 [-InstallFfmpeg]
# 하는 일: 가상환경(<데이터 폴더>\.runtime\venv) 생성 → 패키지 설치 → Chromium 설치 → ffmpeg 확인 → doctor
param([switch]$InstallFfmpeg)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

function Find-Python {
  foreach ($c in @(@("py", "-3"), @("python"), @("python3"))) {
    try {
      $exe = $c[0]; $rest = @($c | Select-Object -Skip 1)
      $v = & $exe @rest -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
      if ($LASTEXITCODE -eq 0 -and [version]$v -ge [version]"3.11") { return ,$c }
    } catch {}
  }
  return $null
}

$py = Find-Python
if (-not $py) {
  Write-Host "✘ Python 3.11 이상이 없습니다. 설치: winget install Python.Python.3.12  (설치 후 새 터미널에서 다시 실행)"
  exit 1
}
$pyExe = $py[0]; $pyArgs = @($py | Select-Object -Skip 1)
Write-Host "✔ Python: $(& $pyExe @pyArgs --version)"

$HomeDir = & $pyExe @pyArgs -c "import sys; sys.path.insert(0, r'$Root\scripts'); from common import paths; print(paths.home())"
$Venv = Join-Path $HomeDir ".runtime\venv"
Write-Host "· 데이터 폴더: $HomeDir"
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
  Write-Host "· 가상환경 생성: $Venv"
  New-Item -ItemType Directory -Force (Split-Path $Venv) | Out-Null
  & $pyExe @pyArgs -m venv $Venv
}
$VPy = Join-Path $Venv "Scripts\python.exe"
& $VPy -m pip install --upgrade pip -q
& $VPy -m pip install -q -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { Write-Host "✘ 패키지 설치 실패"; exit 1 }
Write-Host "✔ 패키지 설치"
& $VPy -m playwright install chromium
if ($LASTEXITCODE -ne 0) { Write-Host "✘ Chromium 설치 실패"; exit 1 }
Write-Host "✔ Chromium"

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
  if ($InstallFfmpeg) {
    winget install --id Gyan.FFmpeg -e --accept-source-agreements
    Write-Host "· ffmpeg 설치 후 새 터미널을 열어야 PATH 가 반영됩니다."
  } else {
    Write-Host "⚠ ffmpeg 없음 → 설치: winget install Gyan.FFmpeg  (또는 setup.ps1 -InstallFfmpeg)"
  }
}

& $VPy (Join-Path $Root "scripts\ia.py") doctor
exit $LASTEXITCODE
