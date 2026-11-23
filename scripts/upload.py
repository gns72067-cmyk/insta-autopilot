"""업로드 — 이용자 본인 Claude 가 본인 계정에. 방식은 profile.yaml upload.method (browser=A | api=B | manual=C).

    python upload.py preflight <job_dir> [--at ISO]                 # 최종 확인 9항목 표 (upload 규칙 1절)
    python upload.py run <job_dir> --approval "<이용자 응답 원문>" [--label 시사1] [--dry-run] [--at ISO]
    python upload.py check-account <job_dir> --seen @handle          # 방식 A: 브라우저에 로그인된 핸들 확인
    python upload.py record <job_dir> --url <permalink> [--method browser|manual] [--screenshot PNG]
    python upload.py fail <job_dir> --reason "..."
    python upload.py next-slot <account> [--method browser]           # 간격 규칙상 가장 이른 업로드 시각
    python upload.py due                                              # 예약(--at) 된 방식 B 게시 실행 (예약 작업에서)

- 실제 업로드는 이용자의 응답 원문에 '업로드' 가 있을 때만 (R4 규칙, 자동 수락 없음). --dry-run 은 승인 없이 가능.
- 캡션은 항상 caption.txt 최신본. 승인 뒤 바뀌었으면 다시 검사하고 알린다.
- 게시 후 permalink·시각·방식을 meta.json 과 accounts/<handle>/upload_history.jsonl 에 남긴다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import approval as apv  # noqa: E402
from common import config as cfgmod  # noqa: E402
from common import log, paths  # noqa: E402
from common.rules import compose, get  # noqa: E402

L = log.get("upload")
METHODS = {"browser": "A", "api": "B", "manual": "C"}
PHOTO_SOURCES = {"library", "kogl1", "wikimedia", "flickr", "unsplash", "pexels", "press"}


class UploadBlocked(Exception):
    pass


def now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def method_of(profile: dict) -> str:
    m = (profile.get("upload") or {}).get("method") or "browser"
    m = {"A": "browser", "B": "api", "C": "manual"}.get(m, m)
    if m not in METHODS:
        raise UploadBlocked(f"upload.method 은 browser|api|manual: {m}")
    return m


# ---------------------------------------------------------------- 이력
def history_file(handle: str) -> Path:
    return paths.account_dir(handle) / "upload_history.jsonl"


def history(handle: str) -> list[dict]:
    f = history_file(handle)
    if not f.is_file():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def all_history() -> list[dict]:
    return [e for h in cfgmod.list_accounts() for e in history(h)]


def next_slot(handle: str, method: str, ck: dict, at: dt.datetime | None = None) -> dt.datetime:
    at = at or now()
    same = [dt.datetime.fromisoformat(e["published_at"]) for e in history(handle) if e.get("status") == "published"]
    t = at
    if same:
        t = max(t, max(same) + dt.timedelta(minutes=int(get(ck, "upload.min_interval_minutes", 30))))
    if method == "browser":
        others = [dt.datetime.fromisoformat(e["published_at"]) for e in all_history()
                  if e.get("status") == "published" and e.get("handle") != handle and e.get("method") == "browser"]
        if others:
            t = max(t, max(others) + dt.timedelta(minutes=int(get(ck, "upload.min_interval_between_accounts_minutes", 5))))
    return t


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


# ---------------------------------------------------------------- 최종 확인 (upload 규칙 1절)
def preflight(job: Path, at: dt.datetime | None = None, client=None, method: str | None = None) -> dict:
    import caption as capmod
    import checks
    import render_reel

    meta = paths.read_json(job / "meta.json")
    if not meta:
        raise UploadBlocked(f"meta.json 없음: {job}")
    profile = cfgmod.load_profile(meta["handle"])
    c = compose(profile, (meta.get("rules") or {}).get("version"))
    ck = c["checks"]
    method = method or method_of(profile)
    fs = paths.read_json(job / "factsheet.json", {}) or {}
    items = []

    def add(no, name, ok, detail):
        items.append({"no": no, "name": name, "ok": ok, "detail": detail})

    # 1 계정
    ig_id = None
    if method == "api":
        try:
            import ig_api

            cl = client or ig_api.Client(meta["handle"])
            m = cl.verify_account()
            ig_id = str(m["user_id"])
            add(1, "계정", True, f"토큰 계정 @{m['username']} = 대상")
        except Exception as e:
            add(1, "계정", False, str(e))
    elif method == "browser":
        add(1, "계정", None, f"업로드 직전 브라우저에서 @{meta['handle']} 로그인 확인")
    else:
        add(1, "계정", True, f"@{meta['handle']} (이용자가 직접 게시)")
    # 2 파일
    reel = job / "reel.mp4"
    if not reel.is_file():
        add(2, "파일", False, "reel.mp4 없음")
    else:
        try:
            info = render_reel.probe(reel)
            errs = render_reel.verify_probe(info, int(get(ck, "render.reel.width", 1080)), int(get(ck, "render.reel.height", 1920)),
                                            int(get(ck, "render.reel.fps", 30)), float(get(ck, "render.reel.seconds", 10)))
            card = job / "card.png"
            if card.is_file() and reel.stat().st_mtime < card.stat().st_mtime:
                errs.append("릴스가 지금 카드보다 먼저 만들어짐 — render_reel 로 다시 만들기")
            add(2, "파일", not errs, "; ".join(errs) or "1080×1920 · 10초 · H.264")
        except Exception as e:
            add(2, "파일", False, f"ffprobe 실패: {e}")
    # 3 훅 근거
    if not meta.get("hook"):
        add(3, "훅 근거", False, "선택된 훅 없음")
    else:
        hr = checks.check_hooks({"selected": meta["hook"]}, fs, meta, c, "selected")
        errs = [e["msg"] for e in hr["versions"]["selected"]["errors"]]
        add(3, "훅 근거", not errs, "; ".join(errs[:3]) or "세 줄 모두 확정 claim 연결")
    # 4 사진 라이선스
    photo = meta.get("photo") or {}
    text, changed = ("", False)
    if (job / "caption.txt").is_file():
        text, changed = capmod.caption_for_upload(job)
    parts = checks.caption_parts(text)
    if meta.get("template") == "C" or not photo:
        ok4 = not parts["photo_lines"]
        add(4, "사진 라이선스", ok4, "사진 없음(C)" if ok4 else "C 템플릿인데 캡션에 사진 출처 줄이 있음")
    else:
        want = photo.get("caption_credit") if "caption_credit" in photo else photo.get("credit")
        okp = photo.get("source") in PHOTO_SOURCES and (job / "credits.txt").is_file()
        same = (not want) or (bool(parts["photo_lines"]) and checks.ws(parts["photo_lines"][0]) == checks.ws(want))
        add(4, "사진 라이선스", okp and same,
            f"{photo.get('source')} · {photo.get('license')}" + ("" if same else f" · 캡션 사진 출처 줄 ≠ {want}"))
    # 5 캡션
    if not text:
        add(5, "캡션", False, "caption.txt 없음")
    elif not (meta.get("caption") or {}).get("approved"):
        add(5, "캡션", False, "캡션이 아직 확정되지 않음 (caption.py done)")
    else:
        cr = checks.check_caption(text, fs, meta, ck, c["banned_phrases"], user_edit=True)
        lo, hi = get(ck, "caption.hashtags", [3, 5])
        probs = list(cr["errors"])
        if not lo <= cr["hashtags"] <= hi:
            probs.append(f"해시태그 {cr['hashtags']}개 (기준 {lo}~{hi})")
        if not parts["source_lines"]:
            probs.append("출처 줄 없음")
        add(5, "캡션", not probs, "; ".join(probs) or f"{cr['chars']}자 · 해시태그 {cr['hashtags']}개"
            + (" · ⚠ 확정 후 파일이 바뀜(최신본 사용)" if changed else ""))
    # 6 금지 표현
    hook_text = " ".join((meta.get("hook") or {}).get(k, "") for k in ("l1", "l2", "l3"))
    hits = sorted(set(checks.banned_hits(hook_text + "\n" + text, c["banned_phrases"])))
    add(6, "금지 표현", not hits, ", ".join(hits) or "없음")
    # 7 간격
    planned = at or now()
    slot = next_slot(meta["handle"], method, ck, planned)
    add(7, "간격", slot <= planned + dt.timedelta(seconds=30),
        "기준 충족" if slot <= planned + dt.timedelta(seconds=30) else f"{slot:%H:%M} 이후 가능")
    # 8 한도
    if method == "api" and ig_id:
        try:
            lim = cl.limit(ig_id)
            add(8, "한도", (lim["remaining"] or 0) > 0, f"24시간 남은 게시 {lim['remaining']}/{lim['total']}")
        except Exception as e:
            add(8, "한도", False, f"조회 실패: {e}")
    elif method == "api":
        add(8, "한도", False, "계정 확인 실패로 조회 못 함")
    else:
        add(8, "한도", True, "해당 없음 (방식 A·C)")
    # 9 중복
    days = int(get(ck, "brief.repeat_window_days", 14))
    cut = now() - dt.timedelta(days=days)
    mine = _bigrams(meta.get("title", "") + ((meta.get("cluster") or {}).get("title") or ""))
    dup = [e for e in history(meta["handle"]) if e.get("status") == "published" and e.get("job") != str(job)
           and dt.datetime.fromisoformat(e["published_at"]) >= cut
           and len(mine & _bigrams(e.get("title", ""))) / max(1, len(mine | _bigrams(e.get("title", "")))) >= 0.35]
    add(9, "중복", not dup, f"최근 {days}일 같은 사안: {dup[0]['title']}" if dup else "없음")
    ok = all(i["ok"] is not False for i in items)
    return {"job": str(job), "handle": meta["handle"], "method": method, "planned_at": planned.isoformat(timespec="minutes"),
            "ok": ok, "items": items, "caption_changed_after_approval": changed, "ig_id": ig_id}


def table(pf: dict) -> str:
    rows = [f"@{pf['handle']} · 방식 {METHODS[pf['method']]}({pf['method']}) · 예정 {pf['planned_at']}",
            "| # | 항목 | 결과 | 내용 |", "|---|---|---|---|"]
    for i in pf["items"]:
        mark = "✅" if i["ok"] else ("⏳" if i["ok"] is None else "❌")
        rows.append(f"| {i['no']} | {i['name']} | {mark} | {i['detail']} |")
    return "\n".join(rows)


# ---------------------------------------------------------------- 승인
def approval_ok(job: Path, text: str, label: str | None) -> bool:
    if not label:
        return apv.single_upload_approved(text)
    b = paths.read_json(paths.output_root() / job.parent.parent.name / "batch.json") or {}
    ps = [p for posts in (b.get("accounts") or {}).values() for p in posts]
    mine = next((p for p in ps if p["label"] == label), None)
    if ps and (mine is None or ("job" in mine and Path(mine["job"]).resolve() != Path(job).resolve())):
        return False                                   # 라벨이 이 작업 폴더의 것이 아님
    labels = [p["label"] for p in ps] or [label]
    return label in apv.parse_r4(text, labels)["approved"]


# ---------------------------------------------------------------- 실행
def _save_meta(job: Path, fn) -> dict:
    meta = paths.read_json(job / "meta.json")
    fn(meta)
    paths.write_json(job / "meta.json", meta)
    return meta


def record(job: Path, url: str | None, method: str, status: str = "published", extra: dict | None = None) -> dict:
    meta = paths.read_json(job / "meta.json")
    at = now().isoformat(timespec="seconds")
    entry = {"handle": meta["handle"], "job": str(job), "title": meta.get("title"), "method": method,
             "status": status, "permalink": url, "published_at": at, **(extra or {})}
    with open(history_file(meta["handle"]), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def upd(m):
        m["upload"] = {**(m.get("upload") or {}), "status": status, "method": method, "permalink": url,
                       "published_at": at, **(extra or {})}
        m.setdefault("stages", {})["upload"] = "done" if status == "published" else "failed"
    _save_meta(job, upd)
    return entry


def run_api(job: Path, pf: dict, text: str, dry_run: bool, client=None, sleep=None) -> dict:
    import time as _t

    import ig_api
    import tempurl

    meta = paths.read_json(job / "meta.json")
    profile = cfgmod.load_profile(meta["handle"])
    ck = compose(profile, (meta.get("rules") or {}).get("version"))["checks"]
    cl = client or ig_api.Client(meta["handle"])
    sleep = sleep or _t.sleep
    try:
        cl.refresh_if_due()
    except ig_api.ApiError as e:
        raise UploadBlocked(f"토큰 갱신 실패: {e} — 방식 C(수동)로 전환할지 이용자에게 묻는다")
    if dry_run:
        return {"dry_run": True, "stopped_before": "임시 URL 업로드·컨테이너 생성", "ig_id": pf["ig_id"]}
    retries = int(get(ck, "upload.retry", 1))
    delay = float(get(ck, "upload.retry_delay_minutes", 2)) * 60
    last_err = None
    for attempt in range(retries + 1):
        tmp = None
        try:
            if attempt:
                for m in cl.recent_media(pf["ig_id"]):        # 중복 게시 의심 → 먼저 확인
                    if (m.get("caption") or "").strip()[:80] == text.strip()[:80]:
                        L.info("재시도 전 확인: 이미 게시됨 %s", m.get("permalink"))
                        return {"media_id": m["id"], "permalink": m.get("permalink"), "attempts": attempt, "recovered": True}
            tmp = tempurl.put(job / "reel.mp4")
            cid = cl.create_reel(pf["ig_id"], tmp["url"], text)
            cl.wait_container(cid, float(get(ck, "upload.api_poll_seconds", 10)), float(get(ck, "upload.api_poll_timeout_minutes", 5)))
            mid = cl.publish(pf["ig_id"], cid)
            info = cl.media(mid)
            return {"media_id": mid, "permalink": info.get("permalink"), "attempts": attempt + 1}
        except (ig_api.ApiError, tempurl.TempHostError) as e:
            last_err = e
            if isinstance(e, ig_api.TokenError) or isinstance(e, tempurl.TempHostError) or attempt >= retries:
                break
            sleep(delay)
        finally:
            if tmp:
                tempurl.delete(tmp["id"])
    raise UploadBlocked(f"방식 B 업로드 실패: {last_err}")


def browser_plan(job: Path, pf: dict, text: str, dry_run: bool) -> dict:
    meta = paths.read_json(job / "meta.json")
    plan = {"handle": meta["handle"], "file": str(job / "reel.mp4"), "caption_file": str(job / "caption.txt"),
            "caption_chars": len(text), "caption_first_line": text.split("\n")[0], "caption_last_line": text.split("\n")[-1],
            "dry_run": dry_run, "stop_before": "캡션 입력" if dry_run else "없음",
            "steps_rule": "rules_compose <계정> upload --job JOB 의 2절(방식 A) 순서를 따른다",
            "after": f"ia upload record \"{job}\" --url <게시물 URL> --method browser --screenshot <공유 직전 스크린샷>"}
    paths.write_json(job / "upload_browser.json", plan)
    return plan


def manual_package(job: Path, text: str, c: dict) -> Path:
    meta = paths.read_json(job / "meta.json")
    hints = c["settings"].get("posting_time_hint") or []
    md = [f"# 수동 업로드 — @{meta['handle']} · {meta.get('title')}", "",
          f"- 파일: `{job / 'reel.mp4'}`", f"- 캡션 파일: `{job / 'caption.txt'}` (아래와 같음)",
          f"- 권장 게시 시각: {', '.join(hints) or '-'}", "", "## 체크리스트",
          f"1. instagram.com 오른쪽 위 프로필이 @{meta['handle']} 인지 확인",
          "2. 만들기(＋) → 게시물 → 컴퓨터에서 선택 → reel.mp4", "3. 형식이 릴스인지 확인, 원본 비율(9:16) 유지 → 다음",
          "4. 필터 변경 없이 다음", "5. 아래 캡션 붙여 넣기 → 첫 줄·마지막 해시태그 확인", "6. 공유하기 → 게시물 URL 을 Claude 에게 알려 주기",
          "", "## 캡션 (복사용)", "", "```", text, "```", ""]
    p = job / "upload_manual.md"
    paths.write_text_atomic(p, "\n".join(md))
    return p


def run(job: Path, approval: str | None, label: str | None = None, dry_run: bool = False,
        at: dt.datetime | None = None, client=None, sleep=None) -> dict:
    import caption as capmod

    meta = paths.read_json(job / "meta.json")
    profile = cfgmod.load_profile(meta["handle"])
    c = compose(profile, (meta.get("rules") or {}).get("version"))
    method = method_of(profile)
    if not dry_run and not approval_ok(job, approval or "", label):
        raise UploadBlocked("이용자의 명시적 '업로드' 응답이 없어 업로드하지 않습니다 (R4 자동 수락 없음)")
    pf = preflight(job, at, client=client, method=method)
    if not pf["ok"]:
        raise UploadBlocked("최종 확인 실패 — 업로드하지 않음\n" + table(pf))
    text, changed = capmod.caption_for_upload(job)
    if changed and not dry_run:
        capmod.finish(job)        # 승인 뒤 바뀐 최신본 → 다시 기록(검사는 경고만)
    if not dry_run:
        _save_meta(job, lambda m: m.update(upload={**(m.get("upload") or {}), "approval": {"text": approval, "label": label,
                                                                                         "at": now().isoformat(timespec="seconds")}}))
    if at and at > now() + dt.timedelta(minutes=1) and not dry_run:
        if method != "api":
            _save_meta(job, lambda m: m["upload"].update(planned_at=at.isoformat(timespec="minutes"), status="scheduled"))
            return {"scheduled": at.isoformat(timespec="minutes"), "method": method,
                    "note": "방식 A·C 는 예정 시각에 Claude 와 함께 진행"}
        q = paths.read_json(queue_file(), []) or []
        q.append({"job": str(job), "at": at.isoformat(timespec="minutes"), "approval": approval, "label": label})
        paths.write_json(queue_file(), q)
        _save_meta(job, lambda m: m["upload"].update(planned_at=at.isoformat(timespec="minutes"), status="scheduled"))
        return {"scheduled": at.isoformat(timespec="minutes"), "method": method}
    if method == "api":
        r = run_api(job, pf, text, dry_run, client=client, sleep=sleep)
        if not dry_run:
            record(job, r.get("permalink"), "api", extra={"media_id": r.get("media_id")})
        return {"method": method, **r}
    if method == "browser":
        return {"method": method, "plan": browser_plan(job, pf, text, dry_run)}
    p = manual_package(job, text, c)
    _save_meta(job, lambda m: m.setdefault("stages", {}).update(upload="waiting_user"))
    return {"method": method, "package": str(p)}


def queue_file() -> Path:
    return paths.home() / "upload_queue.json"


def run_due() -> list[str]:
    q = paths.read_json(queue_file(), []) or []
    keep, out = [], []
    for e in q:
        if dt.datetime.fromisoformat(e["at"]) > now():
            keep.append(e)
            continue
        job = Path(e["job"])
        try:
            r = run(job, e["approval"], e.get("label"))
            out.append(f"✔ {job.name}: {r.get('permalink')}")
        except UploadBlocked as ex:
            record(job, None, "api", status="failed", extra={"reason": str(ex)[:300]})
            out.append(f"✘ {job.name}: {str(ex).splitlines()[0]}")
    paths.write_json(queue_file(), keep)
    return out


def check_account(job: Path, seen: str) -> None:
    meta = paths.read_json(job / "meta.json")
    try:
        s = paths.normalize_handle(seen)
    except ValueError:
        s = (seen or "").strip()
    if s != meta["handle"]:
        _save_meta(job, lambda m: m.update(upload={**(m.get("upload") or {}), "status": "blocked",
                                                   "reason": f"로그인 계정 @{s} ≠ 대상 @{meta['handle']}"}))
        raise UploadBlocked(f"중단: 브라우저 로그인 계정 @{s} ≠ 대상 계정 @{meta['handle']} — 이용자에게 계정 전환을 요청")


def _parse_at(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    t = dt.datetime.fromisoformat(s)
    return t if t.tzinfo else t.astimezone()


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("preflight")
    p.add_argument("job", type=Path)
    p.add_argument("--at")
    p = sub.add_parser("run")
    p.add_argument("job", type=Path)
    p.add_argument("--approval")
    p.add_argument("--label")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--at")
    p = sub.add_parser("check-account")
    p.add_argument("job", type=Path)
    p.add_argument("--seen", required=True)
    p = sub.add_parser("record")
    p.add_argument("job", type=Path)
    p.add_argument("--url", required=True)
    p.add_argument("--method", default="browser", choices=list(METHODS))
    p.add_argument("--screenshot")
    p = sub.add_parser("fail")
    p.add_argument("job", type=Path)
    p.add_argument("--reason", required=True)
    p = sub.add_parser("next-slot")
    p.add_argument("account")
    p.add_argument("--method", default="browser", choices=list(METHODS))
    sub.add_parser("due")
    a = ap.parse_args()
    try:
        if a.cmd == "preflight":
            pf = preflight(a.job, _parse_at(a.at))
            paths.write_json(a.job / "upload_check.json", pf)
            print(table(pf))
            print("\n✔ 업로드 가능" if pf["ok"] else "\n✘ 확인 항목 실패 — 업로드 대상에서 제외")
            return 0 if pf["ok"] else 1
        if a.cmd == "run":
            r = run(a.job, a.approval, a.label, a.dry_run, _parse_at(a.at))
            if r.get("scheduled"):
                print(f"✔ 예약됨: {r['scheduled']} ({r['method']})" + (f" — {r['note']}" if r.get("note") else ""))
            elif r["method"] == "api":
                print("✔ dry-run: 계정·한도·토큰 확인까지 완료, 업로드하지 않음" if r.get("dry_run")
                      else f"✔ 게시 완료: {r.get('permalink')} (시도 {r.get('attempts')}회)")
            elif r["method"] == "browser":
                pl = r["plan"]
                print(f"✔ 방식 A 준비: @{pl['handle']} · 파일 {pl['file']}")
                print(f"  캡션 {pl['caption_chars']}자 · 첫 줄 \"{pl['caption_first_line']}\"")
                print("  → 브라우저에서 로그인 계정 확인(check-account) 후 upload 규칙 2절 순서대로 진행"
                      + (" · dry-run: 캡션 입력 직전에 멈춤" if pl["dry_run"] else ""))
            else:
                print(f"✔ 방식 C: 수동 업로드 안내 {r['package']}")
            return 0
        if a.cmd == "check-account":
            check_account(a.job, a.seen)
            print(f"✔ 로그인 계정 확인: @{paths.normalize_handle(a.seen)}")
            return 0
        if a.cmd == "record":
            e = record(a.job, a.url, a.method, extra={"screenshot": a.screenshot} if a.screenshot else None)
            print(f"✔ 게시 기록: {e['permalink']} · {e['published_at']} · {e['method']}")
            return 0
        if a.cmd == "fail":
            record(a.job, None, paths.read_json(a.job / "meta.json").get("upload", {}).get("method") or "browser",
                   status="failed", extra={"reason": a.reason})
            print("✔ 실패 기록")
            return 0
        if a.cmd == "next-slot":
            h = cfgmod.resolve_account(a.account)
            ck = compose(cfgmod.load_profile(h))["checks"]
            print(next_slot(h, a.method, ck).isoformat(timespec="minutes"))
            return 0
        if a.cmd == "due":
            for line in run_due() or ["· 예정된 게시 없음"]:
                print(line)
            return 0
    except UploadBlocked as e:
        print(f"✘ {e}")
        return 3
    return 2


if __name__ == "__main__":
    sys.exit(main())
