"""빠른 모드 — 훅·사진이 준비된 작업을 카드·릴스까지 한 번에 만든다.

    python make.py <job_dir> (--photo N | --none) [--hook strong|normal|soft] [--variant A|B|C]

전제: factsheet.json(검사 통과)·hooks.json·photo_candidates.json 이 있다 (조사·훅 쓰기·사진 검색은 Claude 몫).
순서: 훅 자동 선택(검사 통과 버전 중 recommended 우선) → 선택 훅 재검사 → 사진 받기 → 카드 → 릴스.
자동으로 고른 항목은 meta.auto 에 남겨 검토 화면에 "자동 선택"으로 표시한다.
캡션은 이 명령이 만들지 않는다(Claude 가 초안 → caption draft).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import config as cfgmod  # noqa: E402
from common import paths  # noqa: E402
from common.rules import compose, get  # noqa: E402

HERE = Path(__file__).resolve().parent
ORDER = ["strong", "normal", "soft"]


def ia(*args: str, capture: bool = True) -> subprocess.CompletedProcess:
    """같은 파이썬으로 ia.py 하위 명령 실행 (출력은 utf-8)."""
    import os

    env = dict(os.environ, PYTHONIOENCODING="utf-8", IA_IN_VENV="1")
    return subprocess.run([sys.executable, str(HERE / "ia.py"), *args], capture_output=capture, text=True,
                          encoding="utf-8", errors="replace", env=env)


def choose_hook(job: Path, prefer: str | None = None) -> tuple[str, dict]:
    import checks

    meta = paths.read_json(job / "meta.json")
    hooks = paths.read_json(job / "hooks.json")
    fs = paths.read_json(job / "factsheet.json")
    if not hooks or not fs:
        raise SystemExit("✘ hooks.json·factsheet.json 이 필요합니다")
    c = compose(cfgmod.load_profile(meta["handle"]), (meta.get("rules") or {}).get("version"))
    res = checks.check_hooks(hooks, fs, meta, c)
    paths.write_json(job / "checks_hook.json", res)
    versions = list(get(c["checks"], "hook.versions", ORDER))
    passing = [v for v in versions if (res["versions"].get(v) or {}).get("ok")]
    if prefer:
        if prefer not in passing:
            raise SystemExit(f"✘ {prefer} 버전이 검사를 통과하지 못했습니다")
        return prefer, res
    if not passing:
        raise SystemExit("✘ 검사를 통과한 훅 버전이 없습니다 — hooks.json 을 고친 뒤 다시")
    rec = hooks.get("recommended")
    return (rec if rec in passing else passing[0]), res


def make(job: Path, photo: int | None, hook: str | None = None, variant: str | None = None, log=print) -> dict:
    meta = paths.read_json(job / "meta.json")
    if not meta:
        raise SystemExit(f"✘ meta.json 없음: {job}")
    auto = {}
    # 1 훅
    if hook or not meta.get("hook"):
        v, _ = choose_hook(job, hook)
        r = ia("job", "select-hook", str(job), v)
        if r.returncode:
            raise SystemExit(r.stdout + r.stderr)
        if not hook:
            auto["hook"] = v
        log(f"✔ 훅: {v}{' (자동 선택)' if not hook else ''}")
    r = ia("checks", "hook", str(job), "--selected")
    if r.returncode:
        raise SystemExit("✘ 선택 훅 검사 실패\n" + r.stdout)
    ia("job", "stage", str(job), "hooks", "approved", "--note", "빠른 모드")
    # 2 사진
    r = ia("image_fetch", str(job), *(["--none"] if photo is None else ["--pick", str(photo)]))
    if r.returncode:
        raise SystemExit("✘ 사진 받기 실패\n" + r.stdout + r.stderr)
    auto["photo"] = "C" if photo is None else photo
    ia("job", "stage", str(job), "photo", "approved", "--note", "빠른 모드")
    log(f"✔ 사진: {'없음(C 템플릿)' if photo is None else f'후보 {photo}번'}")
    # 3 카드
    r = ia("render_card", str(job), *(["--variant", variant] if variant else []))
    log(r.stdout.strip())
    if r.returncode not in (0, 1):
        raise SystemExit("✘ 카드 렌더 실패\n" + r.stderr[-800:])
    # 4 릴스
    r = ia("render_reel", str(job))
    log(r.stdout.strip())
    if r.returncode:
        raise SystemExit("✘ 릴스 렌더 실패\n" + (r.stdout + r.stderr)[-800:])
    meta = paths.read_json(job / "meta.json")
    meta["auto"] = {**(meta.get("auto") or {}), **auto, "mode": "quick"}
    meta.setdefault("stages", {})["card"] = "waiting_user"
    paths.write_json(job / "meta.json", meta)
    return {"job": str(job), "auto": meta["auto"], "card_ok": (meta.get("card") or {}).get("ok"),
            "reel_ok": (meta.get("reel") or {}).get("ok")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("job", type=Path)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--photo", type=int, help="photo_candidates.json 의 후보 번호")
    g.add_argument("--none", action="store_true", help="사진 없이 C 템플릿")
    ap.add_argument("--hook", choices=ORDER, help="자동 선택 대신 이 버전")
    ap.add_argument("--variant", choices=["A", "B", "C"])
    a = ap.parse_args()
    r = make(a.job, None if a.none else a.photo, a.hook, a.variant)
    print(f"✔ 카드·릴스 완료 — 다음: 캡션 초안 → ia caption draft → ia web open \"{a.job}\"")
    print(json.dumps(r, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
