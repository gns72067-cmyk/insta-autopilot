"""규칙 합성 순서: base → content_type → 메인 → 서브(sources·trend_keywords 만) → 계정."""
from __future__ import annotations

import pytest


@pytest.fixture
def composed(server, sync):
    server.publish("1.0.0")
    assert sync()["status"] == "updated"
    from common.rules import compose

    def run(profile):
        p = {"handle": "tester", "display_name": "테스터", "alias": "테", "main_category": "alpha",
             "sub_categories": [], "brand": {}, "cta": {}, "extra_sources": {}, **profile}
        return compose(p)

    return run


def test_order_and_main_overrides_type_defaults(composed):
    c = composed({})
    assert c["applied"] == ["base", "content_type:news", "main:alpha", "profile"]
    s = c["settings"]
    assert s["brief_weights"]["recency"] == 0.4          # 메인이 유형 기본값을 이김
    assert s["sources"]["google_trends"] is True          # 유형 기본값의 다른 키는 유지(깊은 병합)
    assert s["default_template"] == "B"
    assert s["risk_notes"] == ["유형 기본 리스크", "알파 리스크"]   # append_lists 합집합


def test_sub_category_only_sources_and_keywords(composed):
    c = composed({"sub_categories": ["beta"]})
    s = c["settings"]
    assert c["applied"][-2:] == ["sub:beta", "profile"]
    urls = [r["url"] for r in s["sources"]["rss"]]
    assert urls == ["https://a.example/rss", "https://shared.example/rss", "https://b.example/rss"]   # url 기준 중복 제거
    assert s["sources"]["google_news"] == ["알파 검색", "베타 검색"]
    assert s["trend_keywords"] == ["가", "나", "다"]
    assert s["hook_style"]["default"] == "strong"          # 서브의 hook_style 무시
    assert s["palettes"][0]["name"] == "P1"                # 서브의 palettes 무시
    assert s["default_cta"]["pitch"] == "피치\n둘째"         # 서브의 default_cta 무시
    assert "베타 리스크" not in s["risk_notes"]
    assert "베타 규칙" not in c["docs"]["hook.md"]           # 서브의 오버라이드 md 미적용


def test_override_md_and_banned_appended(composed):
    c = composed({})
    hook = c["docs"]["hook.md"]
    assert hook.startswith("# hook\n\n기본 훅 규칙")
    assert "## 카테고리 추가 규칙 (알파)\n\n- 알파 추가 훅 규칙" in hook
    assert c["banned_phrases"] == ["충격", "re:경악+", "알파금지"]


def test_profile_is_last(composed):
    c = composed({
        "cta": {"pitch": "내 피치", "after": ""},
        "extra_sources": {"rss": [{"name": "내 RSS", "url": "https://mine.example/rss"},
                                  {"name": "dup", "url": "https://a.example/rss"}],
                          "google_news": ["내 검색"]},
        "template": "C",
        "exclude_keywords": ["빼기"],
        "brand": {"palette": {"accent": "#ABCDEF", "bg": "#000000", "cta": "#111111", "text": "#FFFFFF"}, "chip": "내칩"},
        "custom_category": {"name": "커스텀", "trend_keywords": ["라"]},
    })
    s = c["settings"]
    assert s["default_cta"] == {"pitch": "내 피치", "pitch_em": "둘째", "card_desc": "설명", "after": "이후"}   # 빈 값은 덮지 않음
    assert [r["url"] for r in s["sources"]["rss"]][-1] == "https://mine.example/rss"
    assert len(s["sources"]["rss"]) == 3
    assert s["sources"]["google_news"][-1] == "내 검색"
    assert s["default_template"] == "C"
    assert s["exclude_keywords"] == ["빼기"]
    assert s["trend_keywords"][-1] == "라"
    assert c["brand"]["accent"] == "#ABCDEF" and c["chip"] == "내칩"


def test_default_brand_is_first_palette_and_chip_is_name(composed):
    c = composed({})
    assert c["brand"] == {"accent": "#111111", "bg": "#222222", "cta": "#333333", "text": "#FFFFFF"}
    assert c["chip"] == "알파"


def test_unknown_main_category_rejected(composed):
    from common.rules import RulesError

    with pytest.raises(RulesError):
        composed({"main_category": "nope"})


def test_job_pins_rules_version(server, sync, home):
    """작업 시작 때 고정한 버전은 새 버전이 와도 그대로 쓴다."""
    from common import config
    from common.rules import compose
    import job as jobmod

    server.publish("1.0.0", published_at="2026-11-19T10:00:00+09:00")
    sync()
    config.save_profile({"handle": "tester", "display_name": "T", "alias": "테", "main_category": "alpha"})
    j = jobmod.new_job("tester", "제목")
    server.publish("1.1.0", published_at="2026-11-19T11:00:00+09:00",
                   files=__import__("conftest").base_files({"base/hook.md": "# hook NEW\n"}))
    assert sync()["version"] == "1.1.0"
    import rules_compose

    v = rules_compose.pinned_version(j)
    assert v == "1.0.0"
    assert compose(config.load_profile("tester"), v)["docs"]["hook.md"].startswith("# hook\n")
