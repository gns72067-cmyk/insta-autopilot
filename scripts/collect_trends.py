"""추천 후보 수집 — 카테고리 프리셋 소스 + Google Trends(geo=KR) + 계정 추가 소스 → 같은 사안 묶기.

    python collect_trends.py <account> [--max-clusters 40] [--json]

- 소스: 합성 규칙의 sources.rss / sources.google_news(→ _catalog.yaml google_news_rss) / google_trends
  (→ google_trends_rss), 계정 extra_sources. Google 요청은 순차·1초 이상 간격.
- 수집 범위: checks.yaml brief.lookback_hours.<content_type>
- 제외 키워드(계정 설정)가 제목에 있으면 뺀다.
- 제목 유사도(문자 bigram·핵심 어절·숫자)로 같은 사안을 묶고, 사안별 원자료 신호를 계산한다:
  첫 보도 후 경과 시간, 독립 매체 수, 트렌드 키워드 일치, Google Trends 급상승 일치, 단독 표시, 최근 게시 이력.
  점수 계산·10개 선정은 brief 규칙에 따라 Claude 가 한다(이 스크립트는 신호와 예비 정렬만).
결과: output/<날짜>/<handle>/candidates.json
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import email.utils
import html
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import config as cfgmod  # noqa: E402
from common import log, paths  # noqa: E402
from common.rules import compose, get, plugin_version  # noqa: E402

L = log.get("trends")
UA = f"Mozilla/5.0 (compatible; insta-autopilot/{plugin_version()})"
GOOGLE_HOSTS = ("news.google.com", "trends.google.com")
JOSA = ("에서는", "으로는", "에서", "으로", "에게", "까지", "부터", "보다", "처럼", "이다", "했다", "한다",
        "은", "는", "이", "가", "을", "를", "의", "에", "로", "와", "과", "도", "만")
TAG_RE = re.compile(r"\[[^\]]{1,12}\]|【[^】]{1,12}】|\([^)]{0,12}(종합|속보|단독|포토|영상|사진)[^)]{0,6}\)")


# ---------------------------------------------------------------- 가져오기
def fetch(url: str, timeout: int = 25, retry_429: int = 2) -> bytes:
    import requests

    for attempt in range(retry_429 + 1):
        r = requests.get(url, timeout=timeout, headers={"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})
        if r.status_code == 429 and attempt < retry_429:
            ra = r.headers.get("Retry-After", "")
            time.sleep(min(30.0, float(ra)) if ra.isdigit() else 6.0 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.content
    r.raise_for_status()
    return r.content


def to_dt(entry) -> dt.datetime | None:
    for k in ("published_parsed", "updated_parsed"):
        t = entry.get(k)
        if t:
            return dt.datetime(*t[:6], tzinfo=dt.timezone.utc)
    for k in ("published", "updated", "pubDate"):
        s = entry.get(k)
        if s:
            try:
                return email.utils.parsedate_to_datetime(s).astimezone(dt.timezone.utc)
            except Exception:
                pass
    return None


def clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def parse_feed(data: bytes, feed_name: str, lenient: bool, google_news: bool) -> list[dict]:
    import feedparser

    f = feedparser.parse(data)
    if f.bozo and not f.entries and not lenient:
        raise ValueError(f"RSS 파싱 실패: {type(f.bozo_exception).__name__}")
    out = []
    for e in f.entries:
        title = clean(e.get("title", ""))
        outlet = feed_name
        if google_news:
            src = e.get("source") or {}
            outlet = clean(src.get("title", "")) or outlet
            if outlet and title.endswith(f" - {outlet}"):
                title = title[: -len(outlet) - 3].strip()
        if not title:
            continue
        out.append({"title": title, "link": e.get("link"), "outlet": outlet, "feed": feed_name,
                    "published": (to_dt(e).isoformat() if to_dt(e) else None),
                    "summary": clean(e.get("summary", ""))[:200] if not google_news else ""})
    return out


def parse_trends(data: bytes) -> list[dict]:
    ns = {"ht": "https://trends.google.com/trending/rss"}
    root = ET.fromstring(data)
    out = []
    for it in root.iter("item"):
        kw = (it.findtext("title") or "").strip()
        if not kw:
            continue
        news = []
        for n in it.findall("ht:news_item", ns):
            news.append({"title": clean(n.findtext("ht:news_item_title", "", ns)),
                         "link": n.findtext("ht:news_item_url", "", ns),
                         "outlet": clean(n.findtext("ht:news_item_source", "", ns))})
        pub = it.findtext("pubDate")
        try:
            pub = email.utils.parsedate_to_datetime(pub).astimezone(dt.timezone.utc).isoformat() if pub else None
        except Exception:
            pub = None
        out.append({"keyword": kw, "traffic": it.findtext("ht:approx_traffic", "", ns), "published": pub, "news": news})
    return out


def gather(c: dict) -> tuple[list[dict], list[dict], list[dict]]:
    st = c["settings"]
    src = st.get("sources") or {}
    cat = c["catalog"]
    jobs = []   # (name, url, kind, lenient)
    for r in src.get("rss") or []:
        if isinstance(r, dict) and r.get("url"):
            jobs.append((r.get("name") or urlsplit(r["url"]).netloc, r["url"], "rss", bool(r.get("lenient"))))
    for q in src.get("google_news") or []:
        jobs.append((f"Google News: {q}", cat["google_news_rss"].replace("{query}", quote_plus(q)), "gnews", True))
    if src.get("google_trends") and cat.get("google_trends_rss"):
        jobs.append(("Google Trends KR", cat["google_trends_rss"], "trends", True))

    items, trends, status = [], [], []

    def run(job):
        name, url, kind, lenient = job
        t0 = time.monotonic()
        try:
            data = fetch(url)
            if kind == "trends":
                res = parse_trends(data)
            else:
                res = parse_feed(data, name, lenient, kind == "gnews")
            return job, res, None, time.monotonic() - t0
        except Exception as e:
            return job, [], f"{type(e).__name__}: {str(e)[:120]}", time.monotonic() - t0

    google = [j for j in jobs if urlsplit(j[1]).hostname in GOOGLE_HOSTS]
    others = [j for j in jobs if j not in google]
    results = []
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(run, j) for j in others]
        for j in google:                      # Google 은 순차, 1.1초 간격
            results.append(run(j))
            time.sleep(1.5)
        results += [f.result() for f in futs]
    for (name, url, kind, _), res, err, secs in results:
        status.append({"name": name, "kind": kind, "ok": err is None, "count": len(res), "error": err,
                       "host": urlsplit(url).hostname})
        if kind == "trends":
            trends += res
        else:
            items += res
    return items, trends, status


# ---------------------------------------------------------------- 묶기
def norm_title(t: str) -> str:
    t = TAG_RE.sub(" ", t)
    t = re.sub(r"[\"'“”‘’`·…|]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def key_tokens(t: str) -> set[str]:
    out = set()
    for tok in re.findall(r"[0-9A-Za-z가-힣.%]+", t):
        for j in JOSA:
            if len(tok) > len(j) + 1 and tok.endswith(j):
                tok = tok[: -len(j)]
                break
        if len(tok) >= 2:
            out.add(tok.lower())
    return out


def bigrams(t: str) -> set[str]:
    s = re.sub(r"\s+", "", t.lower())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def numbers(t: str) -> set[str]:
    return {n.replace(",", "") for n in re.findall(r"\d[\d,.]*\d|\d", t) if len(n.replace(",", "")) >= 2}


def similar(a: dict, b: dict) -> bool:
    ba, bb = a["_bg"], b["_bg"]
    if not ba or not bb:
        return False
    jac = len(ba & bb) / len(ba | bb)
    if a["_num"] and b["_num"] and not (a["_num"] & b["_num"]):
        return jac >= 0.5          # 숫자가 서로 다르면(매일 반복되는 시황 등) 거의 같은 제목일 때만
    shared = len(a["_kt"] & b["_kt"])
    nums = a["_num"] & b["_num"]
    return jac >= 0.32 or (shared >= 3 and jac >= 0.18) or (shared >= 2 and bool(nums) and jac >= 0.12)


def cluster(items: list[dict], window_h: float = 48.0) -> list[list[dict]]:
    """시간순 탐욕 묶기: 사안의 첫 기사(seed)와 비슷하거나, 사안 기사 2개 이상과 비슷하면 합류.
    (아무 기사 하나와만 닮아도 이어지는 연쇄 병합을 막는다) seed 와 window_h 시간 넘게 떨어지면 다른 사안."""
    for it in items:
        nt = norm_title(it["title"])
        it["_bg"], it["_kt"], it["_num"] = bigrams(nt), key_tokens(nt), numbers(nt)
        it["_t"] = dt.datetime.fromisoformat(it["published"]) if it.get("published") else None
    groups: list[list[dict]] = []
    for it in sorted(items, key=lambda x: x["published"] or "9999"):
        best = None
        for g in groups:
            seed = g[0]
            if it["_t"] and seed["_t"] and (it["_t"] - seed["_t"]).total_seconds() > window_h * 3600:
                continue
            if similar(it, seed) or sum(1 for m in g[1:8] if similar(it, m)) >= 2:
                best = g
                break
        if best is None:
            groups.append([it])
        else:
            best.append(it)
    return groups


def outlet_key(o: str) -> str:
    o = re.sub(r"\s+", "", o or "").lower()
    return re.sub(r"(tv|방송|닷컴|일보|신문|뉴스)$", "", o) or o


def recent_posts(handle: str, days: int) -> list[str]:
    root = paths.output_root()
    if not root.is_dir():
        return []
    cut = dt.date.today() - dt.timedelta(days=days)
    titles = []
    for d in root.iterdir():
        try:
            if dt.date.fromisoformat(d.name) < cut:
                continue
        except ValueError:
            continue
        for m in (d / handle).glob("*/meta.json"):
            meta = paths.read_json(m, {}) or {}
            if (meta.get("stages") or {}).get("reel") == "done" or (meta.get("stages") or {}).get("card") in ("approved", "done"):
                titles.append(meta.get("title") or "")
                if meta.get("cluster"):
                    titles.append(meta["cluster"].get("title", ""))
    return [t for t in titles if t]


def build(handle: str, max_clusters: int = 40) -> dict:
    profile = cfgmod.load_profile(handle)
    c = compose(profile)
    st, ck = c["settings"], c["checks"]
    ctype = c["content_type"]
    lookback = int(get(ck, f"brief.lookback_hours.{ctype}", 72))
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=lookback)
    excl = [k.lower() for k in (st.get("exclude_keywords") or []) if k]
    kws = [k for k in (st.get("trend_keywords") or []) if k]

    items, trends, status = gather(c)
    kept, dropped_old, dropped_excl = [], 0, 0
    seen_links = set()
    for it in items:
        if it["link"] in seen_links:
            continue
        seen_links.add(it["link"])
        if it["published"] and dt.datetime.fromisoformat(it["published"]) < cutoff:
            dropped_old += 1
            continue
        if any(x in it["title"].lower() for x in excl):
            dropped_excl += 1
            continue
        kept.append(it)
    for tr in trends:   # 트렌드 키워드의 관련 기사도 후보 기사로 넣는다
        for n in tr["news"]:
            if n["title"] and n["link"] not in seen_links and not any(x in n["title"].lower() for x in excl):
                seen_links.add(n["link"])
                kept.append({**n, "feed": "Google Trends KR", "published": tr["published"], "summary": "",
                             "trend_keyword": tr["keyword"]})

    posted = [bigrams(norm_title(t)) for t in recent_posts(handle, int(get(ck, "brief.repeat_window_days", 14)))]
    clusters = []
    for g in cluster(kept):
        g.sort(key=lambda x: x["published"] or "9999")
        outlets = sorted({it["outlet"] for it in g if it.get("outlet")})
        okeys = {outlet_key(o) for o in outlets}
        dated = [dt.datetime.fromisoformat(x["published"]) for x in g if x["published"]]
        first = min(dated) if dated else None
        text = " ".join(x["title"] for x in g)
        rep = max(g, key=lambda x: sum(len(x["_bg"] & y["_bg"]) / max(1, len(x["_bg"] | y["_bg"])) for y in g))
        trend_hits = sorted({tr["keyword"] for tr in trends if tr["keyword"] and
                             (tr["keyword"].replace(" ", "") in text.replace(" ", "") or
                              any(x.get("trend_keyword") == tr["keyword"] for x in g))})
        rb = rep["_bg"]
        repeat = any(len(rb & p) / max(1, len(rb | p)) >= 0.35 for p in posted)
        clusters.append({
            "title": rep["title"],
            "outlet_count": len(okeys),
            "outlets": outlets[:12],
            "first_seen": first.isoformat() if first else None,
            "hours_since_first": round((now - first).total_seconds() / 3600, 1) if first else None,
            "fit_keywords": [k for k in kws if k.lower() in text.lower()],
            "trends": [{"keyword": k, "traffic": next((t["traffic"] for t in trends if t["keyword"] == k), "")} for k in trend_hits],
            "exclusive_only": len(okeys) <= 1 and "단독" in text,
            "single_outlet": len(okeys) <= 1,
            "recently_posted": repeat,
            "articles": [{k: x.get(k) for k in ("title", "outlet", "published", "link")} for x in g[:15]],
        })
    clusters.sort(key=lambda x: (-(x["outlet_count"] + 2 * bool(x["trends"]) + len(x["fit_keywords"]) * 0.5),
                                 x["hours_since_first"] if x["hours_since_first"] is not None else 1e9))
    clusters = clusters[:max_clusters]
    for i, cl in enumerate(clusters, 1):
        cl["id"] = i
    out = {
        "handle": handle, "collected_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "rules_version": c["version"], "content_type": ctype, "lookback_hours": lookback,
        "brief_weights": st.get("brief_weights") or get(ck, "brief.default_weights"),
        "candidates_wanted": int(get(ck, "brief.candidates", 10)),
        "counts": {"articles": len(items), "kept": len(kept), "dropped_old": dropped_old, "dropped_excluded": dropped_excl,
                   "clusters": len(clusters)},
        "sources": status, "trends": trends[:30], "clusters": clusters,
    }
    p = paths.day_dir(handle) / "candidates.json"
    paths.write_json(p, out)
    out["_path"] = str(p)
    L.info("수집 %s: 기사 %d → 사안 %d", handle, len(kept), len(clusters))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("account")
    ap.add_argument("--max-clusters", type=int, default=40)
    ap.add_argument("--show", type=int, default=25)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    handle = cfgmod.resolve_account(a.account)
    out = build(handle, a.max_clusters)
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    ok = [s for s in out["sources"] if s["ok"]]
    bad = [s for s in out["sources"] if not s["ok"]]
    print(f"✔ 소스 {len(ok)}/{len(out['sources'])} 성공 · 기사 {out['counts']['kept']}건(범위 {out['lookback_hours']}시간) → 사안 {out['counts']['clusters']}개")
    for s in bad:
        print(f"  ⚠ {s['name']}: {s['error']}")
    if out["trends"]:
        print("  급상승: " + ", ".join(f"{t['keyword']}({t['traffic']})" for t in out["trends"][:10]))
    print("\nid | 매체수 | 경과h | 적합 | 트렌드 | 표시 | 대표 제목")
    for cl in out["clusters"][: a.show]:
        flags = ",".join(f for f, v in (("단독", cl["exclusive_only"]), ("1매체", cl["single_outlet"]),
                                        ("재게시", cl["recently_posted"])) if v) or "-"
        print(f"{cl['id']} | {cl['outlet_count']} | {cl['hours_since_first']} | {','.join(cl['fit_keywords'][:3]) or '-'} | "
              f"{','.join(t['keyword'] for t in cl['trends']) or '-'} | {flags} | {cl['title']}")
    print(f"\n후보 파일: {out['_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
