"""insta-autopilot 공용 모듈 (paths, config, rules, log)."""
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass
