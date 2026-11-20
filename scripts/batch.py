"""다계정 일괄 승인 모드 — 라운드 R1~R4 (workflow 규칙 2절). 계정별 분기 코드 없음: 모든 차이는 profile.yaml 에서.

    python batch.py run_all [--date D]                  # 규칙 동기화 1회 → 활성 계정 전체 후보 수집 → briefing_raw.md
    python batch.py set-brief <account> --file brief.json   # Claude 가 brief 규칙으로 고른 10개 (계정별)
    python batch.py briefing                            # 전 계정 R1 브리핑 briefing.md (계정별 섹션, ★ 추천)
    python batch.py r1 "<응답>"                          # 이슈 선택 → 작업 폴더 생성, 같은 사안 묶음(팩트 시트 공유) 표시
    python batch.py share-factsheet <원본 job> <대상 job>...
    python batch.py set-rec <라벨> --hook normal --photo 2   # R2 추천안(★) 기록 (Claude 가 훅·사진 준비 후)
    python batch.py r2 "<응답>"                          # 훅·사진 선택 적용 → 렌더 대상 목록
    python batch.py render [--labels 시사1,연애2]         # 카드+릴스 병렬 렌더 (render_concurrency)
    python batch.py r3 "<응답>"                          # 결과물 승인·재작업
    python batch.py r4-table [--start ISO]              # 업로드 표 (계정 × 게시물 × 방식 × 예정 시각 × 확인 결과)
    python batch.py r4 "<응답>"                          # '업로드' 명시 응답만 → 업로드 순서
    python batch.py status | review                     # output/<date>/status.md · review.html

상태: output/<date>/batch.json. 모든 라운드는 채팅 안에서 끝낼 수 있게 표·짧은 응답 위주로 출력한다.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import approval as apv  # noqa: E402
from common import config as cfgmod  # noqa: E402
from common import paths  # noqa: E402
from common.rules import compose, get  # noqa: E402

HOOK_KO = {"strong": "강력", "normal": "보통", "soft": "순화"}
ROUNDS = ["R1", "R2", "R3", "R4"]


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def day(date: str | None = None) -> str:
    return date or paths.today_str()


def batch_file(date: str | None = None) -> Path:
    return paths.output_root() / day(date) / "batch.json"


def load(date: str | None = None) -> dict:
    b = paths.read_json(batch_file(date))
    if b is None:
        b = {"date": day(date), "created_at": now_iso(), "aliases": {}, "briefs": {}, "accounts": {},
             "shared_groups": [], "rounds": {}, "notes": []}
    return b


def save(b: dict) -> None:
    b["updated_at"] = now_iso()
    paths.write_json(batch_file(b["date"]), b)
    write_status(b)


def active_accounts() -> list[str]:
    out = []
    for h in cfgmod.list_accounts():
        p = cfgmod.load_profile(h)
        if p.get("active", True):
            out.append(h)
    return out


def approval_mode(n_accounts: int) -> str:
    m = cfgmod.load_config().get("approval_mode") or "auto"
    if m in ("batch", "step"):
        return m
    return "batch" if n_accounts >= 2 else "step"


def posts(b: dict) -> list[dict]:
    return [p for ps in b["accounts"].values() for p in ps]


def by_label(b: dict) -> dict[str, dict]:
    return {p["label"]: p for p in posts(b)}


def ia(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", IA_IN_VENV="1")
    return subprocess.run([sys.executable, str(Path(__file__).with_name("ia.py")), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", env=env)


# ---------------------------------------------------------------- run_all · 브리핑
def run_all(date: str | None = None) -> dict:
    import collect_trends
    import rules_sync

    b = load(date)
    sync = rules_sync.sync()
    b["rules"] = {"status": sync["status"], "version": sync["version"], "changelog": sync["changelog"],
                  "warnings": sync["warnings"]}
    if sync["status"] == "error":
        b["notes"].append(f"{now_iso()} 규칙 동기화 실패 — 작업 시작 안 함: {'; '.join(sync['warnings'])}")
        save(b)
        return b
    accts = active_accounts()
    b["mode"] = approval_mode(len(accts))
    b["aliases"] = {cfgmod.load_profile(h).get("alias") or h: h for h in accts}
    raw = [f"# 후보 원자료 {b['date']} (규칙 {sync['version']})", ""]
    b["collect"] = {}
    for h in accts:
        try:
            out = collect_trends.build(h)
            bad = [f"{s['name']}: {s['error']}" for s in out["sources"] if not s["ok"]]
            b["collect"][h] = {"ok": True, "path": out["_path"], "clusters": len(out["clusters"]), "failed_sources": bad}
            raw.append(f"## @{h} — 사안 {len(out['clusters'])}개, 실패 소스 {len(bad)}개 ({out['_path']})")
        except Exception as e:
            b["collect"][h] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            raw.append(f"## @{h} — 수집 실패: {e}")
    paths.write_text_atomic(paths.output_root() / b["date"] / "briefing_raw.md", "\n".join(raw) + "\n")
    save(b)
    return b


def set_brief(account: str, items: list[dict], date: str | None = None) -> dict:
    """items: [{no, cluster_id, summary, why, outlets, reaction, risk, star?}] — brief 규칙 출력 형식."""
    b = load(date)
    h = cfgmod.resolve_account(account)
    for i, it in enumerate(items, 1):
        it.setdefault("no", i)
    if not any(it.get("star") for it in items) and items:
        items[0]["star"] = True
    b["briefs"][h] = items
    alias = cfgmod.load_profile(h).get("alias") or h
    b["aliases"][alias] = h
    save(b)
    return b


def briefing_md(b: dict) -> str:
    out = [f"# 아침 브리핑 {b['date']}", ""]
    r = b.get("rules") or {}
    if r.get("status") == "updated":
        out.append(f"- 규칙 {r['version']} 로 업데이트됨: {' / '.join(r.get('changelog') or [])}")
    elif r.get("status") == "cached":
        out.append(f"- ⚠ 규칙 서버 확인 실패 → 검증된 캐시 {r['version']} 사용")
    for h, c in (b.get("collect") or {}).items():
        if not c.get("ok"):
            out.append(f"- ⚠ @{h} 후보 수집 실패: {c.get('error')}")
        elif c.get("failed_sources"):
            out.append(f"- ⚠ @{h} 소스 장애 {len(c['failed_sources'])}곳: " + "; ".join(c["failed_sources"][:3]))
    for n in b.get("health", []):
        out.append(f"- ⚠ {n}")
    out.append("")
    for alias, h in b["aliases"].items():
        items = b["briefs"].get(h) or []
        out += [f"## {alias} (@{h})", "", "번호 | 한 줄 요약 | 왜 지금 | 매체 수 | 예상 반응 | 리스크", "---|---|---|---|---|---"]
        for it in items:
            out.append(f"{'★' if it.get('star') else ''}{it['no']} | {it.get('summary', '')} | {it.get('why', '')} | "
                       f"{it.get('outlets', '')} | {it.get('reaction', '')} | {it.get('risk', '')}")
        if not items:
            out.append("(추천 없음)")
        out.append("")
    ex = " / ".join(f"{a}: {','.join(str(i['no']) for i in (b['briefs'].get(h) or []) if i.get('star')) or '없음'}"
                    for a, h in b["aliases"].items())
    out += ["응답 예: `" + ex + "` · `전체 추천대로` · `<별칭>: 없음`", ""]
    return "\n".join(out)


# ---------------------------------------------------------------- R1
def _cluster(h: str, cid, date: str) -> dict | None:
    cand = paths.read_json(paths.output_root() / date / h / "candidates.json", {}) or {}
    return next((c for c in cand.get("clusters", []) if str(c.get("id")) == str(cid)), None)


def _bg(s: str) -> set[str]:
    s = re.sub(r"\s+", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


def apply_r1(text: str, date: str | None = None) -> dict:
    import job as jobmod

    b = load(date)
    aliases = list(b["aliases"])
    maxes = {a: len(b["briefs"].get(h) or []) for a, h in b["aliases"].items()}
    res = apv.parse_r1(text, aliases, maxes)
    created = []
    for alias, h in b["aliases"].items():
        if res["all_recommended"]:
            picks = [it["no"] for it in b["briefs"].get(h, []) if it.get("star")]
        elif alias in res["items"]:
            picks = res["items"][alias]["picks"]
            if picks == "recommended":
                picks = [it["no"] for it in b["briefs"].get(h, []) if it.get("star")]
        else:
            continue
        existing = b["accounts"].setdefault(h, [])
        for no in picks:
            it = next((x for x in b["briefs"].get(h, []) if x["no"] == no), None)
            if not it or any(p.get("brief_no") == no for p in existing):
                continue
            cl = _cluster(h, it.get("cluster_id"), b["date"])
            jd = jobmod.new_job(h, it.get("summary") or (cl or {}).get("title", f"이슈{no}"),
                                it.get("slug"), cl, b["date"])
            lab = f"{alias}{len(existing) + 1}"
            existing.append({"label": lab, "handle": h, "alias": alias, "brief_no": no, "job": str(jd),
                             "title": it.get("summary"), "round": "R2", "status": {"R1": "approved"}})
            created.append(lab)
    for alias in res["hold"]:
        b["notes"].append(f"{now_iso()} R1 {alias} 보류")
    # 같은 사안이 여러 계정에 걸리면 팩트 시트 공유 묶음
    ps = posts(b)
    groups, seen = [], set()
    for i, p in enumerate(ps):
        if p["label"] in seen:
            continue
        g = [p["label"]]
        bp = _bg(p.get("title", ""))
        for q in ps[i + 1:]:
            if q["handle"] != p["handle"] and q["label"] not in seen:
                bq = _bg(q.get("title", ""))
                if bp and bq and len(bp & bq) / len(bp | bq) >= 0.35:
                    g.append(q["label"])
        if len(g) > 1:
            groups.append(g)
            seen.update(g)
    b["shared_groups"] = groups
    b["rounds"]["R1"] = {"text": text, "at": now_iso(), "parsed": res}
    save(b)
    return {"parsed": res, "created": created, "shared_groups": groups}


def share_factsheet(src: Path, dsts: list[Path]) -> list[str]:
    import shutil

    out = []
    for d in dsts:
        for f in ("factsheet.json", "factsheet.md", "checks_fact.json"):
            if (src / f).is_file():
                shutil.copyfile(src / f, d / f)
        meta = paths.read_json(d / "meta.json")
        meta["factsheet_shared_from"] = str(src)
        meta.setdefault("stages", {})["research"] = "done"
        paths.write_json(d / "meta.json", meta)
        out.append(d.name)
    return out


# ---------------------------------------------------------------- R2
def set_rec(label: str, hook: str, photo, date: str | None = None) -> None:
    b = load(date)
    p = by_label(b)[label]
    p["rec"] = {"hook": hook, "photo": photo}
    save(b)


def apply_r2(text: str, date: str | None = None) -> dict:
    import image_fetch
    import job as jobmod

    b = load(date)
    pend = [p for p in posts(b) if p["status"].get("R2") != "approved" and p["status"].get("R1") == "approved"
            and p.get("held") is not True]
    labels = [p["label"] for p in pend]
    counts = {}
    for p in pend:
        pc = paths.read_json(Path(p["job"]) / "photo_candidates.json", {}) or {}
        counts[p["label"]] = len(pc.get("candidates", []))
    res = apv.parse_r2(text, labels, counts)
    done, errors = [], []
    for p in pend:
        lb = p["label"]
        if res["all_recommended"] or (lb in res["items"] and "recommended" in (res["items"][lb]["hook"], res["items"][lb]["photo"])):
            rec = p.get("rec") or {}
            if not rec:
                errors.append(f"{lb}: 추천안(★)이 기록되지 않음 — 되묻기")
                continue
            item = dict(res["items"].get(lb) or {"edits": {}})
            item["hook"] = rec["hook"] if item.get("hook") in (None, "recommended") else item["hook"]
            item["photo"] = rec["photo"] if item.get("photo") in (None, "recommended") else item["photo"]
        elif lb in res["items"]:
            item = res["items"][lb]
        else:
            continue
        job = Path(p["job"])
        try:
            e = item.get("edits") or {}
            jobmod.select_hook(job, item["hook"], e.get("l1"), e.get("l2"), e.get("l3"))
            if e:
                r = ia("checks", "hook", str(job), "--selected")
                if r.returncode != 0:
                    errors.append(f"{lb}: 수정한 줄이 검사에 걸림 —\n{r.stdout.strip()}")
                    continue
            if item["photo"] == "C":
                image_fetch.choose_none(job)
            else:
                image_fetch.fetch(job, int(item["photo"]))
            jobmod.set_stage(job, "hooks", "approved")
            p["status"]["R2"] = "approved"
            p["choice"] = {"hook": item["hook"], "photo": item["photo"], "edits": e}
            p["round"] = "R3"
            done.append(lb)
        except SystemExit as ex:
            errors.append(f"{lb}: {ex}")
    for lb in res["hold"]:
        by_label(b)[lb]["held"] = True
    b["rounds"]["R2"] = {"text": text, "at": now_iso(), "parsed": res}
    save(b)
    return {"parsed": res, "ready_to_render": done, "errors": errors}


# ---------------------------------------------------------------- 병렬 렌더
def render(labels: list[str] | None = None, date: str | None = None) -> dict:
    b = load(date)
    targets = [p for p in posts(b) if p["status"].get("R2") == "approved" and (not labels or p["label"] in labels)]
    conc = max(1, int(cfgmod.load_config().get("render_concurrency", 2)))

    def one(p):
        r1 = ia("render_card", p["job"])
        r2 = ia("render_reel", p["job"]) if (Path(p["job"]) / "card.png").is_file() else None
        ok = r2 is not None and r2.returncode == 0
        return p["label"], ok, (r1.stdout + (r2.stdout if r2 else "")).strip()

    out = {}
    with cf.ThreadPoolExecutor(max_workers=conc) as ex:
        for lb, ok, log_ in ex.map(one, targets):
            out[lb] = {"ok": ok, "log": log_}
            by_label(b)[lb]["rendered"] = ok
    save(b)
    return out


# ---------------------------------------------------------------- R3
def apply_r3(text: str, date: str | None = None) -> dict:
    import job as jobmod

    b = load(date)
    pend = [p for p in posts(b) if p["status"].get("R2") == "approved" and p["status"].get("R3") != "approved"
            and not p.get("held")]
    res = apv.parse_r3(text, [p["label"] for p in pend])
    approved, redo = [], []
    for p in pend:
        it = res["items"].get(p["label"])
        if not it:
            continue
        job = Path(p["job"])
        if it["action"] == "approve":
            meta = paths.read_json(job / "meta.json")
            if not (meta.get("caption") or {}).get("approved"):
                import caption as capmod

                if (job / "caption_original.txt").is_file():
                    capmod.finish(job)       # R3 OK = 캡션 초안(또는 수정한 파일) 확정
            jobmod.set_stage(job, "card", "approved")
            p["status"]["R3"] = "approved"
            p["round"] = "R4"
            approved.append(p["label"])
        else:
            p["status"]["R3"] = "redo"
            p["redo"] = {"targets": it["targets"], "request": it["request"], "at": now_iso()}
            redo.append({"label": p["label"], "job": p["job"], **p["redo"]})
    for lb in res["hold"]:
        by_label(b)[lb]["held"] = True
    b["rounds"]["R3"] = {"text": text, "at": now_iso(), "parsed": res}
    save(b)
    return {"parsed": res, "approved": approved, "redo": redo}


# ---------------------------------------------------------------- R4
def r4_table(start: dt.datetime | None = None, date: str | None = None) -> dict:
    import upload

    b = load(date)
    ready = [p for p in posts(b) if p["status"].get("R3") == "approved" and not p.get("held")
             and p["status"].get("R4") != "done"]
    t = start or dt.datetime.now().astimezone()
    rows, eligible = [], []
    order = []
    # 방식 A 는 계정을 번갈아 가며 순차 → 같은 계정이 연달아 오지 않게 섞는다
    queues = {}
    for p in ready:
        queues.setdefault(p["handle"], []).append(p)
    while any(queues.values()):
        for h in list(queues):
            if queues[h]:
                order.append(queues[h].pop(0))
    last_at: dict[str, dt.datetime] = {}
    prev_any = None
    for p in order:
        job = Path(p["job"])
        prof = cfgmod.load_profile(p["handle"])
        ck = compose(prof, (paths.read_json(job / "meta.json").get("rules") or {}).get("version"))["checks"]
        method = upload.method_of(prof)
        at = upload.next_slot(p["handle"], method, ck, t)
        if p["handle"] in last_at:
            at = max(at, last_at[p["handle"]] + dt.timedelta(minutes=int(get(ck, "upload.min_interval_minutes", 30))))
        if method == "browser" and prev_any:
            at = max(at, prev_any + dt.timedelta(minutes=int(get(ck, "upload.min_interval_between_accounts_minutes", 5))))
        try:
            pf = upload.preflight(job, at, method=method)
            fails = [f"{i['no']}.{i['name']}: {i['detail']}" for i in pf["items"] if i["ok"] is False]
        except Exception as e:
            pf, fails = {"ok": False}, [str(e)]
        p["upload_plan"] = {"method": method, "at": at.isoformat(timespec="minutes"), "ok": pf["ok"], "fails": fails}
        rows.append(p)
        if pf["ok"]:
            eligible.append(p["label"])
            last_at[p["handle"]] = at
            if method == "browser":
                prev_any = at
    b["r4"] = {"eligible": eligible, "order": [p["label"] for p in rows], "made_at": now_iso()}
    save(b)
    lines = ["계정 | 게시물 | 방식 | 예정 시각 | 확인", "---|---|---|---|---"]
    for p in rows:
        u = p["upload_plan"]
        lines.append(f"@{p['handle']} | {p['label']} {p.get('title', '')} | {upload.METHODS[u['method']]}({u['method']}) | "
                     f"{u['at'][11:16]} | {'✅' if u['ok'] else '❌ ' + '; '.join(u['fails'])}")
    lines.append("")
    ex = eligible or [p["label"] for p in rows]
    pick = ", ".join(ex[:2]) or "<게시물>"
    lines.append(f"응답: `업로드` (✅ 전부) · `업로드: {pick}` · `{ex[-1] if ex else '<게시물>'} 보류하고 업로드`"
                 " — '업로드' 라는 말이 없으면 올리지 않습니다")
    return {"table": "\n".join(lines), "eligible": eligible}


def apply_r4(text: str, date: str | None = None) -> dict:
    b = load(date)
    r4 = b.get("r4") or {}
    labels = r4.get("order") or []
    res = apv.parse_r4(text, labels, r4.get("eligible") or [])
    plan = []
    for lb in [x for x in labels if x in res["approved"]]:
        p = by_label(b)[lb]
        plan.append({"label": lb, "job": p["job"], "handle": p["handle"], **p["upload_plan"]})
    for lb in res["hold"]:
        by_label(b)[lb]["held"] = True
    b["rounds"]["R4"] = {"text": text, "at": now_iso(), "parsed": res, "plan": plan}
    save(b)
    return {"parsed": res, "plan": plan}


# ---------------------------------------------------------------- 현황 · 리뷰
def write_status(b: dict) -> Path:
    rows = [f"# 진행 현황 {b['date']}", "", f"- 모드: {b.get('mode', '-')} · 갱신 {now_iso()}", "",
            "계정 | 게시물 | 제목 | R1 | R2 | R3 | R4 | 업로드", "---|---|---|---|---|---|---|---"]
    for p in posts(b):
        meta = paths.read_json(Path(p["job"]) / "meta.json", {}) or {}
        up = meta.get("upload") or {}
        st = p["status"]
        held = " (보류)" if p.get("held") else ""
        rows.append(f"@{p['handle']} | {p['label']}{held} | {p.get('title', '')} | {st.get('R1', '-')} | {st.get('R2', '-')} | "
                    f"{st.get('R3', '-')} | {st.get('R4', '-')} | {up.get('status', '-')} {up.get('permalink') or ''}")
    f = paths.output_root() / b["date"] / "status.md"
    paths.write_text_atomic(f, "\n".join(rows) + "\n")
    return f


def write_review(b: dict) -> Path:
    root = paths.output_root() / b["date"]

    def rel(p: Path) -> str:
        return html.escape(os.path.relpath(p, root).replace("\\", "/"))

    cards = []
    for p in posts(b):
        job = Path(p["job"])
        meta = paths.read_json(job / "meta.json", {}) or {}
        hook = meta.get("hook") or {}
        cap = (job / "caption.txt").read_text(encoding="utf-8") if (job / "caption.txt").is_file() else ""
        imgs = "".join(f'<figure><img src="{rel(job / n)}" loading="lazy"><figcaption>{t}</figcaption></figure>'
                       for n, t in (("card.png", "카드"), ("card_grid.png", "그리드")) if (job / n).is_file())
        video = f'<video src="{rel(job / "reel.mp4")}" controls preload="none"></video>' if (job / "reel.mp4").is_file() else ""
        cards.append(f"""<section><h2>{html.escape(p['label'])} · @{html.escape(p['handle'])}</h2>
<p class="t">{html.escape(p.get('title') or '')}</p>
<p class="h">{html.escape(' / '.join(hook.get(k, '') for k in ('l1', 'l2', 'l3')))}</p>
<div class="row">{imgs}{video}</div><pre>{html.escape(cap)}</pre>
<p class="s">R1 {p['status'].get('R1', '-')} · R2 {p['status'].get('R2', '-')} · R3 {p['status'].get('R3', '-')} · {html.escape(str((meta.get('upload') or {}).get('status', '')))}</p></section>""")
    doc = f"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>리뷰 {b['date']}</title><style>
:root{{color-scheme:light dark;--bg:#f6f6f4;--fg:#1b1b1b;--card:#fff;--mut:#666}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151617;--fg:#eee;--card:#202224;--mut:#aaa}}}}
body{{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:15px/1.5 Pretendard,'Noto Sans KR',sans-serif}}
section{{background:var(--card);border-radius:14px;padding:16px;margin:0 0 16px}}h2{{margin:0 0 4px;font-size:18px}}
.t,.s{{color:var(--mut);margin:2px 0}}.h{{font-weight:700}}.row{{display:flex;gap:12px;flex-wrap:wrap}}
figure{{margin:0}}img,video{{width:200px;border-radius:8px;display:block}}figcaption{{font-size:12px;color:var(--mut)}}
pre{{white-space:pre-wrap;background:rgba(127,127,127,.12);padding:10px;border-radius:8px}}</style>
<h1>전 계정 리뷰 · {b['date']}</h1>{''.join(cards) or '<p>게시물 없음</p>'}</html>"""
    f = root / "review.html"
    paths.write_text_atomic(f, doc)
    return f


def _print(res: dict) -> None:
    parsed = res.get("parsed") or {}
    for s in parsed.get("summary", []):
        print(f"✔ {s}")
    for a in parsed.get("ambiguous", []):
        print(f"? 되묻기: \"{a['text']}\" — {a['reason']}")
    if parsed.get("unresolved"):
        print(f"? 아직 답하지 않은 항목: {', '.join(parsed['unresolved'])}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run_all")
    p = sub.add_parser("set-brief")
    p.add_argument("account")
    p.add_argument("--file", type=Path, required=True)
    sub.add_parser("briefing")
    for r in ("r1", "r2", "r3", "r4"):
        p = sub.add_parser(r)
        p.add_argument("text")
    p = sub.add_parser("share-factsheet")
    p.add_argument("src", type=Path)
    p.add_argument("dst", type=Path, nargs="+")
    p = sub.add_parser("set-rec")
    p.add_argument("label")
    p.add_argument("--hook", required=True, choices=list(HOOK_KO))
    p.add_argument("--photo", required=True)
    p = sub.add_parser("render")
    p.add_argument("--labels")
    p = sub.add_parser("r4-table")
    p.add_argument("--start")
    sub.add_parser("status")
    sub.add_parser("review")
    a = ap.parse_args()
    d = a.date

    if a.cmd == "run_all":
        b = run_all(d)
        print(f"✔ 모드: {b.get('mode')} · 규칙 {(b.get('rules') or {}).get('version')} ({(b.get('rules') or {}).get('status')})")
        for h, c in (b.get("collect") or {}).items():
            print(f"{'✔' if c.get('ok') else '✘'} @{h}: " + (f"사안 {c['clusters']}개 → {c['path']}" if c.get("ok") else c.get("error", "")))
            for s in c.get("failed_sources") or []:
                print(f"  ⚠ {s}")
        return 0 if (b.get("rules") or {}).get("status") != "error" else 2
    if a.cmd == "set-brief":
        set_brief(a.account, json.loads(a.file.read_text(encoding="utf-8")), d)
        print("✔ 브리핑 항목 저장")
        return 0
    if a.cmd == "briefing":
        b = load(d)
        md = briefing_md(b)
        f = paths.output_root() / b["date"] / "briefing.md"
        paths.write_text_atomic(f, md)
        print(md)
        print(f"(파일: {f})")
        return 0
    if a.cmd == "r1":
        r = apply_r1(a.text, d)
        _print(r)
        print(f"✔ 작업 생성: {', '.join(r['created']) or '없음'}")
        for g in r["shared_groups"]:
            print(f"· 같은 사안 → 팩트 시트 공유: {' = '.join(g)}")
        return 0
    if a.cmd == "share-factsheet":
        print("✔ 공유: " + ", ".join(share_factsheet(a.src, a.dst)))
        return 0
    if a.cmd == "set-rec":
        photo = a.photo if a.photo == "C" else int(a.photo)
        set_rec(a.label, a.hook, photo, d)
        print(f"✔ {a.label} 추천: {HOOK_KO[a.hook]}, 사진 {photo}")
        return 0
    if a.cmd == "r2":
        r = apply_r2(a.text, d)
        _print(r)
        for e in r["errors"]:
            print(f"✘ {e}")
        print(f"✔ 렌더 대상: {', '.join(r['ready_to_render']) or '없음'}")
        return 0
    if a.cmd == "render":
        out = render(a.labels.split(",") if a.labels else None, d)
        for lb, r in out.items():
            print(f"{'✔' if r['ok'] else '✘'} {lb}")
            for line in r["log"].splitlines():
                if line.strip().startswith(("✘", "⚠", "·")) or "릴스" in line:
                    print(f"  {line.strip()}")
        return 0 if all(r["ok"] for r in out.values()) else 1
    if a.cmd == "r3":
        r = apply_r3(a.text, d)
        _print(r)
        for x in r["redo"]:
            print(f"↻ {x['label']} 재작업 {'/'.join(x['targets'])}: {x['request']} ({x['job']})")
        return 0
    if a.cmd == "r4-table":
        st = dt.datetime.fromisoformat(a.start) if a.start else None
        r = r4_table(st.astimezone() if st and not st.tzinfo else st, d)
        print(r["table"])
        return 0
    if a.cmd == "r4":
        r = apply_r4(a.text, d)
        _print(r)
        for x in r["plan"]:
            print(f"→ {x['label']} @{x['handle']} {x['method']} {x['at'][11:16]}  {x['job']}")
        if not r["plan"]:
            print("· 업로드하지 않음")
        return 0
    if a.cmd == "status":
        print(f"✔ {write_status(load(d))}")
        return 0
    if a.cmd == "review":
        print(f"✔ {write_review(load(d))}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
