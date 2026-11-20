"""규칙 번들: 서명 검증(CLIENT_SPEC 3장), 캐시 무결성 확인, 카테고리 합성(6장).

- 공개키는 이 파일의 상수로 내장한다(서버·설정 파일에서 읽지 않음). keys/rules_pubkey.pem 은 같은 키의 사본.
- 캐시 폴더의 규칙을 쓸 때마다 저장해 둔 manifest 봉투의 서명과 파일별 sha256 을 다시 확인한다.
  확인에 실패한 버전은 절대 쓰지 않는다.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from pathlib import Path

from . import config as cfgmod
from . import paths

# CLIENT_SPEC 2장: raw ed25519 공개키 32바이트 (표준 base64)
EMBEDDED_PUBKEY_B64 = "/OPxSlRxnd/fvMH3VovdNQWEUaSN+PxiLRo2hsctfUo="
SCHEMA = 2
MANIFEST_NAME = "_manifest.json"     # 캐시 폴더에 받은 그대로 저장한 manifest 봉투
POINTER_NAME = "_pointer.json"       # 캐시 폴더에 저장한 latest.json 봉투
META_FILES = {MANIFEST_NAME, POINTER_NAME}


class RulesError(Exception):
    pass


# ---------------------------------------------------------------- 서명 봉투
def public_key(b64: str | None = None):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    return Ed25519PublicKey.from_public_bytes(base64.b64decode(b64 or EMBEDDED_PUBKEY_B64, validate=True))


def canonical(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _check_floats(v, where="payload"):
    if isinstance(v, float):
        raise RulesError(f"{where}: 실수 값은 허용되지 않음")
    if isinstance(v, dict):
        for k, x in v.items():
            if not isinstance(k, str) or not k.isascii():
                raise RulesError(f"{where}: ASCII 가 아닌 키")
            _check_floats(x, where)
    elif isinstance(v, list):
        for x in v:
            _check_floats(x, where)


def open_envelope(data: bytes, what: str, pubkey_b64: str | None = None) -> dict:
    if data[:3] == b"\xef\xbb\xbf":
        raise RulesError(f"{what}: BOM 이 있음")
    try:
        env = json.loads(data.decode("utf-8"))
    except Exception:
        raise RulesError(f"{what}: JSON 파싱 실패")
    if not isinstance(env, dict) or set(env) != {"payload", "sig"} \
            or not isinstance(env["payload"], dict) or not isinstance(env["sig"], str):
        raise RulesError(f"{what}: 서명 봉투 형식이 아님")
    _check_floats(env["payload"], what)
    try:
        if not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", env["sig"]):
            raise ValueError
        sig = base64.b64decode(env["sig"], validate=True)
        if len(sig) != 64:
            raise ValueError
        public_key(pubkey_b64).verify(sig, canonical(env["payload"]))
    except Exception:
        raise RulesError(f"{what}: 서명 검증 실패")
    return env["payload"]


def semver(v: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", v or "")
    if not m:
        raise RulesError(f"semver 형식 아님: {v!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def plugin_version() -> str:
    data = json.loads((paths.PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    return data["version"]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- 캐시 상태
def load_state() -> dict:
    st = paths.read_json(paths.rules_state_file(), {}) or {}
    st.setdefault("channels", {})
    return st


def save_state(st: dict) -> None:
    paths.write_json(paths.rules_state_file(), st)


def verify_cached(version: str) -> dict:
    """캐시 폴더의 규칙이 서명된 manifest 와 정확히 같은지 확인. 통과하면 manifest payload 반환."""
    d = paths.rules_version_dir(version)
    mfile = d / MANIFEST_NAME
    if not mfile.is_file():
        raise RulesError(f"캐시 {version}: manifest 없음")
    man = open_envelope(mfile.read_bytes(), f"캐시 {version} manifest")
    if man.get("schema") != SCHEMA or man.get("version") != version:
        raise RulesError(f"캐시 {version}: manifest 버전·schema 불일치")
    want = {f["path"]: f for f in man.get("files", [])}
    seen = set()
    for p in d.rglob("*"):
        if p.is_dir():
            continue
        rel = p.relative_to(d).as_posix()
        if rel in META_FILES:
            continue
        if rel not in want:
            raise RulesError(f"캐시 {version}: manifest 에 없는 파일 {rel}")
        data = p.read_bytes()
        if sha256(data) != want[rel]["sha256"] or len(data) != want[rel]["size"]:
            raise RulesError(f"캐시 {version}: 파일 해시 불일치 {rel}")
        seen.add(rel)
    missing = set(want) - seen
    if missing:
        raise RulesError(f"캐시 {version}: 빠진 파일 {sorted(missing)[:3]}")
    return man


def active_version() -> str | None:
    return (load_state().get("active") or {}).get("version")


def rules_dir(version: str | None = None) -> tuple[str, Path]:
    """검증을 통과한 규칙 폴더. version 이 없으면 현재 활성 버전."""
    v = version or active_version()
    if not v:
        raise RulesError("받은 규칙이 없습니다 — rules_sync 를 먼저 실행하세요 (네트워크 확인)")
    verify_cached(v)
    return v, paths.rules_version_dir(v)


# ---------------------------------------------------------------- 파일 읽기
def _read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8").replace("\r\n", "\n")


def _load_yaml(p: Path):
    import yaml

    return yaml.safe_load(_read_text(p))


def parse_banned(text: str) -> list[str]:
    out = []
    for line in text.replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


# ---------------------------------------------------------------- 합성 (CLIENT_SPEC 6장)
def _uniq_key(item, path: str):
    if path == "sources.rss" and isinstance(item, dict):
        return ("url", item.get("url"))
    return ("v", json.dumps(item, ensure_ascii=False, sort_keys=True))


def merge(a, b, append_lists: set[str], path: str = ""):
    """맵은 깊은 병합(뒤가 이김), 리스트는 교체. append_lists 경로는 합집합(순서 유지·중복 제거)."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = copy.deepcopy(a)
        for k, v in b.items():
            p = f"{path}.{k}" if path else k
            out[k] = merge(out[k], v, append_lists, p) if k in out else copy.deepcopy(v)
        return out
    if isinstance(a, list) and isinstance(b, list) and path in append_lists:
        out, seen = [], set()
        for item in a + b:
            key = _uniq_key(item, path)
            if key in seen:
                continue
            seen.add(key)
            out.append(copy.deepcopy(item))
        return out
    return copy.deepcopy(b)


def _nonempty(d: dict) -> dict:
    return {k: v for k, v in (d or {}).items() if v not in (None, "", [], {})}


def profile_stage(profile: dict) -> dict:
    """5단계: 계정 설정 → 규칙 설정 키로 옮긴 오버레이."""
    st: dict = {}
    cta = _nonempty(profile.get("cta") or {})
    if cta:
        st["default_cta"] = cta
    extra = profile.get("extra_sources") or {}
    src = {}
    if extra.get("rss"):
        src["rss"] = [r for r in extra["rss"] if isinstance(r, dict) and r.get("url")]
    if extra.get("google_news"):
        src["google_news"] = list(extra["google_news"])
    if src:
        st["sources"] = src
    custom = profile.get("custom_category") or {}
    if custom.get("trend_keywords"):
        st["trend_keywords"] = list(custom["trend_keywords"])
    if profile.get("template") in ("A", "B", "C"):
        st["default_template"] = profile["template"]
    if profile.get("exclude_keywords"):
        st["exclude_keywords"] = list(profile["exclude_keywords"])
    if profile.get("tone"):
        st["tone"] = profile["tone"]
    return st


def compose(profile: dict, version: str | None = None) -> dict:
    v, d = rules_dir(version)
    catalog = _load_yaml(d / "categories" / "_catalog.yaml")
    mrg = catalog.get("merge") or {}
    append_lists = set(mrg.get("append_lists") or [])
    overridable = list(mrg.get("overridable_files") or [])
    sub_keys = list(mrg.get("sub_category_keys") or [])
    cat_ids = [c["id"] for c in catalog.get("categories", [])]

    main = profile.get("main_category")
    if main not in cat_ids:
        raise RulesError(f"메인 카테고리 '{main}' 가 규칙 {v} 의 카탈로그에 없습니다 ({', '.join(cat_ids)})")
    preset = _load_yaml(d / "categories" / main / "preset.yaml")
    ctype = preset.get("content_type")
    type_defaults = ((catalog.get("content_types") or {}).get(ctype) or {}).get("defaults") or {}

    # 1. base
    checks = _load_yaml(d / "base" / "checks.yaml") or {}
    docs = {p.name: _read_text(p) for p in sorted((d / "base").glob("*.md"))}
    banned = parse_banned(_read_text(d / "base" / "banned_phrases.txt"))
    applied = ["base"]

    # 2~3. content_type 기본값 → 메인 프리셋
    settings: dict = {}
    settings = merge(settings, type_defaults, append_lists)
    applied.append(f"content_type:{ctype}")
    settings = merge(settings, preset, append_lists)
    applied.append(f"main:{main}")
    for fname in overridable:
        f = d / "categories" / main / fname
        if not f.is_file():
            continue
        if fname == "banned_phrases.txt":
            banned += parse_banned(_read_text(f))
        elif fname in docs:
            docs[fname] = docs[fname].rstrip("\n") + f"\n\n## 카테고리 추가 규칙 ({preset.get('name', main)})\n\n" + _read_text(f).strip("\n") + "\n"

    # 4. 서브 카테고리 (sources, trend_keywords 만)
    subs = []
    for sub in (profile.get("sub_categories") or [])[:2]:
        if sub not in cat_ids or sub == main:
            continue
        sp = _load_yaml(d / "categories" / sub / "preset.yaml")
        settings = merge(settings, {k: sp[k] for k in sub_keys if k in sp}, append_lists)
        subs.append(sub)
        applied.append(f"sub:{sub}")

    # 5. 계정 설정 (+ 이용자가 승인한 개인 금지 표현: accounts/<handle>/banned_phrases.txt)
    settings = merge(settings, profile_stage(profile), append_lists)
    applied.append("profile")
    try:
        personal = paths.account_dir(profile.get("handle", "")) / "banned_phrases.txt"
        if personal.is_file():
            banned += [b for b in parse_banned(_read_text(personal)) if b not in banned]
    except ValueError:
        pass

    brand_p = (profile.get("brand") or {})
    palette = _nonempty(brand_p.get("palette") or {}) or (settings.get("palettes") or [{}])[0]
    brand = {k: palette[k] for k in ("accent", "bg", "cta", "text") if palette.get(k)}
    custom = profile.get("custom_category") or {}
    chip = brand_p.get("chip") or custom.get("name") or preset.get("name", "")

    return {
        "version": v,
        "rules_dir": str(d),
        "applied": applied,
        "content_type": ctype,
        "main": main,
        "subs": subs,
        "catalog": catalog,
        "checks": checks,
        "docs": docs,
        "banned_phrases": banned,
        "settings": settings,
        "profile": profile,
        "brand": brand,
        "chip": chip,
        "template_meta": _load_yaml(d / "templates" / "template_meta.yaml"),
        "templates_dir": str(d / "templates"),
    }


def compose_for(handle: str, version: str | None = None) -> dict:
    return compose(cfgmod.load_profile(handle), version)


def get(checks: dict, dotted: str, default=None):
    cur = checks
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur
