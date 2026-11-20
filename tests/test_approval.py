"""일괄 응답 파서: 정상·부분·애매, R4 명시 승인."""
from __future__ import annotations

import pytest

from common import approval as ap

ALIASES = ["시사", "연애"]
POSTS = ["시사1", "시사2", "시사3", "연애1", "연애2"]


# ---------------------------------------------------------------- R1
def test_r1_normal():
    r = ap.parse_r1("시사: 1,3,5 / 연애: 2,4", ALIASES)
    assert r["items"] == {"시사": {"picks": [1, 3, 5]}, "연애": {"picks": [2, 4]}}
    assert not r["unresolved"] and not r["ambiguous"]


def test_r1_newline_and_none():
    r = ap.parse_r1("시사: 없음\n연애:2", ALIASES)
    assert r["items"]["시사"] == {"picks": []} and r["items"]["연애"] == {"picks": [2]}


def test_r1_partial_answer_reasks_missing():
    r = ap.parse_r1("시사: 1", ALIASES)
    assert r["items"] == {"시사": {"picks": [1]}} and r["unresolved"] == ["연애"]


def test_r1_ambiguous_not_guessed():
    r = ap.parse_r1("시사: 첫번째 거 / 연애: 2", ALIASES)
    assert "시사" not in r["items"] and r["ambiguous"][0]["text"].startswith("시사")
    assert r["items"]["연애"] == {"picks": [2]}
    r = ap.parse_r1("시사: 1, 99", ALIASES, {"시사": 10})
    assert r["ambiguous"] and "범위" in r["ambiguous"][0]["reason"]
    r = ap.parse_r1("경제: 1", ALIASES)
    assert r["ambiguous"] and r["unresolved"] == ALIASES


def test_r1_all_recommended_and_hold():
    assert ap.parse_r1("전체 추천대로", ALIASES)["all_recommended"]
    r = ap.parse_r1("시사: 보류 / 연애: 1", ALIASES)
    assert r["hold"] == ["시사"] and not r["unresolved"]


# ---------------------------------------------------------------- R2
def test_r2_normal():
    r = ap.parse_r2("시사1: 강력, 사진2 / 연애2: 순화, C", ["시사1", "연애2"])
    assert r["items"]["시사1"] == {"hook": "strong", "photo": 2, "edits": {}}
    assert r["items"]["연애2"] == {"hook": "soft", "photo": "C", "edits": {}}
    assert not r["unresolved"] and not r["ambiguous"]


def test_r2_line_edit_keeps_commas_in_text():
    r = ap.parse_r2("시사3: 보통, l1=버스 요금 오른다, 사진1", ["시사3"])
    assert r["items"]["시사3"] == {"hook": "normal", "photo": 1, "edits": {"l1": "버스 요금 오른다"}}


def test_r2_label_prefix_collision():
    """'시사1' 과 '시사10' 이 섞여도 정확히 구분한다."""
    r = ap.parse_r2("시사10: 보통, 사진1 / 시사1: 강력, C", ["시사1", "시사10"])
    assert r["items"]["시사10"]["hook"] == "normal" and r["items"]["시사1"]["hook"] == "strong"


def test_r2_missing_photo_or_hook_is_ambiguous():
    r = ap.parse_r2("시사1: 강력 / 연애2: 사진1", ["시사1", "연애2"])
    assert not r["items"] and len(r["ambiguous"]) == 2


def test_r2_photo_out_of_range():
    r = ap.parse_r2("시사1: 강력, 사진7", ["시사1"], {"시사1": 5})
    assert r["ambiguous"] and "범위" in r["ambiguous"][0]["reason"]


def test_r2_partial():
    r = ap.parse_r2("시사1: 보통, 사진1", POSTS)
    assert list(r["items"]) == ["시사1"] and r["unresolved"] == ["시사2", "시사3", "연애1", "연애2"]


# ---------------------------------------------------------------- R3
def test_r3_all_ok():
    r = ap.parse_r3("OK", ["시사1", "연애2"])
    assert all(v == {"action": "approve"} for v in r["items"].values()) and len(r["items"]) == 2


def test_r3_mixed_with_free_text_edit():
    r = ap.parse_r3("시사1 OK / 연애2 캡션 둘째 줄 삭제", ["시사1", "연애2"])
    assert r["items"]["시사1"] == {"action": "approve"}
    assert r["items"]["연애2"]["action"] == "redo" and r["items"]["연애2"]["targets"] == ["caption"]
    assert r["items"]["연애2"]["request"] == "캡션 둘째 줄 삭제"


def test_r3_ambiguous_request():
    r = ap.parse_r3("시사1: 좀 별로", ["시사1"])
    assert not r["items"] and r["ambiguous"]


# ---------------------------------------------------------------- R4
@pytest.mark.parametrize("text", ["전체 추천대로", "OK", "ㅇㅇ", "네", "좋아요", "올려", "업로드 하지 마", "업로드 취소",
                                  "업로드는 내일"])
def test_r4_never_auto_accepts(text):
    r = ap.parse_r4(text, ["시사1", "연애2"])
    assert r["approved"] == []


def test_r4_upload_all_eligible():
    r = ap.parse_r4("업로드", ["시사1", "연애2", "연애3"], eligible=["시사1", "연애2"])
    assert r["approved"] == ["시사1", "연애2"]


def test_r4_selected_only():
    r = ap.parse_r4("업로드: 시사1, 연애2", ["시사1", "연애2", "연애3"])
    assert r["approved"] == ["시사1", "연애2"]


def test_r4_hold_then_upload():
    r = ap.parse_r4("연애2 보류하고 업로드", ["시사1", "연애2"])
    assert r["approved"] == ["시사1"] and r["hold"] == ["연애2"]


def test_r4_unknown_label_not_uploaded():
    r = ap.parse_r4("업로드: 시사9", ["시사1"])
    assert r["approved"] == [] and r["ambiguous"]


def test_r4_ineligible_excluded():
    r = ap.parse_r4("업로드: 시사1, 연애2", ["시사1", "연애2"], eligible=["시사1"])
    assert r["approved"] == ["시사1"] and r["ambiguous"]


def test_single_mode():
    assert ap.single_upload_approved("업로드")
    assert ap.single_upload_approved("좋아, 업로드해줘")
    assert not ap.single_upload_approved("네")
    assert not ap.single_upload_approved("업로드 하지 마")
