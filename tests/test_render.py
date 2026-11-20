"""템플릿 세이프존·자동 검수."""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

import render_card

TM = {
    "safe_zones": {"reel": {"top": 220, "bottom": 400, "right_bottom": {"width": 150, "y_from": 1000, "y_to": 1520}},
                   "feed": {"margin": 60}},
    "grid_crop": {"reel": {"y_from": 240, "y_to": 1680}},
}
IMG = np.zeros((1920, 1080, 3), np.uint8)


def slot(box, text="가나다", lines=None, color="rgb(255, 255, 255)", size=80):
    return {"text": text, "box": box, "lines": lines or [{"text": text, "box": box}], "color": color,
            "fontSize": size, "overflow": False}


def ev(slots, blocks=None):
    data = {"canvas": [1080, 1920], "slots": slots, "blocks": blocks or {}}
    return {(i["target"], i["code"]) for i in render_card.evaluate(data, TM, "reel", IMG, {})}


def test_inside_safe_area_passes():
    assert ev({"l1": slot([72, 1100, 800, 1200]), "l2": slot([72, 1220, 600, 1300])}) == set()


def test_top_zone():
    assert ("l1", "safe_top") in ev({"l1": slot([72, 150, 800, 260])})


def test_bottom_zone_and_grid():
    r = ev({"l3": slot([72, 1500, 700, 1700])})
    assert ("l3", "safe_bottom") in r and ("l3", "grid") in r


def test_right_button_column():
    assert ("l2", "safe_right") in ev({"l2": slot([500, 1200, 960, 1280])})
    assert ev({"l2": slot([500, 600, 960, 680])}) == set()        # 버튼 열 높이 밖이면 괜찮음


def test_overlap_text_and_tag():
    r = ev({}, {".text": [72, 300, 880, 1480], ".tag": [72, 250, 300, 300]})
    assert ("text", "overlap") in r


def test_widow_and_mid_word():
    lines = [{"text": "한국은행 기준금리 ", "box": [72, 1000, 800, 1090]}, {"text": "동결", "box": [72, 1090, 200, 1180]}]
    assert ("l1", "widow") in ev({"l1": slot([72, 1000, 800, 1180], "한국은행 기준금리 동결", lines)})
    lines = [{"text": "한국은행 기준금", "box": [72, 1000, 800, 1090]}, {"text": "리 동결합니다", "box": [72, 1090, 600, 1180]}]
    assert ("l1", "mid_word") in ev({"l1": slot([72, 1000, 800, 1180], "한국은행 기준금리 동결합니다", lines)})


def test_too_many_lines():
    lines = [{"text": f"줄{i} ", "box": [72, 900 + i * 90, 600, 990 + i * 90]} for i in range(3)]
    assert ("l1", "too_many_lines") in ev({"l1": slot([72, 900, 600, 1170], "줄0 줄1 줄2 ", lines)})


def test_contrast_uses_background_render():
    fg = np.full((1920, 1080, 3), 230, np.uint8)       # 밝은 배경
    bg = fg.copy()
    fg[1010:1060, 100:700] = 255                        # 흰 글자
    assert render_card.contrast_of(fg, bg, [72, 1000, 800, 1080], "rgb(255, 255, 255)") < 1.5
    bg2 = np.full((1920, 1080, 3), 20, np.uint8)
    fg2 = bg2.copy()
    fg2[1010:1060, 100:700] = 255
    assert render_card.contrast_of(fg2, bg2, [72, 1000, 800, 1080], "rgb(255, 255, 255)") > 10


# ---------------------------------------------------------------- 실제 템플릿 (서명된 규칙 번들)
@pytest.fixture(scope="module")
def real_rules(tmp_path_factory):
    import os

    import rules_sync
    from common import config

    h = tmp_path_factory.mktemp("realhome")
    old = os.environ.get("INSTA_AUTOPILOT_HOME")
    os.environ["INSTA_AUTOPILOT_HOME"] = str(h)
    try:
        res = rules_sync.sync("stable")
        if res["status"] == "error":
            pytest.skip("규칙 서버에 접속할 수 없어 실제 템플릿 테스트를 건너뜀")
        config.save_profile({"handle": "render_test", "display_name": "렌더테스트", "alias": "렌더",
                             "main_category": "economy"})
        yield h
    finally:
        if old is None:
            os.environ.pop("INSTA_AUTOPILOT_HOME", None)
        else:
            os.environ["INSTA_AUTOPILOT_HOME"] = old


@pytest.mark.parametrize("variant", ["A", "B", "C"])
def test_real_template_long_text_is_fixed_or_reported(real_rules, tmp_path, variant):
    pytest.importorskip("playwright")
    from common import config
    from common.rules import compose
    from setup_account import sample_photo

    profile = config.load_profile("render_test")
    c = compose(profile)
    hook = {"l1": "가나다라마바사 아자차카타파하 가나", "l2": "연 2.50% 석 달째 그대로 유지돼", "l3": "다음 결정은 내년 1월 15일 금통위에서"}
    photo = None if variant == "C" else str(sample_photo(tmp_path / "p.jpg"))
    rep = asyncio.run(render_card.render_cards(c, profile, hook, variant, tmp_path, photo, "사진: 테스트"))
    r = rep["results"]["reel"]
    assert (tmp_path / "card.png").is_file() and (tmp_path / "card_guide.png").is_file()
    assert (tmp_path / "card_grid.png").is_file()
    # 세이프존·그리드·겹침 문제는 자동 보정으로 해결돼야 한다 (대비 등은 보고로 남을 수 있음)
    layout = [i for i in r["issues"] if i["code"] in ("safe_top", "safe_bottom", "safe_right", "grid", "overlap",
                                                       "overflow", "too_many_lines")]
    assert not layout, r["issues"]
    from PIL import Image

    assert Image.open(tmp_path / "card.png").size == (1080, 1920)


def test_grid_edge_margin():
    tm = {**TM, "grid_crop": {"reel": {"y_from": 240, "y_to": 1680, "min_margin": 32}}}
    data = {"canvas": [1080, 1920], "slots": {"chip": slot([72, 250, 200, 300], "시사")}, "blocks": {}}
    r = {(i["target"], i["code"]) for i in render_card.evaluate(data, tm, "reel", IMG, {})}
    assert ("chip", "grid_edge") in r
    data["slots"]["chip"] = slot([72, 280, 200, 330], "시사")
    assert not render_card.evaluate(data, tm, "reel", IMG, {})
    data["slots"] = {"l1": slot([72, 1100, 800, 1200])}
    assert not render_card.evaluate(data, TM, "reel", IMG, {})      # min_margin 없으면 검사 안 함


def test_badge_wrap_detected_and_fixed():
    lines = [{"text": "오늘이", "box": [72, 1450, 120, 1470]}, {"text": "슈", "box": [72, 1470, 90, 1490]}]
    data = {"canvas": [1080, 1920], "slots": {"logo": slot([72, 1440, 120, 1488], "오늘이슈", lines)}, "blocks": {}}
    issues = render_card.evaluate(data, TM, "reel", IMG, {})
    assert ("logo", "badge_wrap") in {(i["target"], i["code"]) for i in issues}
    adj = render_card.Adjust()
    assert adj.plan(issues, data, "A") and "white-space:nowrap" in adj.css()
