"""일괄 승인 응답 파서 — workflow 규칙 2절 응답 문법.

라운드별 결과 형식 (모든 함수 공통)
    {"items": {label: {...}}, "hold": [label...], "all_recommended": bool,
     "unresolved": [label...],          # 응답에 빠진 항목 → 그 항목만 다시 묻는다
     "ambiguous": [{"text", "reason"}], # 해석 못 한 조각 → 추측 실행 금지, 그 조각만 되묻는다
     "summary": [사람이 읽는 해석 문장]}

- 계정은 별칭(alias)으로, 게시물은 별칭+번호(예: 시사1)로 부른다.
- 구분자: 슬래시·줄바꿈(항목 사이), 쉼표(항목 안 값 사이).
- R4 는 '업로드' 라는 낱말이 있어야만 업로드로 해석한다. `전체 추천대로`·`OK`·`네`·`ㅇㅇ` 는 업로드가 아니다.
"""
from __future__ import annotations

import re

HOOK_NAMES = {"강력": "strong", "보통": "normal", "순화": "soft", "strong": "strong", "normal": "normal", "soft": "soft"}
ALL_RECOMMENDED = re.compile(r"^\s*전체\s*추천\s*대로\s*$")
HOLD_WORDS = ("보류",)
OK_WORDS = {"ok", "오케이", "승인", "좋아", "좋아요", "확인"}
NONE_WORDS = {"없음", "없어", "안함", "건너뛰기", "패스"}
REDO_TARGETS = [("캡션", "caption"), ("훅", "hooks"), ("문구", "hooks"), ("사진", "photo"), ("카드", "card"),
                ("디자인", "card"), ("템플릿", "card"), ("릴스", "reel"), ("영상", "reel")]


def _empty() -> dict:
    return {"items": {}, "hold": [], "all_recommended": False, "unresolved": [], "ambiguous": [], "summary": []}


def split_items(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"[/\n]+", text or "") if p.strip()]


def split_label(chunk: str, labels: list[str]) -> tuple[str | None, str]:
    """'시사1: 강력, 사진2' / '시사1 OK' / '시사: 1,3' → (label, 나머지). 긴 라벨부터 맞춘다."""
    c = chunk.strip()
    for lb in sorted(labels, key=len, reverse=True):
        m = re.match(rf"^{re.escape(lb)}(?![0-9])\s*[:：]?\s*(.*)$", c, re.S)
        if m:
            return lb, m.group(1).strip()
    return None, c


def _finish(res: dict, expected: list[str]) -> dict:
    answered = set(res["items"]) | set(res["hold"])
    if not res["all_recommended"]:
        res["unresolved"] = [lb for lb in expected if lb not in answered]
    return res


# ---------------------------------------------------------------- R1 이슈 선택
def parse_r1(text: str, aliases: list[str], max_by_alias: dict[str, int] | None = None) -> dict:
    """'시사: 1,3,5 / 연애: 2,4' · '시사: 없음' · '보류' · '전체 추천대로'"""
    res = _empty()
    t = (text or "").strip()
    if ALL_RECOMMENDED.match(t):
        res["all_recommended"] = True
        res["summary"].append("모든 계정: 추천안(★) 그대로")
        return res
    if t in HOLD_WORDS:
        res["hold"] = list(aliases)
        res["summary"].append("모든 계정 이번 회차 보류")
        return res
    for chunk in split_items(t):
        lb, rest = split_label(chunk, aliases)
        if lb is None:
            res["ambiguous"].append({"text": chunk, "reason": "계정 별칭을 찾을 수 없음"})
            continue
        r = rest.replace(" ", "")
        if r in NONE_WORDS:
            res["items"][lb] = {"picks": []}
            res["summary"].append(f"{lb}: 이번 회차 건너뜀")
            continue
        if r in HOLD_WORDS:
            res["hold"].append(lb)
            res["summary"].append(f"{lb}: 보류")
            continue
        if r in ("추천대로", "추천"):
            res["items"][lb] = {"picks": "recommended"}
            res["summary"].append(f"{lb}: 추천안 그대로")
            continue
        nums = [x for x in re.split(r"[,，\s]+", rest) if x]
        if not nums or not all(re.fullmatch(r"\d{1,2}", x) for x in nums):
            res["ambiguous"].append({"text": chunk, "reason": "번호(쉼표 구분)·'없음'·'보류' 중 하나가 아님"})
            continue
        picks = list(dict.fromkeys(int(x) for x in nums))
        mx = (max_by_alias or {}).get(lb)
        if mx and any(p < 1 or p > mx for p in picks):
            res["ambiguous"].append({"text": chunk, "reason": f"번호 범위 밖 (1~{mx})"})
            continue
        res["items"][lb] = {"picks": picks}
        res["summary"].append(f"{lb}: {', '.join(map(str, picks))}번 이슈")
    return _finish(res, aliases)


# ---------------------------------------------------------------- R2 훅·사진
def parse_r2(text: str, labels: list[str], photo_counts: dict[str, int] | None = None) -> dict:
    """'시사1: 강력, 사진2 / 연애2: 순화, C / 시사3: 보통, l1=…'"""
    res = _empty()
    t = (text or "").strip()
    if ALL_RECOMMENDED.match(t):
        res["all_recommended"] = True
        res["summary"].append("모든 게시물: 추천 훅·추천 사진 그대로")
        return res
    if t in HOLD_WORDS:
        res["hold"] = list(labels)
        return res
    for chunk in split_items(t):
        lb, rest = split_label(chunk, labels)
        if lb is None:
            res["ambiguous"].append({"text": chunk, "reason": "게시물 이름(별칭+번호)을 찾을 수 없음"})
            continue
        if rest.replace(" ", "") in HOLD_WORDS:
            res["hold"].append(lb)
            res["summary"].append(f"{lb}: 보류")
            continue
        item: dict = {"hook": None, "photo": None, "edits": {}}
        bad = None
        for part in [p.strip() for p in re.split(r"[,，]", rest) if p.strip()]:
            m = re.match(r"^(l[123])\s*=\s*(.+)$", part, re.I)
            low = part.lower().replace(" ", "")
            if m:
                item["edits"][m.group(1).lower()] = m.group(2).strip()
            elif low in HOOK_NAMES:
                if item["hook"] and item["hook"] != HOOK_NAMES[low]:
                    bad = "훅 버전이 둘 이상"
                item["hook"] = HOOK_NAMES[low]
            elif re.fullmatch(r"(사진)?\d{1,2}", low):
                n = int(re.sub(r"\D", "", low))
                mx = (photo_counts or {}).get(lb)
                if mx is not None and not 1 <= n <= mx:
                    bad = f"사진 번호 범위 밖 (1~{mx})"
                item["photo"] = n
            elif low in ("c", "c템플릿", "사진없음", "사진없이"):
                item["photo"] = "C"
            elif low in ("추천", "추천대로"):
                item["hook"] = item["hook"] or "recommended"
                item["photo"] = item["photo"] or "recommended"
            else:
                bad = f"해석할 수 없는 값 '{part}'"
        if bad:
            res["ambiguous"].append({"text": chunk, "reason": bad})
            continue
        if item["hook"] is None and not item["edits"]:
            res["ambiguous"].append({"text": chunk, "reason": "훅 버전(강력/보통/순화)이 없음"})
            continue
        if item["photo"] is None:
            res["ambiguous"].append({"text": chunk, "reason": "사진 번호 또는 C 가 없음"})
            continue
        if item["edits"] and item["hook"] is None:
            res["ambiguous"].append({"text": chunk, "reason": "어느 훅 버전에서 줄을 바꿀지 없음"})
            continue
        res["items"][lb] = item
        res["summary"].append(f"{lb}: 훅 {item['hook']}" + (f" (수정 {', '.join(item['edits'])})" if item["edits"] else "")
                              + f", 사진 {item['photo']}")
    return _finish(res, labels)


# ---------------------------------------------------------------- R3 결과물
def parse_r3(text: str, labels: list[str]) -> dict:
    """'OK' · '시사1: OK / 연애2: 캡션 다시' · '시사1 OK / 연애2 캡션 둘째 줄 삭제'"""
    res = _empty()
    t = (text or "").strip()
    if t.lower().replace(" ", "") in OK_WORDS or ALL_RECOMMENDED.match(t):
        for lb in labels:
            res["items"][lb] = {"action": "approve"}
        res["summary"].append("모든 게시물 승인")
        return res
    if t in HOLD_WORDS:
        res["hold"] = list(labels)
        return res
    for chunk in split_items(t):
        lb, rest = split_label(chunk, labels)
        if lb is None:
            res["ambiguous"].append({"text": chunk, "reason": "게시물 이름(별칭+번호)을 찾을 수 없음"})
            continue
        r = rest.strip()
        low = r.lower().replace(" ", "")
        if low in OK_WORDS:
            res["items"][lb] = {"action": "approve"}
            res["summary"].append(f"{lb}: 승인")
        elif low in HOLD_WORDS:
            res["hold"].append(lb)
            res["summary"].append(f"{lb}: 보류")
        else:
            targets = [code for word, code in REDO_TARGETS if word in r]
            if not targets:
                res["ambiguous"].append({"text": chunk, "reason": "무엇을 고칠지(캡션·훅·사진·카드·릴스) 알 수 없음"})
                continue
            res["items"][lb] = {"action": "redo", "targets": list(dict.fromkeys(targets)), "request": r}
            res["summary"].append(f"{lb}: 재작업 {'/'.join(dict.fromkeys(targets))} — {r}")
    return _finish(res, labels)


# ---------------------------------------------------------------- R4 업로드
UPLOAD_WORD = re.compile(r"(?<![가-힣A-Za-z])업로드(?:해|하자|해줘|해 줘|해주세요|진행|시작)?(?![가-힣]*(취소|말고|하지|금지|안))")
NEGATIVE = re.compile(r"업로드\s*(하지\s*마|취소|말고|금지|안\s*해|보류)|(하지\s*마|말고)\s*업로드\s*$")


def has_upload_word(text: str) -> bool:
    t = text or ""
    return bool(UPLOAD_WORD.search(t)) and not NEGATIVE.search(t)


def parse_r4(text: str, labels: list[str], eligible: list[str] | None = None) -> dict:
    """'업로드' (표의 전부) · '업로드: 시사1, 연애2' (지정만) · '연애2 보류하고 업로드' (지정 제외 전부)
    '업로드' 낱말이 없으면 어떤 응답도 업로드 승인이 아니다(추천 자동 수락 없음)."""
    res = _empty()
    res["approved"] = []
    eligible = list(eligible if eligible is not None else labels)
    t = (text or "").strip()
    if not has_upload_word(t):
        res["ambiguous"].append({"text": t, "reason": "'업로드' 라는 말이 없어 업로드하지 않음 (R4 는 자동 수락 없음)"})
        res["summary"].append("업로드 승인 아님")
        return res
    m = re.match(r"^\s*업로드\s*[:：]\s*(.+)$", t, re.S)
    holds = []
    for lb in labels:
        if re.search(rf"{re.escape(lb)}(?![0-9])\s*(은|는|만|을|를)?\s*보류", t):
            holds.append(lb)
    if m:
        picked, unknown = [], []
        for part in [p.strip() for p in re.split(r"[,，/\n]", m.group(1)) if p.strip()]:
            lb, rest = split_label(part, labels)
            if lb is None or rest:
                unknown.append(part)
            else:
                picked.append(lb)
        if unknown:
            res["ambiguous"].append({"text": ", ".join(unknown), "reason": "게시물 이름을 찾을 수 없음"})
            return res
        approved = [lb for lb in picked if lb not in holds]
    else:
        rest = re.sub(r"(?<![가-힣A-Za-z])업로드\S*", " ", t)
        for lb in holds:
            rest = re.sub(rf"{re.escape(lb)}(?![0-9])\s*(은|는|만|을|를)?\s*보류\S*", " ", rest)
        rest = re.sub(r"[\s,，./]+|(하고|그리고|나머지|전부|모두|다)", "", rest)
        if rest and rest.lower() not in OK_WORDS:
            res["ambiguous"].append({"text": t, "reason": f"해석할 수 없는 부분 '{rest}' — 업로드하지 않고 되묻는다"})
            return res
        approved = [lb for lb in eligible if lb not in holds]
    not_eligible = [lb for lb in approved if lb not in eligible]
    if not_eligible:
        res["ambiguous"].append({"text": t, "reason": f"확인 항목 실패로 업로드 대상이 아닌 게시물: {', '.join(not_eligible)}"})
        approved = [lb for lb in approved if lb in eligible]
    res["approved"] = approved
    res["hold"] = holds
    res["summary"].append(("업로드: " + ", ".join(approved)) if approved else "업로드할 게시물 없음")
    if holds:
        res["summary"].append("보류: " + ", ".join(holds))
    return res


def single_upload_approved(text: str) -> bool:
    """단일 게시물 모드의 업로드 승인 — '업로드' 낱말이 있고 부정이 아니면 승인."""
    r = parse_r4(text, ["_"], ["_"])
    return r["approved"] == ["_"]
