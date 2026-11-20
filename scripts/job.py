"""작업 폴더(output/<날짜>/<handle>/<번호>_<슬러그>/)와 meta.json 관리.

    python job.py new <account> --title "제목" [--slug 슬러그] [--cluster candidates.json#<id>]
    python job.py show <job_dir>
    python job.py set <job_dir> key=value [key.sub=value ...]      (값은 JSON 이면 JSON 으로 해석)
    python job.py stage <job_dir> <stage> <status> [--note ...]
    python job.py select-hook <job_dir> <strong|normal|soft> [--l1 ...] [--l2 ...] [--l3 ...]
    python job.py factsheet-md <job_dir>
    python job.py list [account] [--date YYYY-MM-DD]

meta.json: 규칙 버전(작업 시작 때 고정), 선택 훅, 단계 상태, 사진, 템플릿.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import config as cfgmod  # noqa: E402
from common import paths  # noqa: E402
from common.rules import load_state, verify_cached  # noqa: E402

STAGES = ["brief", "research", "hooks", "photo", "card", "reel", "caption", "upload"]
STATUSES = ["pending", "in_progress", "waiting_user", "approved", "done", "failed", "skipped"]


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def load_meta(job: Path) -> dict:
    m = paths.read_json(Path(job) / "meta.json")
    if m is None:
        raise SystemExit(f"✘ meta.json 이 없습니다: {job}")
    return m


def save_meta(job: Path, meta: dict) -> None:
    meta["updated_at"] = now_iso()
    paths.write_json(Path(job) / "meta.json", meta)


def new_job(account: str, title: str, slug: str | None = None, cluster: dict | None = None,
            date: str | None = None) -> Path:
    handle = cfgmod.resolve_account(account)
    st = load_state()
    active = st.get("active") or {}
    if not active.get("version"):
        raise SystemExit("✘ 규칙이 없습니다. rules_sync 를 먼저 실행하세요.")
    verify_cached(active["version"])
    from common.rules import compose

    c = compose(cfgmod.load_profile(handle), active["version"])
    job = paths.new_job_dir(handle, slug or title, date)
    meta = {
        "handle": handle,
        "title": title,
        "created_at": now_iso(),
        "rules": {"version": active["version"], "channel": active.get("channel"),
                  "published_at": active.get("published_at")},
        "content_type": c["content_type"],
        "main_category": c["main"],
        "cluster": cluster,
        "selected_hook": None,
        "hook": None,
        "template": None,
        "photo": None,
        "stages": {s: "pending" for s in STAGES},
        "history": [],
    }
    meta["stages"]["brief"] = "done"
    save_meta(job, meta)
    return job


def _set_dotted(d: dict, key: str, value) -> None:
    parts = key.split(".")
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = value


def set_stage(job: Path, stage: str, status: str, note: str | None = None) -> dict:
    if stage not in STAGES or status not in STATUSES:
        raise SystemExit(f"✘ stage ∈ {STAGES}, status ∈ {STATUSES}")
    meta = load_meta(job)
    meta.setdefault("stages", {})[stage] = status
    meta.setdefault("history", []).append({"at": now_iso(), "stage": stage, "status": status, **({"note": note} if note else {})})
    save_meta(job, meta)
    return meta


def factsheet_md(job: Path) -> Path:
    fs = paths.read_json(Path(job) / "factsheet.json")
    if not fs:
        raise SystemExit("✘ factsheet.json 이 없습니다")
    ver = [c for c in fs.get("claims", []) if c.get("verified")]
    unv = [c for c in fs.get("claims", []) if not c.get("verified")]
    lines = [f"# 팩트 시트 — {fs.get('topic', '')}", "",
             f"- 분야: {fs.get('category', '')} · 수집: {fs.get('collected_at', '')}",
             f"- 확정 {len(ver)}개 / 미확정 {len(unv)}개", ""]

    def block(title, items):
        out = [f"## {title}", ""]
        if not items:
            out += ["(없음)", ""]
        for c in items:
            extra = ""
            if c.get("type") == "quote":
                extra = f" — {c.get('speaker', '?')}, {c.get('place', '?')}, {c.get('said_at', '?')}"
            out.append(f"- **{c['id']}** [{c.get('type')}] {c.get('claim')}{extra}")
            for s in c.get("sources", []):
                t = f" 「{s['title']}」" if s.get("title") else ""
                out.append(f"  - {s.get('outlet', '?')} ({s.get('published', '?')}){t} {s.get('url', '')}")
            if c.get("note"):
                out.append(f"  - 메모: {c['note']}")
        return out + [""]

    lines += block("확정 항목 (훅·카드에 사용 가능)", ver)
    lines += block("미확정 항목 (훅·카드 사용 금지, 캡션은 주장 주체 명시)", unv)
    p = Path(job) / "factsheet.md"
    paths.write_text_atomic(p, "\n".join(lines))
    return p


def select_hook(job: Path, version: str, l1=None, l2=None, l3=None) -> dict:
    hooks = paths.read_json(Path(job) / "hooks.json")
    if not hooks or version not in hooks:
        raise SystemExit(f"✘ hooks.json 에 {version} 버전이 없습니다")
    h = {k: hooks[version][k] for k in ("l1", "l2", "l3")}
    h["basis"] = hooks[version].get("basis", {})
    edited = {}
    for k, v in (("l1", l1), ("l2", l2), ("l3", l3)):
        if v is not None:
            h[k] = v
            edited[k] = v
    meta = load_meta(job)
    meta["selected_hook"] = version
    meta["hook"] = h
    meta["hook_edited"] = edited or None
    save_meta(job, meta)
    return meta


RERUN_FROM = ["card", "reel", "caption"]      # 새 규칙으로 다시 만들어야 하는 결과물 단계


def rebase_rules(job: Path) -> dict:
    """진행 중인 작업의 고정 규칙 버전을 현재 설치된(검증된) 버전으로 올린다.
    업로드가 끝난 작업은 바꾸지 않는다. 카드 이후 결과물 단계는 다시 만들도록 pending 으로 돌린다."""
    meta = load_meta(job)
    st = meta.get("stages") or {}
    if st.get("upload") == "done":
        raise SystemExit("✘ 이미 업로드한 작업은 규칙 버전을 바꾸지 않습니다.")
    active = load_state().get("active") or {}
    if not active.get("version"):
        raise SystemExit("✘ 설치된 규칙이 없습니다. rules_sync 를 먼저 실행하세요.")
    verify_cached(active["version"])
    old = (meta.get("rules") or {}).get("version")
    if old == active["version"]:
        return {"changed": False, "from": old, "to": old, "reset": []}
    meta["rules"] = {"version": active["version"], "channel": active.get("channel"),
                     "published_at": active.get("published_at")}
    reset = [s for s in RERUN_FROM if st.get(s) not in (None, "pending", "skipped")]
    for s in reset:
        st[s] = "pending"
    meta["stages"] = st
    meta.setdefault("history", []).append({"at": now_iso(), "stage": "rules", "status": "rebased",
                                           "note": f"{old} → {active['version']}, 다시 할 단계: {reset}"})
    save_meta(job, meta)
    return {"changed": True, "from": old, "to": active["version"], "reset": reset}


def list_jobs(account: str | None, date: str | None) -> list[dict]:
    root = paths.output_root() / (date or paths.today_str())
    out = []
    if not root.is_dir():
        return out
    handles = [cfgmod.resolve_account(account)] if account else [p.name for p in root.iterdir() if p.is_dir()]
    for h in handles:
        d = root / h
        if not d.is_dir():
            continue
        for j in sorted(d.iterdir()):
            m = paths.read_json(j / "meta.json")
            if m:
                out.append({"job": str(j), "title": m.get("title"), "stages": m.get("stages"),
                            "rules": (m.get("rules") or {}).get("version")})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("new")
    p.add_argument("account")
    p.add_argument("--title", required=True)
    p.add_argument("--slug")
    p.add_argument("--cluster", help="candidates.json 경로#클러스터id")
    p.add_argument("--date")
    p = sub.add_parser("show")
    p.add_argument("job", type=Path)
    p = sub.add_parser("set")
    p.add_argument("job", type=Path)
    p.add_argument("pairs", nargs="+")
    p = sub.add_parser("stage")
    p.add_argument("job", type=Path)
    p.add_argument("stage")
    p.add_argument("status")
    p.add_argument("--note")
    p = sub.add_parser("select-hook")
    p.add_argument("job", type=Path)
    p.add_argument("version")
    p.add_argument("--l1")
    p.add_argument("--l2")
    p.add_argument("--l3")
    p = sub.add_parser("factsheet-md")
    p.add_argument("job", type=Path)
    p = sub.add_parser("rebase-rules")
    p.add_argument("job", type=Path)
    p = sub.add_parser("list")
    p.add_argument("account", nargs="?")
    p.add_argument("--date")
    a = ap.parse_args()

    if a.cmd == "new":
        cluster = None
        if a.cluster:
            path, _, cid = a.cluster.partition("#")
            data = paths.read_json(Path(path), {}) or {}
            cluster = next((c for c in data.get("clusters", []) if str(c.get("id")) == cid), None)
            if cluster is None:
                raise SystemExit(f"✘ 후보를 찾을 수 없음: {a.cluster}")
        job = new_job(a.account, a.title, a.slug, cluster, a.date)
        print(f"✔ 작업 폴더: {job}")
        print(json.dumps({"job": str(job)}, ensure_ascii=False))
    elif a.cmd == "show":
        print(json.dumps(load_meta(a.job), ensure_ascii=False, indent=2))
    elif a.cmd == "set":
        meta = load_meta(a.job)
        for pair in a.pairs:
            k, _, v = pair.partition("=")
            try:
                val = json.loads(v)
            except json.JSONDecodeError:
                val = v
            _set_dotted(meta, k, val)
        save_meta(a.job, meta)
        print("✔ 저장")
    elif a.cmd == "stage":
        set_stage(a.job, a.stage, a.status, a.note)
        print(f"✔ {a.stage} = {a.status}")
    elif a.cmd == "select-hook":
        m = select_hook(a.job, a.version, a.l1, a.l2, a.l3)
        print(f"✔ 선택 훅: {m['selected_hook']}  {m['hook']['l1']} / {m['hook']['l2']} / {m['hook']['l3']}")
    elif a.cmd == "factsheet-md":
        print(f"✔ {factsheet_md(a.job)}")
    elif a.cmd == "rebase-rules":
        r = rebase_rules(a.job)
        if r["changed"]:
            print(f"✔ 규칙 {r['from']} → {r['to']} 로 변경. 다시 할 단계: {', '.join(r['reset']) or '없음'}")
            print("  다음: ia checks hook JOB --selected 로 선택 훅을 새 기준으로 다시 검사")
        else:
            print(f"✔ 이미 최신 규칙({r['to']})입니다.")
        print(json.dumps(r, ensure_ascii=False))
    elif a.cmd == "list":
        print(json.dumps(list_jobs(a.account, a.date), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
