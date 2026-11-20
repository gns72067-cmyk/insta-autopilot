"""로그: <HOME>/logs/YYYY-MM-DD.log (이용자 PC 에만). 키·토큰은 기록하지 않는다.

(stdlib logging 과 이름이 겹치지 않도록 모듈명은 log 로 둔다.)
"""
from __future__ import annotations

import datetime as dt
import logging
import re

from . import paths

_SECRET_RE = re.compile(r"(?i)(client_id=|authorization:\s*|key=|token=)[^\s&\"']+")
_configured: set[str] = set()


class _Redact(logging.Filter):
    def filter(self, record):
        record.msg = _SECRET_RE.sub(r"\1***", str(record.msg))
        return True


def get(name: str) -> logging.Logger:
    lg = logging.getLogger(f"ia.{name}")
    if name in _configured:
        return lg
    lg.setLevel(logging.INFO)
    lg.propagate = False
    try:
        d = paths.logs_dir()
        d.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(d / f"{dt.date.today():%Y-%m-%d}.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        fh.addFilter(_Redact())
        lg.addHandler(fh)
    except OSError:
        lg.addHandler(logging.NullHandler())
    _configured.add(name)
    return lg
