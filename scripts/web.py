"""검토 화면 — 날짜별 게시물, 카드·릴스 미리보기, 훅·사진·템플릿·캡션 수정, 업로드(확인창).

    python web.py start [--where local|tailscale|lan] [--port 8765]   # 백그라운드로 켜기 (이미 켜져 있으면 주소만)
    python web.py status | stop
    python web.py open [<job_dir>]          # 켜져 있지 않으면 켜고, 열 주소(접속 코드 포함)를 출력
    python web.py code [--reset]            # 접속 코드 보기 / 새로 만들기 (새로 만들면 기존 로그인은 모두 끊김)
    python web.py serve ...                 # (내부용) 서버 본체

- 서버는 이용자 PC 에서만 돈다. 이용자 데이터는 밖으로 보내지 않는다(외부 요청 없음).
- 접속 범위: local = 이 PC 만 · tailscale = 이 PC + 같은 Tailscale 계정 기기(밖에서 휴대폰) · lan = 같은 Wi-Fi
- 모든 화면·파일은 접속 코드로 로그인해야 열린다. 코드는 HOME/web/access_code (이 PC 에만).
- 업로드는 확인창(대상 계정·시각·캡션 첫 줄)에서 한 번 더 눌러야 실행된다 — workflow 규칙 1절.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.server
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import config as cfgmod  # noqa: E402
from common import log, paths  # noqa: E402

L = log.get("web")
HERE = Path(__file__).resolve().parent
UI_FILE = paths.PLUGIN_ROOT / "assets" / "web" / "index.html"
DEFAULT_PORT = 8765
COOKIE = "ia_s"
SESSION_DAYS = 30
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"      # 헷갈리는 0/O·1/I 제외
MAX_FAILS, LOCK_MIN = 5, 15
FILE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".txt", ".md", ".json"}
TS_NET = [ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48")]


# ---------------------------------------------------------------- 상태 파일
def web_dir() -> Path:
    d = paths.home() / "web"
    d.mkdir(parents=True, exist_ok=True)
    return d


def access_code(reset: bool = False) -> str:
    f = web_dir() / "access_code"
    if reset or not f.is_file():
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(10))
        paths.write_text_atomic(f, code + "\n")
        paths.write_json(web_dir() / "sessions.json", {})
        return code
    return f.read_text(encoding="utf-8").strip()


def server_file() -> Path:
    return web_dir() / "server.json"


def web_cfg() -> dict:
    w = cfgmod.load_config().get("web") or {}
    return {"port": int(w.get("port") or DEFAULT_PORT), "where": w.get("where") or "local"}


# ---------------------------------------------------------------- 접속 범위
def tailscale_exe() -> str | None:
    exe = shutil.which("tailscale")
    if exe:
        return exe
    for p in (r"C:\Program Files\Tailscale\tailscale.exe", "/Applications/Tailscale.app/Contents/MacOS/Tailscale"):
        if Path(p).is_file():
            return p
    return None


def tailscale_info() -> dict | None:
    exe = tailscale_exe()
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=10)
        st = json.loads(r.stdout or "{}")
    except Exception:
        return None
    me = st.get("Self") or {}
    ips = [ip for ip in (me.get("TailscaleIPs") or st.get("TailscaleIPs") or []) if "." in ip]
    if st.get("BackendState") != "Running" or not ips:
        return {"running": False, "state": st.get("BackendState")}
    return {"running": True, "ip": ips[0], "dns": (me.get("DNSName") or "").rstrip(".") or None}


def lan_ip() -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))        # 패킷은 나가지 않음 — 기본 경로의 내 주소만 얻는다
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def plan_bind(where: str, port: int) -> dict:
    """어디에 열지와 이용자에게 보여 줄 주소."""
    urls = [f"http://127.0.0.1:{port}/"]
    if where == "local":
        return {"bind": ["127.0.0.1"], "urls": urls, "hosts": {"127.0.0.1", "localhost"}, "allow": "local"}
    if where == "tailscale":
        ts = tailscale_info()
        if not ts:
            raise SystemExit("✘ Tailscale 이 설치돼 있지 않습니다. README 의 '밖에서 보기' 를 따라 설치·로그인한 뒤 다시 실행하세요.")
        if not ts["running"]:
            raise SystemExit(f"✘ Tailscale 이 연결돼 있지 않습니다(상태: {ts.get('state')}). Tailscale 앱에서 로그인·연결 후 다시.")
        hosts = {"127.0.0.1", "localhost", ts["ip"]} | ({ts["dns"], ts["dns"].split(".")[0]} if ts["dns"] else set())
        ext = [f"http://{ts['dns']}:{port}/"] if ts["dns"] else []
        return {"bind": ["127.0.0.1", ts["ip"]], "urls": ext + [f"http://{ts['ip']}:{port}/"] + urls,
                "hosts": hosts, "allow": "tailscale"}
    if where == "lan":
        ip = lan_ip()
        return {"bind": ["0.0.0.0"], "urls": ([f"http://{ip}:{port}/"] if ip else []) + urls,
                "hosts": {"127.0.0.1", "localhost"} | ({ip} if ip else set()), "allow": "lan"}
    raise SystemExit("✘ --where 는 local | tailscale | lan")


def client_allowed(ip: str, allow: str) -> bool:
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    if a.is_loopback:
        return True
    if allow == "tailscale":
        return any(a in n for n in TS_NET)
    if allow == "lan":
        return a.is_private or any(a in n for n in TS_NET)
    return False


# ---------------------------------------------------------------- 게시물 데이터
def job_from_id(pid: str) -> Path:
    """'2026-11-19/handle/01_slug' → 작업 폴더 (출력 폴더 밖이면 거부)."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}/[a-z0-9._]{1,30}/[^/\\]{1,80}", pid or ""):
        raise KeyError(pid)
    root = paths.output_root().resolve()
    job = (root / pid).resolve()
    if root not in job.parents or not (job / "meta.json").is_file():
        raise KeyError(pid)
    return job


def post_id(job: Path) -> str:
    return f"{job.parent.parent.name}/{job.parent.name}/{job.name}"


def status_of(meta: dict) -> tuple[str, str]:
    st = meta.get("stages") or {}
    up = meta.get("upload") or {}
    if st.get("upload") == "done" or up.get("status") == "published":
        return "published", "게시됨"
    if up.get("status") == "scheduled":
        return "scheduled", f"예약 {str(up.get('planned_at', ''))[5:16].replace('T', ' ')}"
    if up.get("status") in ("failed", "blocked") or st.get("upload") == "failed":
        return "failed", "업로드 실패"
    if up.get("status") == "approved_browser":
        return "approved", "업로드 승인됨 · Claude 진행 대기"
    if st.get("upload") == "waiting_user":
        return "manual", "직접 게시 대기"
    if st.get("card") == "approved" and st.get("caption") == "approved":
        return "ready", "업로드 대기"
    if st.get("reel") == "done" or st.get("card") == "waiting_user":
        return "review", "검토 필요"
    if any(v == "failed" for v in st.values()):
        return "failed", "제작 실패"
    cur = next((k for k in ("research", "hooks", "photo", "card", "reel", "caption") if st.get(k) not in ("done", "approved")), "")
    return "making", f"만드는 중 · {cur}"


def file_url(pid: str, name: str, job: Path) -> str | None:
    f = job / name
    if not f.is_file():
        return None
    return f"/f/{urllib.parse.quote(pid)}/{urllib.parse.quote(name)}?v={int(f.stat().st_mtime)}"


def overview() -> dict:
    root = paths.output_root()
    dates = []
    aliases = {}
    for h in cfgmod.list_accounts():
        try:
            p = cfgmod.load_profile(h)
            aliases[h] = {"alias": p.get("alias") or h, "display_name": p.get("display_name") or h,
                          "active": p.get("active", True)}
        except Exception:
            aliases[h] = {"alias": h, "display_name": h, "active": True}
    if root.is_dir():
        for d in sorted((x for x in root.iterdir() if x.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", x.name)),
                        reverse=True):
            posts = []
            for hd in sorted(x for x in d.iterdir() if x.is_dir()):
                for job in sorted(x for x in hd.iterdir() if (x / "meta.json").is_file()):
                    m = paths.read_json(job / "meta.json") or {}
                    pid = post_id(job)
                    code, label = status_of(m)
                    hook = m.get("hook") or {}
                    posts.append({"id": pid, "handle": m.get("handle") or hd.name, "title": m.get("title"),
                                  "status": code, "status_label": label, "hook": [hook.get("l1"), hook.get("l2")],
                                  "thumb": f"/t/{urllib.parse.quote(pid)}?v={int((job / 'card.png').stat().st_mtime)}"
                                  if (job / "card.png").is_file() else None,
                                  "permalink": (m.get("upload") or {}).get("permalink"),
                                  "updated_at": m.get("updated_at") or m.get("created_at")})
            if posts:
                dates.append({"date": d.name, "posts": posts})
    return {"dates": dates, "accounts": aliases, "today": dt.date.today().isoformat()}


def post_detail(pid: str) -> dict:
    import checks
    from common.rules import compose

    job = job_from_id(pid)
    m = paths.read_json(job / "meta.json") or {}
    profile = cfgmod.load_profile(m["handle"])
    method = (profile.get("upload") or {}).get("method") or "browser"
    hooks = paths.read_json(job / "hooks.json")
    hc = paths.read_json(job / "checks_hook.json") or {}
    fs = paths.read_json(job / "factsheet.json") or {}
    pc = paths.read_json(job / "photo_candidates.json") or {}
    cands = []
    for cnd in pc.get("candidates") or []:
        prev = (cnd.get("analysis") or {}).get("preview_916")
        rel = None
        if prev:
            # 저장된 절대 경로 대신 작업 폴더 안의 같은 파일 (데이터 폴더를 옮겨도 보이게)
            cand_f = job / "photo_candidates" / Path(prev.replace("\\", "/")).name
            rel = f"photo_candidates/{cand_f.name}" if cand_f.is_file() else None
        cands.append({"no": cnd.get("no"), "source": cnd.get("source"), "title": cnd.get("title"),
                      "license": cnd.get("license"), "credit": cnd.get("credit"), "page_url": cnd.get("page_url"),
                      "faces": (cnd.get("analysis") or {}).get("faces"),
                      "preview": file_url(pid, rel, job) if rel else None})
    cap = (job / "caption.txt").read_text(encoding="utf-8") if (job / "caption.txt").is_file() else None
    cap_orig = (job / "caption_original.txt").read_text(encoding="utf-8") if (job / "caption_original.txt").is_file() else None
    cap_check = None
    if cap is not None:
        try:
            c = compose(profile, (m.get("rules") or {}).get("version"))
            cap_check = checks.check_caption(cap, fs, m, c["checks"], c["banned_phrases"], user_edit=True)
        except Exception as e:
            cap_check = {"errors": [f"검사 실패: {e}"], "warnings": []}
    claims = [{"id": c.get("id"), "text": c.get("claim"), "type": c.get("type"), "verified": c.get("verified"),
               "sources": [{"outlet": s.get("outlet"), "title": s.get("title"), "url": s.get("url")}
                           for s in c.get("sources") or []]} for c in fs.get("claims") or []]
    code, label = status_of(m)
    files = {k: file_url(pid, k, job) for k in ("card.png", "card_grid.png", "card_guide.png", "reel.mp4",
                                                  "cta_last.png", "credits.txt", "factsheet.md")}
    return {"id": pid, "status": code, "status_label": label, "method": method, "reel_stale": reel_stale(job),
            "account": {"handle": m["handle"], "alias": profile.get("alias"), "display_name": profile.get("display_name")},
            "photo_fit": m.get("photo_fit") or cfgmod.load_config().get("photo_fit") or "fill",
            "meta": {k: m.get(k) for k in ("title", "created_at", "updated_at", "rules", "stages", "selected_hook", "hook",
                                           "hook_edited", "template", "photo", "auto", "card", "reel", "caption", "upload")},
            "hooks": hooks, "hook_checks": {v: {"ok": r.get("ok"), "errors": [e["msg"] for e in r.get("errors", [])],
                                                "warnings": [w["msg"] for w in r.get("warnings", [])]}
                                            for v, r in (hc.get("versions") or {}).items()},
            "photos": cands, "caption": cap, "caption_original": cap_orig, "caption_check": cap_check,
            "claims": claims, "files": files}


# ---------------------------------------------------------------- 실시간 미리보기 (브라우저가 같은 템플릿을 직접 그림)
TPL_FILE_RE = re.compile(r"[A-Za-z0-9_.-]{1,80}\.(html|css|js|png|svg|woff2?)")
BRAND_KINDS = ("logo", "character", "font")


def templates_dir_of(job: Path) -> Path:
    from common.rules import verify_cached

    m = paths.read_json(job / "meta.json") or {}
    ver = (m.get("rules") or {}).get("version")
    if not ver:
        raise KeyError("rules")
    verify_cached(ver)                                   # 서명 검증된 캐시만
    return paths.rules_version_dir(ver) / "templates"


def preview_data(pid: str) -> dict:
    """renderSlots 에 넘길 값 — 서버 렌더(render_card.card_inputs)와 같은 입력, 이미지 주소만 이 서버 경로."""
    import render_card
    from common.rules import compose

    job = job_from_id(pid)
    m = paths.read_json(job / "meta.json") or {}
    profile = cfgmod.load_profile(m["handle"])
    c = compose(profile, (m.get("rules") or {}).get("version"))
    variant = m.get("template") or "A"
    photo = m.get("photo") or {}
    date = dt.date.fromisoformat(job.parent.parent.name)
    slots, brand, bfont = render_card.card_inputs(c, profile, m.get("hook") or {}, variant, photo.get("credit", ""), "", date)
    bp = profile.get("brand") or {}
    q = urllib.parse.quote
    for k in ("logo", "character"):
        slots[k] = f"/asset/{q(m['handle'])}/{k}" if bp.get(k) and slots.get(k) else ""
    reel_photo = job / "photo_reel.jpg"
    t = c["template_meta"]["templates"]["reel_9x16"]
    return {"tpl": f"/tpl/{q(pid)}/{t['file']}", "size": t["size"], "variant": variant, "brand": brand, "slots": slots,
            "photo": file_url(pid, "photo_reel.jpg", job) if reel_photo.is_file() else None,
            "brand_font": f"/asset/{q(m['handle'])}/font" if bfont else None}


# ---------------------------------------------------------------- 작업 실행 (렌더 등 오래 걸리는 것)
class Tasks:
    def __init__(self):
        self.lock = threading.Lock()
        self.items: dict[str, dict] = {}

    def busy(self, pid: str) -> dict | None:
        return next((t for t in self.items.values() if t["post"] == pid and t["status"] == "running"), None)

    def start(self, pid: str, title: str, steps, after=None) -> dict:
        with self.lock:
            if self.busy(pid):
                raise RuntimeError("이 게시물은 이미 작업 중입니다. 끝난 뒤 다시 눌러 주세요.")
            tid = secrets.token_hex(6)
            t = {"id": tid, "post": pid, "title": title, "status": "running", "log": [],
                 "started": time.time(), "ended": None}
            self.items[tid] = t
            for old in sorted(self.items.values(), key=lambda x: x["started"])[:-50]:
                self.items.pop(old["id"], None)
        threading.Thread(target=self._run, args=(t, steps, after), daemon=True).start()
        return t

    def _run(self, t, steps, after):
        try:
            for label, args, ok_codes in steps:
                t["log"].append(f"▶ {label}")
                r = ia(*args)
                out = (r.stdout or "").strip()
                if r.returncode in ok_codes:
                    out = "\n".join(ln for ln in out.splitlines() if _brief_line(ln))
                if out:
                    t["log"].append(out[-3000:])
                if r.returncode not in ok_codes or "Traceback (most recent call last)" in (r.stderr or ""):
                    # 종료 코드 1 은 '검수 문제 남음'과 '오류로 멈춤'이 같아서, 오류 흔적이 있으면 실패로 본다
                    t["log"].append((r.stderr or "").strip()[-1500:])
                    t["status"] = "failed"
                    return
            if after:
                after(t)
            t["status"] = "done"
        except Exception as e:
            t["log"].append(f"✘ {e}")
            L.error("task %s 실패: %s", t["title"], traceback.format_exc())
            t["status"] = "failed"
        finally:
            t["ended"] = time.time()


def _brief_line(ln: str) -> bool:
    """진행 표시용: 결과·경고 줄만 (파일 경로·JSON 은 뺀다)."""
    t = ln.strip()
    return bool(t) and t[0] in "✔⚠✘·" and ":\\" not in t and not t.startswith("{")


def ia(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", IA_IN_VENV="1")
    return subprocess.run([sys.executable, str(HERE / "ia.py"), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)


TASKS = Tasks()


def render_steps(job: Path, variant: str | None = None) -> list:
    """수정 직후에는 카드만 다시 만든다(몇 초). 릴스는 미리보기를 누르거나 확정·업로드할 때 만든다."""
    return [("카드 렌더", ["render_card", str(job), *(["--variant", variant] if variant else [])], (0, 1))]


def reel_stale(job: Path) -> bool:
    """릴스가 없거나 지금 카드보다 먼저 만들어졌으면 True."""
    reel, card = job / "reel.mp4", job / "card.png"
    return not reel.is_file() or (card.is_file() and reel.stat().st_mtime < card.stat().st_mtime)


def reel_step(job: Path) -> tuple:
    return ("릴스 렌더", ["render_reel", str(job)], (0,))


def _mark_manual(job: Path, key: str) -> None:
    m = paths.read_json(job / "meta.json") or {}
    a = m.get("auto") or {}
    if key in a:
        a[key + "_changed_by_user"] = True
        m["auto"] = a
        paths.write_json(job / "meta.json", m)


def act_hook(pid: str, body: dict) -> dict:
    import checks
    from common.rules import compose

    job = job_from_id(pid)
    m = paths.read_json(job / "meta.json")
    hooks = paths.read_json(job / "hooks.json") or {}
    v = body.get("version")
    if v not in hooks:
        raise ValueError("훅 버전이 없습니다")
    h = {k: (body.get(k) if body.get(k) is not None else hooks[v][k]).strip() for k in ("l1", "l2", "l3")}
    h["basis"] = hooks[v].get("basis", {})
    fs = paths.read_json(job / "factsheet.json") or {}
    c = compose(cfgmod.load_profile(m["handle"]), (m.get("rules") or {}).get("version"))
    res = checks.check_hooks({"selected": h}, fs, m, c, "selected")
    r = res["versions"]["selected"]
    if not r["ok"]:
        return {"ok": False, "errors": [e["msg"] for e in r["errors"]], "warnings": [w["msg"] for w in r["warnings"]]}
    args = ["job", "select-hook", str(job), v]
    for k in ("l1", "l2", "l3"):
        if h[k] != hooks[v][k]:
            args += [f"--{k}", h[k]]
    steps = [("훅 적용", args, (0,)), ("훅 검사", ["checks", "hook", str(job), "--selected"], (0,))] + render_steps(job)
    t = TASKS.start(pid, "훅 바꾸고 다시 렌더", steps, after=lambda t: _mark_manual(job, "hook"))
    return {"ok": True, "task": t["id"], "warnings": [w["msg"] for w in r["warnings"]]}


def fix_caption_credit(job: Path, old: str | None, t: dict | None = None) -> bool:
    """사진을 바꾸면 캡션의 사진 출처 줄도 새 사진 것으로 바꾼다(초안·현재본 모두, 다른 글자는 그대로)."""
    import caption as capmod

    m = paths.read_json(job / "meta.json") or {}
    ph = m.get("photo") or {}
    new = ph.get("caption_credit") if "caption_credit" in ph else ph.get("credit")
    if not old or old == new:
        return False
    changed = False
    for name in ("caption_original.txt", "caption.txt"):
        f = job / name
        if f.is_file() and old in (txt := f.read_text(encoding="utf-8")):
            if new:
                txt = txt.replace(old, new)
            else:  # 새 사진은 출처 줄이 필요 없음 — 이전 출처 줄을 지운다
                txt = "\n".join(l for l in txt.split("\n") if l.strip() != old.strip())
            paths.write_text_atomic(f, txt)
            changed = True
    if changed:
        cap = m.get("caption") or {}
        if cap.get("approved") and (job / "caption.txt").is_file():
            cap["sha256"] = capmod.sha((job / "caption.txt").read_text(encoding="utf-8").strip() + "\n")
            m["caption"] = cap
            paths.write_json(job / "meta.json", m)
        if t is not None:
            t["log"].append(f"✔ 캡션 사진 출처 줄도 바꿈: {new}" if new else "✔ 캡션의 이전 사진 출처 줄을 지움(새 사진은 표기 불필요)")
    elif t is not None and (job / "caption.txt").is_file():
        t["log"].append("⚠ 캡션에서 이전 사진 출처 줄을 찾지 못했습니다 — 캡션의 출처 줄을 직접 확인해 주세요")
    return changed


def act_photo(pid: str, body: dict) -> dict:
    job = job_from_id(pid)
    pick = body.get("pick")
    if pick is None:
        fetch = ["image_fetch", str(job), "--none"]
    elif isinstance(pick, int) and pick > 0:
        fetch = ["image_fetch", str(job), "--pick", str(pick)]
    else:
        raise ValueError("사진 번호가 올바르지 않습니다")
    cur = (paths.read_json(job / "meta.json") or {}).get("template")
    variant = "C" if pick is None else (cur if cur in ("A", "B") else "A")
    m0 = paths.read_json(job / "meta.json") or {}
    ph0 = m0.get("photo") or {}
    old_credit = ph0.get("caption_credit") if "caption_credit" in ph0 else ph0.get("credit")

    def after(t):
        _mark_manual(job, "photo")
        fix_caption_credit(job, old_credit, t)
    t = TASKS.start(pid, "사진 바꾸고 다시 렌더", [("사진 받기", fetch, (0,))] + render_steps(job, variant), after=after)
    return {"ok": True, "task": t["id"]}


def act_photo_search(pid: str, body: dict) -> dict:
    """이용자가 넣은 검색어로 화이트리스트 출처에서 사진 후보를 다시 찾는다 (렌더는 사진을 고를 때)."""
    job = job_from_id(pid)
    qs = [q.strip() for q in re.split(r"[,\n]", str(body.get("query") or "")) if q.strip()][:4]
    if not qs or any(len(q) > 80 for q in qs):
        raise ValueError("검색어를 넣어 주세요 (쉼표로 4개까지, 각 80자 이내)")
    args = ["image_search", str(job)]
    for q in qs:
        args += ["--query", q]
    t = TASKS.start(pid, "사진 검색", [("사진 검색", args, (0,))])
    return {"ok": True, "task": t["id"]}


def act_fit(pid: str, body: dict) -> dict:
    job = job_from_id(pid)
    fit = body.get("fit")
    if fit not in ("fill", "inset"):
        raise ValueError("배치는 fill(꽉 채우기) 또는 inset(사진 박스)")
    m = paths.read_json(job / "meta.json") or {}
    if not (m.get("photo") or {}).get("path"):
        raise ValueError("사진이 없습니다")
    m["photo_fit"] = fit
    paths.write_json(job / "meta.json", m)
    t = TASKS.start(pid, "사진 배치 바꾸고 다시 렌더", render_steps(job))
    return {"ok": True, "task": t["id"]}


def act_template(pid: str, body: dict) -> dict:
    job = job_from_id(pid)
    v = body.get("variant")
    if v not in ("A", "B", "C"):
        raise ValueError("템플릿은 A·B·C")
    if v != "C" and not ((paths.read_json(job / "meta.json") or {}).get("photo") or {}).get("path"):
        raise ValueError("사진이 없어 A·B 템플릿을 쓸 수 없습니다. 먼저 사진을 고르세요.")
    t = TASKS.start(pid, f"템플릿 {v} 로 다시 렌더", render_steps(job, v))
    return {"ok": True, "task": t["id"]}


def act_caption(pid: str, body: dict) -> dict:
    import caption as capmod

    job = job_from_id(pid)
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("캡션이 비어 있습니다")
    if TASKS.busy(pid):
        raise RuntimeError("이 게시물은 작업 중입니다. 끝난 뒤 저장해 주세요.")
    r = capmod.finish(job, text)
    return {"ok": True, "errors": r["check"]["errors"], "warnings": r["check"]["warnings"], "edited": r["edited"]}


def act_reel(pid: str, body: dict) -> dict:
    job = job_from_id(pid)
    if not (job / "card.png").is_file():
        raise ValueError("카드가 아직 없습니다")
    t = TASKS.start(pid, "릴스 만들기", [reel_step(job)])
    return {"ok": True, "task": t["id"]}


def act_approve(pid: str, body: dict) -> dict:
    """확정 — 릴스가 카드보다 오래됐으면 먼저 다시 만든 뒤 확정한다."""
    job = job_from_id(pid)
    m = paths.read_json(job / "meta.json") or {}
    st = m.get("stages") or {}
    if not (job / "card.png").is_file():
        raise ValueError("카드가 아직 없습니다")
    if st.get("caption") != "approved":
        raise ValueError("캡션을 먼저 확정(저장)해 주세요")
    stage = ("확정", ["job", "stage", str(job), "card", "approved", "--note", "검토 화면에서 확정"], (0,))
    if reel_stale(job) or st.get("reel") != "done":
        t = TASKS.start(pid, "릴스 만들고 확정", [reel_step(job), stage])
        return {"ok": True, "task": t["id"]}
    if TASKS.busy(pid):
        raise RuntimeError("이 게시물은 작업 중입니다. 끝난 뒤 다시 눌러 주세요.")
    r = ia(*stage[1])
    if r.returncode:
        raise RuntimeError(r.stdout + r.stderr)
    return {"ok": True}


def act_preflight(pid: str, body: dict) -> dict:
    import upload

    job = job_from_id(pid)
    at = upload._parse_at(body.get("at")) if body.get("at") else None
    try:
        pf = upload.preflight(job, at)
    except upload.UploadBlocked as e:
        return {"ok": False, "items": [], "error": str(e)}
    return pf


def act_upload(pid: str, body: dict) -> dict:
    job = job_from_id(pid)
    m = paths.read_json(job / "meta.json") or {}
    if body.get("confirm_handle") != m.get("handle"):
        raise ValueError("확인창의 계정이 이 게시물의 계정과 다릅니다")
    dry = bool(body.get("dry_run"))
    if not dry and (m.get("stages") or {}).get("card") != "approved":
        raise ValueError("먼저 '확정'을 눌러 주세요")
    args = ["upload", "run", str(job)]
    if dry:
        args.append("--dry-run")
    else:
        args += ["--approval", "업로드"]
    if body.get("at"):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", body["at"]):
            raise ValueError("예약 시각 형식이 올바르지 않습니다")
        args += ["--at", body["at"]]

    def after(t):
        mm = paths.read_json(job / "meta.json") or {}
        up = mm.get("upload") or {}
        if not dry:
            up["approval"] = {**(up.get("approval") or {}), "via": "web-confirm", "handle_confirmed": m["handle"]}
            if (up.get("status") not in ("published", "scheduled")) and (paths.read_json(job / "upload_browser.json")):
                up["status"] = "approved_browser"
            mm["upload"] = up
            paths.write_json(job / "meta.json", mm)
    steps = ([reel_step(job)] if reel_stale(job) else []) + [("업로드", args, (0,))]   # 릴스가 오래됐으면 먼저 다시
    t = TASKS.start(pid, "업로드 리허설" if dry else "업로드", steps, after=after)
    return {"ok": True, "task": t["id"]}


def act_record(pid: str, body: dict) -> dict:
    """방식 C(직접 게시) — 이용자가 올린 게시물 주소를 기록."""
    job = job_from_id(pid)
    url = str(body.get("url") or "").strip()
    if not re.fullmatch(r"https://(www\.)?instagram\.com/(p|reel|reels)/[A-Za-z0-9_-]+/?(\?.*)?", url):
        raise ValueError("인스타그램 게시물 주소(https://www.instagram.com/reel/… 또는 /p/…)를 넣어 주세요")
    r = ia("upload", "record", str(job), "--url", url, "--method", "manual")
    if r.returncode:
        raise RuntimeError((r.stdout + r.stderr).strip()[-500:])
    return {"ok": True}


ACTIONS = {"fit": act_fit, "record": act_record, "reel": act_reel, "photo_search": act_photo_search, "hook": act_hook, "photo": act_photo, "template": act_template, "caption": act_caption,
           "approve": act_approve, "preflight": act_preflight, "upload": act_upload}


# ---------------------------------------------------------------- 썸네일
def thumb(job: Path) -> Path | None:
    src = job / "card.png"
    if not src.is_file():
        return None
    key = hashlib.sha1(f"{src}|{src.stat().st_mtime}".encode()).hexdigest()[:16]
    d = web_dir() / "thumbs"
    d.mkdir(exist_ok=True)
    out = d / f"{key}.jpg"
    if not out.is_file():
        from PIL import Image

        im = Image.open(src).convert("RGB")
        im.thumbnail((360, 640))
        im.save(out, "JPEG", quality=82)
    return out


# ---------------------------------------------------------------- 로그인
class Auth:
    def __init__(self):
        self.lock = threading.Lock()
        self.fails: dict[str, list[float]] = {}

    def _sessions(self) -> dict:
        return paths.read_json(web_dir() / "sessions.json", {}) or {}

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        s = self._sessions().get(hashlib.sha256(token.encode()).hexdigest())
        return bool(s) and s.get("exp", 0) > time.time()

    def locked(self, ip: str) -> bool:
        now = time.time()
        f = [x for x in self.fails.get(ip, []) if now - x < LOCK_MIN * 60]
        self.fails[ip] = f
        return len(f) >= MAX_FAILS

    def login(self, ip: str, code: str) -> str | None:
        with self.lock:
            if self.locked(ip):
                return None
            if not secrets.compare_digest((code or "").strip().upper().replace("-", "").replace(" ", ""), access_code()):
                self.fails.setdefault(ip, []).append(time.time())
                L.warning("웹 로그인 실패 %s", ip)
                return None
            self.fails.pop(ip, None)
            tok = secrets.token_urlsafe(32)
            ss = {k: v for k, v in self._sessions().items() if v.get("exp", 0) > time.time()}
            ss[hashlib.sha256(tok.encode()).hexdigest()] = {"exp": time.time() + SESSION_DAYS * 86400, "ip": ip,
                                                             "at": dt.datetime.now().isoformat(timespec="seconds")}
            paths.write_json(web_dir() / "sessions.json", ss)
            return tok

    def logout(self, token: str | None) -> None:
        if token:
            ss = self._sessions()
            ss.pop(hashlib.sha256(token.encode()).hexdigest(), None)
            paths.write_json(web_dir() / "sessions.json", ss)


AUTH = Auth()


# ---------------------------------------------------------------- HTTP
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "insta-autopilot"
    sys_version = ""
    allow = "local"
    hosts: set = set()

    def log_message(self, fmt, *args):
        L.info("%s %s", self.client_address[0], fmt % args)

    # 공통 검사 --------------------------------------------------
    def _guard(self) -> bool:
        if not client_allowed(self.client_address[0], self.allow):
            self._send(403, b"forbidden", "text/plain")
            return False
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if host not in {h.lower() for h in self.hosts}:
            self._send(421, b"unknown host", "text/plain")       # DNS 재바인딩 방지
            return False
        return True

    def _token(self) -> str | None:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return None

    def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        headers = dict(headers or {})
        self.send_header("Cache-Control", headers.pop("Cache-Control", None)
                         or ("no-store" if ctype.startswith(("application/json", "text/html")) else "private, max-age=86400"))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", headers.pop("X-Frame-Options", "DENY"))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, data, headers=None):
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", headers)

    # GET -------------------------------------------------------
    def do_GET(self):
        if not self._guard():
            return
        u = urllib.parse.urlsplit(self.path)
        p = u.path
        if p in ("/", "/index.html"):
            html = UI_FILE.read_bytes()
            self._send(200, html, "text/html; charset=utf-8",
                       {"Content-Security-Policy": "default-src 'self'; img-src 'self' data:; media-src 'self'; "
                                                   "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                                                   "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"})
            return
        if p == "/healthz":
            self._json(200, {"ok": True})
            return
        if p == "/fonts/Pretendard.woff2":
            f = paths.FONTS_DIR / "PretendardVariable.woff2"
            if f.is_file():
                self._send(200, f.read_bytes(), "font/woff2")
            else:
                self._send(404, b"", "text/plain")
            return
        if not AUTH.valid(self._token()):
            self._json(401, {"error": "login"})
            return
        try:
            if p == "/api/overview":
                self._json(200, overview())
            elif p.startswith("/api/preview/"):
                self._json(200, preview_data(urllib.parse.unquote(p[len("/api/preview/"):])))
            elif p.startswith("/tpl/"):
                pid, _, name = urllib.parse.unquote(p[5:]).rpartition("/")
                if not TPL_FILE_RE.fullmatch(name):
                    raise KeyError(name)
                f = templates_dir_of(job_from_id(pid)) / name
                if not f.is_file():
                    raise KeyError(name)
                ctype = {"html": "text/html; charset=utf-8", "css": "text/css; charset=utf-8",
                         "js": "text/javascript; charset=utf-8"}.get(f.suffix[1:], mimetypes.guess_type(f.name)[0] or "application/octet-stream")
                self._send(200, f.read_bytes(), ctype, {
                    "X-Frame-Options": "SAMEORIGIN", "Cache-Control": "no-store",
                    "Content-Security-Policy": "default-src 'self' data:; img-src 'self' data:; font-src 'self' data:; "
                                               "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                                               "connect-src 'none'; frame-ancestors 'self'; base-uri 'self'"})
            elif p.startswith("/asset/"):
                handle, _, kind = urllib.parse.unquote(p[7:]).partition("/")
                if kind not in BRAND_KINDS or handle not in cfgmod.list_accounts():
                    raise KeyError(kind)
                rel = (cfgmod.load_profile(handle).get("brand") or {}).get(kind)
                f = cfgmod.account_asset_path(handle, rel) if rel else None
                if not f or not Path(f).is_file():
                    raise KeyError(kind)
                self._send(200, Path(f).read_bytes(), mimetypes.guess_type(str(f))[0] or "application/octet-stream")
            elif p.startswith("/api/post/"):
                self._json(200, post_detail(urllib.parse.unquote(p[len("/api/post/"):])))
            elif p == "/api/tasks":
                q = urllib.parse.parse_qs(u.query)
                post = (q.get("post") or [None])[0]
                items = [t for t in TASKS.items.values() if not post or t["post"] == post]
                self._json(200, sorted(items, key=lambda t: -t["started"])[:20])
            elif p.startswith("/t/"):
                job = job_from_id(urllib.parse.unquote(p[3:]))
                f = thumb(job)
                if not f:
                    raise KeyError(p)
                self._send(200, f.read_bytes(), "image/jpeg")
            elif p.startswith("/f/"):
                self._file(urllib.parse.unquote(p[3:]))
            else:
                self._json(404, {"error": "없음"})
        except KeyError:
            self._json(404, {"error": "없음"})
        except Exception as e:
            L.error("GET %s: %s", p, traceback.format_exc())
            self._json(500, {"error": str(e)})

    def _file(self, rest: str):
        parts = rest.split("/")
        if len(parts) < 4:
            raise KeyError(rest)
        job = job_from_id("/".join(parts[:3]))
        f = (job / "/".join(parts[3:])).resolve()
        if job not in f.parents or not f.is_file() or f.suffix.lower() not in FILE_EXT:
            raise KeyError(rest)
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or f.suffix == ".json":
            ctype = ("application/json" if f.suffix == ".json" else "text/plain") + "; charset=utf-8"
        size = f.stat().st_size
        rng = self.headers.get("Range")
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng or "")
        if m and (m.group(1) or m.group(2)):                     # 휴대폰 동영상 재생에 필요
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
            else:
                start, end = max(0, size - int(m.group(2))), size - 1
            if start > end or start >= size:
                self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
                return
            with open(f, "rb") as fh:
                fh.seek(start)
                data = fh.read(end - start + 1)
            self._send(206, data, ctype, {"Content-Range": f"bytes {start}-{end}/{size}", "Accept-Ranges": "bytes"})
            return
        self._send(200, f.read_bytes(), ctype, {"Accept-Ranges": "bytes"})

    def do_HEAD(self):
        self.do_GET()

    # POST ------------------------------------------------------
    def do_POST(self):
        if not self._guard():
            return
        # 다른 사이트에서 보낸 요청 차단: 사용자 정의 헤더 + 같은 출처
        origin = self.headers.get("Origin")
        if self.headers.get("X-IA") != "1" or (origin and urllib.parse.urlsplit(origin).hostname not in self.hosts):
            self._json(403, {"error": "forbidden"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 200_000)) or b"{}") if n else {}
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "잘못된 요청"})
            return
        p = urllib.parse.urlsplit(self.path).path
        ip = self.client_address[0]
        if p == "/api/login":
            if AUTH.locked(ip):
                self._json(429, {"error": f"실패가 많아 {LOCK_MIN}분 동안 잠겼습니다"})
                return
            tok = AUTH.login(ip, str(body.get("code") or ""))
            if not tok:
                self._json(401, {"error": "접속 코드가 맞지 않습니다"})
                return
            self._json(200, {"ok": True}, {"Set-Cookie": f"{COOKIE}={tok}; Path=/; HttpOnly; SameSite=Strict; "
                                                          f"Max-Age={SESSION_DAYS * 86400}"})
            return
        if not AUTH.valid(self._token()):
            self._json(401, {"error": "login"})
            return
        if p == "/api/logout":
            AUTH.logout(self._token())
            self._json(200, {"ok": True}, {"Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0"})
            return
        m = re.fullmatch(r"/api/post/(.+)/(\w+)", p)
        if not m or m.group(2) not in ACTIONS:
            self._json(404, {"error": "없음"})
            return
        pid = urllib.parse.unquote(m.group(1))
        try:
            L.info("action %s %s from %s", m.group(2), pid, ip)
            self._json(200, ACTIONS[m.group(2)](pid, body))
        except KeyError:
            self._json(404, {"error": "게시물을 찾을 수 없습니다"})
        except (ValueError, RuntimeError) as e:
            self._json(409, {"error": str(e)})
        except SystemExit as e:
            self._json(409, {"error": str(e.code)})
        except Exception as e:
            L.error("POST %s: %s", p, traceback.format_exc())
            self._json(500, {"error": str(e)})


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def serve(where: str, port: int) -> int:
    plan = plan_bind(where, port)
    access_code()
    Handler.allow = plan["allow"]
    Handler.hosts = plan["hosts"]
    servers = []
    for addr in plan["bind"]:
        try:
            servers.append(Server((addr, port), Handler))
        except OSError as e:
            for s in servers:
                s.server_close()
            print(f"✘ {addr}:{port} 에 열 수 없습니다: {e}")
            return 2
    paths.write_json(server_file(), {"pid": os.getpid(), "port": port, "where": where, "urls": plan["urls"],
                                     "code": code_stamp(),
                                     "started_at": dt.datetime.now().astimezone().isoformat(timespec="seconds")})
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers[1:]]
    for t in threads:
        t.start()
    print(f"✔ 검토 화면 실행 중: {', '.join(plan['urls'])}", flush=True)
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for s in servers[1:]:
            s.shutdown()
        for s in servers:
            s.server_close()
    return 0


# ---------------------------------------------------------------- 시작·중지
def running() -> dict | None:
    info = paths.read_json(server_file())
    if not info:
        return None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{info['port']}/healthz", timeout=2) as r:
            if r.status == 200:
                return info
    except Exception:
        pass
    return None


def port_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def code_stamp() -> str:
    """서버 코드 버전 — 플러그인 스크립트가 바뀌면 달라진다(떠 있는 옛 서버를 알아보고 다시 켜기 위해)."""
    fs = sorted(HERE.glob("*.py")) + sorted((HERE / "common").glob("*.py"))
    return hashlib.sha1("|".join(f"{f.name}:{f.stat().st_mtime_ns}" for f in fs).encode()).hexdigest()[:12]


def start(where: str | None, port: int | None) -> dict:
    cfg = web_cfg()
    where = where or cfg["where"]
    explicit_port = port is not None
    port = port or cfg["port"]
    cur = running()
    if cur and cur.get("where") == where and cur.get("port") == port and cur.get("code") == code_stamp():
        return cur
    if cur and cur.get("code") != code_stamp():
        print("· 플러그인이 업데이트되어 검토 화면을 새 버전으로 다시 켭니다 (로그인은 유지)")
        port = cur.get("port") or port
    plan_bind(where, port)                       # 미리 확인(Tailscale 미설치 등) — 실패면 켜져 있던 서버는 그대로 두고 종료
    if cur:
        stop()
        for _ in range(40):                      # 옛 서버가 포트를 놓을 때까지 (같은 주소 유지)
            if port_free(port):
                break
            time.sleep(0.25)
    if not port_free(port):
        if explicit_port:
            raise SystemExit(f"✘ 포트 {port} 를 다른 프로그램이 쓰고 있습니다. 다른 번호로: ia web start --port {port + 1}")
        port = next((p for p in range(port + 1, port + 20) if port_free(p)), None)
        if port is None:
            raise SystemExit("✘ 비어 있는 포트를 찾지 못했습니다 — ia web start --port <번호>")
        print(f"· 기본 포트를 다른 프로그램이 쓰고 있어 {port} 번으로 켭니다")
    cfgall = cfgmod.load_config()
    if (cfgall.get("web") or {}).get("where") != where or (cfgall.get("web") or {}).get("port") != port:
        cfgall["web"] = {**(cfgall.get("web") or {}), "where": where, "port": port}   # 고른 범위를 기억
        cfgmod.save_config(cfgall)
    logf = open(web_dir() / "server.log", "a", encoding="utf-8")
    kw = {}
    if sys.platform.startswith("win"):
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED | NEW_GROUP | NO_WINDOW
    else:
        kw["start_new_session"] = True
    env = dict(os.environ, PYTHONIOENCODING="utf-8", IA_IN_VENV="1")
    subprocess.Popen([sys.executable, str(HERE / "web.py"), "serve", "--where", where, "--port", str(port)],
                     stdout=logf, stderr=logf, stdin=subprocess.DEVNULL, env=env, cwd=str(paths.home()), **kw)
    for _ in range(40):
        time.sleep(0.25)
        cur = running()
        if cur:
            return cur
    raise SystemExit(f"✘ 검토 화면을 켜지 못했습니다 — {web_dir() / 'server.log'} 확인")


def stop() -> bool:
    info = paths.read_json(server_file())
    if not info:
        return False
    pid = info.get("pid")
    try:
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        else:
            os.kill(pid, 15)
    except Exception:
        pass
    server_file().unlink(missing_ok=True)
    return True


def open_url(info: dict, job: Path | None = None) -> str:
    base = info["urls"][0]
    frag = f"code={access_code()}"
    if job:
        frag += "&post=" + urllib.parse.quote(post_id(Path(job).resolve()))
    return f"{base}#{frag}"


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("start", "serve"):
        p = sub.add_parser(name)
        p.add_argument("--where", choices=["local", "tailscale", "lan"])
        p.add_argument("--port", type=int)
    sub.add_parser("status")
    sub.add_parser("stop")
    p = sub.add_parser("open")
    p.add_argument("job", nargs="?", type=Path)
    p = sub.add_parser("code")
    p.add_argument("--reset", action="store_true")
    a = ap.parse_args()

    if a.cmd == "serve":
        cfg = web_cfg()
        return serve(a.where or cfg["where"], a.port or cfg["port"])
    if a.cmd in ("start", "open"):
        info = start(getattr(a, "where", None), getattr(a, "port", None))
        print(f"✔ 검토 화면 켜짐 ({ {'local': '이 PC 만', 'tailscale': '이 PC + Tailscale 기기', 'lan': '같은 Wi-Fi'}[info['where']] })")
        for u in info["urls"]:
            print(f"  주소: {u}")
        print(f"  접속 코드: {access_code()}")
        if a.cmd == "open":
            print(f"  바로 열기: {open_url(info, a.job)}")
        print(json.dumps({**info, "code": access_code(), **({"open": open_url(info, a.job)} if a.cmd == 'open' else {})},
                         ensure_ascii=False))
        return 0
    if a.cmd == "status":
        info = running()
        if not info:
            print("· 검토 화면이 꺼져 있습니다 (ia web start)")
            return 1
        print(f"✔ 실행 중 ({info['where']}) — " + ", ".join(info["urls"]))
        return 0
    if a.cmd == "stop":
        print("✔ 껐습니다" if stop() else "· 켜져 있지 않습니다")
        return 0
    if a.cmd == "code":
        c = access_code(a.reset)
        print(f"✔ 접속 코드: {c}" + (" (새로 만듦 — 다른 기기는 다시 로그인)" if a.reset else ""))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
