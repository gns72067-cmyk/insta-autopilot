"""데이터 폴더 유지 · 경로 규칙 · 스마트 크롭 계산."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_home_resolution_order(tmp_path, monkeypatch):
    from common import paths

    monkeypatch.setattr(paths, "os_config_dir", lambda: tmp_path / "oscfg")
    monkeypatch.delenv("INSTA_AUTOPILOT_HOME", raising=False)
    assert paths.home() == paths.default_home()
    chosen = paths.set_home(tmp_path / "MyData")
    assert paths.home() == chosen                                   # 포인터 파일
    monkeypatch.setenv("INSTA_AUTOPILOT_HOME", str(tmp_path / "Env"))
    assert paths.home() == (tmp_path / "Env").resolve()            # 환경변수 우선


def test_home_cannot_be_inside_plugin(tmp_path, monkeypatch):
    from common import paths

    monkeypatch.setattr(paths, "os_config_dir", lambda: tmp_path / "oscfg")
    with pytest.raises(ValueError):
        paths.set_home(paths.PLUGIN_ROOT / "data")


def test_user_data_lives_outside_plugin(home):
    """플러그인 폴더를 지우고 다시 설치해도 이용자 데이터는 그대로다."""
    from common import config, paths

    p = config.save_profile({"handle": "@My.Shop", "display_name": "가게", "alias": "가게", "main_category": "alpha"})
    assert p == home / "accounts" / "my.shop" / "profile.yaml"
    assert paths.PLUGIN_ROOT not in p.parents
    for d in ("assets", "assets/character", "assets/fonts", "library"):
        assert (p.parent / d).is_dir()
    assert (p.parent / "style_examples.md").is_file()
    # 다시 저장해도 style_examples.md 는 덮어쓰지 않는다
    (p.parent / "style_examples.md").write_text("내 예시", encoding="utf-8")
    config.save_profile({"handle": "my.shop", "display_name": "가게2", "alias": "가게", "main_category": "alpha"})
    assert (p.parent / "style_examples.md").read_text(encoding="utf-8") == "내 예시"
    assert config.load_profile("my.shop")["display_name"] == "가게2"
    assert config.resolve_account("가게") == "my.shop"


def test_handle_validation():
    from common import paths

    with pytest.raises(ValueError):
        paths.normalize_handle("../etc")
    with pytest.raises(ValueError):
        paths.normalize_handle("한글핸들")
    assert paths.normalize_handle("@Today_News") == "today_news"


def test_job_dirs_are_numbered(home):
    from common import paths

    a = paths.new_job_dir("acc", "기준금리 동결!", "2026-11-19")
    b = paths.new_job_dir("acc", "두 번째/이슈", "2026-11-19")
    assert a.name == "01_기준금리-동결" and b.name == "02_두-번째-이슈"
    assert a.parent == home / "output" / "2026-11-19" / "acc"


def test_asset_path_cannot_escape_account(home):
    from common import config

    config.save_profile({"handle": "acc", "display_name": "A", "alias": "A", "main_category": "alpha"})
    (home / "secret.png").write_bytes(b"x")
    assert config.account_asset_path("acc", "../../secret.png") is None


# ---------------------------------------------------------------- smart crop
def test_plan_crop_places_subject_in_band():
    import smart_crop

    # 4000×3000 사진 아래쪽(y 1500~1900)에 얼굴 → cover 만으로는 못 올리고 확대가 필요
    subj = {"x": 1800, "y": 1500, "w": 400, "h": 400}
    info = smart_crop.plan_crop(4000, 3000, 1080, 1920, subj)
    assert info["subject_in_band"] is True and info["zoom"] > 1.0 and not info["upscaled"]
    top, bot = info["subject_canvas"][1], info["subject_canvas"][3]
    assert 0.15 * 1920 - 40 <= top and bot <= 0.55 * 1920 + 40


def test_plan_crop_with_partial_box():
    """B 패널형처럼 사진 상자가 캔버스 상단 1100px 일 때도 캔버스 기준 대역을 맞춘다."""
    import smart_crop

    subj = {"x": 900, "y": 1500, "w": 300, "h": 300}
    info = smart_crop.plan_crop(2000, 3000, 1080, 1100, subj, box_top=0, canvas_h=1920)
    assert info["subject_in_band"] is True
    top = smart_crop.plan_crop(2000, 3000, 1080, 1100, {"x": 900, "y": 100, "w": 300, "h": 300}, 0, 1920)
    assert top["subject_in_band"] is False      # 사진 맨 위의 피사체는 아래로 내릴 수 없음 → 후보 제외 대상


def test_plan_crop_reports_impossible():
    import smart_crop

    subj = {"x": 0, "y": 2900, "w": 100, "h": 100}      # 맨 아래 → cover 크롭으로는 대역에 못 올림
    info = smart_crop.plan_crop(1080, 3000, 1080, 1920, subj)
    assert info["subject_in_band"] is False


def test_smart_crop_writes_exact_size(tmp_path):
    import cv2
    import numpy as np

    import smart_crop

    img = np.full((900, 1600, 3), 80, np.uint8)
    cv2.circle(img, (1200, 500), 120, (250, 250, 250), -1)
    src = tmp_path / "한글 이름.jpg"
    smart_crop.write_image(src, img)
    out = tmp_path / "out.jpg"
    info = smart_crop.smart_crop(src, out, 1080, 1920)
    res = smart_crop.read_image(out)
    assert res.shape[:2] == (1920, 1080) and info["upscaled"] is True


@pytest.mark.parametrize("v,ok", [("me@example.com", True), ("a.b+c@sub.example.co.kr", True),
                                  ("insta-autopilot e2e test", False), ("https://example.com", False),
                                  ("me@", False), ("", False)])
def test_contact_must_be_email(v, ok):
    import setup_account

    assert setup_account.valid_email(v) is ok


@pytest.mark.parametrize("t,bad", [("Woman with pubic hair and wedding ring.jpg", True), ("Nude couple", True),
                                   ("누드 사진", True), ("Category:Erotic photography", True),
                                   ("Placing a wedding ring.jpg", False), ("Bank of Korea Sussex", False),
                                   ("Essex county hall", False), ("시험장으로 선정된 고등학교", False),
                                   ("성인 남성 마라톤", False), ("선정적 화보", True)])
def test_nsfw_filter(t, bad):
    import image_search

    assert image_search.is_nsfw(t) is bad


def test_photo_relevance_order_and_spread():
    import image_search as im

    qs = ["College Scholastic Ability Test", "exam paper", "Seoul city"]
    found = [{"source": "kogl1", "query": "Seoul city", "title": "Nightview of Gwanghwamun Square", "text": ""},
             {"source": "wikimedia", "query": qs[0], "title": "Scantron pen for College Scholastic Ability Test", "text": ""},
             {"source": "wikimedia", "query": qs[1], "title": "History exam paper", "text": ""},
             {"source": "wikimedia", "query": qs[1], "title": "Totally unrelated cat", "text": ""}]
    out = im.order_candidates(found, qs, per_query=1)
    assert out[0]["title"].startswith("Scantron")                   # 출처 순위(kogl1)보다 관련도가 먼저
    assert all(c["title"] != "Totally unrelated cat" for c in out)    # 검색어와 겹치는 낱말이 없으면 뺀다
    assert im.upscale_of(1024, 768) > 2 and im.upscale_of(3000, 4000) < 1


def test_gov_flickr_creators_excluded():
    import image_search as im

    assert im.GOV_CREATOR_RE.search("KOREA.NET - Official page of the Republic of Korea")
    assert im.GOV_RESTRICT_RE.search("Files from Korea.net Flickr stream|Suneung")
    assert not im.GOV_CREATOR_RE.search("Jane Doe")


def test_article_image_and_non_press_hosts():
    import image_search as im

    html = '<meta property="og:image" content="https://img.yna.co.kr/photo/a.jpg"/><meta name="twitter:image" content="/b.jpg">'
    assert im.article_images(html, "https://www.yna.co.kr/view/1") == ["https://img.yna.co.kr/photo/a.jpg", "https://www.yna.co.kr/b.jpg"]
    assert im.NOT_PRESS_RE.search("www.mafra.go.kr") and not im.NOT_PRESS_RE.search("www.hankyung.com")


def test_press_off_by_default_and_rules_gate(tmp_path):
    import image_search as im

    job = tmp_path / "j"
    job.mkdir()
    (job / "factsheet.json").write_text('{"claims":[{"claim":"c","sources":[{"url":"https://www.yna.co.kr/view/1","outlet":"연합뉴스"}]}]}',
                                        encoding="utf-8")
    assert im.search_press({}, job, True) == ([], [])                      # 꺼져 있으면 아무것도 안 함
    cfg = {"press_photos": {"enabled": True, "confirm_text": "확인했고 책임은 나에게"}}
    r, notes = im.search_press(cfg, job, False)
    assert r == [] and "허용하지 않아" in notes[0]                          # 규칙이 막으면 건너뜀


def test_guarded_get_extra_hosts_only_for_that_call():
    import image_search as im

    with pytest.raises(im.BlockedHost):
        im.guarded_get("https://www.yna.co.kr/view/1")


def test_inset_layout_keeps_whole_photo():
    import numpy as np

    import smart_crop as sc

    img = np.full((413, 620, 3), 200, np.uint8)
    info = sc.plan_inset(620, 413, 1080, 1920, 0, 1920)
    out = sc.apply_inset(img, info, 1080, 1920)
    x, y, w, h = info["fg"]
    assert out.shape == (1920, 1080, 3) and round(w) == 1000 and info["scale"] < 1.7
    assert 0.15 * 1920 - 1 <= y + h / 2 <= 0.55 * 1920 + 1                 # 피사체 대역 가운데


def test_press_toggle_requires_user_confirmation(home):
    import argparse

    import setup_account as sa
    from common import config

    assert sa.cmd_press(argparse.Namespace(press_cmd="on", confirm="네")) == 2
    assert sa.cmd_press(argparse.Namespace(press_cmd="on", confirm="언론사 사진 사용 권한 확인했고 책임은 나에게 있음")) == 0
    pp = config.load_config()["press_photos"]
    assert pp["enabled"] and "책임" in pp["confirm_text"]
    assert sa.cmd_press(argparse.Namespace(press_cmd="off")) == 0
    assert not config.load_config()["press_photos"]["enabled"]


def test_fill_crop_sharpens_small_photos():
    import numpy as np

    import smart_crop as sc

    img = (np.random.default_rng(0).random((413, 620, 3)) * 255).astype(np.uint8)
    info = sc.plan_crop(620, 413, 1080, 1920, None, 0, 1920)
    out = sc.apply_crop(img, info)
    assert out.shape == (1920, 1080, 3) and info["scale"] > 4
