"""셋팅 마법사(insta-setup) 보조 명령.

    python setup_account.py init [--home PATH] [--channel stable|beta] [--contact TEXT]
    python setup_account.py catalog                       # 카테고리 목록 (규칙 캐시 기준)
    python setup_account.py preset <id>                   # 팔레트 3안, 기본 CTA, 기본 템플릿
    python setup_account.py import-asset <handle> <file> --kind logo|character|font
    python setup_account.py validate <draft.yaml>
    python setup_account.py sample <draft.yaml> [--out DIR]   # 샘플 카드 + CTA 마지막 프레임
    python setup_account.py save <draft.yaml>
    python setup_account.py show [handle]
    python setup_account.py set-key unsplash|pexels [--clear]   # 사진 검색 API 키 (이용자 터미널에서 직접 입력)
    python setup_account.py press list
    python setup_account.py press add --name 연합뉴스 --domain yna.co.kr [--domain ...] --basis "사용 근거" --confirm "<이용자 확인 문구>"
    python setup_account.py press remove --name 연합뉴스
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import config as cfgmod  # noqa: E402
from common import paths  # noqa: E402
from common.rules import RulesError, compose, rules_dir  # noqa: E402

HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")


def valid_email(v: str) -> bool:
    return bool(EMAIL.fullmatch((v or "").strip()))


def _yaml(p: Path):
    import yaml

    return yaml.safe_load(Path(p).read_text(encoding="utf-8"))


def cmd_init(a) -> int:
    if a.home:
        h = paths.set_home(Path(a.home))
    else:
        h = paths.home()
    paths.ensure_home_layout()
    cfg = cfgmod.load_config()
    if a.channel:
        cfg["channel"] = a.channel
    if a.contact is not None:
        c = a.contact.strip()
        if c and not valid_email(c):
            print(f"✘ 이메일 형식이 아닙니다: {c!r} — Wikimedia 연락처는 이용자 본인 이메일을 넣습니다 (비우려면 --contact \"\")")
            return 2
        cfg["contact"] = c
    p = cfgmod.save_config(cfg)
    print(f"✔ 데이터 폴더: {h}")
    print(f"✔ 설정 파일: {p}")
    print(json.dumps(cfgmod.redacted(cfg), ensure_ascii=False, indent=2))
    return 0


def cmd_catalog(a) -> int:
    v, d = rules_dir()
    cat = _yaml(d / "categories" / "_catalog.yaml")
    types = cat.get("content_types") or {}
    print(f"# 카테고리 (규칙 {v})\n")
    print("| id | 이름 | 유형 | 기본 템플릿 | 트렌드 키워드(일부) |\n|---|---|---|---|---|")
    for c in cat["categories"]:
        p = _yaml(d / "categories" / c["id"] / "preset.yaml")
        kw = ", ".join((p.get("trend_keywords") or [])[:5])
        print(f"| {c['id']} | {c['name']} | {types.get(c['content_type'], {}).get('label', c['content_type'])} "
              f"| {p.get('default_template')} | {kw} |")
    return 0


def cmd_preset(a) -> int:
    v, d = rules_dir()
    f = d / "categories" / a.id / "preset.yaml"
    if not f.is_file():
        print(f"✘ 카테고리 없음: {a.id}")
        return 2
    p = _yaml(f)
    print(f"# {p['name']} ({p['id']}, {p['content_type']}) — 규칙 {v}\n")
    print("## 팔레트 3안\n")
    for i, pal in enumerate(p.get("palettes") or [], 1):
        print(f"{i}. {pal['name']} — accent {pal['accent']} · bg {pal['bg']} · cta {pal['cta']} · text {pal['text']}")
    cta = p.get("default_cta") or {}
    print("\n## 기본 CTA\n")
    for k in ("pitch", "pitch_em", "card_desc", "after"):
        print(f"- {k}: {json.dumps(cta.get(k, ''), ensure_ascii=False)}")
    print(f"\n- 기본 템플릿: {p.get('default_template')}  (A 마커형 / B 패널형 / C 타이포+캐릭터)")
    print(f"- 칩 기본값: {p['name']}")
    hs = p.get("hook_style") or {}
    print(f"- 훅 기본 강도: {hs.get('default')}")
    for r in p.get("risk_notes") or []:
        print(f"- 리스크: {r}")
    return 0


def cmd_import_asset(a) -> int:
    h = paths.normalize_handle(a.handle)
    src = Path(a.file).expanduser()
    if not src.is_file():
        print(f"✘ 파일 없음: {src}")
        return 2
    if a.kind in ("logo", "character"):
        from PIL import Image

        try:
            with Image.open(src) as im:
                im.verify()
            with Image.open(src) as im:
                size = im.size
        except Exception:
            print("✘ 이미지 파일이 아닙니다")
            return 2
        sub = "assets" if a.kind == "logo" else "assets/character"
        name = ("logo" if a.kind == "logo" else src.stem) + src.suffix.lower()
    else:
        if src.suffix.lower() not in (".ttf", ".otf", ".woff", ".woff2"):
            print("✘ 글꼴 파일(.ttf .otf .woff .woff2)만 가능합니다")
            return 2
        sub, name, size = "assets/fonts", src.name, None
    dest_dir = paths.account_dir(h) / sub
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    shutil.copyfile(src, dest)
    rel = dest.relative_to(paths.account_dir(h)).as_posix()
    print(f"✔ 복사: {dest}" + (f" ({size[0]}×{size[1]})" if size else ""))
    if size and min(size) < 200:
        print("⚠ 해상도가 낮습니다(짧은 변 200px 미만). 카드에서 흐릿할 수 있습니다.")
    print(json.dumps({"path": rel}, ensure_ascii=False))
    return 0


def validate(draft: dict) -> list[str]:
    errs = []
    try:
        h = paths.normalize_handle(draft.get("handle", ""))
    except ValueError as e:
        return [str(e)]
    if not draft.get("display_name"):
        errs.append("display_name(표시 이름)이 비었습니다")
    alias = (draft.get("alias") or "").strip()
    if not alias or re.search(r"[\s,/:]", alias) or len(alias) > 8:
        errs.append("alias(별칭)는 공백·쉼표·슬래시·콜론 없이 8자 이내")
    else:
        other = cfgmod.find_account_by_alias(alias)
        if other and other != h:
            errs.append(f"별칭 '{alias}' 는 @{other} 가 이미 씁니다")
    try:
        v, d = rules_dir()
        ids = [c["id"] for c in _yaml(d / "categories" / "_catalog.yaml")["categories"]]
    except RulesError as e:
        return errs + [str(e)]
    if draft.get("main_category") not in ids:
        errs.append(f"main_category 는 {', '.join(ids)} 중 하나 (목록에 없으면 가장 가까운 것 + custom_category)")
    subs = draft.get("sub_categories") or []
    if len(subs) > 2 or any(s not in ids or s == draft.get("main_category") for s in subs):
        errs.append("sub_categories 는 메인과 다른 카테고리 최대 2개")
    brand = draft.get("brand") or {}
    pal = brand.get("palette") or {}
    for k in ("accent", "bg", "cta", "text"):
        if pal and not HEX.match(str(pal.get(k, ""))):
            errs.append(f"brand.palette.{k} 는 #RRGGBB 형식")
    chip = brand.get("chip") or (draft.get("custom_category") or {}).get("name") or ""
    if chip and len(chip) > 6:
        errs.append(f"칩 문구 '{chip}' 는 6자 이내여야 합니다 (brand.chip 으로 짧게 지정)")
    for k in ("logo", "character", "font"):
        if brand.get(k) and not cfgmod.account_asset_path(h, brand[k]):
            errs.append(f"brand.{k} 파일이 계정 폴더에 없습니다: {brand[k]} (import-asset 으로 복사)")
    cta = draft.get("cta") or {}
    if cta.get("pitch_em") and cta.get("pitch") and cta["pitch_em"] not in cta["pitch"]:
        errs.append("cta.pitch_em 은 cta.pitch 안에 있는 문구여야 합니다")
    if ((draft.get("upload") or {}).get("method") or "browser") not in ("browser", "api", "manual"):
        errs.append("upload.method 는 browser(A) | api(B) | manual(C)")
    if draft.get("template") not in (None, "", "A", "B", "C"):
        errs.append("template 은 A|B|C 또는 비움")
    for r in ((draft.get("extra_sources") or {}).get("rss") or []):
        if not isinstance(r, dict) or not str(r.get("url", "")).startswith(("https://", "http://")):
            errs.append(f"extra_sources.rss 항목은 {{name, url}} 형식: {r}")
    return errs


def cmd_validate(a) -> int:
    errs = validate(_yaml(a.draft) or {})
    for e in errs:
        print(f"✘ {e}")
    print("✔ 설정 검사 통과" if not errs else f"✘ {len(errs)}건")
    return 0 if not errs else 1


def sample_photo(out: Path) -> Path:
    """샘플용 합성 사진 (실제 사진 아님)."""
    from PIL import Image, ImageDraw, ImageFilter

    W, H = 1080, 1920
    im = Image.new("RGB", (W, H))
    px = im.load()
    for y in range(H):
        for x in range(0, W, 4):
            c = (int(40 + 60 * y / H), int(70 + 50 * x / W), int(110 + 60 * (1 - y / H)))
            for dx in range(4):
                px[x + dx, y] = c
    d = ImageDraw.Draw(im)
    for i, (cx, cy, r) in enumerate([(300, 700, 260), (760, 520, 200), (620, 980, 320)]):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(200 - i * 40, 180 - i * 20, 120 + i * 40))
    im = im.filter(ImageFilter.GaussianBlur(18))
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out, quality=90)
    return out


def cmd_sample(a) -> int:
    import render_card
    import render_reel

    draft = _yaml(a.draft) or {}
    errs = validate(draft)
    if errs:
        for e in errs:
            print(f"✘ {e}")
        return 1
    profile = cfgmod._deep_merge(cfgmod.PROFILE_TEMPLATE, draft)
    profile["handle"] = paths.normalize_handle(profile["handle"])
    c = compose(profile)
    out = Path(a.out) if a.out else paths.account_dir(profile["handle"]) / "sample"
    out.mkdir(parents=True, exist_ok=True)
    variant = profile.get("template") or c["settings"].get("default_template") or "A"
    photo = None if variant == "C" else str(sample_photo(out / "sample_photo.jpg"))
    hook = {"l1": "샘플 카드 미리보기", "l2": f"{c['chip']} 브랜드 확인", "l3": "실제 문구는 작업 때 채워져요"}
    rep = asyncio.run(render_card.render_cards(c, profile, hook, variant, out, photo,
                                               "사진: 샘플 이미지" if photo else "", feed=False))
    card = out / "card.png"
    tl = c["template_meta"].get("timeline") or {}
    last_t = float(tl.get("cta_duration", 3.0)) - 1.0 / float(tl.get("fps", 30))
    frames = asyncio.run(render_reel.capture_cta(c, profile, card, [last_t], out / "_cta", 1, variant))
    shutil.copyfile(frames[0], out / "cta_last.png")
    shutil.rmtree(out / "_cta", ignore_errors=True)
    r = rep["results"]["reel"]
    print(f"✔ 샘플 카드 (variant {variant}, 규칙 {c['version']}): {card}")
    print(f"✔ CTA 마지막 프레임: {out / 'cta_last.png'}")
    print(f"  가이드: {out / 'card_guide.png'}")
    for adj in r["adjustments"]:
        print(f"  · 보정: {adj}")
    for i in r["issues"]:
        print(f"  ⚠ {i['msg']}")
    return 0


def cmd_save(a) -> int:
    draft = _yaml(a.draft) or {}
    errs = validate(draft)
    if errs:
        for e in errs:
            print(f"✘ {e}")
        return 1
    p = cfgmod.save_profile(draft)
    print(f"✔ 저장: {p}")
    print(f"  등록된 계정: {', '.join('@' + h for h in cfgmod.list_accounts())}")
    return 0


def cmd_set_key(a) -> int:
    """Unsplash·Pexels 무료 API 키 — 채팅으로 받지 않고 이용자 터미널에서 보이지 않게 입력받아 config.yaml 에 저장."""
    import getpass

    cfg = cfgmod.load_config()
    keys = dict(cfg.get("api_keys") or {})
    if a.clear:
        keys[a.service] = ""
    else:
        if not sys.stdin.isatty():
            print(f"✘ 키는 채팅이 아니라 이용자 본인 터미널에서 입력합니다. 터미널에서 실행하세요:\n"
                  f"   python \"{Path(__file__).resolve().parent / 'ia.py'}\" setup_account set-key {a.service}")
            return 2
        k = getpass.getpass(f"{a.service} API 키 (입력 내용은 보이지 않음): ").strip()
        if not re.fullmatch(r"[A-Za-z0-9_\-]{20,100}", k):
            print("✘ 키 형식이 아닙니다 (공백 없이 20~100자)")
            return 2
        keys[a.service] = k
    cfg["api_keys"] = keys
    cfgmod.save_config(cfg)
    print(f"✔ {a.service} 키 {'지움' if a.clear else '저장'} — 다음 사진 검색부터 적용")
    return 0


PRESS_CONFIRM_WORDS = ("확인", "책임")
DOMAIN_RE = re.compile(r"^(?=.{4,100}$)([a-z0-9-]+\.)+[a-z]{2,}$")


def cmd_press(a) -> int:
    """기사 사진 사용 허락을 받은 언론사 등록 — 등록·사용 책임은 이용자. 근거와 확인 문구를 기록으로 남긴다."""
    import datetime as _dt

    cfg = cfgmod.load_config()
    pp = dict(cfg.get("press_photos") or {})
    outlets = list(pp.get("outlets") or [])
    if a.press_cmd == "list":
        if not outlets:
            print("· 등록된 언론사 없음 — 기사 사진은 쓰지 않습니다")
        for o in outlets:
            print(f"- {o['name']} ({', '.join(o['domains'])}) · 근거: {o.get('basis') or '-'} · 등록 {o.get('registered_at', '')[:10]}")
        return 0
    if a.press_cmd == "remove":
        n = len(outlets)
        outlets = [o for o in outlets if o["name"] != a.name]
        if len(outlets) == n:
            print(f"✘ 등록되지 않은 언론사: {a.name}")
            return 2
        print(f"✔ {a.name} 등록 해제 — 이 언론사 기사 사진은 더 이상 후보에 넣지 않음")
    else:
        doms = sorted({d.strip().lower().removeprefix("www.") for d in a.domain})
        bad = [d for d in doms if not DOMAIN_RE.match(d)]
        if bad:
            print(f"✘ 도메인 형식이 아닙니다: {', '.join(bad)} (예: yna.co.kr)")
            return 2
        if not a.basis or not a.basis.strip():
            print("✘ 사용 근거(--basis)를 적어 주세요. 예: \"연합뉴스 사진 구독 계약 2026-11\", \"편집국 서면 허락\"")
            return 2
        if not a.confirm or not all(w in a.confirm for w in PRESS_CONFIRM_WORDS):
            print("✘ 이용자 본인의 확인 문구가 필요합니다(이용자가 채팅에 직접 쓴 문장, '확인'과 '책임'이 들어가야 함).\n"
                  "   예: \"연합뉴스 사진 사용 허락을 확인했고 사용 책임은 나에게 있음\"")
            return 2
        outlets = [o for o in outlets if o["name"] != a.name]
        outlets.append({"name": a.name.strip(), "domains": doms, "basis": a.basis.strip(), "confirmed": True,
                        "confirm_text": a.confirm.strip(),
                        "registered_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds")})
        print(f"✔ {a.name} 등록 ({', '.join(doms)}) — 이 언론사 기사의 대표 사진을 후보에 넣습니다. 출처 표기: 사진: {a.name}")
    pp["outlets"] = outlets
    cfg["press_photos"] = pp
    cfgmod.save_config(cfg)
    log_f = paths.home() / "press_photos_log.jsonl"
    with open(log_f, "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"), "action": a.press_cmd,
                            "name": a.name, "domains": getattr(a, "domain", None), "basis": getattr(a, "basis", None),
                            "confirm_text": getattr(a, "confirm", None)}, ensure_ascii=False) + "\n")
    return 0


def cmd_show(a) -> int:
    if a.handle:
        print(cfgmod.dump_yaml(cfgmod.load_profile(cfgmod.resolve_account(a.handle))))
        return 0
    print(f"데이터 폴더: {paths.home()}")
    print(json.dumps(cfgmod.redacted(cfgmod.load_config()), ensure_ascii=False, indent=2))
    for h in cfgmod.list_accounts():
        p = cfgmod.load_profile(h)
        print(f"- @{h} ({p.get('alias')}) {p.get('display_name')} · {p.get('main_category')}"
              + (f" + {', '.join(p.get('sub_categories') or [])}" if p.get("sub_categories") else ""))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--home")
    p.add_argument("--channel", choices=["stable", "beta"])
    p.add_argument("--contact", help="Wikimedia 요청 헤더에 넣을 이용자 본인 이메일 (빈 문자열이면 지움)")
    sub.add_parser("catalog")
    p = sub.add_parser("preset")
    p.add_argument("id")
    p = sub.add_parser("import-asset")
    p.add_argument("handle")
    p.add_argument("file")
    p.add_argument("--kind", choices=["logo", "character", "font"], required=True)
    for name in ("validate", "save"):
        p = sub.add_parser(name)
        p.add_argument("draft", type=Path)
    p = sub.add_parser("sample")
    p.add_argument("draft", type=Path)
    p.add_argument("--out")
    p = sub.add_parser("show")
    p.add_argument("handle", nargs="?")
    p = sub.add_parser("press")
    ps = p.add_subparsers(dest="press_cmd", required=True)
    ps.add_parser("list")
    q = ps.add_parser("add")
    q.add_argument("--name", required=True)
    q.add_argument("--domain", action="append", required=True)
    q.add_argument("--basis", required=True)
    q.add_argument("--confirm", required=True)
    q = ps.add_parser("remove")
    q.add_argument("--name", required=True)
    p = sub.add_parser("set-key")
    p.add_argument("service", choices=["unsplash", "pexels"])
    p.add_argument("--clear", action="store_true")
    a = ap.parse_args()
    try:
        return {"init": cmd_init, "catalog": cmd_catalog, "preset": cmd_preset, "import-asset": cmd_import_asset,
                "validate": cmd_validate, "sample": cmd_sample, "save": cmd_save, "show": cmd_show,
                "set-key": cmd_set_key, "press": cmd_press}[a.cmd](a)
    except (RulesError, FileNotFoundError, ValueError) as e:
        print(f"✘ {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
