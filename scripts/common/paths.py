"""모든 경로는 이 모듈로만 만든다.

- 플러그인(코드) 폴더: PLUGIN_ROOT — 업데이트·재설치로 바뀔 수 있음. 이용자 데이터를 두지 않는다.
- 이용자 데이터 폴더(HOME): 우선순위
    1) 환경변수 INSTA_AUTOPILOT_HOME
    2) 위치 포인터 파일(셋팅 마법사가 기록) — <OS 설정 폴더>/insta-autopilot/home.txt
    3) 기본값 ~/Documents/InstaAutopilot
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
FONTS_DIR = PLUGIN_ROOT / "assets" / "fonts"
MODELS_DIR = PLUGIN_ROOT / "models"
KEYS_DIR = PLUGIN_ROOT / "keys"

ENV_HOME = "INSTA_AUTOPILOT_HOME"
HANDLE_RE = re.compile(r"^[a-z0-9._]{1,30}$")


# ---------------------------------------------------------------- 위치 포인터
def os_config_dir() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "insta-autopilot"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "insta-autopilot"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "insta-autopilot"


def pointer_file() -> Path:
    return os_config_dir() / "home.txt"


def default_home() -> Path:
    return Path.home() / "Documents" / "InstaAutopilot"


def home() -> Path:
    env = os.environ.get(ENV_HOME)
    if env:
        return Path(env).expanduser().resolve()
    pf = pointer_file()
    if pf.is_file():
        txt = pf.read_text(encoding="utf-8").strip()
        if txt:
            return Path(txt).expanduser().resolve()
    return default_home()


def set_home(path: Path) -> Path:
    """셋팅 마법사에서 고른 데이터 폴더를 기록한다(환경변수가 있으면 환경변수가 우선)."""
    path = Path(path).expanduser().resolve()
    if path == PLUGIN_ROOT or PLUGIN_ROOT in path.parents:
        raise ValueError("데이터 폴더를 플러그인 폴더 안에 둘 수 없습니다 (업데이트 때 지워짐)")
    path.mkdir(parents=True, exist_ok=True)
    pf = pointer_file()
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(str(path), encoding="utf-8")
    return path


# ---------------------------------------------------------------- 데이터 폴더 구조
def config_file() -> Path:
    return home() / "config.yaml"


def accounts_dir() -> Path:
    return home() / "accounts"


def normalize_handle(handle: str) -> str:
    h = (handle or "").strip().lstrip("@").lower()
    if not HANDLE_RE.fullmatch(h):
        raise ValueError(f"인스타 핸들 형식이 아닙니다: {handle!r} (영문 소문자·숫자·._ 30자 이내)")
    return h


def account_dir(handle: str) -> Path:
    return accounts_dir() / normalize_handle(handle)


def profile_file(handle: str) -> Path:
    return account_dir(handle) / "profile.yaml"


def account_assets_dir(handle: str) -> Path:
    return account_dir(handle) / "assets"


def library_dirs(handle: str | None = None) -> list[Path]:
    dirs = [home() / "library"]
    if handle:
        dirs.append(account_dir(handle) / "library")
    return dirs


def rules_cache_dir() -> Path:
    return home() / "cache" / "rules"


def rules_state_file() -> Path:
    return rules_cache_dir() / "state.json"


def rules_version_dir(version: str) -> Path:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version or ""):
        raise ValueError(f"잘못된 규칙 버전: {version!r}")
    return rules_cache_dir() / version


def output_root() -> Path:
    return home() / "output"


def today_str(now: dt.datetime | None = None) -> str:
    return (now or dt.datetime.now()).strftime("%Y-%m-%d")


def day_dir(handle: str, date: str | None = None) -> Path:
    return output_root() / (date or today_str()) / normalize_handle(handle)


def slugify(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[\\/:*?\"<>|\s]+", "-", (text or "").strip())
    s = re.sub(r"[^\w\-가-힣]", "", s)
    s = re.sub(r"-{2,}", "-", s).strip("-._")
    return (s[:max_len].rstrip("-._")) or "untitled"


def new_job_dir(handle: str, slug: str, date: str | None = None) -> Path:
    d = day_dir(handle, date)
    d.mkdir(parents=True, exist_ok=True)
    nums = [int(m.group(1)) for p in d.iterdir() if p.is_dir() and (m := re.match(r"^(\d{2,})_", p.name))]
    n = (max(nums) + 1) if nums else 1
    job = d / f"{n:02d}_{slugify(slug)}"
    job.mkdir()
    return job


def runtime_dir() -> Path:
    """venv 등 실행 환경 (숨김 폴더)."""
    return home() / ".runtime"


def venv_dir() -> Path:
    return runtime_dir() / "venv"


def venv_python() -> Path:
    v = venv_dir()
    return v / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")


def logs_dir() -> Path:
    return home() / "logs"


def ensure_home_layout() -> Path:
    h = home()
    for p in (h, accounts_dir(), h / "library", rules_cache_dir(), output_root(), logs_dir()):
        p.mkdir(parents=True, exist_ok=True)
    return h


# ---------------------------------------------------------------- 파일 유틸
def write_text_atomic(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path: Path, data) -> None:
    write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path, default=None):
    path = Path(path)
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def file_url(path: Path) -> str:
    return Path(path).resolve().as_uri()
