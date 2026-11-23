"""config.yaml (전역) · accounts/<handle>/profile.yaml (계정) 읽기·쓰기.

이 파일들은 이용자 PC 에만 있다. 키·토큰 값은 로그·출력에 내보내지 않는다.
"""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

from . import paths

DEFAULT_RULES_URL = "https://rules.sonicent.co.kr"

DEFAULT_CONFIG = {
    "rules_url": DEFAULT_RULES_URL,
    "channel": "stable",            # stable | beta
    "approval_mode": "auto",        # auto(계정 2개 이상이면 일괄) | single | batch
    "render_concurrency": 2,
    "contact": "",                  # Wikimedia API User-Agent 연락처 — 셋팅 마법사에서 받은 이용자 본인 이메일
    "api_keys": {"unsplash": "", "pexels": ""},
    "web": {"port": 8765, "where": "local"},
    "photo_fit": "fill",                  # 카드 사진 배치: fill(꽉 채우기) | inset(흐린 배경 + 사진 박스) | auto
    "press_photos": {"enabled": False},   # 언론사 기사 사진 사용 (켜면 이용자 확인 문구 기록, 책임은 이용자)   # 검토 화면: local(이 PC) | tailscale(밖에서 휴대폰) | lan(같은 Wi-Fi)
}

SECRET_KEYS = {"api_keys"}

PROFILE_TEMPLATE = {
    "handle": "",
    "display_name": "",
    "alias": "",
    "main_category": "",
    "sub_categories": [],
    "custom_category": None,        # {name, based_on, trend_keywords[]} — 목록에 없는 분야
    "brand": {
        "palette": {},              # {name?, accent, bg, cta, text}
        "chip": "",                 # 카드 칩 문구(6자 이내). 비우면 카테고리 이름
        "logo": "",                 # assets/ 기준 상대 경로
        "character": "",
        "font": "",                 # assets/fonts/ 의 파일 (선택)
    },
    "template": "",                 # A|B|C — 비우면 카테고리 기본값
    "cta": {},                      # pitch, pitch_em, card_desc, after, button, button_done
    "tone": "",
    "exclude_keywords": [],
    "extra_sources": {"rss": [], "google_news": []},
    "upload": {"method": "browser"},   # browser(A, 기본) | api(B) | manual(C)
    "active": True,                    # run_all·일괄 승인 대상
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_yaml(path: Path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def dump_yaml(data) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)


# ---------------------------------------------------------------- config.yaml
def load_config() -> dict:
    p = paths.config_file()
    data = load_yaml(p) if p.is_file() else {}
    return _deep_merge(DEFAULT_CONFIG, data or {})


def save_config(cfg: dict) -> Path:
    p = paths.config_file()
    body = "# insta-autopilot 설정 (이 PC 에만 저장됨)\n" + dump_yaml(cfg)
    paths.write_text_atomic(p, body)
    return p


def redacted(cfg: dict) -> dict:
    out = copy.deepcopy(cfg)
    for k in SECRET_KEYS:
        if isinstance(out.get(k), dict):
            out[k] = {kk: ("설정됨" if vv else "") for kk, vv in out[k].items()}
    return out


# ---------------------------------------------------------------- profile.yaml
def list_accounts() -> list[str]:
    d = paths.accounts_dir()
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if (p / "profile.yaml").is_file())


def load_profile(handle: str) -> dict:
    p = paths.profile_file(handle)
    if not p.is_file():
        raise FileNotFoundError(f"계정 설정이 없습니다: @{paths.normalize_handle(handle)} — 셋팅 마법사(insta-setup)를 먼저 실행하세요")
    return _deep_merge(PROFILE_TEMPLATE, load_yaml(p) or {})


def save_profile(profile: dict) -> Path:
    h = paths.normalize_handle(profile.get("handle", ""))
    profile = _deep_merge(PROFILE_TEMPLATE, profile)
    profile["handle"] = h
    p = paths.profile_file(h)
    body = f"# @{h} 계정 설정 (insta-setup 으로 다시 만들 수 있음)\n" + dump_yaml(profile)
    paths.write_text_atomic(p, body)
    for sub in ("assets", "assets/character", "assets/fonts", "library"):
        (p.parent / sub).mkdir(parents=True, exist_ok=True)
    se = p.parent / "style_examples.md"
    if not se.exists():
        se.write_text("# 스타일 예시\n\n마음에 드는 훅·캡션 예시를 여기에 적어 두면 글쓰기 때 참고합니다.\n", encoding="utf-8")
    return p


def find_account_by_alias(alias: str) -> str | None:
    for h in list_accounts():
        try:
            if (load_profile(h).get("alias") or "") == alias:
                return h
        except Exception:
            continue
    return None


def resolve_account(name: str) -> str:
    """핸들(@ 포함 가능) 또는 별칭 → 핸들."""
    try:
        h = paths.normalize_handle(name)
        if paths.profile_file(h).is_file():
            return h
    except ValueError:
        pass
    h = find_account_by_alias(name)
    if h:
        return h
    raise FileNotFoundError(f"계정을 찾을 수 없습니다: {name} (등록된 계정: {', '.join(list_accounts()) or '없음'})")


def account_asset_path(handle: str, rel: str) -> Path | None:
    if not rel:
        return None
    p = (paths.account_dir(handle) / rel).resolve()
    if paths.account_dir(handle).resolve() not in p.parents:
        return None
    return p if p.is_file() else None
