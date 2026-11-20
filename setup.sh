#!/usr/bin/env bash
# insta-autopilot 설치 (macOS / Linux)
#   bash setup.sh [--install-ffmpeg]
# 하는 일: 가상환경(<데이터 폴더>/.runtime/venv) 생성 → 패키지 설치 → Chromium 설치 → ffmpeg 확인 → doctor
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
export PYTHONIOENCODING=utf-8

PY=""
for c in python3.13 python3.12 python3.11 python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "✘ Python 3.11 이상이 없습니다. macOS: brew install python@3.12"; exit 1
fi
echo "✔ Python: $($PY --version)"

HOME_DIR="$($PY -c "import sys; sys.path.insert(0, '$ROOT/scripts'); from common import paths; print(paths.home())")"
VENV="$HOME_DIR/.runtime/venv"
echo "· 데이터 폴더: $HOME_DIR"
if [ ! -x "$VENV/bin/python" ]; then
  echo "· 가상환경 생성: $VENV"
  mkdir -p "$(dirname "$VENV")"
  "$PY" -m venv "$VENV"
fi
VPY="$VENV/bin/python"
"$VPY" -m pip install --upgrade pip -q
"$VPY" -m pip install -q -r "$ROOT/requirements.txt"
echo "✔ 패키지 설치"
"$VPY" -m playwright install chromium
echo "✔ Chromium"

if ! command -v ffmpeg >/dev/null 2>&1; then
  if [ "${1:-}" = "--install-ffmpeg" ] && command -v brew >/dev/null 2>&1; then
    brew install ffmpeg
  else
    echo "⚠ ffmpeg 없음 → 설치: brew install ffmpeg (macOS) / sudo apt install ffmpeg (Ubuntu)"
  fi
fi

"$VPY" "$ROOT/scripts/ia.py" doctor
