"""캡션 검사·수정 기록, 플러그인 업데이트 후 데이터 폴더 유지."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import checks
from test_checks import BANNED, CK as HOOK_CK, FS

CK = {**HOOK_CK, "caption": {"hashtags": [3, 5], "body_lines": [3, 6], "max_chars": 2200, "max_emoji": 3,
                             "max_dashes": 1, "max_same_ending_run": 2, "max_sources_in_line": 4}}
META = {"photo": {"credit": "사진: Jane / Wikimedia Commons (CC BY 4.0)", "caption_credit": "사진: Jane / Wikimedia Commons (CC BY 4.0)"},
        "template": "A"}

GOOD = """A시 버스 기본요금이 12월부터 바뀝니다
기본요금이 1,500원에서 1,800원으로 오른다고 시가 밝혔어요.
시의회 일부 의원은 재논의를 요구했습니다.
적용은 12월 1일부터라 출근길 교통비가 달라집니다.
다음 달 시의회 논의를 지켜볼 만해요.
출처: 연합뉴스, 한겨레, KBS
사진: Jane / Wikimedia Commons (CC BY 4.0)
#알파 #버스요금 #교통비"""


def run(text, user_edit=False, meta=META):
    return checks.check_caption(text, FS, meta, CK, BANNED, user_edit)


def test_good_caption_passes():
    r = run(GOOD)
    assert r["ok"], r


def test_hashtag_count_and_ig_limits():
    r = run(GOOD.replace("#알파 #버스요금 #교통비", "#알파"))
    assert any("해시태그 1개" in e for e in r["errors"])
    r = run(GOOD + " " + " ".join(f"#t{i}" for i in range(30)), user_edit=True)
    assert any("인스타 한도" in e for e in r["errors"])          # 이용자 수정본이어도 인스타 한도는 실패


def test_number_not_in_factsheet():
    r = run(GOOD.replace("1,800원으로", "2,000원으로"))
    assert any("2000" in e for e in r["errors"])


def test_banned_and_plagiarism():
    r = run(GOOD.replace("다음 달 시의회 논의를 지켜볼 만해요.", "충격적인 소식이 화제가 되고 있다."))
    assert any("충격" in e for e in r["errors"])
    r = run(GOOD.replace("기본요금이 1,500원에서", "A시 시내버스 요금 12월부터 1800원으로 인상 확정 1,500원에서"))
    assert any("연속 일치" in e for e in r["errors"])


def test_photo_credit_line_rules():
    r = run(GOOD.replace("사진: Jane / Wikimedia Commons (CC BY 4.0)\n", ""))
    assert any("사진 출처 줄이 없습니다" in e for e in r["errors"])
    r = run(GOOD, meta={"template": "C"})
    assert any("C" in e for e in r["errors"])


def test_same_ending_run():
    t = GOOD.replace("밝혔어요.", "밝혔습니다.").replace("다음 달 시의회 논의를 지켜볼 만해요.", "논의는 다음 달 이어집니다.")
    r = run(t)
    assert any("종결어미" in e for e in r["errors"])


def test_user_edit_only_warns():
    r = run(GOOD.replace("1,800원으로", "2,000원으로") + " 충격", user_edit=True)
    assert r["ok"] and any("충격" in w for w in r["warnings"]) and any("2000" in w for w in r["warnings"])


def test_caption_done_logs_diff(server, sync, home, tmp_path):
    from common import config
    import caption as capmod
    import job as jobmod

    server.publish("1.0.0")
    sync()
    config.save_profile({"handle": "tester", "display_name": "T", "alias": "테", "main_category": "alpha"})
    j = jobmod.new_job("tester", "버스 요금")
    (j / "factsheet.json").write_text(json.dumps(FS, ensure_ascii=False), encoding="utf-8")
    capmod.save_draft(j, GOOD)
    (j / "caption.txt").write_text(GOOD.replace("지켜볼 만해요.", "같이 봐요."), encoding="utf-8")   # PC 에서 직접 수정
    r = capmod.finish(j)
    assert r["edited"]
    log = [json.loads(l) for l in (home / "accounts/tester/edit_log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert log[-1]["edited"] and "지켜볼" in " ".join(log[-1]["removed_phrases"])
    text, changed = capmod.caption_for_upload(j)
    assert "같이 봐요." in text and not changed
    (j / "caption.txt").write_text(text + "\n#추가", encoding="utf-8")   # 승인 뒤 다시 고침 → 최신본 사용
    text2, changed2 = capmod.caption_for_upload(j)
    assert changed2 and text2.endswith("#추가")


def test_suggest_and_apply_personal_banned(server, sync, home):
    from common import config
    from common.rules import compose
    import caption as capmod

    server.publish("1.0.0")
    sync()
    config.save_profile({"handle": "tester", "display_name": "T", "alias": "테", "main_category": "alpha"})
    log = home / "accounts/tester/edit_log.jsonl"
    with open(log, "w", encoding="utf-8") as f:
        for i in range(3):
            f.write(json.dumps({"at": capmod.now_iso(), "job": f"0{i}", "date": "2026-11-19", "edited": True,
                                "similarity": 0.7, "removed_phrases": ["눈여겨볼"], "added_phrases": [],
                                "final": f"내가 고친 캡션 {i} 입니다 충분히 길게 씁니다"}, ensure_ascii=False) + "\n")
    out = capmod.suggest("tester")
    banned = [it for it in out["items"] if it["kind"] == "banned"]
    assert banned and banned[0]["value"] == "눈여겨볼"
    assert capmod.suggest("tester", if_due=True) is None              # 주 1회
    capmod.apply("tester", Path(out["_json"]), [banned[0]["no"]])
    c = compose(config.load_profile("tester"))
    assert "눈여겨볼" in c["banned_phrases"]                           # 승인한 개인 금지 표현이 합성에 들어감


def test_data_home_survives_plugin_update(tmp_path):
    """플러그인 폴더를 새 버전으로 바꿔도(재설치) 이용자 데이터 폴더와 내용은 그대로."""
    src = Path(__file__).resolve().parents[1]
    v1, v2 = tmp_path / "plugin-1.0.0", tmp_path / "plugin-1.1.0"
    ignore = shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "assets", "models", "tests")
    shutil.copytree(src, v1, ignore=ignore)
    env = {k: v for k, v in os.environ.items() if k != "INSTA_AUTOPILOT_HOME"}
    env.update(APPDATA=str(tmp_path / "appdata"), XDG_CONFIG_HOME=str(tmp_path / "xdg"), HOME=str(tmp_path / "userhome"),
               USERPROFILE=str(tmp_path / "userhome"), PYTHONIOENCODING="utf-8")
    data = tmp_path / "MyInsta"
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from common import paths, config;"
            "paths.set_home(__import__('pathlib').Path(sys.argv[2])); paths.ensure_home_layout();"
            "config.save_profile({'handle':'keep_me','display_name':'K','alias':'K','main_category':'x'});"
            "print(paths.home())")
    out1 = subprocess.run([sys.executable, "-c", code, str(v1 / "scripts"), str(data)], env=env, capture_output=True, text=True)
    assert out1.returncode == 0, out1.stderr
    shutil.copytree(v1, v2)
    shutil.rmtree(v1)                       # 업데이트: 옛 플러그인 폴더 삭제
    code2 = ("import sys; sys.path.insert(0, sys.argv[1]); from common import paths, config;"
             "print(paths.home()); print(config.list_accounts())")
    out2 = subprocess.run([sys.executable, "-c", code2, str(v2 / "scripts")], env=env, capture_output=True, text=True)
    assert out2.returncode == 0, out2.stderr
    home_line, accts = out2.stdout.strip().splitlines()
    assert Path(home_line) == data.resolve() and "keep_me" in accts
    assert (data / "accounts/keep_me/profile.yaml").is_file()


def test_rebase_rules_moves_job_to_active_version(tmp_path, monkeypatch):
    import job as jobmod

    j = tmp_path / "job"
    j.mkdir()
    st = {s: "pending" for s in jobmod.STAGES}
    st.update(brief="done", research="done", hooks="approved", photo="approved", card="waiting_user")
    jobmod.paths.write_json(j / "meta.json", {"rules": {"version": "1.0.3"}, "stages": st, "history": []})
    monkeypatch.setattr(jobmod, "load_state", lambda: {"active": {"version": "1.2.0", "channel": "beta"}})
    monkeypatch.setattr(jobmod, "verify_cached", lambda v: None)
    r = jobmod.rebase_rules(j)
    assert r == {"changed": True, "from": "1.0.3", "to": "1.2.0", "reset": ["card"]}
    m = jobmod.load_meta(j)
    assert m["rules"]["version"] == "1.2.0" and m["stages"]["card"] == "pending" and m["stages"]["photo"] == "approved"
    assert jobmod.rebase_rules(j)["changed"] is False
    m["stages"]["upload"] = "done"
    jobmod.paths.write_json(j / "meta.json", m)
    import pytest
    with pytest.raises(SystemExit):
        jobmod.rebase_rules(j)
