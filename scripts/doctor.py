"""실행 환경 점검 — 의존성·폰트·얼굴 모델·ffmpeg·브라우저·데이터 폴더·규칙 URL.

    python doctor.py [--json] [--offline]
종료 코드: 0 = 모두 통과(경고 가능), 1 = 필수 항목 실패
"""
from __future__ import annotations

import argparse
import base64
import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import config as cfgmod  # noqa: E402
from common import paths  # noqa: E402

PACKAGES = {"yaml": "pyyaml", "requests": "requests", "cryptography": "cryptography", "playwright": "playwright",
            "cv2": "opencv-python-headless", "numpy": "numpy", "PIL": "pillow", "feedparser": "feedparser"}
FONT_FILE = paths.FONTS_DIR / "PretendardVariable.woff2"
FACE_MODEL = paths.MODELS_DIR / "face_detection_yunet_2023mar.onnx"


def run_checks(offline: bool = False) -> list[dict]:
    res: list[dict] = []

    def add(name, ok, detail="", required=True, fix=""):
        res.append({"name": name, "ok": bool(ok), "required": required, "detail": detail, "fix": fix})

    v = sys.version_info
    add("Python 3.11+", v >= (3, 11), f"{v.major}.{v.minor}.{v.micro} ({sys.executable})", fix="Python 3.11 이상 설치")

    missing = []
    for mod, pkg in PACKAGES.items():
        try:
            importlib.import_module(mod)
        except Exception:
            missing.append(pkg)
    add("파이썬 패키지", not missing, "모두 설치됨" if not missing else "없음: " + ", ".join(missing),
        fix="setup 스크립트 실행 (setup.ps1 / setup.sh)")

    ff, fp = shutil.which("ffmpeg"), shutil.which("ffprobe")
    x264 = False
    if ff:
        try:
            out = subprocess.run([ff, "-hide_banner", "-encoders"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20).stdout
            x264 = "libx264" in out
        except Exception:
            pass
    add("ffmpeg·ffprobe (libx264)", ff and fp and x264,
        f"{ff or '없음'} / {fp or '없음'} / libx264 {'있음' if x264 else '없음'}",
        fix="Windows: winget install Gyan.FFmpeg · macOS: brew install ffmpeg")

    if "playwright" not in missing:
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                b = p.chromium.launch()
                ver = b.version
                b.close()
            add("Playwright Chromium", True, ver)
        except Exception as e:
            add("Playwright Chromium", False, str(e).splitlines()[0][:160], fix="python -m playwright install chromium")
    else:
        add("Playwright Chromium", False, "playwright 패키지 없음", fix="setup 스크립트 실행")

    add("폰트 (Pretendard, OFL)", FONT_FILE.is_file() and FONT_FILE.stat().st_size > 500_000, str(FONT_FILE),
        fix="플러그인 재설치 또는 setup 스크립트 실행")
    add("얼굴 검출 모델 (YuNet)", FACE_MODEL.is_file(), str(FACE_MODEL), required=False,
        fix="없으면 OpenCV 기본 검출기로 대체됩니다")

    try:
        from common.rules import EMBEDDED_PUBKEY_B64

        pem = (paths.KEYS_DIR / "rules_pubkey.pem").read_text(encoding="ascii")
        body = "".join(l for l in pem.splitlines() if "-----" not in l)
        same = base64.b64decode(body)[-32:] == base64.b64decode(EMBEDDED_PUBKEY_B64)
        add("내장 공개키", same, "keys/rules_pubkey.pem 과 코드 상수 일치" if same else "불일치")
    except Exception as e:
        add("내장 공개키", False, str(e))

    h = paths.home()
    try:
        h.mkdir(parents=True, exist_ok=True)
        t = h / ".doctor-write-test"
        t.write_text("ok", encoding="utf-8")
        t.unlink()
        add("데이터 폴더 쓰기", True, str(h))
    except OSError as e:
        add("데이터 폴더 쓰기", False, f"{h}: {e}", fix="INSTA_AUTOPILOT_HOME 을 쓸 수 있는 폴더로 지정")
    add("설정 파일", paths.config_file().is_file(), str(paths.config_file()), required=False,
        fix="insta-setup 으로 만들기")
    add("등록된 계정", bool(cfgmod.list_accounts()), ", ".join("@" + a for a in cfgmod.list_accounts()) or "없음",
        required=False, fix="insta-setup 으로 계정 추가")

    cfg = cfgmod.load_config()
    from setup_account import valid_email

    contact = (cfg.get("contact") or "").strip()
    add("Wikimedia 연락처(본인 이메일)", valid_email(contact), contact or "비어 있음 — Wikimedia 사진 검색을 건너뜀",
        required=False, fix="insta-setup 2단계에서 본인 이메일 입력")
    keys = cfg.get("api_keys") or {}
    srcs = ["라이브러리"] + (["Wikimedia(공공누리 포함)"] if valid_email(contact) else []) + ["Flickr CC"] \
        + [n for n, k in (("Unsplash", "unsplash"), ("Pexels", "pexels")) if keys.get(k)]         + (["언론사 기사 사진"] if (cfg.get("press_photos") or {}).get("enabled") else [])
    add("사진 검색 출처", True, " · ".join(srcs), required=False,
        fix="" if keys.get("unsplash") or keys.get("pexels") else "무료 키 등록 시 더 다양: setup_account set-key unsplash")
    if not offline:
        try:
            import rules_sync
            from common.rules import open_envelope

            base = rules_sync.check_base(cfg["rules_url"])
            ch = cfg.get("channel", "stable")
            ptr = open_envelope(rules_sync.http_fetch(f"{base}/{ch}/latest.json", base, True), "latest.json")
            add("규칙 URL", True, f"{base} — {ch} {ptr.get('version')} 서명 OK")
        except Exception as e:
            add("규칙 URL", False, f"{cfg['rules_url']}: {e}", required=False,
                fix="네트워크 확인 (캐시가 있으면 캐시로 진행)")
    try:
        from common.rules import active_version, verify_cached

        av = active_version()
        if av:
            verify_cached(av)
        add("규칙 캐시", bool(av), f"{av} 검증 OK" if av else "아직 받지 않음", required=False,
            fix="rules_sync 실행")
    except Exception as e:
        add("규칙 캐시", False, str(e), required=False, fix="rules_sync 실행 (자동으로 다시 받음)")
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--offline", action="store_true")
    a = ap.parse_args()
    res = run_checks(a.offline)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        for r in res:
            mark = "✔" if r["ok"] else ("✘" if r["required"] else "⚠")
            print(f"{mark} {r['name']}: {r['detail']}" + ("" if r["ok"] or not r["fix"] else f"  → {r['fix']}"))
    bad = [r for r in res if r["required"] and not r["ok"]]
    print(f"\n{'✘ 필수 항목 ' + str(len(bad)) + '개 실패' if bad else '✔ 실행 준비 완료'}") if not a.json else None
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
