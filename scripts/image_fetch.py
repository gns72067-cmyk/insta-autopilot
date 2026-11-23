"""선택한 사진 후보 다운로드 + credits.txt + meta.photo 기록.

    python image_fetch.py <job_dir> --pick N [--credit "직접 지정한 출처 문구"]
    python image_fetch.py <job_dir> --none        # 사진 없이 C 템플릿

- 다운로드도 image_search.ALLOWED_HOSTS 안에서만.
- Unsplash 는 API 가이드에 따라 download_location 을 호출한다.
- CC BY-SA 사진은 크롭하므로 캡션 사진 출처 줄 끝에 "(편집)" 을 붙인다(image 규칙 5절).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import config as cfgmod  # noqa: E402
from common import paths  # noqa: E402
from image_search import guarded_get, ua  # noqa: E402

MAX_LONG_SIDE = 3840     # Wikimedia 표준 썸네일 폭 중 가장 큰 값


def _ext(content_type: str, url: str) -> str:
    ct = (content_type or "").lower()
    if "png" in ct:
        return ".png"
    if "webp" in ct:
        return ".webp"
    return ".jpg"


def fetch(job: Path, pick: int, credit_override: str | None = None) -> dict:
    meta = paths.read_json(job / "meta.json")
    pc = paths.read_json(job / "photo_candidates.json")
    if not meta or not pc:
        raise SystemExit("✘ meta.json / photo_candidates.json 이 없습니다 (image_search 먼저)")
    cand = next((c for c in pc["candidates"] if c["no"] == pick), None)
    if not cand:
        raise SystemExit(f"✘ 후보 {pick}번이 없습니다")
    cfg = cfgmod.load_config()
    for old in job.glob("photo_original.*"):
        old.unlink()

    if cand["source"] == "library":
        src = Path(cand["local_path"])
        dest = job / f"photo_original{src.suffix.lower()}"
        shutil.copyfile(src, dest)
        file_url = None
    else:
        from image_search import PRESS_UA

        headers = ({"User-Agent": PRESS_UA, "Referer": cand["referer"]} if cand.get("referer")
                   else {"User-Agent": ua(cfg)})                      # 이메일 연락처는 Wikimedia 에만
        url = cand.get("file_url")
        if cand["source"] in ("wikimedia", "kogl1"):
            # 원본이 너무 크면 긴 변 MAX_LONG_SIDE 의 썸네일을 받는다
            w, h = cand.get("width") or 0, cand.get("height") or 0
            if w > MAX_LONG_SIDE and cand.get("wikimedia_title"):
                width = MAX_LONG_SIDE
                r = guarded_get("https://commons.wikimedia.org/w/api.php", headers=headers, params={
                    "action": "query", "format": "json", "formatversion": "2", "titles": cand["wikimedia_title"],
                    "prop": "imageinfo", "iiprop": "url", "iiurlwidth": str(width)})
                ii = r.json()["query"]["pages"][0]["imageinfo"][0]
                url = ii.get("thumburl") or url
        elif cand["source"] == "unsplash":
            key = (cfg.get("api_keys") or {}).get("unsplash")
            if cand.get("download_location") and key:
                guarded_get(cand["download_location"], headers={"Authorization": f"Client-ID {key}"})
            url = f"{cand['file_url']}&w=2400&q=90&fm=jpg" if "?" in cand["file_url"] else f"{cand['file_url']}?w=2400&q=90&fm=jpg"
        r = guarded_get(url, headers=headers, extra_hosts=cand.get("press_hosts"))
        dest = job / f"photo_original{_ext(r.headers.get('Content-Type', ''), url)}"
        dest.write_bytes(r.content)
        file_url = url

    credit = credit_override or cand["credit"]
    caption_line = credit + (" (편집)" if cand.get("share_alike") else "")
    photo = {"path": str(dest), "credit": credit, "caption_credit": caption_line, "source": cand["source"],
             "license": cand.get("license"), "license_url": cand.get("license_url"), "author": cand.get("author"),
             "page_url": cand.get("page_url"), "file_url": file_url, "title": cand.get("title"), "pick": pick,
             "fetched_at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    lines = ["# 사진 출처 (카드 credit 슬롯 · 캡션 사진 출처 줄)", f"카드 credit: {credit}", f"캡션 출처 줄: {caption_line}",
             f"출처 종류: {cand['source']}", f"라이선스: {cand.get('license')}" + (f" ({cand['license_url']})" if cand.get("license_url") else ""),
             f"저작자: {cand.get('author')}", f"원본 페이지: {cand.get('page_url') or '이용자 라이브러리'}",
             f"파일: {file_url or cand.get('local_path')}", f"받은 시각: {photo['fetched_at']}"]
    if cand.get("share_alike"):
        lines.append("주의: CC BY-SA — 크롭·보정본도 같은 라이선스로 공유됨. 캡션 출처 줄에 (편집) 표기.")
    paths.write_text_atomic(job / "credits.txt", "\n".join(lines) + "\n")
    meta["photo"] = photo
    if meta.get("template") == "C":
        meta["template"] = None
    meta.setdefault("stages", {})["photo"] = "approved"
    paths.write_json(job / "meta.json", meta)
    return photo


def choose_none(job: Path) -> None:
    meta = paths.read_json(job / "meta.json")
    meta["photo"] = None
    meta["template"] = "C"
    meta.setdefault("stages", {})["photo"] = "skipped"
    paths.write_json(job / "meta.json", meta)
    (job / "credits.txt").unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("job", type=Path)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--pick", type=int)
    g.add_argument("--none", action="store_true")
    ap.add_argument("--credit")
    a = ap.parse_args()
    if a.none:
        choose_none(a.job)
        print("✔ 사진 없이 C 템플릿으로 진행")
        return 0
    p = fetch(a.job, a.pick, a.credit)
    print(f"✔ 사진 저장: {p['path']}")
    print(f"  credit: {p['credit']}")
    print(f"  credits.txt: {a.job / 'credits.txt'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
