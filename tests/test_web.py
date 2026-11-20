"""검토 화면: 접속 범위·로그인·경로 차단·확인창 업로드 게이트·직접 게시 기록·빠른 모드 훅 자동 선택."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

import web


@pytest.fixture
def job(server, sync, home):
    from common import config
    import caption as capmod
    import job as jobmod

    server.publish("1.0.0")
    sync()
    config.save_profile({"handle": "tester", "display_name": "T", "alias": "테", "main_category": "alpha",
                         "upload": {"method": "manual"}})
    j = jobmod.new_job("tester", "A시 버스 요금 인상")
    (j / "card.png").write_bytes(b"\x89PNG fake")
    (j / "reel.mp4").write_bytes(bytes(range(256)) * 4)
    capmod.save_draft(j, "캡션 첫 줄\n본문\n출처: 연합뉴스\n#a #b #c\n")
    return j


@pytest.fixture
def http(job):
    web.Handler.allow = "local"
    web.Handler.hosts = {"127.0.0.1", "localhost"}
    srv = web.Server(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    jar = {}

    def call(path, body=None, headers=None, raw=False):
        h = {"X-IA": "1", "Content-Type": "application/json", **(headers or {})}
        if jar.get("c"):
            h["Cookie"] = jar["c"]
        req = urllib.request.Request(base + urllib.parse.quote(path, safe="/?=&"), data=None if body is None else json.dumps(body).encode(),
                                     headers=h, method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.headers.get("Set-Cookie"):
                    jar["c"] = r.headers["Set-Cookie"].split(";")[0]
                data = r.read()
                return r.status, (data if raw else json.loads(data or b"null")), r.headers
        except urllib.error.HTTPError as e:
            data = e.read()
            return e.code, (data if raw else json.loads(data or b"null")), e.headers

    call.jar = jar
    yield call
    srv.shutdown()
    srv.server_close()


def test_client_filter():
    assert web.client_allowed("127.0.0.1", "local")
    assert not web.client_allowed("192.168.0.5", "local")
    assert web.client_allowed("100.101.102.103", "tailscale")
    assert not web.client_allowed("192.168.0.5", "tailscale")
    assert not web.client_allowed("8.8.8.8", "lan")
    assert web.client_allowed("192.168.0.5", "lan")


def test_post_id_cannot_escape_output(job):
    pid = web.post_id(job)
    assert web.job_from_id(pid) == job.resolve()
    for bad in ("../x/y/z", f"{pid}/..", "2026-11-19/../../config", "2026-11-19/tester/..", ""):
        with pytest.raises(KeyError):
            web.job_from_id(bad)


def test_login_required_and_lockout(http, job):
    st, body, _ = http("/api/overview")
    assert st == 401
    for _ in range(web.MAX_FAILS):
        assert http("/api/login", {"code": "WRONGCODE1"})[0] == 401
    assert http("/api/login", {"code": web.access_code()})[0] == 429      # 잠김: 맞는 코드도 거부
    web.AUTH.fails.clear()
    assert http("/api/login", {"code": web.access_code().lower()})[0] == 200
    st, ov, _ = http("/api/overview")
    assert st == 200 and ov["dates"][0]["posts"][0]["id"] == web.post_id(job)


def test_post_without_custom_header_or_foreign_origin_is_rejected(http, job):
    assert http("/api/login", {"code": web.access_code()})[0] == 200
    pid = web.post_id(job)
    st, _, _ = http(f"/api/post/{pid}/approve", {}, headers={"X-IA": "0"})
    assert st == 403
    st, _, _ = http(f"/api/post/{pid}/approve", {}, headers={"Origin": "https://evil.example"})
    assert st == 403


def test_unknown_host_rejected(http):
    st, _, _ = http("/healthz", headers={"Host": "evil.example"}, raw=True)
    assert st == 421


def test_files_and_range(http, job):
    http("/api/login", {"code": web.access_code()})
    pid = web.post_id(job)
    st, data, h = http(f"/f/{pid}/reel.mp4", raw=True, headers={"Range": "bytes=10-19"})
    assert st == 206 and data == bytes(range(10, 20)) and h["Content-Range"] == "bytes 10-19/1024"
    assert http(f"/f/{pid}/meta.json", raw=True)[0] == 200
    assert http(f"/f/{pid}/../../../config.yaml", raw=True)[0] == 404
    assert http(f"/f/{pid}/caption.txt", raw=True)[0] == 200
    (job / "secret.py").write_text("x")
    assert http(f"/f/{pid}/secret.py", raw=True)[0] == 404


def test_upload_needs_approve_and_matching_handle(http, job):
    http("/api/login", {"code": web.access_code()})
    pid = web.post_id(job)
    st, body, _ = http(f"/api/post/{pid}/upload", {"confirm_handle": "someone_else"})
    assert st == 409 and "계정" in body["error"]
    st, body, _ = http(f"/api/post/{pid}/upload", {"confirm_handle": "tester"})
    assert st == 409 and "확정" in body["error"]
    from common import paths
    m = paths.read_json(job / "meta.json")
    m["stages"]["reel"] = "done"
    paths.write_json(job / "meta.json", m)
    st, body, _ = http(f"/api/post/{pid}/approve", {})
    assert st == 409 and "캡션" in body["error"]                      # 캡션 확정 전
    st, body, _ = http(f"/api/post/{pid}/caption", {"text": "고친 첫 줄\n본문\n출처: 연합뉴스\n#a #b #c"})
    assert st == 200 and body["edited"]
    assert http(f"/api/post/{pid}/approve", {})[0] == 200
    assert paths.read_json(job / "meta.json")["stages"]["card"] == "approved"


def test_manual_record_validates_url(http, job):
    from common import paths

    http("/api/login", {"code": web.access_code()})
    pid = web.post_id(job)
    st, body, _ = http(f"/api/post/{pid}/record", {"url": "https://evil.example/reel/abc"})
    assert st == 409
    st, body, _ = http(f"/api/post/{pid}/record", {"url": "https://www.instagram.com/reel/AbC_12-x/"})
    assert st == 200
    m = paths.read_json(job / "meta.json")
    assert m["upload"]["permalink"].startswith("https://www.instagram.com/reel/") and web.status_of(m)[0] == "published"


def test_status_labels():
    assert web.status_of({"stages": {"card": "waiting_user"}})[0] == "review"
    assert web.status_of({"stages": {"card": "approved", "caption": "approved"}})[0] == "ready"
    assert web.status_of({"stages": {"upload": "waiting_user"}})[0] == "manual"
    assert web.status_of({"stages": {}, "upload": {"status": "approved_browser"}})[0] == "approved"
    assert web.status_of({"stages": {}, "upload": {"status": "scheduled", "planned_at": "2026-11-20T18:00"}})[0] == "scheduled"


def test_caption_credit_follows_photo(job):
    from common import paths

    (job / "caption.txt").write_text("본문\n사진: 옛 작가 (CC BY 4.0)\n#a\n", encoding="utf-8")
    m = paths.read_json(job / "meta.json")
    m["photo"] = {"credit": "사진: 새 작가 (CC0)"}
    paths.write_json(job / "meta.json", m)
    assert web.fix_caption_credit(job, "사진: 옛 작가 (CC BY 4.0)")
    assert "사진: 새 작가 (CC0)" in (job / "caption.txt").read_text(encoding="utf-8")


def test_quick_mode_picks_recommended_passing_hook(job, monkeypatch):
    import make
    from common import paths

    paths.write_json(job / "factsheet.json", {"claims": []})
    paths.write_json(job / "hooks.json", {"strong": {"l1": "s"}, "normal": {"l1": "n"}, "soft": {"l1": "o"},
                                          "recommended": "soft"})

    def fake(hooks, fs, meta, c, only=None):
        return {"ok": False, "versions": {"strong": {"ok": True}, "normal": {"ok": True}, "soft": {"ok": False}}}
    import checks
    monkeypatch.setattr(checks, "check_hooks", fake)
    assert make.choose_hook(job)[0] == "strong"            # 추천(soft)이 검사 실패 → 통과한 첫 버전
    with pytest.raises(SystemExit):
        make.choose_hook(job, "soft")


def test_card_edit_renders_card_only_and_reel_follows_on_approve(job, monkeypatch):
    import os
    import time

    from common import paths

    assert [s[0] for s in web.render_steps(job)] == ["카드 렌더"]          # 수정 직후엔 카드만
    assert not web.reel_stale(job)
    later = time.time() + 5
    os.utime(job / "card.png", (later, later))                             # 카드를 다시 만든 상황
    assert web.reel_stale(job)
    m = paths.read_json(job / "meta.json")
    m["stages"].update(reel="done", caption="approved")
    paths.write_json(job / "meta.json", m)
    started = {}

    def fake_start(pid, title, steps, after=None):
        started["steps"] = [s[0] for s in steps]
        return {"id": "t1"}
    monkeypatch.setattr(web.TASKS, "start", fake_start)
    r = web.act_approve(web.post_id(job), {})
    assert r["task"] == "t1" and started["steps"] == ["릴스 렌더", "확정"]
    m["stages"]["card"] = "approved"
    paths.write_json(job / "meta.json", m)
    web.act_upload(web.post_id(job), {"confirm_handle": "tester", "dry_run": True})
    assert started["steps"] == ["릴스 렌더", "업로드"]                     # 업로드 전에도 오래된 릴스는 다시
