"""규칙 동기화: 서명 위조·해시 불일치 거부, 네트워크 실패 시 캐시 사용, 재전송·버전 조건."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile

import pytest

from conftest import base_files


def test_spec_test_vector_with_embedded_key():
    """CLIENT_SPEC 3.3 테스트 벡터 — 실제 내장 공개키로 검증 성공, 변조 시 실패."""
    from common import rules

    env = ('{"payload":{"a":{"b":"시작","z":"끝"},"list":[1,true,null],"schema":2,"text":"규칙 \\"따옴표\\"\\n줄바꿈"},'
           '"sig":"3KtbKNc9hrKW9SxpaP98VZ97MI730LlpeXYARjgK65gOGCLdze8TX+rBDlqbNkaImkUf6z1luPoAFhWgLOrwDg=="}')
    p = rules.open_envelope(env.encode("utf-8"), "vector")
    assert rules.canonical(p).decode() == '{"a":{"b":"시작","z":"끝"},"list":[1,true,null],"schema":2,"text":"규칙 \\"따옴표\\"\\n줄바꿈"}'
    with pytest.raises(rules.RulesError):
        rules.open_envelope(env.replace("끝", "끗").encode("utf-8"), "vector")
    with pytest.raises(rules.RulesError):
        rules.open_envelope(env.replace("3KtbK", "3KtbL").encode("utf-8"), "vector")
    with pytest.raises(rules.RulesError):   # 추가 키
        rules.open_envelope(env[:-1].encode() + b',"x":1}', "vector")


def test_first_sync_then_unchanged(server, sync, home):
    server.publish("1.0.0", changelog=["첫 배포"])
    r = sync()
    assert r["status"] == "updated" and r["version"] == "1.0.0" and r["changelog"] == ["첫 배포"]
    assert (home / "cache/rules/1.0.0/base/hook.md").is_file()
    assert (home / "cache/rules/1.0.0/_manifest.json").is_file()
    r = sync()
    assert r["status"] == "unchanged"


def test_new_version_reports_changelog(server, sync):
    server.publish("1.0.0", published_at="2026-11-19T10:00:00+09:00")
    sync()
    server.publish("1.0.1", published_at="2026-11-19T11:00:00+09:00", changelog=["훅 기준 변경"],
                   files=base_files({"base/hook.md": "# hook v2\n"}))
    r = sync()
    assert r["status"] == "updated" and r["version"] == "1.0.1" and r["previous_version"] == "1.0.0"
    assert r["changelog"] == ["훅 기준 변경"]


def test_forged_pointer_signature_rejected_uses_cache(server, sync, home):
    server.publish("1.0.0", published_at="2026-11-19T10:00:00+09:00")
    sync()
    server.publish("1.0.1", published_at="2026-11-19T11:00:00+09:00")
    env = json.loads(server.files["stable/latest.json"])
    env["payload"]["version"] = "9.9.9"          # 서명 후 변조
    server.files["stable/latest.json"] = json.dumps(env).encode()
    slept = []
    r = sync(sleep=lambda s: slept.append(s))
    assert r["status"] == "cached" and r["version"] == "1.0.0"
    assert slept == [5]                            # 5초 후 1회 재시도
    assert any("서명" in w for w in r["warnings"])
    assert not (home / "cache/rules/1.0.1").exists()


def test_pointer_signed_by_other_key_rejected(server, sync):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    server.publish("1.0.0")
    server.key = Ed25519PrivateKey.generate()      # 공격자 키로 다시 서명
    server.publish("1.0.0")
    r = sync()
    assert r["status"] == "error" and r["version"] is None


def test_manifest_hash_mismatch_rejected(server, sync, home):
    server.publish("1.0.0")
    m = server.files["bundles/1.0.0.manifest.json"]
    server.files["bundles/1.0.0.manifest.json"] = m + b"\n"   # 바이트가 달라짐
    r = sync()
    assert r["status"] == "error"
    assert any("manifest" in w for w in r["warnings"])
    assert not (home / "cache/rules/1.0.0").exists()


def test_bundle_hash_mismatch_rejected(server, sync, home):
    server.publish("1.0.0")
    tb = server.files["bundles/1.0.0.tar.gz"]
    server.files["bundles/1.0.0.tar.gz"] = tb[:-1] + bytes([tb[-1] ^ 1])
    r = sync()
    assert r["status"] == "error" and any("번들" in w for w in r["warnings"])
    assert not (home / "cache/rules/1.0.0").exists()
    assert not list((home / "cache/rules").glob(".tmp-*"))


def test_file_hash_mismatch_in_bundle_rejected(server, sync, home):
    files = base_files()
    files["bundle.json"] = b"{}"
    mf = [{"path": p, "sha256": hashlib.sha256(d).hexdigest(), "size": len(d)} for p, d in sorted(files.items())]
    files["base/hook.md"] = b"# hook TAMPERED\n"          # 번들 안 파일만 바뀜 (manifest 는 원래 해시)
    tb = server.make_tar(files)
    server.publish("1.0.0", files=files, tar_bytes=tb, manifest_files=mf)
    r = sync()
    assert r["status"] == "error" and any("해시" in w for w in r["warnings"])


@pytest.mark.parametrize("name", ["../evil.md", "/abs.md", "base\\win.md"])
def test_path_traversal_entry_rejected(server, sync, home, name):
    files = base_files()
    ti = tarfile.TarInfo(name)
    ti.size = 3
    tb = server.make_tar({**files, "bundle.json": b"{}"}, [(ti, b"bad")])
    server.publish("1.0.0", files=files, tar_bytes=tb)
    r = sync()
    assert r["status"] == "error"
    assert not (home.parent / "evil.md").exists()


def test_symlink_entry_rejected(server, sync):
    files = base_files()
    ti = tarfile.TarInfo("base/link.md")
    ti.type = tarfile.SYMTYPE
    ti.linkname = "../../secret"
    tb = server.make_tar({**files, "bundle.json": b"{}"}, [(ti, None)])
    server.publish("1.0.0", files=files, tar_bytes=tb)
    assert sync()["status"] == "error"


def test_network_failure_uses_cache(server, sync):
    server.publish("1.0.0")
    sync()
    server.fail = True
    r = sync()
    assert r["status"] == "cached" and r["version"] == "1.0.0"
    assert any("접속 실패" in w for w in r["warnings"])


def test_no_cache_and_failure_is_error(server, sync):
    server.fail = True
    r = sync()
    assert r["status"] == "error" and r["version"] is None


def test_replayed_older_pointer_rejected(server, sync):
    server.publish("1.0.1", published_at="2026-11-19T11:00:00+09:00")
    sync()
    server.publish("1.0.0", published_at="2026-11-19T10:00:00+09:00")   # 과거 포인터 재전송
    r = sync()
    assert r["status"] == "cached" and r["version"] == "1.0.1"
    assert any("재전송" in w for w in r["warnings"])


def test_rollback_with_newer_published_at_accepted(server, sync):
    server.publish("1.0.0", published_at="2026-11-19T10:00:00+09:00")
    sync()
    server.publish("1.0.1", published_at="2026-11-19T11:00:00+09:00")
    sync()
    server.publish("1.0.0", published_at="2026-11-19T12:00:00+09:00")   # 롤백
    r = sync()
    assert r["status"] == "updated" and r["version"] == "1.0.0"


def test_min_client_version_blocks_update(server, sync):
    server.publish("1.0.0", published_at="2026-11-19T10:00:00+09:00")
    sync()
    server.publish("2.0.0", published_at="2026-11-19T11:00:00+09:00", min_client="2.0.0")
    r = sync()
    assert r["status"] == "cached" and r["version"] == "1.0.0" and r["update_required"] == "2.0.0"
    assert not any("bundles/2.0.0.tar.gz" in u for u in server.requests)


def test_channel_mismatch_rejected(server, sync):
    server.publish("1.0.0", channel="beta")
    server.files["stable/latest.json"] = server.files["beta/latest.json"]
    assert sync()["status"] == "error"


def test_tampered_cache_is_never_used(server, sync, home):
    from common import rules

    server.publish("1.0.0")
    sync()
    (home / "cache/rules/1.0.0/base/hook.md").write_text("# 변조\n", encoding="utf-8")
    with pytest.raises(rules.RulesError):
        rules.rules_dir("1.0.0")
    # 추가로 심은 파일도 거부
    server.fail = True
    r = sync()
    assert r["status"] == "error"          # 캐시도 검증 실패 → 사용 안 함
    server.fail = False
    r = sync()                              # 서버에서 다시 받아 복구
    assert r["status"] == "unchanged" and any("다시 받았" in w for w in r["warnings"])
    (home / "cache/rules/1.0.0/categories/alpha/image.md").write_text("심은 파일\n", encoding="utf-8")
    with pytest.raises(rules.RulesError):
        rules.rules_dir("1.0.0")


def test_https_only():
    import rules_sync

    with pytest.raises(rules_sync.NetError):
        rules_sync.check_base("http://rules.example.com")
    assert rules_sync.check_base("http://127.0.0.1:8000") == "http://127.0.0.1:8000"
