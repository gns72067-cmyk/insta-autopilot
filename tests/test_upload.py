"""업로드: 명시 승인 게이트, dry-run, 방식 B 흐름·재시도·중복 확인, 계정 확인 실패 중단, 토큰 갱신."""
from __future__ import annotations

import datetime as dt
import json

import pytest

import upload
from conftest import base_files


@pytest.fixture
def job(server, sync, home, monkeypatch):
    from common import config
    import ig_api
    import job as jobmod

    monkeypatch.setattr(ig_api, "_keyring", lambda: None)      # 테스트가 실제 OS 키체인을 건드리지 않게
    server.publish("1.0.0")
    sync()

    def make(method="browser", handle="tester"):
        config.save_profile({"handle": handle, "display_name": "T", "alias": "테" + handle[-1], "main_category": "alpha",
                             "upload": {"method": method}})
        j = jobmod.new_job(handle, "A시 버스 요금 인상")
        (j / "reel.mp4").write_bytes(b"fake")
        import caption as capmod

        capmod.save_draft(j, "캡션 첫 줄\n본문\n출처: 연합뉴스\n#a #b #c\n")
        capmod.finish(j)
        return j

    return make


def ok_preflight(ig_id="17841400000000000"):
    def pf(job, at=None, client=None, method=None):
        return {"ok": True, "items": [], "ig_id": ig_id, "handle": "tester", "method": method,
                "planned_at": "", "caption_changed_after_approval": False}
    return pf


# ---------------------------------------------------------------- 승인 게이트
@pytest.mark.parametrize("text", ["네", "OK", "전체 추천대로", "ㅇㅇ", "업로드 하지 마", "", None])
def test_no_upload_without_explicit_word(job, monkeypatch, text):
    j = job("api")
    monkeypatch.setattr(upload, "preflight", ok_preflight())
    called = []
    monkeypatch.setattr(upload, "run_api", lambda *a, **k: called.append(1) or {})
    with pytest.raises(upload.UploadBlocked):
        upload.run(j, text)
    assert not called


def test_batch_label_must_be_in_r4_approval(job, monkeypatch, home):
    j = job("api")
    date = j.parent.parent.name
    (home / "output" / date / "batch.json").write_text(json.dumps(
        {"accounts": {"tester": [{"label": "테r1", "job": str(j)}, {"label": "테r2", "job": str(j)},
                                 {"label": "남의것", "job": str(j.parent / "other")}]}}), encoding="utf-8")
    assert not upload.approval_ok(j, "업로드", "남의것")     # 다른 작업 폴더의 라벨로는 승인 안 됨
    assert upload.approval_ok(j, "업로드: 테r1", "테r1")
    assert not upload.approval_ok(j, "업로드: 테r1", "테r2")
    assert not upload.approval_ok(j, "테r2 보류하고 업로드", "테r2")
    assert upload.approval_ok(j, "테r2 보류하고 업로드", "테r1")


def test_preflight_failure_blocks_even_with_approval(job, monkeypatch):
    j = job("manual")
    monkeypatch.setattr(upload, "preflight", lambda *a, **k: {"ok": False, "items": [
        {"no": 6, "name": "금지 표현", "ok": False, "detail": "충격"}], "handle": "tester", "method": "manual",
        "planned_at": ""})
    with pytest.raises(upload.UploadBlocked, match="최종 확인 실패"):
        upload.run(j, "업로드")


# ---------------------------------------------------------------- dry-run
class FakeClient:
    def __init__(self, username="tester", statuses=("FINISHED",), fail_publish=0, recent=None):
        self.username = username
        self.calls = []
        self.statuses = list(statuses)
        self.fail_publish = fail_publish
        self.recent = recent or []

    def refresh_if_due(self):
        self.calls.append("refresh")
        return {"refreshed": False}

    def verify_account(self):
        import ig_api

        self.calls.append("me")
        if self.username != "tester":
            raise ig_api.ApiError(f"토큰의 계정 @{self.username} ≠ 대상 계정 @tester — 중단")
        return {"user_id": "1784", "username": "tester"}

    def limit(self, ig_id):
        return {"used": 1, "total": 100, "remaining": 99}

    def create_reel(self, ig_id, url, caption):
        self.calls.append(("create", url, caption))
        return "C1"

    def wait_container(self, cid, *a):
        self.calls.append("wait")
        return "FINISHED"

    def publish(self, ig_id, cid):
        import ig_api

        self.calls.append("publish")
        if self.fail_publish:
            self.fail_publish -= 1
            raise ig_api.ApiError("일시 오류")
        return "M1"

    def media(self, mid):
        return {"permalink": "https://www.instagram.com/reel/XYZ/"}

    def recent_media(self, ig_id, n=5):
        self.calls.append("recent")
        return self.recent


@pytest.fixture
def temp_host(monkeypatch):
    import tempurl

    put, deleted = [], []
    monkeypatch.setattr(tempurl, "put", lambda f, wait=True: put.append(f) or {"id": "t1", "url": "https://tmp.example/x.mp4"})
    monkeypatch.setattr(tempurl, "delete", lambda i: deleted.append(i) or True)
    return put, deleted


def test_api_dry_run_creates_nothing(job, monkeypatch, temp_host):
    j = job("api")
    monkeypatch.setattr(upload, "preflight", ok_preflight())
    fc = FakeClient()
    r = upload.run(j, None, dry_run=True, client=fc)
    assert r["dry_run"] and not any(isinstance(c, tuple) for c in fc.calls) and "publish" not in fc.calls
    assert temp_host[0] == []
    meta = json.loads((j / "meta.json").read_text(encoding="utf-8"))
    assert (meta.get("upload") or {}).get("status") != "published"


def test_browser_dry_run_stops_before_caption(job, monkeypatch):
    j = job("browser")
    monkeypatch.setattr(upload, "preflight", ok_preflight())
    r = upload.run(j, None, dry_run=True)
    assert r["plan"]["dry_run"] and r["plan"]["stop_before"] == "캡션 입력"
    assert (j / "upload_browser.json").is_file()


def test_api_upload_records_permalink_and_deletes_temp(job, monkeypatch, temp_host):
    j = job("api")
    monkeypatch.setattr(upload, "preflight", ok_preflight())
    fc = FakeClient()
    r = upload.run(j, "업로드", client=fc)
    assert r["permalink"].endswith("/XYZ/") and ("create", "https://tmp.example/x.mp4", "캡션 첫 줄\n본문\n출처: 연합뉴스\n#a #b #c") in fc.calls
    assert temp_host[1] == ["t1"]
    meta = json.loads((j / "meta.json").read_text(encoding="utf-8"))
    assert meta["upload"]["permalink"].endswith("/XYZ/") and meta["upload"]["method"] == "api"
    assert meta["upload"]["approval"]["text"] == "업로드"
    assert upload.history("tester")[-1]["status"] == "published"


def test_retry_checks_for_duplicate_first(job, monkeypatch, temp_host):
    j = job("api")
    monkeypatch.setattr(upload, "preflight", ok_preflight())
    fc = FakeClient(fail_publish=1, recent=[{"id": "M0", "caption": "캡션 첫 줄\n본문\n출처: 연합뉴스\n#a #b #c",
                                              "permalink": "https://www.instagram.com/reel/OLD/"}])
    r = upload.run(j, "업로드", client=fc, sleep=lambda s: None)
    assert r["recovered"] and fc.calls.count("publish") == 1      # 이미 올라가 있어 재게시하지 않음


def test_account_mismatch_stops_api(job, monkeypatch, temp_host):
    j = job("api")
    (j / "meta.json").write_text((j / "meta.json").read_text(encoding="utf-8"), encoding="utf-8")
    pf = upload.preflight(j, client=FakeClient(username="someone_else"), method="api")
    assert pf["items"][0]["ok"] is False and "중단" in pf["items"][0]["detail"] and not pf["ok"]


def test_browser_account_mismatch_stops(job):
    j = job("browser")
    with pytest.raises(upload.UploadBlocked, match="중단"):
        upload.check_account(j, "@other_account")
    upload.check_account(j, "@Tester")      # 대소문자·@ 무시


def test_next_slot_respects_intervals(job, home):
    from common.rules import compose
    from common import config

    j = job("browser")
    ck = compose(config.load_profile("tester"))["checks"]
    t0 = dt.datetime.now().astimezone()
    upload.record(j, "https://x/1", "browser")
    assert upload.next_slot("tester", "browser", ck, t0) >= t0 + dt.timedelta(minutes=29)


# ---------------------------------------------------------------- 토큰
class FakeResp:
    def __init__(self, body, status=200):
        self._b, self.status_code, self.text = body, status, json.dumps(body)

    def json(self):
        return self._b


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def request(self, method, url, params=None, data=None, timeout=None, headers=None):
        self.calls.append((method, url, params, headers))
        for k, v in self.routes.items():
            if k in url:
                return FakeResp(v(params) if callable(v) else v)
        return FakeResp({"error": {"message": "not found"}}, 404)

    def get(self, url, params=None, timeout=None):
        return self.request("GET", url, params)


def _save_tok(handle, days_left, age_days):
    import ig_api

    n = ig_api.now()
    ig_api.save_token(handle, {"access_token": "OLD", "username": handle,
                               "obtained_at": (n - dt.timedelta(days=age_days)).isoformat(),
                               "expires_at": (n + dt.timedelta(days=days_left)).isoformat()})


def test_token_refresh_when_due(job):
    import ig_api

    job("api")
    _save_tok("tester", 5, 55)
    s = FakeSession({"refresh_access_token": {"access_token": "NEW", "expires_in": 5184000}})
    r = ig_api.Client("tester", session=s).refresh_if_due()
    assert r["refreshed"] and ig_api.load_token("tester")["access_token"] == "NEW"
    assert (dt.datetime.fromisoformat(r["expires_at"]) - ig_api.now()).days >= 59
    assert s.calls[0][3]["Authorization"] == "Bearer OLD"


def test_token_not_refreshed_early_or_too_young(job):
    import ig_api

    job("api")
    _save_tok("tester", 40, 20)
    s = FakeSession({})
    assert not ig_api.Client("tester", session=s).refresh_if_due()["refreshed"] and not s.calls
    _save_tok("tester", 3, 0.5)            # 발급 24시간 이내 → 갱신 불가
    r = ig_api.Client("tester", session=s).refresh_if_due()
    assert not r["refreshed"] and "24시간" in r["note"]


def test_expired_token_raises(job):
    import ig_api

    job("api")
    _save_tok("tester", -1, 61)
    with pytest.raises(ig_api.TokenError, match="만료"):
        ig_api.Client("tester", session=FakeSession({})).me()


def test_register_token_rejects_other_account(job):
    import ig_api

    job("api")
    s = FakeSession({"/me": {"user_id": "1", "username": "someone_else", "account_type": "BUSINESS"}})
    with pytest.raises(ig_api.TokenError, match="someone_else"):
        ig_api.register_token("tester", "TOK", session=s)
    assert ig_api.load_token("tester") is None


def test_token_file_fallback_not_in_output(job, home):
    import ig_api

    j = job("api")
    _save_tok("tester", 30, 1)
    f = home / "accounts" / "tester" / ".token"
    assert f.is_file() and not list(j.glob("*token*"))


def test_wait_container_exponential_backoff(job):
    import ig_api

    job("api")
    _save_tok("tester", 30, 1)
    seq = iter(["IN_PROGRESS", "IN_PROGRESS", "IN_PROGRESS", "FINISHED"])
    slept = []
    c = ig_api.Client("tester", session=FakeSession({"C1": lambda p: {"status_code": next(seq)}}), sleep=slept.append)
    assert c.wait_container("C1", first=10, timeout_min=5) == "FINISHED"
    assert slept == [10, 20, 40]
    seq2 = iter(["IN_PROGRESS"] * 50)
    c2 = ig_api.Client("tester", session=FakeSession({"C2": lambda p: {"status_code": next(seq2)}}), sleep=lambda s: None)
    with pytest.raises(ig_api.ApiError, match="시간 초과"):
        c2.wait_container("C2", first=10, timeout_min=5)
