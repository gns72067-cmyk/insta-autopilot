"""캡션 초안 저장 → 이용자 직접 수정 → 완료, 수정 기록과 주간 개선안.

    python caption.py style-status <account>
    python caption.py style-add <account> --file captions.txt [--source paste|profile]
    python caption.py draft <job_dir> --file draft.txt [--force]
    python caption.py done <job_dir> [--from-file 수정본.txt]
    python caption.py suggest <account> [--if-due] [--days 7]
    python caption.py apply <account> <suggestion.json> --items 1,3

- 초안은 caption_original.txt(바뀌지 않음)와 caption.txt(이용자가 고치는 파일) 두 개로 저장한다.
- 이용자는 채팅으로 수정본을 주거나(→ --from-file) PC 에서 caption.txt 를 직접 고친 뒤 "완료"라고 한다.
  업로드는 항상 caption.txt 최신본을 쓴다.
- 원본 대비 수정 내용은 accounts/<handle>/edit_log.jsonl 에 쌓인다(이용자 PC 에만).
- suggest: 최근 수정 기록에서 style_examples.md 추가안·개인 금지 표현안을 만든다. 이용자가 승인한 항목만 apply.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import difflib
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import config as cfgmod  # noqa: E402
from common import paths  # noqa: E402
from common.rules import compose  # noqa: E402

EXAMPLE_SEP = "\n---\n"
STYLE_HEADER = "# 스타일 예시"


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- style_examples.md
def style_file(handle: str) -> Path:
    return paths.account_dir(handle) / "style_examples.md"


def style_examples(handle: str) -> list[str]:
    f = style_file(handle)
    if not f.is_file():
        return []
    body = f.read_text(encoding="utf-8").replace("\r\n", "\n")
    blocks = [b.strip() for b in re.split(r"\n-{3,}\n", body)]
    out = []
    for b in blocks:
        lines = [l for l in b.split("\n") if l.strip() and not l.startswith("#")
                 and "적어 두면" not in l]
        if len("".join(lines)) >= 20:
            out.append("\n".join(lines))
    return out


def style_add(handle: str, text: str, source: str) -> int:
    parts = [p.strip() for p in re.split(r"\n-{3,}\n|\n\s*\n\s*\n", text.replace("\r\n", "\n")) if len(p.strip()) >= 20]
    f = style_file(handle)
    cur = f.read_text(encoding="utf-8") if f.is_file() else STYLE_HEADER + "\n"
    stamp = f"\n<!-- {now_iso()} {source} {len(parts)}개 -->"
    paths.write_text_atomic(f, cur.rstrip("\n") + stamp + "".join(EXAMPLE_SEP + p for p in parts) + "\n")
    return len(parts)


# ---------------------------------------------------------------- 초안 · 완료
def save_draft(job: Path, text: str, force: bool = False) -> None:
    orig, cur = job / "caption_original.txt", job / "caption.txt"
    if cur.is_file() and orig.is_file() and cur.read_text(encoding="utf-8") != orig.read_text(encoding="utf-8") and not force:
        raise SystemExit("✘ caption.txt 가 이미 수정돼 있습니다. 새 초안으로 덮어쓰려면 --force")
    text = text.replace("\r\n", "\n").strip() + "\n"
    paths.write_text_atomic(orig, text)
    paths.write_text_atomic(cur, text)
    meta = paths.read_json(job / "meta.json")
    meta.setdefault("stages", {})["caption"] = "waiting_user"
    meta["caption"] = {"draft_sha256": sha(text), "drafted_at": now_iso(), "approved": False}
    paths.write_json(job / "meta.json", meta)


def eojeol_ngrams(text: str, n_max: int = 3) -> collections.Counter:
    toks = re.findall(r"[^\s#]+", re.sub(r"#[^\s#]+", " ", text))
    c = collections.Counter()
    for n in range(1, n_max + 1):
        for i in range(len(toks) - n + 1):
            g = " ".join(toks[i:i + n])
            if len(g) >= 2:
                c[g] += 1
    return c


def diff_summary(orig: str, final: str) -> dict:
    ol, fl = orig.strip().split("\n"), final.strip().split("\n")
    sm = difflib.SequenceMatcher(None, orig, final)
    ops = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            ops.append({"op": tag, "from": orig[i1:i2], "to": final[j1:j2]})
    on, fn = eojeol_ngrams(orig), eojeol_ngrams(final)
    removed = sorted(g for g in on if g not in fn)
    added = sorted(g for g in fn if g not in on)
    return {
        "similarity": round(sm.ratio(), 3),
        "unified": "\n".join(difflib.unified_diff(ol, fl, "초안", "수정본", lineterm="", n=0)),
        "ops": ops[:60],
        "removed_phrases": removed[:40],
        "added_phrases": added[:40],
    }


def finish(job: Path, new_text: str | None = None) -> dict:
    import checks

    meta = paths.read_json(job / "meta.json")
    orig_f, cur_f = job / "caption_original.txt", job / "caption.txt"
    if not orig_f.is_file():
        raise SystemExit("✘ 캡션 초안이 없습니다 (caption.py draft 먼저)")
    if new_text is not None:
        paths.write_text_atomic(cur_f, new_text.replace("\r\n", "\n").strip() + "\n")
    final = cur_f.read_text(encoding="utf-8")
    orig = orig_f.read_text(encoding="utf-8")
    profile = cfgmod.load_profile(meta["handle"])
    c = compose(profile, (meta.get("rules") or {}).get("version"))
    fs = paths.read_json(job / "factsheet.json", {}) or {}
    res = checks.check_caption(final, fs, meta, c["checks"], c["banned_phrases"], user_edit=True)
    d = diff_summary(orig, final)
    entry = {"at": now_iso(), "handle": meta["handle"], "job": job.name, "date": job.parent.parent.name,
             "edited": orig != final, "original": orig, "final": final, **{k: d[k] for k in
             ("similarity", "removed_phrases", "added_phrases", "ops")}, "warnings": res["warnings"] + res["errors"]}
    log = paths.account_dir(meta["handle"]) / "edit_log.jsonl"
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    meta["caption"] = {**(meta.get("caption") or {}), "approved": True, "approved_at": now_iso(),
                       "sha256": sha(final.strip() + "\n"), "chars": len(final.strip()), "edited": orig != final,
                       "warnings": res["warnings"] + res["errors"]}
    meta.setdefault("stages", {})["caption"] = "approved"
    paths.write_json(job / "meta.json", meta)
    return {"check": res, "diff": d, "edited": orig != final}


def caption_for_upload(job: Path) -> tuple[str, bool]:
    """업로드용 캡션: 항상 caption.txt 최신본. 승인 후 바뀌었으면 True."""
    meta = paths.read_json(job / "meta.json") or {}
    text = (job / "caption.txt").read_text(encoding="utf-8").strip()
    changed = sha(text + "\n") != (meta.get("caption") or {}).get("sha256")
    return text, changed


# ---------------------------------------------------------------- 주간 개선안
def state_file(handle: str) -> Path:
    return paths.account_dir(handle) / "state.json"


def suggest(handle: str, days: int = 7, if_due: bool = False) -> dict | None:
    st = paths.read_json(state_file(handle), {}) or {}
    last = st.get("last_suggest_at")
    if if_due and last and dt.datetime.fromisoformat(last) > dt.datetime.now().astimezone() - dt.timedelta(days=7):
        return None
    log = paths.account_dir(handle) / "edit_log.jsonl"
    entries = []
    if log.is_file():
        cut = dt.datetime.now().astimezone() - dt.timedelta(days=days)
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if dt.datetime.fromisoformat(e["at"]) >= cut:
                entries.append(e)
    edited = [e for e in entries if e.get("edited")]
    removed = collections.Counter(g for e in edited for g in set(e.get("removed_phrases") or []))
    added = collections.Counter(g for e in edited for g in set(e.get("added_phrases") or []))
    personal = paths.account_dir(handle) / "banned_phrases.txt"
    have = set(personal.read_text(encoding="utf-8").splitlines()) if personal.is_file() else set()
    items = []
    for g, n in removed.most_common(30):
        if n >= 2 and len(g) >= 2 and not re.search(r"\d", g) and g not in have and added[g] == 0:
            items.append({"kind": "banned", "value": g, "why": f"초안에서 {n}번 지움"})
    known = set(style_examples(handle))
    for e in sorted(edited, key=lambda x: x.get("similarity", 1))[:3]:
        if e["final"].strip() not in known and e.get("similarity", 1) < 0.9:
            items.append({"kind": "style_example", "value": e["final"].strip(),
                          "why": f"{e['date']} {e['job']} — 초안과 유사도 {e['similarity']} (많이 고침)"})
    for i, it in enumerate(items, 1):
        it["no"] = i
    d = paths.account_dir(handle) / "suggestions"
    d.mkdir(exist_ok=True)
    day = dt.date.today().isoformat()
    out = {"handle": handle, "created_at": now_iso(), "period_days": days, "entries": len(entries),
           "edited": len(edited), "items": items}
    paths.write_json(d / f"{day}.json", out)
    md = [f"# @{handle} 캡션 개선안 ({day}, 최근 {days}일 · 캡션 {len(entries)}건 중 수정 {len(edited)}건)", ""]
    if not items:
        md.append("제안할 항목이 없습니다.")
    for it in items:
        label = "개인 금지 표현 추가" if it["kind"] == "banned" else "스타일 예시 추가"
        val = it["value"] if it["kind"] == "banned" else it["value"].replace("\n", " / ")[:200]
        md.append(f"{it['no']}. **{label}**: {val}  ({it['why']})")
    paths.write_text_atomic(d / f"{day}.md", "\n".join(md) + "\n")
    st["last_suggest_at"] = now_iso()
    paths.write_json(state_file(handle), st)
    out["_md"] = str(d / f"{day}.md")
    out["_json"] = str(d / f"{day}.json")
    return out


def apply(handle: str, sugg: Path, nos: list[int]) -> list[str]:
    data = paths.read_json(sugg)
    done = []
    personal = paths.account_dir(handle) / "banned_phrases.txt"
    for it in data["items"]:
        if it["no"] not in nos:
            continue
        if it["kind"] == "banned":
            cur = personal.read_text(encoding="utf-8") if personal.is_file() else "# 개인 금지 표현 (캡션 수정 기록에서 승인한 것)\n"
            paths.write_text_atomic(personal, cur.rstrip("\n") + "\n" + it["value"] + "\n")
            done.append(f"금지 표현 추가: {it['value']}")
        else:
            style_add(handle, it["value"], "suggestion")
            done.append("스타일 예시 추가: " + it["value"].split("\n")[0][:40])
    return done


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("style-status")
    p.add_argument("account")
    p = sub.add_parser("style-add")
    p.add_argument("account")
    p.add_argument("--file", type=Path, required=True)
    p.add_argument("--source", default="paste", choices=["paste", "profile"])
    p = sub.add_parser("draft")
    p.add_argument("job", type=Path)
    p.add_argument("--file", type=Path, required=True)
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("done")
    p.add_argument("job", type=Path)
    p.add_argument("--from-file", type=Path, help="채팅으로 받은 수정본을 저장한 파일")
    p = sub.add_parser("suggest")
    p.add_argument("account")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--if-due", action="store_true")
    p = sub.add_parser("apply")
    p.add_argument("account")
    p.add_argument("suggestion", type=Path)
    p.add_argument("--items", required=True)
    a = ap.parse_args()

    if a.cmd == "style-status":
        h = cfgmod.resolve_account(a.account)
        ex = style_examples(h)
        print(f"{'✔' if len(ex) >= 3 else '⚠'} @{h} 스타일 예시 {len(ex)}개 ({style_file(h)})")
        if len(ex) < 3:
            print("  → 과거 캡션 10~20개를 붙여넣거나, 브라우저로 본인 프로필 최근 게시물 캡션을 읽어 채울지 이용자에게 묻는다")
        return 0
    if a.cmd == "style-add":
        h = cfgmod.resolve_account(a.account)
        n = style_add(h, a.file.read_text(encoding="utf-8"), a.source)
        print(f"✔ @{h} 스타일 예시 {n}개 추가 → 총 {len(style_examples(h))}개")
        return 0
    if a.cmd == "draft":
        save_draft(a.job, a.file.read_text(encoding="utf-8"), a.force)
        print(f"✔ 초안 저장: {a.job / 'caption.txt'}")
        print("  → 이 파일을 PC 에서 직접 고치거나, 채팅으로 수정본을 보내고 '완료'라고 하면 됩니다")
        return 0
    if a.cmd == "done":
        r = finish(a.job, a.from_file.read_text(encoding="utf-8") if a.from_file else None)
        ck = r["check"]
        print(f"✔ 캡션 확정 ({ck['chars']}자 · 해시태그 {ck['hashtags']}개) — "
              + ("수정 반영, 기록 저장" if r["edited"] else "초안 그대로"))
        for x in ck["errors"]:
            print(f"  ✘ {x}")
        for x in ck["warnings"]:
            print(f"  ⚠ {x}")
        if r["edited"] and r["diff"]["unified"]:
            print("  변경:\n" + "\n".join("    " + l for l in r["diff"]["unified"].splitlines()[2:20]))
        return 0 if ck["ok"] else 1
    if a.cmd == "suggest":
        h = cfgmod.resolve_account(a.account)
        out = suggest(h, a.days, a.if_due)
        if out is None:
            print(f"· @{h} 개선안: 이번 주 이미 제안함")
            return 0
        print(f"✔ @{h} 개선안 {len(out['items'])}건: {out['_md']}")
        for it in out["items"]:
            print(f"  {it['no']}. [{it['kind']}] {it['value'][:60]} ({it['why']})")
        return 0
    if a.cmd == "apply":
        h = cfgmod.resolve_account(a.account)
        for d in apply(h, a.suggestion, [int(x) for x in re.split(r"[,\s]+", a.items) if x]):
            print(f"✔ {d}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
