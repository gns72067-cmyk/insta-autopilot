"""방식 B 용 임시 공개 URL — Instagram 로그인 API 는 로컬 파일 업로드를 받지 않아 video_url 이 필요하다.

config.yaml
    temp_host:
      type: folder            # folder | s3 | none
      folder:                 # 웹으로 공개되는 폴더 (예: 시놀로지 Web Station 의 별도 공유 폴더, Drive 동기화 폴더)
        path: "D:/SynologyDrive/ig-temp"
        public_base_url: "https://files.example.com/ig-temp"
      s3:                     # S3 · Cloudflare R2 등 S3 호환 (boto3 필요). 비공개 버킷 + 서명 URL
        endpoint_url: "https://<account>.r2.cloudflarestorage.com"
        bucket: "ig-temp"
        region: "auto"
        access_key_env: "IA_S3_KEY"        # 키 값은 환경변수에만
        secret_key_env: "IA_S3_SECRET"
        presign_seconds: 3600

- 파일 이름은 추측 불가능한 무작위 이름. 게시 확인 직후 지우고, 남은 것은 24시간 뒤 cleanup 으로 지운다.
- 규칙 서버 웹 루트(rules.*)는 쓰지 않는다 — 배포 때 허용 목록 밖 파일이 자동 삭제된다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import secrets
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import config as cfgmod  # noqa: E402
from common import log, paths  # noqa: E402

L = log.get("tempurl")
MAX_AGE_HOURS = 24


class TempHostError(Exception):
    pass


def ledger_file() -> Path:
    return paths.home() / "cache" / "temp_uploads.json"


def _ledger() -> list[dict]:
    return paths.read_json(ledger_file(), []) or []


def _save(items: list[dict]) -> None:
    paths.write_json(ledger_file(), items)


def settings() -> dict:
    return cfgmod.load_config().get("temp_host") or {"type": "none"}


def _s3(cfg: dict):
    try:
        import boto3
    except ImportError:
        raise TempHostError("S3/R2 임시 호스팅에는 boto3 가 필요합니다 (pip install boto3)")
    key, sec = os.environ.get(cfg.get("access_key_env", "")), os.environ.get(cfg.get("secret_key_env", ""))
    if not key or not sec:
        raise TempHostError("S3 키 환경변수가 비었습니다 (config 의 access_key_env / secret_key_env)")
    return boto3.client("s3", endpoint_url=cfg.get("endpoint_url") or None, region_name=cfg.get("region") or None,
                        aws_access_key_id=key, aws_secret_access_key=sec)


def wait_public(url: str, timeout: int = 180) -> None:
    import requests

    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            r = requests.head(url, timeout=15, allow_redirects=True)
            if r.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(5)
    raise TempHostError(f"공개 URL 에 파일이 보이지 않습니다 ({timeout}초): {url.split('?')[0]}")


def put(file: Path, wait: bool = True) -> dict:
    cfg = settings()
    kind = cfg.get("type", "none")
    name = f"{secrets.token_urlsafe(18)}{Path(file).suffix.lower()}"
    if kind == "folder":
        f = cfg.get("folder") or {}
        if not f.get("path") or not f.get("public_base_url", "").startswith("https://"):
            raise TempHostError("temp_host.folder.path / public_base_url(https) 설정이 필요합니다")
        dest = Path(f["path"]) / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, dest)
        url = f"{f['public_base_url'].rstrip('/')}/{name}"
        entry = {"type": "folder", "path": str(dest), "url": url}
    elif kind == "s3":
        s = cfg.get("s3") or {}
        cli = _s3(s)
        cli.upload_file(str(file), s["bucket"], name, ExtraArgs={"ContentType": "video/mp4"})
        url = cli.generate_presigned_url("get_object", Params={"Bucket": s["bucket"], "Key": name},
                                         ExpiresIn=int(s.get("presign_seconds", 3600)))
        entry = {"type": "s3", "bucket": s["bucket"], "key": name, "url": url}
    else:
        raise TempHostError("방식 B 에는 임시 공개 URL 설정(config.yaml temp_host)이 필요합니다 — 없으면 방식 A·C 를 쓰세요")
    entry["uploaded_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    entry["id"] = name
    items = _ledger()
    items.append(entry)
    _save(items)
    if wait:
        try:
            wait_public(url)
        except TempHostError:
            delete(name)
            raise
    return entry


def delete(entry_id: str) -> bool:
    items = _ledger()
    keep, ok = [], False
    for e in items:
        if e["id"] != entry_id:
            keep.append(e)
            continue
        try:
            if e["type"] == "folder":
                Path(e["path"]).unlink(missing_ok=True)
            elif e["type"] == "s3":
                _s3((settings().get("s3") or {})).delete_object(Bucket=e["bucket"], Key=e["key"])
            ok = True
        except Exception as ex:
            L.warning("임시 파일 삭제 실패 %s: %s", entry_id, ex)
            keep.append(e)
    _save(keep)
    return ok


def cleanup(max_age_hours: float = MAX_AGE_HOURS) -> int:
    cut = dt.datetime.now().astimezone() - dt.timedelta(hours=max_age_hours)
    n = 0
    for e in list(_ledger()):
        if dt.datetime.fromisoformat(e["uploaded_at"]) <= cut and delete(e["id"]):
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("test")
    p.add_argument("file", type=Path)
    sub.add_parser("cleanup")
    a = ap.parse_args()
    try:
        if a.cmd == "test":
            e = put(a.file)
            print(f"✔ 공개 URL 확인됨 → 바로 삭제: {e['url'].split('?')[0]}")
            delete(e["id"])
        else:
            print(f"✔ 24시간 지난 임시 파일 {cleanup()}개 삭제")
        return 0
    except TempHostError as e:
        print(f"✘ {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
