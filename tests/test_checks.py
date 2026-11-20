"""훅·팩트 시트 검사 로직."""
from __future__ import annotations

import copy

import pytest

import checks

CK = {
    "hook": {"max_chars": {"l1": 18, "l2": 20, "l3": 22}, "versions": ["strong", "normal", "soft"],
             "max_punct_per_version": 1, "allow_emoji": False},
    "plagiarism": {"max_consecutive_eojeol": 4},
    "fact": {"min_sources": 3, "min_verified_claims": 3},
    "number": {"min_sources": 2},
}
BANNED = ["충격", "re:(파렴치|철면피)", "알아볼까요"]


def src(outlet, title="", d="2026-11-18"):
    return {"url": f"https://{outlet}.example/a", "outlet": outlet, "published": d, "title": title}


FS = {
    "topic": "A시 버스 요금", "category": "politics", "collected_at": "2026-11-19T08:00:00+09:00",
    "claims": [
        {"id": "c1", "type": "number", "verified": True,
         "claim": "A시 시내버스 기본요금이 12월 1일부터 1,500원에서 1,800원으로 오른다",
         "sources": [src("A시청", "A시 시내버스 요금 12월부터 1800원으로 인상 확정"), src("연합뉴스", "A시 버스요금 300원 오른다")]},
        {"id": "c2", "type": "fact", "verified": True, "claim": "A시의회 일부 의원이 재논의를 요구했다",
         "sources": [src("연합뉴스"), src("한겨레"), src("KBS")]},
        {"id": "c3", "type": "quote", "verified": True, "claim": "김의원은 \"요금 인상을 재논의하라\"고 말했다",
         "quote": "요금 인상을 재논의하라", "speaker": "김의원", "place": "A시의회 본회의", "said_at": "2026-11-18",
         "sources": [src("A시의회 속기록")]},
        {"id": "c4", "type": "allegation", "verified": False, "claim": "시민단체는 절차 위반이라고 주장했다",
         "sources": [src("한겨레")]},
        {"id": "c5", "type": "fact", "verified": False, "claim": "다른 시도 인상을 검토 중이다", "sources": [src("뉴스1")]},
    ],
}


def good():
    return {
        "strong": {"l1": "A시 버스요금 300원 인상", "l2": "\"요금 인상을 재논의하라\"", "l3": "12월 1일부터 1,800원",
                   "basis": {"l1": ["c1"], "l2": ["c3"], "l3": ["c1"]}},
        "normal": {"l1": "A시 버스 기본요금 인상", "l2": "1,500원 → 1,800원", "l3": "시의회 일부 재논의 요구",
                   "basis": {"l1": ["c1"], "l2": ["c1"], "l3": ["c2"]}},
        "soft": {"l1": "A시 버스 요금, 12월 조정", "l2": "기본요금 1,800원 적용", "l3": "시의회서 재논의 의견도",
                 "basis": {"l1": ["c1"], "l2": ["c1"], "l3": ["c2"]}},
        "recommended": "normal",
    }


def run(hooks, fs=FS, meta=None):
    c = {"checks": CK, "banned_phrases": BANNED}
    return checks.check_hooks(hooks, fs, meta or {}, c)


def codes(res, v):
    return {e["code"] for e in res["versions"][v]["errors"]}


def test_good_hooks_pass():
    # "300원" 은 c1 의 1,500→1,800 에서 계산된 값이라 근거 claim 에 숫자가 없음 → number 오류가 나야 정상
    res = run(good())
    assert codes(res, "strong") == {"number"}
    h = good()
    h["strong"]["l1"] = "A시 버스요금 인상 확정"
    res = run(h)
    assert res["ok"], res


def test_length_limit():
    h = good()
    h["normal"]["l1"] = "가" * 19
    assert "length" in codes(run(h), "normal")


def test_banned_substring_and_regex():
    h = good()
    h["normal"]["l3"] = "충격 재논의 요구"
    h["soft"]["l3"] = "철면피 의원들"
    res = run(h)
    assert "banned" in codes(res, "normal") and "banned" in codes(res, "soft")


def test_plagiarism_consecutive_eojeol():
    h = good()
    h["normal"]["l1"] = "A시 버스요금 300원 오른다"          # 출처 제목과 4어절 연속 일치
    h["normal"]["basis"]["l1"] = ["c1"]
    res = run(h)
    assert "plagiarism" in codes(res, "normal")
    h["normal"]["l1"] = "A시 버스요금 오른다"               # 3어절 → 통과(연속 아님)
    assert "plagiarism" not in codes(run(h), "normal")


def test_plagiarism_against_cluster_titles():
    h = good()
    h["soft"]["l3"] = "시의회서 재논의 의견도 나와"
    meta = {"cluster": {"articles": [{"title": "속보 시의회서 재논의 의견도 나와 논란"}]}}
    assert "plagiarism" in codes(run(h, meta=meta), "soft")


def test_quote_must_exist_in_factsheet():
    h = good()
    h["strong"]["l2"] = "\"즉시 철회하라\""
    assert "quote" in codes(run(h), "strong")
    h["strong"]["l2"] = "“요금 인상을 재논의하라”"           # 둥근 따옴표도 인식, 원문 일치 → 통과
    assert "quote" not in codes(run(h), "strong")
    h["strong"]["basis"]["l2"] = ["c1"]                   # 원문은 있지만 근거에 quote claim 없음
    assert "quote" in codes(run(h), "strong")


def test_basis_rules():
    h = good()
    h["normal"]["basis"]["l3"] = []
    assert "basis" in codes(run(h), "normal")
    h = good()
    h["normal"]["basis"]["l3"] = ["c9"]
    assert "basis" in codes(run(h), "normal")
    h = good()
    h["normal"]["basis"]["l3"] = ["c4"]                    # allegation
    assert "basis" in codes(run(h), "normal")
    h = good()
    h["normal"]["basis"]["l3"] = ["c5"]                    # 미확정
    assert "basis" in codes(run(h), "normal")


def test_numbers_must_be_in_basis():
    h = good()
    h["normal"]["l2"] = "1,500원 → 2,000원"
    assert "number" in codes(run(h), "normal")


def test_punct_and_emoji():
    h = good()
    h["soft"]["l1"] = "A시 버스 요금 오른다?"
    h["soft"]["l3"] = "시의회서 재논의 의견도!"
    assert "punct" in codes(run(h), "soft")
    h = good()
    h["soft"]["l3"] = "시의회서 재논의 의견도 🚌"
    assert "emoji" in codes(run(h), "soft")


def test_missing_version_and_recommended():
    h = good()
    del h["soft"]
    h["recommended"] = "best"
    res = run(h)
    assert not res["ok"] and any("soft" in e for e in res["errors"]) and any("recommended" in e for e in res["errors"])


# ---------------------------------------------------------------- fact
def test_fact_ok():
    r = checks.check_fact(FS, CK)
    assert r["ok"], r
    assert r["verified"] == 3


def test_fact_number_needs_two_sources():
    fs = copy.deepcopy(FS)
    fs["claims"][0]["sources"] = fs["claims"][0]["sources"][:1]
    r = checks.check_fact(fs, CK)
    assert any("c1" in e and "숫자" in e for e in r["errors"])


def test_fact_needs_three_independent_sources():
    fs = copy.deepcopy(FS)
    fs["claims"][1]["sources"] = [src("연합뉴스"), src("연합뉴스")]
    r = checks.check_fact(fs, CK)
    assert any("c2" in e for e in r["errors"])


def test_quote_fields_and_allegation():
    fs = copy.deepcopy(FS)
    del fs["claims"][2]["speaker"]
    fs["claims"][3]["verified"] = True
    r = checks.check_fact(fs, CK)
    assert any("speaker" in e for e in r["errors"]) and any("allegation" in e for e in r["errors"])


def test_insufficient_verified_claims():
    fs = copy.deepcopy(FS)
    for c in fs["claims"][:2]:
        c["verified"] = False
    r = checks.check_fact(fs, CK)
    assert not r["ok"] and any("근거 부족" in e for e in r["errors"])


@pytest.mark.parametrize("a,b,n", [("가 나 다 라", "x 가 나 다 y", 3), ("가 나", "다 라", 0)])
def test_longest_run(a, b, n):
    assert checks.longest_run(a.split(), b.split())[0] == n


def test_decimal_numbers_are_one_eojeol_and_equal():
    assert checks.eojeols("결혼 자금 부족(29.0%)") == ["결혼", "자금", "부족", "29.0%"]
    assert checks.eojeols("요금 1,800원, 오른다.") == ["요금", "1,800원", "오른다"]
    assert checks.num_tokens("55.0% 2.50% 1,800원 07") == {"55", "2.5", "1800", "7"}
    fs = {"claims": [{"id": "c1", "type": "number", "verified": True, "claim": "응답은 55.0%다", "sources": []}]}
    h = {"l1": "응답 55%", "l2": "가", "l3": "나", "basis": {"l1": ["c1"], "l2": ["c1"], "l3": ["c1"]}}
    r = checks.check_version("x", h, fs, CK, [], [])
    assert not [e for e in r["errors"] if e["code"] == "number"]
