"""사진 후보 검색 — image 규칙의 화이트리스트 출처만.

    python image_search.py <job_dir> --query "검색어" [--query "english term" ...] [--max 5]

출처: ① 이용자 라이브러리(library/, accounts/<h>/library/ — 사이드카 yaml 에 owner 필수)
      ② 공공누리 제1유형 (Wikimedia Commons 에 KOGL Type 1 로 올라온 정부·기관 사진 — 파일별 라이선스 확인됨)
      ③ Wikimedia Commons (CC0 · CC BY · CC BY-SA)
      ④ Flickr CC (Openverse 검색 — CC0 · CC BY · CC BY-SA, 정부·기관 계정 제외) — 키 불필요
      ⑤ Unsplash / Pexels (config.yaml 에 API 키가 있을 때만)
      ⑥ 언론사 기사 사진 — 이용자가 '언론사 사진 사용'을 켰을 때(config press_photos.enabled, 사용 책임은 이용자),
         팩트 시트 출처 기사의 대표 사진. 규칙(checks image.allow_press)이 허용할 때만.
- 사진은 기본 꽉 채우기(fill). 원본이 작아 많이 키워야 하면 선명도 보정, 너무 작으면 뺀다(image.fill_max_upscale).
- 순서: **검색어 관련도** 우선(앞 검색어일수록 구체적이라 가중), 출처 우선순위는 동점일 때만. 한 검색어가 후보를 독차지하지 않게 나눈다.
- 9:16 로 자를 때 확대가 너무 큰(흐려지는) 사진은 뺀다(checks image.max_upscale, 기본 1.6).
- 네트워크 요청은 ALLOWED_HOSTS 의 호스트로만 나간다(리디렉션 포함). 그 외 도메인은 코드에서 차단.
- Wikimedia 요청에는 config.yaml 의 contact 를 넣은 User-Agent 를 쓴다(없으면 Wikimedia 건너뜀).
- 후보마다 9:16·4:5 크롭 미리보기를 만들고, 얼굴이 피사체 대역에 들어오지 않는 후보는 뺀다.
결과: photo_candidates.json, photo_candidates/ (미리보기), photo_candidates.png (번호 붙은 모아보기)
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import config as cfgmod  # noqa: E402
from common import log, paths  # noqa: E402
from common.rules import compose, get, plugin_version  # noqa: E402
import smart_crop  # noqa: E402

L = log.get("image")
ALLOWED_HOSTS = {
    "commons.wikimedia.org", "upload.wikimedia.org", "thumb.wikimedia.org",
    "api.unsplash.com", "images.unsplash.com",
    "api.pexels.com", "images.pexels.com",
    "api.openverse.org", "live.staticflickr.com",
}
ALLOWED_SUFFIXES = (".staticflickr.com",)   # farm1.staticflickr.com 등
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
CC_RE = re.compile(r"^(CC0|CC[ -]BY(?:[ -]SA)?(?:[ -]\d(?:\.\d)?)?(?:[ -][A-Za-z]{2,})?)$", re.I)
KOGL1_RE = re.compile(r"^KOGL[ -]?(Type)?[ -]?1$", re.I)
MAX_BYTES = 40 * 1024 * 1024
WIKI_THUMB_PREVIEW = 960          # Wikimedia 표준 썸네일 폭 (20·40·60·120·250·330·500·960·1280·1920·3840)
MIN_INTERVAL = {"wikimedia.org": 1.0, "unsplash.com": 0.3, "pexels.com": 0.3, "openverse.org": 3.2, "staticflickr.com": 0.3}
_last_req: dict[str, float] = {}


PRESS_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def press_enabled(cfg: dict | None = None) -> bool:
    """이용자가 '언론사 사진 사용'을 켰는지 (config.yaml press_photos.enabled + 확인 문구)."""
    cfg = cfg if cfg is not None else cfgmod.load_config()
    pp = cfg.get("press_photos") or {}
    return bool(pp.get("enabled") and pp.get("confirm_text"))


NOT_PRESS_RE = re.compile(r"(?i)\.(go|or|ac|mil)\.kr$|\.gov(\.\w+)?$|wikipedia\.org$|youtube\.com$|instagram\.com$|facebook\.com$")


def _throttle(host: str) -> None:
    import time

    for dom, gap in MIN_INTERVAL.items():
        if host == dom or host.endswith("." + dom):
            wait = _last_req.get(dom, 0) + gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last_req[dom] = time.monotonic()


class BlockedHost(Exception):
    pass


def guarded_get(url: str, *, headers=None, params=None, timeout=30, max_bytes=MAX_BYTES, extra_hosts=None):
    """화이트리스트 호스트만 허용(+ extra_hosts: 이번 요청에만 허용하는 언론사 기사·사진 호스트). 리디렉션도 직접 확인."""
    import time

    import requests

    retries = 2
    for _ in range(8):
        u = urlsplit(url)
        extra = {h.lower() for h in (extra_hosts or ())}
        host = (u.hostname or "").lower()
        if u.scheme not in ("https", "http") or (u.scheme == "http" and host not in extra) \
                or not (host in ALLOWED_HOSTS or host.endswith(ALLOWED_SUFFIXES) or host in extra):
            raise BlockedHost(f"허용되지 않은 출처: {u.hostname}")
        _throttle(u.hostname)
        r = requests.get(url, headers=headers, params=params, timeout=timeout, allow_redirects=False, stream=True)
        if r.status_code in (429, 503) and retries > 0:
            retries -= 1
            ra = r.headers.get("Retry-After", "")
            time.sleep(min(30.0, float(ra)) if ra.isdigit() else 5.0)
            continue
        params = None
        if r.status_code in (301, 302, 303, 307, 308):
            url = requests.compat.urljoin(url, r.headers.get("Location", ""))
            continue
        r.raise_for_status()
        data = bytearray()
        for chunk in r.iter_content(65536):
            data += chunk
            if len(data) > max_bytes:
                raise ValueError("파일이 너무 큼")
        r._content = bytes(data)
        return r
    raise ValueError("리디렉션이 너무 많음")


# 성인·노출 콘텐츠 표지 (Wikimedia API 에는 세이프서치가 없다 → 제목·설명·분류로 거른다)
NSFW_RE = re.compile(
    r"(?i)(?<![a-z])(nud(e|es|ity|ism|ist)|naked|pubic|genital\w*|penis|vagina|vulva|nipple\w*|breasts?|topless|bottomless|"
    r"erotic\w*|porn\w*|sex(ual|ually)?|fetish\w*|bdsm|lingerie|undress\w*|masturbat\w*|intercourse|explicit|nsfw)(?![a-z])"
    r"|나체|누드|신체 ?노출|성인물|성인용|음란|선정적")       # '선정된'·'성인 남성' 같은 일반어는 걸리지 않게


# 정부 홍보 계정(Korea.net 등) 사진: CC 표시와 함께 "보도 기관만·변형 금지" 같은 추가 제한이 붙는다 → KOGL 1유형이 아니면 제외
GOV_RESTRICT_RE = re.compile(r"(?i)files from korea\.net|korea\.net flickr|cheong wa dae|official photograph|"
                             r"only for publication by news organizations|may not be manipulated")


def is_nsfw(*texts: str) -> bool:
    return any(NSFW_RE.search(t or "") for t in texts)


def strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def ua(cfg: dict) -> str:
    return f"insta-autopilot/{plugin_version()} ({cfg.get('contact', '').strip()}) python-requests"


# ---------------------------------------------------------------- 제공자
def search_library(handle: str, queries: list[str]) -> list[dict]:
    import yaml

    toks = {t.lower() for q in queries for t in re.split(r"\s+", q) if len(t) >= 2}
    out = []
    for d in paths.library_dirs(handle):
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*")):
            if f.suffix.lower() not in IMAGE_EXT:
                continue
            side = f.with_suffix(".yaml")
            if not side.is_file():
                side = f.with_name(f.name + ".yaml")
            if not side.is_file():
                continue
            meta = yaml.safe_load(side.read_text(encoding="utf-8")) or {}
            if not meta.get("owner"):
                continue
            hay = " ".join([f.stem, str(meta.get("title", "")), " ".join(map(str, meta.get("keywords") or []))]).lower()
            if toks and not any(t in hay for t in toks):
                continue
            owner = str(meta["owner"]).strip()
            credit = "사진: 본인 촬영" if owner in ("본인", "self", "me") else f"사진: {meta.get('provider') or owner}"
            out.append({"source": "library", "title": meta.get("title") or f.stem, "local_path": str(f),
                        "license": meta.get("license", "이용자 보유"), "author": owner, "credit": credit,
                        "page_url": None, "share_alike": False})
    return out


def search_wikimedia(cfg: dict, queries: list[str], limit: int = 20) -> tuple[list[dict], list[str]]:
    if not (cfg.get("contact") or "").strip():
        return [], ["Wikimedia Commons: 연락처(본인 이메일)가 없어 건너뜀 — insta-setup 2단계에서 입력"]
    out, notes, seen = [], [], set()
    for q in queries:
        params = {"action": "query", "format": "json", "formatversion": "2", "generator": "search",
                  "gsrsearch": f"{q} filetype:bitmap", "gsrnamespace": "6", "gsrlimit": str(limit),
                  "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": str(WIKI_THUMB_PREVIEW),
                  "iiextmetadatafilter": "LicenseShortName|Artist|Credit|ObjectName|ImageDescription|LicenseUrl|Categories|Restrictions"}
        try:
            r = guarded_get("https://commons.wikimedia.org/w/api.php", params=params,
                            headers={"User-Agent": ua(cfg)})
            pages = (r.json().get("query") or {}).get("pages") or []
        except Exception as e:
            notes.append(f"Wikimedia 검색 실패({q}): {type(e).__name__}")
            continue
        for p in sorted(pages, key=lambda x: x.get("index", 0)):
            ii = (p.get("imageinfo") or [{}])[0]
            if ii.get("mime") not in ("image/jpeg", "image/png", "image/webp") or p["title"] in seen:
                continue
            seen.add(p["title"])
            em = ii.get("extmetadata") or {}
            lic = strip_html((em.get("LicenseShortName") or {}).get("value", ""))
            artist = strip_html((em.get("Artist") or {}).get("value", "")) or strip_html((em.get("Credit") or {}).get("value", ""))
            if KOGL1_RE.match(lic):
                src = "kogl1"
                agency = artist or strip_html((em.get("Credit") or {}).get("value", "")) or "정부기관"
                credit = f"사진: {agency} (공공누리 1유형)"
            elif CC_RE.match(lic):
                src = "wikimedia"
                credit = f"사진: {artist or '작자 미상'} / Wikimedia Commons ({lic})"
            else:
                continue
            if not artist:
                continue
            if src != "kogl1" and (GOV_RESTRICT_RE.search(" ".join([
                    artist, (em.get("Categories") or {}).get("value", ""),
                    strip_html((em.get("ImageDescription") or {}).get("value", "")),
                    strip_html((em.get("Restrictions") or {}).get("value", ""))])) or GOV_CREATOR_RE.search(artist)):
                notes.append(f"정부 홍보 사진(추가 이용 제한)으로 제외: {p['title'].removeprefix('File:')[:60]}")
                continue
            if is_nsfw(p["title"], strip_html((em.get("ImageDescription") or {}).get("value", "")),
                       strip_html((em.get("ObjectName") or {}).get("value", "")),
                       (em.get("Categories") or {}).get("value", "").replace("|", " "),
                       (em.get("Restrictions") or {}).get("value", "")):
                notes.append(f"성인·노출 표지로 제외: {p['title'].removeprefix('File:')[:60]}")
                continue
            out.append({"source": src, "query": q, "title": p["title"].removeprefix("File:"), "page_url": ii.get("descriptionurl"),
                        "text": " ".join([strip_html((em.get("ImageDescription") or {}).get("value", ""))[:400],
                                          strip_html((em.get("ObjectName") or {}).get("value", "")),
                                          (em.get("Categories") or {}).get("value", "").replace("|", " ")]),
                        "file_url": ii.get("url"), "thumb_url": ii.get("thumburl"), "width": ii.get("width"),
                        "height": ii.get("height"), "license": lic,
                        "license_url": strip_html((em.get("LicenseUrl") or {}).get("value", "")) or None,
                        "author": artist, "credit": credit, "share_alike": "SA" in lic.upper(),
                        "wikimedia_title": p["title"]})
    return out, notes


# 정부·기관 Flickr 계정은 CC 표시 옆에 "보도 목적만·변형 금지" 같은 추가 제한을 붙이는 경우가 많다 → 제외
GOV_CREATOR_RE = re.compile(
    r"(?i)korea\.?net|republic of korea|cheong ?wa ?dae|청와대|대통령|president|white ?house|ministry|ministère|"
    r"government|gouvernement|embassy|대사관|정부|부처|국방부|외교부|army|navy|air ?force|marine|nato|"
    r"\bun\b|united nations|unicef|european (commission|parliament)|parliament|congress|senate|국회")


def search_openverse(cfg: dict, queries: list[str]) -> tuple[list[dict], list[str]]:
    """Openverse(워드프레스 재단) 로 Flickr 의 CC 사진 검색. 키 없이 하루 200회·분당 20회."""
    out, notes = [], []
    for q in queries:
        if re.search(r"(?i)[가-힣]|\b(incategory|intitle|deepcat):", q):
            continue                                   # Openverse 는 영문 제목·태그 위주, 위키 전용 문법은 건너뜀
        try:
            r = guarded_get("https://api.openverse.org/v1/images/",
                            params={"q": q, "source": "flickr", "license_type": "commercial,modification",
                                    "license": "cc0,by,by-sa", "page_size": "20", "mature": "false"},
                            headers={"User-Agent": f"insta-autopilot/{plugin_version()}"})   # 이메일은 Wikimedia 에만
            res = r.json().get("results") or []
        except Exception as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            notes.append(f"Openverse 검색 실패({q}): {type(e).__name__}{f' {code}' if code else ''}"
                         + (" — 무료 한도(분당 20·하루 200회) 초과, 잠시 뒤 다시" if code == 429 else ""))
            continue
        for p in res:
            lic = (p.get("license") or "").lower()
            if lic not in ("cc0", "by", "by-sa") or p.get("mature"):
                continue
            creator = (p.get("creator") or "").strip()
            tags = " ".join(t.get("name", "") for t in p.get("tags") or [])
            if not creator or GOV_CREATOR_RE.search(creator):
                continue
            if is_nsfw(p.get("title") or "", tags):
                continue
            ver = p.get("license_version") or ""
            lic_name = "CC0" if lic == "cc0" else f"CC {lic.upper()} {ver}".strip()
            out.append({"source": "flickr", "query": q, "title": p.get("title") or p.get("id"), "text": tags,
                        "page_url": p.get("foreign_landing_url"), "file_url": p.get("url"), "thumb_url": p.get("url"),
                        "width": p.get("width"), "height": p.get("height"), "license": lic_name,
                        "license_url": p.get("license_url"), "author": creator,
                        "credit": f"사진: {creator} / Flickr ({lic_name})", "share_alike": lic == "by-sa"})
    return out, notes


META_IMG_RE = re.compile(r"<meta\s+[^>]*(?:property|name)=[\"'](?:og:image|twitter:image)(?::url)?[\"'][^>]*>", re.I)
CONTENT_RE = re.compile(r"content=[\"']([^\"']+)[\"']", re.I)


def article_images(html_text: str, base: str) -> list[str]:
    """기사 페이지의 대표 사진 주소(og:image → twitter:image)."""
    from urllib.parse import urljoin

    out = []
    for m in META_IMG_RE.finditer(html_text or ""):
        c = CONTENT_RE.search(m.group(0))
        if c:
            u = urljoin(base, html.unescape(c.group(1)).strip())
            if u not in out and re.search(r"(?i)\.(jpe?g|png|webp)(\?|$)|/photo/|/image", u):
                out.append(u)
    return out[:2]


def search_press(cfg: dict, job: Path, allowed: bool, max_articles: int = 6) -> tuple[list[dict], list[str]]:
    """팩트 시트 출처 기사(언론사)의 대표 사진. 이용자가 켜고(press_enabled) 규칙이 허용할 때만. 책임은 이용자."""
    if not press_enabled(cfg):
        return [], []
    if not allowed:
        return [], ["언론사 사진: 켜져 있지만 현재 규칙 버전이 허용하지 않아 건너뜀"]
    fs = paths.read_json(job / "factsheet.json", {}) or {}
    arts, seen = [], set()
    for cl in fs.get("claims") or []:
        for s in cl.get("sources") or []:
            u = s.get("url") or ""
            host = (urlsplit(u).hostname or "").lower()
            if host and u not in seen and not NOT_PRESS_RE.search(host):
                seen.add(u)
                arts.append((u, s, cl, host))
    out, notes = [], []
    for u, s, cl, host in arts[:max_articles]:
        outlet = (s.get("outlet") or "").strip() or host.removeprefix("www.").removeprefix("m.")
        try:
            r = guarded_get(u, headers={"User-Agent": PRESS_UA}, max_bytes=5 * 1024 * 1024, extra_hosts={host})
            imgs = article_images(r.content.decode(r.encoding or "utf-8", "replace"), u)
        except Exception as e:
            notes.append(f"언론사 기사 열기 실패({outlet}): {type(e).__name__}")
            continue
        for img in imgs:
            out.append({"source": "press", "query": "기사 사진", "fixed_relevance": 1.2,
                        "title": s.get("title") or outlet, "text": cl.get("claim") or "",
                        "page_url": u, "file_url": img, "thumb_url": img, "referer": u,
                        "press_hosts": sorted({host, (urlsplit(img).hostname or "").lower()}),
                        "license": "언론사 사진 (이용자가 사용 권한 확인·책임)",
                        "author": outlet, "credit": f"사진: {outlet}", "share_alike": False})
    return out, notes


def search_unsplash(cfg: dict, queries: list[str]) -> tuple[list[dict], list[str]]:
    key = (cfg.get("api_keys") or {}).get("unsplash")
    if not key:
        return [], []
    out, notes = [], []
    for q in queries:
        try:
            r = guarded_get("https://api.unsplash.com/search/photos",
                            params={"query": q, "per_page": "10", "orientation": "portrait", "content_filter": "high"},
                            headers={"Authorization": f"Client-ID {key}", "Accept-Version": "v1"})
            res = r.json().get("results") or []
        except Exception as e:
            notes.append(f"Unsplash 검색 실패({q}): {type(e).__name__}")
            continue
        for p in res:
            if is_nsfw(p.get("alt_description") or "", p.get("description") or ""):
                continue
            name = (p.get("user") or {}).get("name") or "Unknown"
            out.append({"source": "unsplash", "query": q, "text": p.get("description") or "",
                        "title": p.get("alt_description") or p.get("id"),
                        "page_url": (p.get("links") or {}).get("html"), "thumb_url": (p.get("urls") or {}).get("regular"),
                        "file_url": (p.get("urls") or {}).get("raw"), "download_location": (p.get("links") or {}).get("download_location"),
                        "width": p.get("width"), "height": p.get("height"), "license": "Unsplash License",
                        "author": name, "credit": f"사진: {name} / Unsplash", "share_alike": False})
    return out, notes


def search_pexels(cfg: dict, queries: list[str]) -> tuple[list[dict], list[str]]:
    key = (cfg.get("api_keys") or {}).get("pexels")
    if not key:
        return [], []
    out, notes = [], []
    for q in queries:
        try:
            r = guarded_get("https://api.pexels.com/v1/search", params={"query": q, "per_page": "10", "orientation": "portrait"},
                            headers={"Authorization": key})
            res = r.json().get("photos") or []
        except Exception as e:
            notes.append(f"Pexels 검색 실패({q}): {type(e).__name__}")
            continue
        for p in res:
            if is_nsfw(p.get("alt") or ""):
                continue
            name = p.get("photographer") or "Unknown"
            src = p.get("src") or {}
            out.append({"source": "pexels", "query": q, "title": p.get("alt") or str(p.get("id")), "page_url": p.get("url"),
                        "thumb_url": src.get("large"), "file_url": src.get("original"), "width": p.get("width"),
                        "height": p.get("height"), "license": "Pexels License", "author": name,
                        "credit": f"사진: {name} / Pexels", "share_alike": False})
    return out, notes


# ---------------------------------------------------------------- 미리보기·필터
def load_preview(cand: dict, cfg: dict) -> np.ndarray | None:
    if cand.get("local_path"):
        img = smart_crop.read_image(Path(cand["local_path"]))
        h, w = img.shape[:2]
        cand["width"], cand["height"] = w, h
        return img
    headers = {"User-Agent": PRESS_UA, "Referer": cand["referer"]} if cand.get("referer") else {"User-Agent": ua(cfg)}
    r = guarded_get(cand["thumb_url"], headers=headers, extra_hosts=cand.get("press_hosts"))
    import cv2

    img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
    if img is not None and cand.get("thumb_url") == cand.get("file_url"):
        cand["height"], cand["width"] = img.shape[:2]       # 받은 파일이 곧 원본 → 실제 크기로
    return img


def analyse(cand: dict, img: np.ndarray, band, out_dir: Path, idx: int, inset: bool = False) -> dict:
    subject, faces = smart_crop.subject_of(img)
    h, w = img.shape[:2]
    res = {"faces": len(faces), "subject": subject["kind"] if subject else None}
    for tag, (bw, bh) in (("916", (1080, 1920)), ("45", (1080, 1350))):
        info = smart_crop.plan_crop(w, h, bw, bh, subject, 0, bh, band)
        if inset:                 # 원본 크기 기준으로 판정(미리보기 이미지는 작게 받은 것이라 크기로 판단하지 않음)
            info = smart_crop.plan_inset(w, h, bw, bh, 0, bh, band)
            crop = smart_crop.apply_inset(img, info, bw, bh)
            res["layout"] = "inset"
        else:
            crop = smart_crop.apply_crop(img, info)
        import cv2

        p = out_dir / f"cand{idx}_{tag}.jpg"
        smart_crop.write_image(p, cv2.resize(crop, (bw // 3, bh // 3), interpolation=cv2.INTER_AREA), 85)
        res[f"preview_{tag}"] = str(p)
        if tag == "916":
            res["in_band"] = info["subject_in_band"]
    return res


def contact_sheet(cands: list[dict], out: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    tiles = [Image.open(c["analysis"]["preview_916"]) for c in cands]
    if not tiles:
        return
    tw, th = tiles[0].size
    sheet = Image.new("RGB", (len(tiles) * (tw + 16) + 16, th + 32), (24, 24, 28))
    try:
        font = ImageFont.load_default(size=64)
    except TypeError:
        font = ImageFont.load_default()
    d = ImageDraw.Draw(sheet)
    for i, t in enumerate(tiles):
        x = 16 + i * (tw + 16)
        sheet.paste(t, (x, 16))
        d.rectangle([x, 16, x + 84, 96], fill=(255, 210, 63))
        d.text((x + 22, 20), str(i + 1), fill=(17, 17, 17), font=font)
    sheet.save(out)


RANK = {"library": 0, "press": 0, "kogl1": 1, "wikimedia": 2, "flickr": 3, "unsplash": 3, "pexels": 3}
SOURCE_BONUS = {"library": 0.30, "kogl1": 0.10, "wikimedia": 0.05}
STOP = {"the", "a", "an", "of", "and", "in", "on", "at", "for", "to", "with", "by", "from", "file", "jpg", "jpeg", "png",
        "photo", "image", "picture", "korea", "korean", "south", "filetype", "bitmap", "사진", "이미지"}


def tokens(s: str) -> set[str]:
    s = re.sub(r"(?i)(incategory|intitle|insource|deepcat|haswbstatement):\S+", " ", s or "")
    return {t for t in re.split(r"[^0-9a-z가-힣]+", s.lower()) if len(t) >= 2 and t not in STOP}


def relevance(cand: dict, queries: list[str]) -> float:
    """검색어 낱말이 사진 제목·설명·분류·태그에 얼마나 들어 있나 (0~1) × 검색어 순서 가중 + 출처 가산."""
    if cand.get("fixed_relevance") is not None:          # 사안 기사 자체의 사진
        return float(cand["fixed_relevance"])
    hay = " ".join([cand.get("title") or "", cand.get("text") or ""]).lower()
    hay_toks = tokens(hay)
    q = cand.get("query") or ""
    qt = tokens(q)
    if qt:
        hit = sum(1 for t in qt if t in hay_toks or (len(t) >= 4 and t in hay))
        frac = hit / len(qt)
    else:
        frac = 0.5 if re.search(r"(?i)incategory:", q) else 0.0      # 분류 검색은 분류 자체가 근거
    if re.search(r"(?i)incategory:", q):
        frac = max(frac, 0.6)
    try:
        order = queries.index(q)
    except ValueError:
        order = len(queries)
    weight = max(0.5, 1.0 - 0.08 * order)
    if cand["source"] == "library":
        frac = max(frac, 0.6)
    return round(frac * weight + SOURCE_BONUS.get(cand["source"], 0.0), 3)


def upscale_of(w: int, h: int, bw: int = 1080, bh: int = 1920) -> float:
    """9:16 로 꽉 채워 자를 때 확대 배율 (1 이하 = 확대 없음)."""
    if not w or not h:
        return 9.9
    crop_h = min(h, w * bh / bw)
    return bh / crop_h


def order_candidates(found: list[dict], queries: list[str], per_query: int) -> list[dict]:
    """관련도순 + 검색어별로 돌아가며 뽑아 한 검색어가 독차지하지 않게."""
    for c in found:
        c["relevance"] = relevance(c, queries)
    found = [c for c in found if c["relevance"] > 0.05]
    found.sort(key=lambda x: (-x["relevance"], RANK.get(x["source"], 9)))
    out, used, by_author = [], {}, {}
    for c in found:
        k, a = c.get("query") or "", (c.get("author") or "").strip().lower()
        if used.get(k, 0) < per_query and by_author.get(a, 0) < 2:
            out.append(c)
            used[k] = used.get(k, 0) + 1
            by_author[a] = by_author.get(a, 0) + 1
    rest = []
    for c in found:                       # 남는 자리: 같은 작가 비슷한 사진이 몰리지 않게 작가당 3장까지
        a = (c.get("author") or "").strip().lower()
        if c not in out and by_author.get(a, 0) < 3:
            rest.append(c)
            by_author[a] = by_author.get(a, 0) + 1
    return out + rest


def run(job: Path, queries: list[str], max_n: int | None = None) -> dict:
    meta = paths.read_json(job / "meta.json")
    if not meta:
        raise SystemExit(f"✘ meta.json 없음: {job}")
    cfg = cfgmod.load_config()
    profile = cfgmod.load_profile(meta["handle"])
    c = compose(profile, (meta.get("rules") or {}).get("version"))
    lo, hi = get(c["checks"], "image.candidates", [3, 5])
    max_n = max_n or hi
    min_side = int(get(c["checks"], "image.min_short_side_px", 1080))
    band = tuple(get(c["checks"], "image.subject_band", list(smart_crop.DEFAULT_BAND)))

    max_up = float(get(c["checks"], "image.max_upscale", 1.6))
    inset_max = float(get(c["checks"], "image.inset_max_upscale", 2.0))
    notes: list[str] = []
    found = search_library(meta["handle"], queries)
    pr, pn = search_press(cfg, job, bool(get(c["checks"], "image.allow_press",
                                             get(c["checks"], "image.allow_registered_press", False))))
    fit = cfg.get("photo_fit") or "fill"
    fill_max = float(get(c["checks"], "image.fill_max_upscale", 6.5))
    found += pr
    notes += pn
    for x in found:
        x.setdefault("query", queries[0] if queries else "")
    seen_urls = set()
    for fn in (search_wikimedia, search_openverse, search_unsplash, search_pexels):
        r, n = fn(cfg, queries)
        for x in r:
            key = x.get("file_url") or x.get("page_url") or x.get("title")
            if key not in seen_urls:
                seen_urls.add(key)
                found.append(x)
        notes += n
    found = order_candidates(found, queries, per_query=max(2, -(-max_n // max(1, len(queries))) + 1))

    pdir = job / "photo_candidates"
    pdir.mkdir(exist_ok=True)
    for old in pdir.glob("cand*_*.jpg"):
        old.unlink()
    picked, excluded = [], []
    for cand in found:
        if len(picked) >= max_n:
            break
        try:
            img = load_preview(cand, cfg)
            if img is None:
                raise ValueError("미리보기 이미지를 읽을 수 없음")
        except Exception as e:
            excluded.append({"title": cand["title"], "reason": f"미리보기 실패: {e}"})
            continue
        up = upscale_of(cand.get("width") or img.shape[1], cand.get("height") or img.shape[0])
        iw = cand.get("width") or img.shape[1]
        if fit == "inset" or (fit == "auto" and up > max_up):
            if (1080 - 2 * smart_crop.INSET_MARGIN) / iw > inset_max:
                excluded.append({"title": cand["title"], "reason": f"해상도 부족: 가로 {iw}px — 사진 박스로도 {inset_max}배 넘게 확대됨"})
                continue
        elif up > fill_max:
            excluded.append({"title": cand["title"], "reason": f"해상도 부족: 꽉 채우면 {up:.1f}배 확대(기준 {fill_max}배)"})
            continue
        an = analyse(cand, img, band, pdir, len(picked) + 1,
                     inset=fit == "inset" or (fit == "auto" and up > max_up))
        if an["faces"] and an["in_band"] is False:
            excluded.append({"title": cand["title"], "reason": "9:16 크롭에서 얼굴이 세이프존 대역 밖"})
            for k in ("preview_916", "preview_45"):
                Path(an[k]).unlink(missing_ok=True)
            continue
        cand["analysis"] = an
        short = min(cand.get("width") or 0, cand.get("height") or 0)
        cand["warnings"] = [f"해상도 낮음: 짧은 변 {short}px < {min_side}px"] if short and short < min_side else []
        if an.get("layout") == "inset":
            cand["warnings"].append("흐린 배경 + 사진 박스로 배치(자르지 않음)")
        elif up > max_up:
            cand["warnings"].append(f"꽉 채우면 {up:.1f}배 확대 — 다소 흐릴 수 있음(검토 화면에서 '사진 박스'로 바꿀 수 있음)")
        cand["no"] = len(picked) + 1
        picked.append(cand)
    contact_sheet(picked, job / "photo_candidates.png")
    res = {"queries": queries, "searched_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
           "candidates": picked, "excluded": excluded, "notes": notes,
           "enough": len(picked) >= lo, "min_candidates": lo,
           "fallback": None if picked else "C"}
    paths.write_json(job / "photo_candidates.json", res)
    if meta.get("photo"):
        meta["photo"]["pick"] = None          # 쓰고 있는 사진은 그대로, 후보 번호만 새로 매겨짐
    else:
        meta.setdefault("stages", {})["photo"] = "waiting_user"
    paths.write_json(job / "meta.json", meta)
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("job", type=Path)
    ap.add_argument("--query", action="append", required=True)
    ap.add_argument("--max", type=int)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    res = run(a.job, a.query, a.max)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    for c in res["candidates"]:
        an = c["analysis"]
        print(f"{c['no']}. [{c['source']}] {c['title']}  (관련도 {c.get('relevance', 0):.2f} · 검색어 \"{c.get('query', '')}\")")
        print(f"   {c['credit']} · 라이선스 {c['license']} · {c.get('width')}×{c.get('height')} · 얼굴 {an['faces']}")
        if c.get("page_url"):
            print(f"   원본: {c['page_url']}")
        print(f"   크롭 미리보기 9:16 {an['preview_916']} · 4:5 {an['preview_45']}")
        for w in c["warnings"]:
            print(f"   ⚠ {w}")
    for e in res["excluded"]:
        print(f"· 제외: {e['title']} — {e['reason']}")
    for n in res["notes"]:
        print(f"· {n}")
    if res["candidates"]:
        print(f"✔ 후보 {len(res['candidates'])}장 — 모아보기: {a.job / 'photo_candidates.png'}"
              + ("" if res["enough"] else f" (기준 {res['min_candidates']}장 미만)"))
    else:
        print("⚠ 화이트리스트 사진 없음 → C 템플릿(타이포+캐릭터)으로 진행")
    return 0


if __name__ == "__main__":
    sys.exit(main())
