"""훅·팩트 시트 자동 검사 (수치는 합성 규칙의 checks.yaml 만 사용).

    python checks.py fact <job_dir>
    python checks.py hook <job_dir> [--selected] [--only strong|normal|soft]
    python checks.py caption <job_dir> [--user-edit] [--file caption.txt]

hook 검사 항목
  - 글자 수 (hook.max_chars.l1/l2/l3, 공백 포함)
  - 표절: 출처 기사 제목(팩트 시트 sources[].title + 추천 단계 사안 기사 제목)과 연속 일치 어절 수
          ≥ plagiarism.max_consecutive_eojeol 이면 실패 (줄 단위 + 세 줄 이어서)
  - 따옴표 안 문구가 팩트 시트 quote 항목 원문에 실제로 있는지 (그리고 그 줄의 근거에 포함되는지)
  - 줄별 근거 claim id: 존재·확정(verified)·allegation 아님
  - 줄에 나온 숫자가 그 줄 근거 claim 에 실제로 있는지
  - banned_phrases (부분 일치, re: 정규식) · 물음표/느낌표 합계(hook.max_punct_per_version) · 이모지(hook.allow_emoji)
fact 검사 항목
  - 스키마(fact 규칙 3절), quote 필수 필드, allegation 은 verified=false
  - number 확정 = 서로 다른 매체 number.min_sources 이상, fact 확정 = fact.min_sources 이상
  - 사안 전체 독립 출처 수 ≥ fact.min_sources, 확정 항목 ≥ fact.min_verified_claims
caption 검사 항목 (caption 규칙)
  - 2,200자(caption.max_chars)·해시태그 30개(인스타 한도), 해시태그 개수(caption.hashtags), 본문 줄 수(caption.body_lines)
  - 이모지(caption.max_emoji)·줄표(caption.max_dashes)·같은 종결어미 연속(caption.max_same_ending_run)
  - 출처 줄 존재·매체 수(caption.max_sources_in_line), 사진 출처 줄(meta.photo 와 일치, C 템플릿이면 없어야 함)
  - banned_phrases, 출처 문장(제목·excerpt)과 연속 어절 일치, 팩트 시트 밖 숫자
  --user-edit: 이용자 수정본 — 인스타 한도 외에는 경고만(고치지 않음). 업로드 확인 단계에서 금지 표현은 실패 처리.
결과는 job_dir/checks_<kind>.json 에도 저장. 종료 코드 0 = 통과, 1 = 실패.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import config as cfgmod  # noqa: E402
from common import paths  # noqa: E402
from common.rules import compose, get  # noqa: E402

CLAIM_TYPES = {"fact", "quote", "number", "allegation"}
QUOTE_RE = re.compile(r"[\"“”]([^\"“”]{1,80})[\"“”]|[‘’']([^‘’']{1,80})[‘’']|「([^」]{1,80})」|『([^』]{1,80})』")
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF\U00002B00-\U00002BFF️]")
PUNCT_RE = re.compile(r"[?!？！‽]")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def eojeols(s: str) -> list[str]:
    s = re.sub(r"(?<!\d)[.,]|[.,](?!\d)", " ", s or "")          # 29.0% · 1,800 의 점·쉼표는 숫자 안이면 유지
    toks = re.split(r"\s+", re.sub(r"[\"'“”‘’「」『』\[\]()<>…·!?:;~|/]", " ", s))
    return [t for t in toks if t]


def longest_run(a: list[str], b: list[str]) -> tuple[int, list[str]]:
    best, where = 0, []
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best, where = cur[j], a[i - cur[j]:i]
        prev = cur
    return best, where


def _norm_num(n: str) -> str:
    n = n.replace(",", "")
    if "." in n:
        n = n.rstrip("0").rstrip(".")          # 55.0 == 55, 2.50 == 2.5
    return n.lstrip("0") or "0"


def num_tokens(s: str) -> set[str]:
    return {_norm_num(n) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", s or "")}


def banned_hits(text: str, banned: list[str]) -> list[str]:
    hits = []
    for b in banned:
        if b.startswith("re:"):
            try:
                if re.search(b[3:], text):
                    hits.append(b)
            except re.error:
                continue
        elif b in text:
            hits.append(b)
    return hits


def outlet_key(o: str) -> str:
    return re.sub(r"\s+", "", (o or "").lower())


# ---------------------------------------------------------------- fact
def check_fact(fs: dict, ck: dict) -> dict:
    errs, warns = [], []
    if not isinstance(fs, dict) or not isinstance(fs.get("claims"), list):
        return {"ok": False, "errors": ["factsheet.json 형식 오류: claims 배열 없음"], "warnings": []}
    for k in ("topic", "category", "collected_at"):
        if not fs.get(k):
            errs.append(f"{k} 가 비었습니다")
    ids = set()
    min_fact = int(get(ck, "fact.min_sources", 3))
    min_num = int(get(ck, "number.min_sources", 2))
    all_outlets = set()
    for c in fs["claims"]:
        cid = c.get("id", "?")
        if not re.fullmatch(r"c\d+", str(cid)):
            errs.append(f"{cid}: id 는 c+번호")
        if cid in ids:
            errs.append(f"{cid}: id 중복")
        ids.add(cid)
        if not ws(c.get("claim")):
            errs.append(f"{cid}: claim 비었음")
        t = c.get("type")
        if t not in CLAIM_TYPES:
            errs.append(f"{cid}: type 은 fact|quote|number|allegation")
        if not isinstance(c.get("verified"), bool):
            errs.append(f"{cid}: verified 는 true/false")
        srcs = c.get("sources") or []
        if not srcs:
            errs.append(f"{cid}: sources 1개 이상 필요")
        for s in srcs:
            if not (s.get("url") and s.get("outlet") and DATE_RE.match(str(s.get("published", "")))):
                errs.append(f"{cid}: source 는 url·outlet·published(YYYY-MM-DD) 필수 ({s.get('outlet')})")
            all_outlets.add(outlet_key(s.get("outlet")))
        n_out = len({outlet_key(s.get("outlet")) for s in srcs})
        if t == "quote":
            for k in ("speaker", "place", "said_at"):
                if not c.get(k):
                    errs.append(f"{cid}: quote 는 {k} 필수")
        if t == "allegation" and c.get("verified"):
            errs.append(f"{cid}: allegation 은 verified=false 여야 함")
        if t == "number" and c.get("verified") and n_out < min_num:
            errs.append(f"{cid}: 숫자 확정에는 서로 다른 출처 {min_num}개 이상 필요 (현재 {n_out})")
        if t == "fact" and c.get("verified") and n_out < min_fact:
            errs.append(f"{cid}: fact 확정에는 독립 출처 {min_fact}개 이상 필요 (현재 {n_out})")
        if t == "quote" and c.get("verified") and not c.get("quote") and '"' not in c.get("claim", "") \
                and "“" not in c.get("claim", ""):
            warns.append(f"{cid}: quote 원문을 quote 필드(또는 claim 안 따옴표)로 남기면 훅 인용 검사가 정확해집니다")
    verified = [c for c in fs["claims"] if c.get("verified") and c.get("type") != "allegation"]
    if len(all_outlets - {""}) < min_fact:
        errs.append(f"사안 전체 독립 출처 {len(all_outlets - {''})}개 < {min_fact}개")
    if len(verified) < int(get(ck, "fact.min_verified_claims", 3)):
        errs.append(f"근거 부족: 확정 항목 {len(verified)}개 < {get(ck, 'fact.min_verified_claims', 3)}개 → 훅을 만들지 않음")
    return {"ok": not errs, "errors": errs, "warnings": warns, "verified": len(verified),
            "unverified": len(fs["claims"]) - len(verified), "outlets": len(all_outlets - {""})}


# ---------------------------------------------------------------- hook
def source_titles(fs: dict, meta: dict) -> list[str]:
    titles = []
    for c in fs.get("claims", []):
        for s in c.get("sources", []):
            if s.get("title"):
                titles.append(s["title"])
            for ex in (s.get("excerpts") or ([s["excerpt"]] if s.get("excerpt") else [])):
                titles.append(ex)
    for a in ((meta.get("cluster") or {}).get("articles") or []):
        if a.get("title"):
            titles.append(a["title"])
    if (meta.get("cluster") or {}).get("title"):
        titles.append(meta["cluster"]["title"])
    return list(dict.fromkeys(titles))


def quote_texts(c: dict) -> list[str]:
    out = [ws(c.get("quote", ""))] if c.get("quote") else []
    out += [ws(next(g for g in m.groups() if g)) for m in QUOTE_RE.finditer(c.get("claim", ""))]
    out.append(ws(c.get("claim", "")))
    return [q for q in out if q]


def check_version(name: str, h: dict, fs: dict, ck: dict, banned: list[str], titles: list[str]) -> dict:
    errs: list[dict] = []
    warns: list[dict] = []
    claims = {c["id"]: c for c in fs.get("claims", []) if "id" in c}
    maxc = get(ck, "hook.max_chars", {}) or {}
    plag = int(get(ck, "plagiarism.max_consecutive_eojeol", 4))
    max_punct = int(get(ck, "hook.max_punct_per_version", 1))
    allow_emoji = bool(get(ck, "hook.allow_emoji", False))
    basis = h.get("basis") or {}
    title_toks = [(t, eojeols(t)) for t in titles]

    def e(line, code, msg):
        errs.append({"line": line, "code": code, "msg": msg})

    for ln in ("l1", "l2", "l3"):
        text = h.get(ln)
        if not isinstance(text, str) or not text.strip():
            e(ln, "empty", f"{ln} 이 비었습니다")
            continue
        if text != text.strip() or "\n" in text:
            e(ln, "format", f"{ln}: 앞뒤 공백·줄바꿈 금지")
        lim = maxc.get(ln)
        if lim is not None and len(text) > int(lim):
            e(ln, "length", f"{ln}: {len(text)}자 > {lim}자")
        for b in banned_hits(text, banned):
            e(ln, "banned", f"{ln}: 금지 표현 '{b}'")
        if not allow_emoji and EMOJI_RE.search(text):
            e(ln, "emoji", f"{ln}: 이모지 금지")
        ids = basis.get(ln) or []
        if not ids:
            e(ln, "basis", f"{ln}: 근거 claim id 가 없습니다")
        used = []
        for cid in ids:
            c = claims.get(cid)
            if not c:
                e(ln, "basis", f"{ln}: 팩트 시트에 없는 claim {cid}")
            elif c.get("type") == "allegation":
                e(ln, "basis", f"{ln}: {cid} 는 allegation(미확정 주장) — 훅에 쓰지 않음")
            elif not c.get("verified"):
                e(ln, "basis", f"{ln}: {cid} 는 미확정 항목")
            else:
                used.append(c)
        if used:
            basis_nums = set().union(*(num_tokens(c.get("claim", "") + " " + c.get("note", "") + " " + c.get("quote", ""))
                                       for c in used))
            for n in num_tokens(text):
                if n not in basis_nums and n.lstrip("0") not in {x.lstrip("0") for x in basis_nums}:
                    e(ln, "number", f"{ln}: 숫자 '{n}' 이 근거 claim({', '.join(c['id'] for c in used)})에 없습니다")
        for m in QUOTE_RE.finditer(text):
            q = ws(next(g for g in m.groups() if g))
            src = [c for c in claims.values() if c.get("type") == "quote" and c.get("verified")
                   and any(q in t for t in quote_texts(c))]
            if not src:
                e(ln, "quote", f"{ln}: 따옴표 문구 \"{q}\" 가 팩트 시트 quote 원문에 없습니다 (요약·변형이면 따옴표를 빼세요)")
            elif not any(c["id"] in ids for c in src):
                e(ln, "quote", f"{ln}: 인용 \"{q}\" 의 근거 {src[0]['id']} 를 basis 에 넣으세요")
        toks = eojeols(text)
        for t, tt in title_toks:
            n, run = longest_run(toks, tt)
            if n >= plag:
                e(ln, "plagiarism", f"{ln}: 출처 제목과 {n}어절 연속 일치 '{' '.join(run)}' — 「{t[:40]}」")
                break
    full = " ".join(h.get(k, "") for k in ("l1", "l2", "l3") if isinstance(h.get(k), str))
    ftoks = eojeols(full)
    if not any(x["code"] == "plagiarism" for x in errs):
        for t, tt in title_toks:
            n, run = longest_run(ftoks, tt)
            if n >= plag:
                e("all", "plagiarism", f"세 줄 이어서 출처 제목과 {n}어절 연속 일치 '{' '.join(run)}' — 「{t[:40]}」")
                break
    pc = len(PUNCT_RE.findall(full))
    if pc > max_punct:
        e("all", "punct", f"물음표·느낌표 {pc}개 > {max_punct}개")
    for b in banned_hits(full, banned):
        if not any(x["code"] == "banned" and b in x["msg"] for x in errs):
            e("all", "banned", f"금지 표현 '{b}' (줄을 이어 읽을 때)")
    if not titles:
        warns.append({"line": "all", "code": "plagiarism", "msg": "비교할 출처 제목이 없습니다 (factsheet sources[].title 권장)"})
    return {"version": name, "ok": not errs, "errors": errs, "warnings": warns,
            "lengths": {k: len(h.get(k, "") or "") for k in ("l1", "l2", "l3")}}


def check_hooks(hooks: dict, fs: dict, meta: dict, c: dict, only: str | None = None) -> dict:
    ck = c["checks"]
    versions = list(get(ck, "hook.versions", ["strong", "normal", "soft"]))
    titles = source_titles(fs, meta)
    res = {"ok": True, "versions": {}, "errors": []}
    for v in ([only] if only else versions):
        if v not in hooks:
            res["errors"].append(f"{v} 버전이 없습니다")
            res["ok"] = False
            continue
        r = check_version(v, hooks[v], fs, ck, c["banned_phrases"], titles)
        res["versions"][v] = r
        res["ok"] &= r["ok"]
    if not only and hooks.get("recommended") not in versions:
        res["errors"].append("recommended 는 " + "|".join(versions) + " 중 하나")
        res["ok"] = False
    return res


# ---------------------------------------------------------------- caption
IG_MAX_CHARS = 2200      # 인스타그램 캡션 한도
IG_MAX_TAGS = 30         # 인스타그램 해시태그 한도
HASHTAG_RE = re.compile(r"#[^\s#]+")
DASH_RE = re.compile(r"[—―]")
SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+|\n+")


def caption_parts(text: str) -> dict:
    lines = [l.rstrip() for l in text.replace("\r\n", "\n").split("\n")]
    nonempty = [l for l in lines if l.strip()]
    src = [l for l in nonempty if re.match(r"^\s*출처\s*[:：]", l)]
    photo = [l for l in nonempty if re.match(r"^\s*사진\s*[:：]", l)]
    tag_lines = [l for l in nonempty if all(t.startswith("#") for t in l.split())]
    first = nonempty[0] if nonempty else ""
    body = [l for l in nonempty[1:] if l not in src and l not in photo and l not in tag_lines]
    return {"lines": nonempty, "first": first, "body": body, "source_lines": src, "photo_lines": photo,
            "hashtags": HASHTAG_RE.findall(text), "tag_lines": tag_lines}


def endings(text: str) -> list[str]:
    out = []
    for sent in SENT_SPLIT.split(text):
        s2 = re.sub(r"[\s.!?…~\"'”’)]+$", "", sent.strip())
        if len(s2) >= 2 and re.search(r"[가-힣]$", s2) and not s2.startswith("#"):
            out.append(s2[-2:])
    return out


def fact_numbers(fs: dict, verified_only: bool) -> set[str]:
    nums = set()
    for c in fs.get("claims", []):
        if verified_only and not (c.get("verified") or c.get("type") == "allegation"):
            continue
        nums |= num_tokens(" ".join(str(c.get(k, "")) for k in ("claim", "note", "quote", "said_at")))
    nums |= num_tokens(fs.get("topic", ""))
    return {n.lstrip("0") or "0" for n in nums}


def check_caption(text: str, fs: dict, meta: dict, ck: dict, banned: list[str], user_edit: bool = False) -> dict:
    errs: list[str] = []
    warns: list[str] = []
    soft = warns if user_edit else errs     # 이용자 수정본은 경고만
    parts = caption_parts(text)
    n_chars = len(text)
    max_chars = min(int(get(ck, "caption.max_chars", IG_MAX_CHARS)), IG_MAX_CHARS)
    if n_chars > max_chars:
        errs.append(f"캡션 {n_chars}자 > {max_chars}자")
    tags = parts["hashtags"]
    if len(tags) > IG_MAX_TAGS:
        errs.append(f"해시태그 {len(tags)}개 > 인스타 한도 {IG_MAX_TAGS}개")
    lo, hi = get(ck, "caption.hashtags", [3, 5])
    if not lo <= len(tags) <= hi:
        soft.append(f"해시태그 {len(tags)}개 (기준 {lo}~{hi}개)")
    if len({t.lower() for t in tags}) != len(tags):
        warns.append("중복 해시태그가 있습니다")
    blo, bhi = get(ck, "caption.body_lines", [3, 6])
    if not blo <= len(parts["body"]) <= bhi:
        soft.append(f"본문 {len(parts['body'])}줄 (기준 {blo}~{bhi}줄)")
    n_emoji = len(EMOJI_RE.findall(text))
    if n_emoji > int(get(ck, "caption.max_emoji", 3)):
        soft.append(f"이모지 {n_emoji}개 > {get(ck, 'caption.max_emoji', 3)}개")
    n_dash = len(DASH_RE.findall(text))
    if n_dash > int(get(ck, "caption.max_dashes", 1)):
        soft.append(f"줄표(—) {n_dash}개 > {get(ck, 'caption.max_dashes', 1)}개")
    run_max = int(get(ck, "caption.max_same_ending_run", 2))
    run, prev = 1, None
    for e in endings("\n".join([parts["first"], *parts["body"]])):
        run = run + 1 if e == prev else 1
        prev = e
        if run > run_max:
            soft.append(f"같은 종결어미 '~{e}' 가 {run}번 연속")
            break
    if not parts["source_lines"]:
        soft.append("출처 줄(출처: 매체1, 매체2…)이 없습니다")
    else:
        outlets = [o.strip() for o in re.split(r"[,，·]", re.sub(r"^\s*출처\s*[:：]", "", parts["source_lines"][0])) if o.strip()]
        if len(outlets) > int(get(ck, "caption.max_sources_in_line", 4)):
            soft.append(f"출처 줄 매체 {len(outlets)}개 > {get(ck, 'caption.max_sources_in_line', 4)}개")
        known = {outlet_key(x.get("outlet")) for c in fs.get("claims", []) for x in c.get("sources", [])}
        for o in outlets:
            if outlet_key(o) not in known:
                warns.append(f"출처 줄의 '{o}' 가 팩트 시트 출처에 없습니다")
    photo = meta.get("photo") or {}
    if meta.get("template") == "C" or not photo:
        if parts["photo_lines"]:
            soft.append("사진 없는(C) 게시물인데 사진 출처 줄이 있습니다")
    else:
        want = photo.get("caption_credit") if "caption_credit" in photo else photo.get("credit")
        if not want:
            pass  # 출처 표기 끔(라이선스상 필요 없는 사진) — 사진 출처 줄을 요구하지 않음
        elif not parts["photo_lines"]:
            soft.append(f"사진 출처 줄이 없습니다 → {want}")
        elif want and ws(parts["photo_lines"][0]) != ws(want):
            soft.append(f"사진 출처 줄이 credits.txt 와 다릅니다 → {want}")
    for b in banned_hits(text, banned):
        soft.append(f"금지 표현 '{b}'")
    plag = int(get(ck, "plagiarism.max_consecutive_eojeol", 4))
    titles = [(t, eojeols(t)) for t in source_titles(fs, meta)]
    for line in [parts["first"], *parts["body"]]:
        lt = eojeols(line)
        for t, tt in titles:
            n, run_toks = longest_run(lt, tt)
            if n >= plag:
                soft.append(f"출처 문장과 {n}어절 연속 일치 '{' '.join(run_toks)}' — 「{t[:40]}」")
                break
    allowed = fact_numbers(fs, verified_only=not user_edit)
    scan = "\n".join([parts["first"], *parts["body"]])
    for n in sorted(num_tokens(HASHTAG_RE.sub(" ", scan))):
        if (n.lstrip("0") or "0") not in allowed:
            soft.append(f"팩트 시트{'' if user_edit else ' 확정 항목'}에 없는 숫자 '{n}'")
    return {"ok": not errs, "errors": errs, "warnings": warns, "chars": n_chars, "hashtags": len(tags),
            "body_lines": len(parts["body"]), "user_edit": user_edit}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["fact", "hook", "caption"])
    ap.add_argument("job", type=Path)
    ap.add_argument("--selected", action="store_true", help="meta.json 의 선택 훅(수정본 포함)만 검사")
    ap.add_argument("--only")
    ap.add_argument("--user-edit", action="store_true", help="이용자 수정본: 인스타 한도 외에는 경고만")
    ap.add_argument("--file", type=Path, help="캡션 파일 (기본 job_dir/caption.txt)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    meta = paths.read_json(a.job / "meta.json")
    if not meta:
        print(f"✘ meta.json 없음: {a.job}")
        return 2
    c = compose(cfgmod.load_profile(meta["handle"]), (meta.get("rules") or {}).get("version"))
    fs = paths.read_json(a.job / "factsheet.json")
    if fs is None:
        print("✘ factsheet.json 이 없습니다")
        return 2
    if a.kind == "fact":
        res = check_fact(fs, c["checks"])
        paths.write_json(a.job / "checks_fact.json", res)
        if a.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(f"{'✔' if res['ok'] else '✘'} 팩트 시트: 확정 {res.get('verified', 0)} · 미확정 {res.get('unverified', 0)} · 독립 출처 {res.get('outlets', 0)}")
            for x in res["errors"]:
                print(f"  ✘ {x}")
            for x in res["warnings"]:
                print(f"  ⚠ {x}")
        return 0 if res["ok"] else 1

    if a.kind == "caption":
        f = a.file or (a.job / "caption.txt")
        if not f.is_file():
            print(f"✘ 캡션 파일이 없습니다: {f}")
            return 2
        res = check_caption(f.read_text(encoding="utf-8"), fs, meta, c["checks"], c["banned_phrases"], a.user_edit)
        paths.write_json(a.job / "checks_caption.json", res)
        if a.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(f"{'✔' if res['ok'] else '✘'} 캡션: {res['chars']}자 · 해시태그 {res['hashtags']}개 · 본문 {res['body_lines']}줄"
                  + (" (이용자 수정본: 경고만)" if a.user_edit else ""))
            for x in res["errors"]:
                print(f"  ✘ {x}")
            for x in res["warnings"]:
                print(f"  ⚠ {x}")
        return 0 if res["ok"] else 1

    if a.selected:
        if not meta.get("hook"):
            print("✘ 선택된 훅이 없습니다")
            return 2
        hooks, only = {"selected": meta["hook"]}, "selected"
    else:
        hooks = paths.read_json(a.job / "hooks.json")
        if hooks is None:
            print("✘ hooks.json 이 없습니다")
            return 2
        only = a.only
    res = check_hooks(hooks, fs, meta, c, only)
    paths.write_json(a.job / ("checks_hook_selected.json" if a.selected else "checks_hook.json"), res)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        for v, r in res["versions"].items():
            h = hooks[v]
            print(f"{'✔' if r['ok'] else '✘'} {v}: {h.get('l1')} / {h.get('l2')} / {h.get('l3')}  "
                  f"({r['lengths']['l1']}/{r['lengths']['l2']}/{r['lengths']['l3']}자)")
            for x in r["errors"]:
                print(f"  ✘ {x['msg']}")
            for x in r["warnings"]:
                print(f"  ⚠ {x['msg']}")
        for x in res["errors"]:
            print(f"✘ {x}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
