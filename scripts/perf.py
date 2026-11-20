"""성과 기록 (선택) — 게시 24시간·72시간 뒤 지표 → accounts/<handle>/performance.jsonl (이용자 PC 에만).

    python perf.py due                                   # 기록할 차례인 게시물 (24h·72h 지난 것)
    python perf.py fetch <job_dir> --at 24h              # 방식 B: 인사이트 API
    python perf.py record <job_dir> --at 24h --json '{"likes": 12, "comments": 3, "views": 820}'
                                                          # 방식 A·C: Claude 가 브라우저로 본인 게시물 공개 지표를 읽어 기록
    python perf.py report <account> [--days 28]          # 주간 리포트: 훅 강도·템플릿·카테고리·게시 시각별 비교 + 조정안
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import config as cfgmod  # noqa: E402
from common import paths  # noqa: E402

CHECKPOINTS = {"24h": 24, "72h": 72}
METRICS = ["views", "reach", "likes", "comments", "shares", "saved"]


def now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def perf_file(handle: str) -> Path:
    return paths.account_dir(handle) / "performance.jsonl"


def records(handle: str) -> list[dict]:
    f = perf_file(handle)
    if not f.is_file():
        return []
    return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]


def due() -> list[dict]:
    import upload

    out = []
    for h in cfgmod.list_accounts():
        have = {(r["job"], r["at"]) for r in records(h)}
        for e in upload.history(h):
            if e.get("status") != "published" or not e.get("published_at"):
                continue
            age = (now() - dt.datetime.fromisoformat(e["published_at"])).total_seconds() / 3600
            for cp, hours in CHECKPOINTS.items():
                if age >= hours and (e["job"], cp) not in have and age < hours + 24 * 7:
                    out.append({"handle": h, "job": e["job"], "at": cp, "method": e.get("method"),
                                "permalink": e.get("permalink"), "media_id": e.get("media_id")})
    return out


def save(job: Path, at: str, metrics: dict, source: str) -> dict:
    meta = paths.read_json(job / "meta.json")
    up = meta.get("upload") or {}
    pub = dt.datetime.fromisoformat(up["published_at"]) if up.get("published_at") else None
    rec = {"job": str(job), "handle": meta["handle"], "at": at, "recorded_at": now().isoformat(timespec="seconds"),
           "source": source, "metrics": {k: metrics.get(k) for k in METRICS if metrics.get(k) is not None},
           "hook_version": meta.get("selected_hook"), "template": meta.get("template"),
           "category": meta.get("main_category"), "content_type": meta.get("content_type"),
           "posted_hour": pub.hour if pub else None, "permalink": up.get("permalink"), "title": meta.get("title")}
    with open(perf_file(meta["handle"]), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def fetch(job: Path, at: str) -> dict:
    import ig_api

    meta = paths.read_json(job / "meta.json")
    mid = (meta.get("upload") or {}).get("media_id")
    if not mid:
        raise SystemExit("✘ media_id 가 없습니다 (방식 B 로 게시한 게시물만 API 조회 가능 — 나머지는 record)")
    m = ig_api.Client(meta["handle"]).insights(mid)
    return save(job, at, m, "api")


def report(handle: str, days: int = 28) -> tuple[str, Path]:
    cut = now() - dt.timedelta(days=days)
    recs = [r for r in records(handle) if dt.datetime.fromisoformat(r["recorded_at"]) >= cut]
    latest: dict[str, dict] = {}
    for r in sorted(recs, key=lambda x: CHECKPOINTS.get(x["at"], 0)):
        latest[r["job"]] = r            # 게시물당 가장 늦은 시점(72h 우선)
    rows = list(latest.values())
    key = "views" if any("views" in r["metrics"] for r in rows) else "reach" if any("reach" in r["metrics"] for r in rows) else "likes"

    def score(r):
        return r["metrics"].get(key)

    md = [f"# @{handle} 성과 리포트 (최근 {days}일 · 게시물 {len(rows)}개 · 기준 지표 {key})", ""]
    suggestions = []
    for dim, label in (("hook_version", "훅 강도"), ("template", "템플릿"), ("category", "카테고리"), ("posted_hour", "게시 시각")):
        groups = collections.defaultdict(list)
        for r in rows:
            if score(r) is not None and r.get(dim) is not None:
                groups[r[dim]].append(score(r))
        if not groups:
            continue
        md += [f"## {label}", "", "값 | 게시물 | 평균 | 중앙값", "---|---|---|---"]
        ranked = sorted(groups.items(), key=lambda kv: -statistics.mean(kv[1]))
        for v, xs in ranked:
            md.append(f"{v}{'시' if dim == 'posted_hour' else ''} | {len(xs)} | {statistics.mean(xs):.0f} | {statistics.median(xs):.0f}")
        md.append("")
        if len(ranked) >= 2 and len(ranked[0][1]) >= 3 and len(ranked[1][1]) >= 3:
            best, second = ranked[0], ranked[1]
            lift = statistics.mean(best[1]) / max(1e-9, statistics.mean(second[1])) - 1
            if lift >= 0.2:
                suggestions.append(f"{label}: '{best[0]}' 가 '{second[0]}' 보다 평균 {lift * 100:.0f}% 높음 "
                                   f"({len(best[1])}개 vs {len(second[1])}개) — 계정 설정 조정 검토")
    md += ["## 조정안 (승인 시 반영)", ""] + ([f"- {s}" for s in suggestions] or ["- 비교할 표본이 아직 적습니다 (그룹당 3개 이상 필요)"])
    d = paths.account_dir(handle) / "reports"
    d.mkdir(exist_ok=True)
    f = d / f"{dt.date.today().isoformat()}.md"
    paths.write_text_atomic(f, "\n".join(md) + "\n")
    return "\n".join(md), f


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("due")
    p = sub.add_parser("fetch")
    p.add_argument("job", type=Path)
    p.add_argument("--at", choices=list(CHECKPOINTS), required=True)
    p = sub.add_parser("record")
    p.add_argument("job", type=Path)
    p.add_argument("--at", choices=list(CHECKPOINTS), required=True)
    p.add_argument("--json", required=True)
    p = sub.add_parser("report")
    p.add_argument("account")
    p.add_argument("--days", type=int, default=28)
    a = ap.parse_args()
    if a.cmd == "due":
        items = due()
        for it in items:
            print(f"· @{it['handle']} {Path(it['job']).name} {it['at']} ({it['method']}) {it.get('permalink') or ''}")
        print(f"✔ 기록할 차례 {len(items)}건")
    elif a.cmd == "fetch":
        r = fetch(a.job, a.at)
        print(f"✔ {a.at} 지표: {r['metrics']}")
    elif a.cmd == "record":
        r = save(a.job, a.at, json.loads(a.json), "browser")
        print(f"✔ {a.at} 지표 기록: {r['metrics']}")
    else:
        md, f = report(cfgmod.resolve_account(a.account), a.days)
        print(md)
        print(f"(파일: {f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
