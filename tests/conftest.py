"""테스트 공용: 임시 데이터 폴더 + 테스트 키로 서명하는 가짜 규칙 서버."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

# 테스트가 실제 이용자 데이터 폴더(문서/InstaAutopilot)를 만들거나 건드리지 않도록, 모듈을 불러오기 전에 임시 폴더로 고정
import os  # noqa: E402
import tempfile  # noqa: E402

os.environ["INSTA_AUTOPILOT_HOME"] = tempfile.mkdtemp(prefix="ia-test-home-")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

BASE = "https://rules.test"

CHECKS_YAML = """hook:
  max_chars: {l1: 18, l2: 20, l3: 22}
  versions: [strong, normal, soft]
  max_punct_per_version: 1
  allow_emoji: false
plagiarism:
  max_consecutive_eojeol: 4
fact:
  min_sources: 3
  min_verified_claims: 3
number:
  min_sources: 2
brief:
  candidates: 10
  lookback_hours: {news: 72, info: 336, empathy: 336}
  default_weights: {recency: 0.25, coverage: 0.25, fit: 0.20, interest: 0.15, visual: 0.15}
image:
  min_short_side_px: 1080
  candidates: [3, 5]
render:
  reel: {width: 1080, height: 1920, seconds: 10, card_until: 7.0, fps: 30}
"""

CATALOG_YAML = """schema_version: 1
merge:
  append_lists: [sources.rss, sources.google_news, trend_keywords, hashtag_seed, risk_notes]
  override_mode: append
  overridable_files: [brief.md, fact.md, hook.md, image.md, caption.md, upload.md, banned_phrases.txt]
  sub_category_keys: [sources, trend_keywords]
google_news_rss: "https://news.google.com/rss/search?q={query}&hl=ko"
google_trends_rss: "https://trends.google.com/trending/rss?geo=KR"
content_types:
  news:
    label: 뉴스
    defaults:
      hook_style: {default: normal}
      brief_weights: {recency: 0.30, coverage: 0.30, fit: 0.15, interest: 0.15, visual: 0.10}
      default_template: A
      sources: {google_trends: true}
      risk_notes: [유형 기본 리스크]
categories:
  - {id: alpha, name: 알파, content_type: news}
  - {id: beta, name: 베타, content_type: news}
"""

PRESET_ALPHA = """id: alpha
name: 알파
content_type: news
sources:
  rss:
    - {name: A1, url: "https://a.example/rss"}
    - {name: A2, url: "https://shared.example/rss"}
  google_news: [알파 검색]
trend_keywords: [가, 나]
brief_weights: {recency: 0.4, coverage: 0.2, fit: 0.2, interest: 0.1, visual: 0.1}
hook_style: {default: strong, strong: s, normal: n, soft: o}
hashtag_seed: [알파]
default_template: B
palettes:
  - {name: P1, accent: "#111111", bg: "#222222", cta: "#333333", text: "#FFFFFF"}
  - {name: P2, accent: "#444444", bg: "#555555", cta: "#666666", text: "#FFFFFF"}
  - {name: P3, accent: "#777777", bg: "#888888", cta: "#999999", text: "#FFFFFF"}
default_cta: {pitch: "피치\\n둘째", pitch_em: "둘째", card_desc: "설명", after: "이후"}
posting_time_hint: ["07:00"]
risk_notes: [알파 리스크]
"""

PRESET_BETA = """id: beta
name: 베타
content_type: news
sources:
  rss:
    - {name: B1, url: "https://b.example/rss"}
    - {name: shared-dup, url: "https://shared.example/rss"}
  google_news: [베타 검색]
trend_keywords: [나, 다]
hook_style: {default: soft}
palettes:
  - {name: X, accent: "#000000", bg: "#000000", cta: "#000000", text: "#000000"}
default_cta: {pitch: "베타 피치"}
risk_notes: [베타 리스크]
"""


def base_files(extra: dict[str, str] | None = None) -> dict[str, bytes]:
    files = {
        "base/workflow.md": "# workflow\n",
        "base/brief.md": "# brief\n",
        "base/fact.md": "# fact\n",
        "base/hook.md": "# hook\n\n기본 훅 규칙\n",
        "base/image.md": "# image\n",
        "base/caption.md": "# caption\n",
        "base/upload.md": "# upload\n",
        "base/checks.yaml": CHECKS_YAML,
        "base/banned_phrases.txt": "# 주석\n충격\nre:경악+\n\n",
        "categories/_catalog.yaml": CATALOG_YAML,
        "categories/alpha/preset.yaml": PRESET_ALPHA,
        "categories/alpha/hook.md": "- 알파 추가 훅 규칙\n",
        "categories/alpha/banned_phrases.txt": "알파금지\n",
        "categories/beta/preset.yaml": PRESET_BETA,
        "categories/beta/hook.md": "- 베타 규칙(서브에선 쓰이면 안 됨)\n",
        "templates/template_meta.yaml": "schema_version: 1\n",
    }
    files.update(extra or {})
    return {k: v.encode("utf-8") for k, v in files.items()}


def canonical(p) -> bytes:
    return json.dumps(p, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


class FakeServer:
    def __init__(self, key: Ed25519PrivateKey):
        self.key = key
        self.files: dict[str, bytes] = {}
        self.fail = False
        self.requests: list[str] = []

    def sign(self, payload: dict) -> bytes:
        sig = base64.b64encode(self.key.sign(canonical(payload))).decode()
        return json.dumps({"payload": payload, "sig": sig}, ensure_ascii=False, indent=2).encode("utf-8")

    @staticmethod
    def make_tar(files: dict[str, bytes], extra_members: list[tuple[tarfile.TarInfo, bytes | None]] = ()) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
            for name in sorted(files):
                ti = tarfile.TarInfo(name)
                ti.size = len(files[name])
                tar.addfile(ti, io.BytesIO(files[name]))
            for ti, data in extra_members:
                tar.addfile(ti, io.BytesIO(data) if data is not None else None)
        return buf.getvalue()

    def publish(self, version: str, channel: str = "stable", files: dict[str, bytes] | None = None,
                published_at: str | None = None, min_client: str = "1.0.0", changelog=None,
                tar_bytes: bytes | None = None, manifest_files: list | None = None) -> dict:
        files = files if files is not None else base_files()
        files = {**files, "bundle.json": json.dumps({"version": version, "changelog": changelog or []}).encode()}
        tb = tar_bytes if tar_bytes is not None else self.make_tar(files)
        mf = manifest_files if manifest_files is not None else [
            {"path": p, "sha256": hashlib.sha256(d).hexdigest(), "size": len(d)} for p, d in sorted(files.items())]
        man = {"schema": 2, "version": version, "created_at": "2026-11-19T10:00:00+09:00",
               "bundle": {"path": f"bundles/{version}.tar.gz", "sha256": hashlib.sha256(tb).hexdigest(), "size": len(tb)},
               "files": mf, "changelog": changelog or [], "min_client_version": min_client}
        mraw = self.sign(man)
        ptr = {"schema": 2, "channel": channel, "version": version, "bundle": f"bundles/{version}.tar.gz",
               "sha256": hashlib.sha256(tb).hexdigest(), "size": len(tb), "manifest": f"bundles/{version}.manifest.json",
               "manifest_sha256": hashlib.sha256(mraw).hexdigest(), "changelog": changelog or [f"{version} 변경"],
               "min_client_version": min_client,
               "published_at": published_at or dt.datetime.now().astimezone().isoformat(timespec="seconds")}
        self.files[f"bundles/{version}.tar.gz"] = tb
        self.files[f"bundles/{version}.manifest.json"] = mraw
        self.files[f"{channel}/latest.json"] = self.sign(ptr)
        return ptr

    def fetch(self, url: str, no_cache: bool = False) -> bytes:
        import rules_sync

        self.requests.append(url)
        if self.fail:
            raise rules_sync.NetError("접속 실패: ConnectionError")
        path = url[len(BASE) + 1:]
        if path not in self.files:
            raise rules_sync.NetError(f"HTTP 404: /{path}")
        return self.files[path]


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("INSTA_AUTOPILOT_HOME", str(h))
    monkeypatch.setattr("common.paths.os_config_dir", lambda: tmp_path / "oscfg")
    from common import paths

    paths.ensure_home_layout()
    (h / "config.yaml").write_text(f"rules_url: {BASE}\nchannel: stable\n", encoding="utf-8")
    return h


@pytest.fixture
def server(monkeypatch, home):
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr("common.rules.EMBEDDED_PUBKEY_B64", base64.b64encode(pub).decode())
    return FakeServer(key)


@pytest.fixture
def sync(server):
    import rules_sync

    def run(**kw):
        kw.setdefault("fetch", server.fetch)
        kw.setdefault("sleep", lambda s: None)
        kw.setdefault("client_version", "1.0.0")
        return rules_sync.sync(**kw)

    return run
