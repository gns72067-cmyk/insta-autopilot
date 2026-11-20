"""합성된 최종 규칙 출력 — base → content_type → 메인 카테고리 → 서브 → 계정 설정.

    python rules_compose.py <handle|alias> <step> [--job JOB_DIR | --version X.Y.Z] [--json]

step: workflow | brief | fact | hook | image | caption | upload | render | all
--job 을 주면 그 작업의 meta.json 에 고정된 규칙 버전을 쓴다(작업 중 규칙이 바뀌어도 끝까지 같은 버전).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import config as cfgmod  # noqa: E402
from common import paths  # noqa: E402
from common.rules import RulesError, compose  # noqa: E402

STEPS = {
    "workflow": {"doc": "workflow.md", "settings": ["content_type"], "checks": []},
    "brief": {"doc": "brief.md",
              "settings": ["sources", "trend_keywords", "brief_weights", "exclude_keywords", "risk_notes"],
              "checks": ["brief"]},
    "fact": {"doc": "fact.md", "settings": ["evidence", "rules", "risk_notes"], "checks": ["fact", "number"]},
    "hook": {"doc": "hook.md", "settings": ["hook_style", "tone", "risk_notes"],
             "checks": ["hook", "plagiarism"], "banned": True},
    "image": {"doc": "image.md", "settings": ["default_template"], "checks": ["image"], "safe_zones": True},
    "caption": {"doc": "caption.md", "settings": ["hashtag_seed", "default_cta", "tone", "risk_notes"],
                "checks": ["caption", "plagiarism"], "banned": True},
    "upload": {"doc": "upload.md", "settings": ["posting_time_hint"], "checks": ["upload"]},
    "render": {"doc": None, "settings": ["default_template", "default_cta", "palettes"], "checks": ["render"],
               "safe_zones": True},
}


def pinned_version(job: Path | None) -> str | None:
    if not job:
        return None
    meta = paths.read_json(Path(job) / "meta.json", {}) or {}
    return (meta.get("rules") or {}).get("version")


def to_markdown(c: dict, step: str) -> str:
    import yaml

    spec = STEPS[step]
    prof = c["profile"]
    out = [f"# 합성 규칙 — {step} (규칙 {c['version']})", "",
           f"- 계정: @{prof.get('handle')} ({prof.get('alias') or '별칭 없음'}) · 메인 {c['main']}"
           + (f" · 서브 {', '.join(c['subs'])}" if c["subs"] else "") + f" · 유형 {c['content_type']}",
           f"- 합성 순서: {' → '.join(c['applied'])}",
           "- 같은 규칙이 충돌하면 더 엄격한 쪽을 따른다. 수치는 아래 checks 값만 쓴다.", ""]
    if spec["doc"]:
        out += [c["docs"].get(spec["doc"], f"({spec['doc']} 없음)").rstrip(), ""]
    settings = {k: c["settings"][k] for k in spec["settings"] if k in c["settings"]}
    if step == "render":
        settings = {"brand": c["brand"], "chip": c["chip"], **settings}
    if settings:
        out += ["## 이 계정에 적용된 설정", "", "```yaml", yaml.safe_dump(settings, allow_unicode=True, sort_keys=False).rstrip(), "```", ""]
    checks = {k: c["checks"][k] for k in spec["checks"] if k in c["checks"]}
    if checks:
        out += ["## checks.yaml (수치 기준)", "", "```yaml", yaml.safe_dump(checks, allow_unicode=True, sort_keys=False).rstrip(), "```", ""]
    if spec.get("banned"):
        out += ["## 금지 표현 (banned_phrases, 부분 일치 · re: 는 정규식)", "", "```", *c["banned_phrases"], "```", ""]
    if spec.get("safe_zones"):
        tm = c["template_meta"]
        out += ["## 템플릿 세이프존", "", "```yaml",
                yaml.safe_dump({"safe_zones": tm.get("safe_zones"), "grid_crop": tm.get("grid_crop"),
                                "variants": tm.get("variants")}, allow_unicode=True, sort_keys=False).rstrip(), "```", ""]
    if step in ("hook", "caption", "brief"):
        se = paths.account_dir(prof["handle"]) / "style_examples.md"
        if se.is_file():
            out += [f"- 계정 스타일 예시: {se}"]
    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="규칙 합성")
    ap.add_argument("account")
    ap.add_argument("step", choices=[*STEPS, "all"])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--job", type=Path)
    g.add_argument("--version")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    try:
        handle = cfgmod.resolve_account(a.account)
        c = compose(cfgmod.load_profile(handle), a.version or pinned_version(a.job))
    except (RulesError, FileNotFoundError, ValueError) as e:
        print(f"✘ {e}")
        return 2
    if a.json:
        print(json.dumps(c, ensure_ascii=False, indent=2))
        return 0
    steps = list(STEPS) if a.step == "all" else [a.step]
    print("\n---\n\n".join(to_markdown(c, s) for s in steps))
    return 0


if __name__ == "__main__":
    sys.exit(main())
