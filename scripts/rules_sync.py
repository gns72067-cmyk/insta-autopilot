"""규칙 동기화 — CLIENT_SPEC 5장 순서 그대로.

    python rules_sync.py [--channel stable|beta] [--json]

1. <rules_url>/<channel>/latest.json → 내장 공개키로 서명 검증 (실패 시 5초 후 1회 재시도)
2. schema·channel·published_at(재전송) 확인, 변경 없으면 끝
3. min_client_version > 플러그인 버전 → 받지 않고 업데이트 안내
4. manifest: 파일 해시 == manifest_sha256 → 서명 검증 → version·bundle 일치
5. 번들: 크기·sha256
6. 임시 폴더에 풀며 항목별 검사(일반 파일·경로·중복·해시), 빠진 파일 확인
7. cache/rules/<version>/ 로 원자적 교체 → 상태 저장 → 변경 요약 안내

어느 단계든 실패하면 마지막으로 검증된 캐시로 진행한다(캐시도 다시 검증). 캐시도 없으면 종료 코드 2.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import shutil
import sys
import tarfile
import time
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import config as cfgmod  # noqa: E402
from common import log, paths  # noqa: E402
from common.rules import (  # noqa: E402
    MANIFEST_NAME, POINTER_NAME, SCHEMA, RulesError, load_state, open_envelope, plugin_version,
    save_state, semver, sha256, verify_cached,
)

L = log.get("rules_sync")
KEEP_VERSIONS = 3


class NetError(Exception):
    pass


# ---------------------------------------------------------------- 네트워크
def check_base(base: str) -> str:
    base = base.rstrip("/")
    u = urlsplit(base)
    local = u.hostname in ("localhost", "127.0.0.1", "::1")
    if u.scheme != "https" and not (u.scheme == "http" and local):
        raise NetError(f"규칙 URL 은 HTTPS 만 허용: {base}")
    if u.path not in ("", "/") or u.query or u.fragment:
        raise NetError(f"규칙 URL 은 경로 없는 주소여야 함: {base}")
    return base


def http_fetch(url: str, base: str, no_cache: bool = False) -> bytes:
    """같은 BASE 호스트 안의 리디렉션만 따라간다. 200 이외·타임아웃은 NetError."""
    import requests

    host = urlsplit(base).netloc
    headers = {"User-Agent": f"insta-autopilot/{plugin_version()}"}
    if no_cache:
        url = f"{url}?t={int(time.time() * 1000)}"
        headers["Cache-Control"] = "no-cache"
    for _ in range(5):
        try:
            r = requests.get(url, timeout=30, headers=headers, allow_redirects=False)
        except requests.RequestException as e:
            raise NetError(f"접속 실패: {type(e).__name__}")
        if r.status_code in (301, 302, 303, 307, 308):
            nxt = requests.compat.urljoin(url, r.headers.get("Location", ""))
            if urlsplit(nxt).netloc != host or urlsplit(nxt).scheme != urlsplit(base).scheme:
                raise NetError(f"다른 호스트로 리디렉션됨: {urlsplit(nxt).netloc}")
            url = nxt
            continue
        if r.status_code != 200:
            raise NetError(f"HTTP {r.status_code}: {urlsplit(url).path}")
        return r.content
    raise NetError("리디렉션이 너무 많음")


# ---------------------------------------------------------------- 검증 단계
def _iso(s: str) -> dt.datetime:
    try:
        t = dt.datetime.fromisoformat(s)
    except Exception:
        raise RulesError(f"published_at 형식 오류: {s!r}")
    if t.tzinfo is None:
        raise RulesError("published_at 에 시간대 오프셋이 없음")
    return t


def check_pointer(ptr: dict, channel: str, last: dict | None) -> None:
    if ptr.get("schema") != SCHEMA:
        raise RulesError(f"지원하지 않는 schema: {ptr.get('schema')} (플러그인 업데이트 필요)")
    if ptr.get("channel") != channel:
        raise RulesError(f"채널 불일치: 요청 {channel}, 받은 {ptr.get('channel')}")
    v = ptr.get("version")
    semver(v)
    if ptr.get("bundle") != f"bundles/{v}.tar.gz" or ptr.get("manifest") != f"bundles/{v}.manifest.json":
        raise RulesError("bundle·manifest 경로 형식 오류")
    for k, t in (("sha256", str), ("size", int), ("manifest_sha256", str), ("min_client_version", str),
                 ("published_at", str), ("changelog", list)):
        if not isinstance(ptr.get(k), t) or isinstance(ptr.get(k), bool):
            raise RulesError(f"latest.json 필드 오류: {k}")
    semver(ptr["min_client_version"])
    _iso(ptr["published_at"])
    if last and last.get("published_at") and _iso(ptr["published_at"]) < _iso(last["published_at"]):
        raise RulesError("published_at 이 마지막으로 받은 포인터보다 과거 — 재전송 의심으로 거부")


def extract_verified(bundle: bytes, man: dict, dest: Path) -> int:
    want = {}
    for f in man.get("files") or []:
        if not isinstance(f, dict) or not isinstance(f.get("path"), str):
            raise RulesError("manifest files 형식 오류")
        want[f["path"]] = f
    got: set[str] = set()
    try:
        tar = tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz")
    except Exception:
        raise RulesError("번들 압축 해제 실패")
    with tar:
        for m in tar.getmembers():
            name = m.name
            p = PurePosixPath(name)
            if not m.isfile() or p.is_absolute() or ".." in p.parts or "\\" in name or ":" in name \
                    or name.startswith("/") or not name or name in (MANIFEST_NAME, POINTER_NAME):
                raise RulesError(f"허용되지 않는 번들 항목: {name}")
            if name not in want or name in got:
                raise RulesError(f"manifest 에 없거나 중복된 파일: {name}")
            data = tar.extractfile(m).read()
            if sha256(data) != want[name]["sha256"] or len(data) != want[name]["size"]:
                raise RulesError(f"파일 해시 불일치: {name}")
            out = dest.joinpath(*p.parts)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            got.add(name)
    missing = set(want) - got
    if missing:
        raise RulesError(f"번들에 없는 파일: {sorted(missing)[:5]}")
    return len(got)


def install_atomic(tmp: Path, target: Path) -> None:
    old = None
    if target.exists():
        old = target.with_name(f".old-{target.name}-{uuid.uuid4().hex[:8]}")
        target.rename(old)
    try:
        tmp.rename(target)
    except Exception:
        if old is not None:
            old.rename(target)
        raise
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)


def prune(keep: set[str]) -> None:
    root = paths.rules_cache_dir()
    vers = []
    for p in root.iterdir():
        if p.is_dir() and (p.name.startswith(".tmp-") or p.name.startswith(".old-")):
            shutil.rmtree(p, ignore_errors=True)
        elif p.is_dir():
            try:
                semver(p.name)
                vers.append(p)
            except RulesError:
                pass
    vers.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for i, p in enumerate(vers):
        if p.name not in keep and i >= KEEP_VERSIONS:
            shutil.rmtree(p, ignore_errors=True)


# ---------------------------------------------------------------- 본체
def sync(channel: str | None = None, base: str | None = None, fetch=None, sleep=time.sleep,
         client_version: str | None = None) -> dict:
    cfg = cfgmod.load_config()
    channel = channel or cfg.get("channel") or "stable"
    if channel not in ("stable", "beta"):
        raise SystemExit(f"channel 은 stable|beta: {channel}")
    client_version = client_version or plugin_version()
    paths.rules_cache_dir().mkdir(parents=True, exist_ok=True)
    st = load_state()
    active = st.get("active") or {}
    res = {"status": None, "channel": channel, "version": active.get("version"),
           "previous_version": active.get("version"), "changelog": [], "warnings": [], "update_required": None}

    def fallback(reason: str) -> dict:
        L.warning("동기화 실패: %s", reason)
        res["warnings"].append(reason)
        v = active.get("version")
        if v:
            try:
                verify_cached(v)
                res.update(status="cached", version=v)
                return res
            except RulesError as e:
                res["warnings"].append(f"캐시도 검증 실패: {e}")
        res.update(status="error", version=None)
        return res

    try:
        base = check_base(base or cfg.get("rules_url") or cfgmod.DEFAULT_RULES_URL)
        get = fetch or (lambda url, no_cache=False: http_fetch(url, base, no_cache))

        # 1. 포인터
        ptr = raw_ptr = None
        for attempt in (1, 2):
            raw_ptr = get(f"{base}/{channel}/latest.json", no_cache=True)
            try:
                ptr = open_envelope(raw_ptr, "latest.json")
                break
            except RulesError:
                if attempt == 2:
                    raise
                sleep(5)
        # 2. 내용
        last = st["channels"].get(channel)
        check_pointer(ptr, channel, last)
        v = ptr["version"]
        cache_ok = False
        try:
            verify_cached(v)
            cache_ok = True
        except RulesError:
            pass
        if last and last.get("version") == v and last.get("published_at") == ptr["published_at"] and cache_ok:
            st["active"] = {"channel": channel, "version": v, "published_at": ptr["published_at"]}
            st["checked_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
            save_state(st)
            res.update(status="unchanged", version=v)
            return res
        # 3. 클라이언트 버전
        if semver(ptr["min_client_version"]) > semver(client_version):
            res["update_required"] = ptr["min_client_version"]
            return fallback(f"플러그인 업데이트 필요: 규칙 {v} 은(는) 플러그인 {ptr['min_client_version']} 이상 필요 (현재 {client_version})")
        # 4. manifest
        mraw = get(f"{base}/{ptr['manifest']}")
        if sha256(mraw) != ptr["manifest_sha256"]:
            raise RulesError("manifest 파일 해시가 latest.json 과 다름")
        man = open_envelope(mraw, "manifest")
        b = man.get("bundle") or {}
        if man.get("schema") != SCHEMA or man.get("version") != v or b.get("path") != ptr["bundle"] \
                or b.get("sha256") != ptr["sha256"] or b.get("size") != ptr["size"]:
            raise RulesError("manifest 의 version·bundle 이 latest.json 과 다름")
        # 5. 번들
        if cache_ok:
            n = None   # 같은 버전이 이미 검증된 채로 있음 (예: 롤백·채널 전환)
        else:
            bundle = get(f"{base}/{ptr['bundle']}")
            if len(bundle) != ptr["size"] or sha256(bundle) != ptr["sha256"]:
                raise RulesError("번들 크기·해시 불일치")
            # 6. 풀기
            tmp = paths.rules_cache_dir() / f".tmp-{uuid.uuid4().hex[:10]}"
            tmp.mkdir()
            try:
                n = extract_verified(bundle, man, tmp)
                (tmp / MANIFEST_NAME).write_bytes(mraw)
                (tmp / POINTER_NAME).write_bytes(raw_ptr)
                # 7. 적용
                install_atomic(tmp, paths.rules_version_dir(v))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            verify_cached(v)
        st["channels"][channel] = {"version": v, "published_at": ptr["published_at"]}
        st["active"] = {"channel": channel, "version": v, "published_at": ptr["published_at"]}
        st["checked_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        save_state(st)
        prune({v, active.get("version") or v})
        L.info("규칙 적용: %s %s (files=%s)", channel, v, n)
        if v == active.get("version"):
            if not cache_ok:
                res["warnings"].append("캐시가 서명과 달라 다시 받았습니다")
            res.update(status="unchanged", version=v)
        else:
            res.update(status="updated", version=v, changelog=list(ptr.get("changelog") or []))
        return res
    except (NetError, RulesError) as e:
        return fallback(str(e))


def report(res: dict) -> str:
    lines = []
    s = res["status"]
    if s == "updated":
        prev = res.get("previous_version")
        head = f"규칙이 {res['version']} 로 업데이트됨" + (f" (이전 {prev})" if prev and prev != res["version"] else "")
        lines.append(f"✔ {head}: " + (" / ".join(res["changelog"]) or "변경 요약 없음"))
    elif s == "unchanged":
        lines.append(f"✔ 규칙 최신 상태: {res['channel']} {res['version']}")
    elif s == "cached":
        lines.append(f"⚠ 규칙 서버 확인 실패 → 마지막으로 검증된 캐시 {res['version']} 로 진행")
    else:
        lines.append("✘ 사용할 수 있는 규칙이 없습니다. 네트워크·규칙 URL 을 확인한 뒤 다시 실행하세요. 작업을 시작하지 않습니다.")
    for w in res["warnings"]:
        lines.append(f"  · {w}")
    if res.get("update_required"):
        lines.append(f"  · 플러그인을 {res['update_required']} 이상으로 업데이트하세요 (/plugin 에서 insta-autopilot 업데이트)")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="규칙 번들 동기화")
    ap.add_argument("--channel", choices=["stable", "beta"])
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    res = sync(a.channel)
    print(json.dumps(res, ensure_ascii=False, indent=2) if a.json else report(res))
    return 0 if res["status"] != "error" else 2


if __name__ == "__main__":
    sys.exit(main())
