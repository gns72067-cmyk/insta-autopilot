"""insta-autopilot 단일 진입점.

    python ia.py <command> [args...]

setup 스크립트가 만든 가상환경(<HOME>/.runtime/venv)이 있으면 그 파이썬으로 다시 실행한다.
"""
from __future__ import annotations

import os
import runpy
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMMANDS = {
    "rules_sync", "rules_compose", "doctor", "job", "setup_account", "collect_trends", "checks",
    "image_search", "image_fetch", "smart_crop", "render_card", "render_reel", "caption",
    "upload", "ig_api", "tempurl", "batch", "perf", "make", "web",
}


def _venv_python() -> Path | None:
    sys.path.insert(0, str(HERE))
    from common import paths

    p = paths.venv_python()
    return p if p.is_file() else None


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print("사용법: python ia.py <command> [args]\ncommand: " + ", ".join(sorted(COMMANDS)))
        return 2
    if sys.version_info < (3, 11):
        print(f"✘ Python 3.11 이상이 필요합니다 (현재 {sys.version.split()[0]})")
        return 2
    vp = None if os.environ.get("IA_NO_VENV") else _venv_python()
    if vp and Path(sys.executable).resolve() != vp.resolve() and not os.environ.get("IA_IN_VENV"):
        env = dict(os.environ, IA_IN_VENV="1", PYTHONIOENCODING="utf-8")
        return subprocess.call([str(vp), str(HERE / "ia.py"), *sys.argv[1:]], env=env)
    cmd = sys.argv[1]
    sys.argv = [str(HERE / f"{cmd}.py"), *sys.argv[2:]]
    try:
        runpy.run_path(str(HERE / f"{cmd}.py"), run_name="__main__")
    except SystemExit as e:
        if isinstance(e.code, int) or e.code is None:
            return e.code or 0
        print(e.code, flush=True)          # raise SystemExit("✘ …") 의 안내 문구를 보여 준다
        return 1
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    sys.exit(main())
