"""방식 B — Instagram API (Instagram 로그인) 개인 토큰.

    python ig_api.py token-set <account> [--short-lived]   ← 이용자가 직접 자기 터미널에서 실행 (토큰을 채팅에 쓰지 않는다)
    python ig_api.py token-status [account]
    python ig_api.py token-refresh-due                     ← 예약 작업에서 매일: 만료 7일 전이면 갱신
    python ig_api.py me <account>
    python ig_api.py limit <account>

토큰 저장: OS 키체인(keyring, 서비스 'insta-autopilot', 이름 'ig:<handle>') 우선,
          키체인을 못 쓰면 accounts/<handle>/.token (현재 사용자만 읽기). 토큰 값은 출력·로그에 남기지 않는다.

Meta 문서 확인 (2026-11-19):
- 콘텐츠 게시: https://developers.facebook.com/docs/instagram-platform/content-publishing/
  · Instagram 로그인 방식(graph.instagram.com)은 resumable(로컬 파일 직접) 업로드를 지원하지 않는다
    ("Only for apps that have implemented Facebook Login for Business") → video_url(공개 URL) 방식만 사용.
  · 한도: GET /<IG_ID>/content_publishing_limit (문서 안에 24시간 100건/50건 표기가 섞여 있음 → 응답의 config.quota_total 사용)
  · 상태: GET /<CONTAINER_ID>?fields=status_code → EXPIRED | ERROR | FINISHED | IN_PROGRESS | PUBLISHED
  · 게시: POST /<IG_ID>/media_publish (creation_id)
- 토큰 갱신: https://developers.facebook.com/docs/instagram-platform/reference/refresh_access_token/
  · GET https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token — 발급 24시간 이후, 만료 전, 갱신 시 60일
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import stat
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import config as cfgmod  # noqa: E402
from common import log, paths  # noqa: E402

L = log.get("ig_api")
GRAPH = "https://graph.instagram.com"
DEFAULT_VERSION = "v27.0"
KEYRING_SERVICE = "insta-autopilot"
REFRESH_BEFORE = dt.timedelta(days=7)
MIN_AGE_FOR_REFRESH = dt.timedelta(hours=24)


class ApiError(Exception):
    pass


class TokenError(ApiError):
    pass


def now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def api_version() -> str:
    return cfgmod.load_config().get("ig_api_version") or DEFAULT_VERSION


# ---------------------------------------------------------------- 토큰 저장소
def _token_file(handle: str) -> Path:
    return paths.account_dir(handle) / ".token"


def _keyring():
    try:
        import keyring

        keyring.get_keyring()
        return keyring
    except Exception:
        return None


def save_token(handle: str, data: dict) -> str:
    blob = json.dumps(data)
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(KEYRING_SERVICE, f"ig:{handle}", blob)
            _token_file(handle).unlink(missing_ok=True)
            return "keychain"
        except Exception:
            pass
    f = _token_file(handle)
    f.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(blob)
    try:
        os.chmod(f, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return "file"


def load_token(handle: str) -> dict | None:
    kr = _keyring()
    if kr is not None:
        try:
            blob = kr.get_password(KEYRING_SERVICE, f"ig:{handle}")
            if blob:
                return json.loads(blob)
        except Exception:
            pass
    f = _token_file(handle)
    if f.is_file():
        return json.loads(f.read_text(encoding="utf-8"))
    return None


# ---------------------------------------------------------------- HTTP
class Client:
    """graph.instagram.com 호출. session 은 테스트에서 바꿔 끼울 수 있다."""

    def __init__(self, handle: str, session=None, sleep=time.sleep):
        import requests

        self.handle = paths.normalize_handle(handle)
        self.s = session or requests.Session()
        self.sleep = sleep
        self.ver = api_version()

    def _tok(self) -> dict:
        t = load_token(self.handle)
        if not t or not t.get("access_token"):
            raise TokenError(f"@{self.handle} 토큰이 없습니다 — 이용자가 터미널에서 `ia ig_api token-set {self.handle}` 실행")
        if t.get("expires_at") and dt.datetime.fromisoformat(t["expires_at"]) <= now():
            raise TokenError(f"@{self.handle} 토큰이 만료됐습니다 ({t['expires_at']}) — token-set 으로 다시 등록")
        return t

    def _call(self, method: str, path: str, params=None, data=None, token: str | None = None, versioned=True) -> dict:
        tok = token or self._tok()["access_token"]
        url = f"{GRAPH}/{self.ver}/{path.lstrip('/')}" if versioned else f"{GRAPH}/{path.lstrip('/')}"
        r = self.s.request(method, url, params=params, data=data, timeout=60,
                           headers={"Authorization": f"Bearer {tok}"})
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:300]}
        if r.status_code >= 400 or "error" in body:
            err = body.get("error") or {}
            msg = err.get("message") or str(body)[:200]
            code = err.get("code")
            L.warning("API 오류 %s %s: %s", method, path, msg)
            if code in (190, 102) or r.status_code == 401:
                raise TokenError(f"토큰 오류: {msg}")
            raise ApiError(f"{method} {path}: {msg}")
        return body

    # -------- 계정·한도
    def me(self) -> dict:
        return self._call("GET", "me", params={"fields": "user_id,username,account_type"})

    def verify_account(self) -> dict:
        m = self.me()
        if (m.get("username") or "").lower() != self.handle:
            raise ApiError(f"토큰의 계정 @{m.get('username')} ≠ 대상 계정 @{self.handle} — 중단")
        return m

    def limit(self, ig_id: str) -> dict:
        b = self._call("GET", f"{ig_id}/content_publishing_limit", params={"fields": "quota_usage,config"})
        d = (b.get("data") or [{}])[0]
        total = (d.get("config") or {}).get("quota_total")
        used = d.get("quota_usage", 0)
        return {"used": used, "total": total, "remaining": (total - used) if total is not None else None}

    # -------- 릴스 게시
    def create_reel(self, ig_id: str, video_url: str, caption: str) -> str:
        b = self._call("POST", f"{ig_id}/media", data={"media_type": "REELS", "video_url": video_url,
                                                        "caption": caption, "share_to_feed": "true", "thumb_offset": "0"})
        return b["id"]

    def wait_container(self, cid: str, first: float = 10, timeout_min: float = 5, cap: float = 60) -> str:
        """지수 백오프(first → ×2, 최대 cap 초)로 최대 timeout_min 분."""
        deadline = time.monotonic() + timeout_min * 60
        delay = first
        waited = 0.0
        while True:
            st = self._call("GET", cid, params={"fields": "status_code"}).get("status_code")
            if st in ("FINISHED", "PUBLISHED"):
                return st
            if st in ("ERROR", "EXPIRED"):
                raise ApiError(f"컨테이너 상태 {st}")
            if waited + delay > timeout_min * 60 or time.monotonic() + delay > deadline:
                raise ApiError(f"컨테이너 처리 시간 초과 ({timeout_min}분)")
            self.sleep(delay)
            waited += delay
            delay = min(cap, delay * 2)

    def publish(self, ig_id: str, cid: str) -> str:
        return self._call("POST", f"{ig_id}/media_publish", data={"creation_id": cid})["id"]

    def media(self, media_id: str) -> dict:
        return self._call("GET", media_id, params={"fields": "id,permalink,timestamp,caption,media_type"})

    def recent_media(self, ig_id: str, n: int = 5) -> list[dict]:
        return self._call("GET", f"{ig_id}/media", params={"fields": "id,caption,timestamp,permalink", "limit": str(n)}).get("data", [])

    def insights(self, media_id: str, metrics: str = "views,reach,likes,comments,shares,saved") -> dict:
        b = self._call("GET", f"{media_id}/insights", params={"metric": metrics})
        return {d["name"]: (d.get("values") or [{}])[0].get("value", d.get("total_value", {}).get("value"))
                for d in b.get("data", [])}

    # -------- 토큰 갱신
    def refresh_if_due(self, force: bool = False) -> dict:
        t = self._tok()
        exp = dt.datetime.fromisoformat(t["expires_at"]) if t.get("expires_at") else None
        obtained = dt.datetime.fromisoformat(t["obtained_at"]) if t.get("obtained_at") else now() - MIN_AGE_FOR_REFRESH
        due = force or exp is None or exp - now() <= REFRESH_BEFORE
        if not due:
            return {"refreshed": False, "expires_at": t.get("expires_at")}
        if now() - obtained < MIN_AGE_FOR_REFRESH:
            return {"refreshed": False, "expires_at": t.get("expires_at"), "note": "발급 24시간 이내라 갱신 불가"}
        b = self._call("GET", "refresh_access_token", params={"grant_type": "ig_refresh_token"},
                       token=t["access_token"], versioned=False)
        new = {**t, "access_token": b["access_token"], "obtained_at": now().isoformat(timespec="seconds"),
               "expires_at": (now() + dt.timedelta(seconds=int(b.get("expires_in", 5184000)))).isoformat(timespec="seconds")}
        save_token(self.handle, new)
        L.info("토큰 갱신 @%s → %s", self.handle, new["expires_at"])
        return {"refreshed": True, "expires_at": new["expires_at"]}


def register_token(handle: str, token: str, session=None, short_lived_secret: str | None = None) -> dict:
    """이용자가 붙여 넣은 토큰을 확인(계정 일치)한 뒤 저장. 단기 토큰이면 장기 토큰으로 교환."""
    c = Client(handle, session=session)
    expires_in = 5184000
    if short_lived_secret:
        b = c.s.get(f"{GRAPH}/access_token", params={"grant_type": "ig_exchange_token", "client_secret": short_lived_secret,
                                                    "access_token": token}, timeout=60).json()
        if "access_token" not in b:
            raise TokenError(f"장기 토큰 교환 실패: {(b.get('error') or {}).get('message', b)}")
        token, expires_in = b["access_token"], int(b.get("expires_in", expires_in))
    m = c._call("GET", "me", params={"fields": "user_id,username,account_type"}, token=token)
    if (m.get("username") or "").lower() != c.handle:
        raise TokenError(f"이 토큰은 @{m.get('username')} 계정입니다 (대상 @{c.handle}) — 저장하지 않음")
    if (m.get("account_type") or "").upper() not in ("BUSINESS", "MEDIA_CREATOR", "CREATOR"):
        raise TokenError(f"비즈니스/크리에이터 계정이 아닙니다 (account_type={m.get('account_type')})")
    data = {"access_token": token, "user_id": str(m.get("user_id")), "username": m.get("username"),
            "account_type": m.get("account_type"), "obtained_at": now().isoformat(timespec="seconds"),
            "expires_at": (now() + dt.timedelta(seconds=expires_in)).isoformat(timespec="seconds")}
    where = save_token(c.handle, data)
    return {"where": where, "username": m.get("username"), "user_id": data["user_id"], "expires_at": data["expires_at"]}


def token_status(handle: str) -> dict:
    t = load_token(handle)
    if not t:
        return {"handle": handle, "status": "없음"}
    exp = dt.datetime.fromisoformat(t["expires_at"]) if t.get("expires_at") else None
    left = (exp - now()).days if exp else None
    st = "만료" if exp and exp <= now() else ("갱신 필요" if exp and exp - now() <= REFRESH_BEFORE else "정상")
    return {"handle": handle, "status": st, "expires_at": t.get("expires_at"), "days_left": left,
            "username": t.get("username"), "store": "keychain" if not _token_file(handle).is_file() else "file"}


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("token-set")
    p.add_argument("account")
    p.add_argument("--short-lived", action="store_true", help="단기 토큰이면 앱 시크릿으로 장기 토큰 교환")
    p = sub.add_parser("token-status")
    p.add_argument("account", nargs="?")
    sub.add_parser("token-refresh-due")
    for n in ("me", "limit"):
        p = sub.add_parser(n)
        p.add_argument("account")
    a = ap.parse_args()
    try:
        if a.cmd == "token-set":
            import getpass

            h = paths.normalize_handle(a.account)
            if not sys.stdin.isatty():
                print("✘ 토큰은 이용자가 직접 자기 터미널에서 입력해야 합니다 (채팅·파일로 넘기지 않음)")
                return 2
            tok = getpass.getpass(f"@{h} 장기 액세스 토큰 (입력 내용은 보이지 않음): ").strip()
            secret = getpass.getpass("앱 시크릿 (단기 토큰 교환용): ").strip() if a.short_lived else None
            r = register_token(h, tok, short_lived_secret=secret)
            print(f"✔ @{r['username']} 토큰 저장 ({'OS 키체인' if r['where'] == 'keychain' else '계정 폴더 .token'}) · 만료 {r['expires_at']}")
            return 0
        if a.cmd == "token-status":
            hs = [cfgmod.resolve_account(a.account)] if a.account else cfgmod.list_accounts()
            for h in hs:
                s = token_status(h)
                print(f"{'✔' if s['status'] == '정상' else '⚠'} @{h}: {s['status']}"
                      + (f" · 만료 {s['expires_at']} ({s['days_left']}일 남음) · {s['store']}" if s.get("expires_at") else ""))
            return 0
        if a.cmd == "token-refresh-due":
            bad = 0
            for h in cfgmod.list_accounts():
                if (cfgmod.load_profile(h).get("upload") or {}).get("method") != "api" or not load_token(h):
                    continue
                try:
                    r = Client(h).refresh_if_due()
                    print(f"{'✔ 갱신' if r['refreshed'] else '· 유지'} @{h}: 만료 {r['expires_at']}" + (f" ({r['note']})" if r.get("note") else ""))
                except ApiError as e:
                    bad += 1
                    print(f"✘ @{h} 토큰 갱신 실패: {e} — 방식 C(수동)로 바꾸거나 token-set 으로 다시 등록")
            return 1 if bad else 0
        h = cfgmod.resolve_account(a.account)
        c = Client(h)
        if a.cmd == "me":
            m = c.verify_account()
            print(f"✔ 토큰 계정 @{m['username']} ({m.get('account_type')}) = 대상 계정")
        else:
            m = c.verify_account()
            lim = c.limit(str(m["user_id"]))
            print(f"✔ @{h} 게시 한도: 24시간 {lim['used']}/{lim['total']} 사용 · 남음 {lim['remaining']}")
        return 0
    except ApiError as e:
        print(f"✘ {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
