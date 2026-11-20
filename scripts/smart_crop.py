"""얼굴·피사체 검출 → 피사체가 캔버스 상단 15~55% 에 오도록 cover 크롭.

    python smart_crop.py <image> --size 1080x1920 [--box-top 0] [--canvas-h 1920] --out photo.jpg [--json]

- 얼굴: OpenCV YuNet (models/face_detection_yunet_2023mar.onnx, MIT) → 없으면 OpenCV Haar(기본 포함)
- 얼굴이 없으면 스펙트럼 잔차 saliency 로 주요 피사체 영역을 잡는다.
- 사진 상자(box)가 캔버스 일부일 때(B 패널형: 상단 1100px 등) --box-top/--canvas-h 로 캔버스 좌표를 맞춘다.
- 피사체 기준 대역은 checks.yaml 의 image.subject_band 가 있으면 그 값을 쓴다(없으면 [0.15, 0.55]).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import os

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")   # YuNet 로드 때 나오는 내부 경고를 이용자 화면에 찍지 않음

import cv2  # noqa: E402
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import paths  # noqa: E402

DEFAULT_BAND = (0.15, 0.55)
YUNET = paths.MODELS_DIR / "face_detection_yunet_2023mar.onnx"


def read_image(path: Path) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)   # 한글 경로 대응
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"이미지를 읽을 수 없음: {path}")
    return img


def write_image(path: Path, img: np.ndarray, quality: int = 92) -> None:
    ext = Path(path).suffix.lower() or ".jpg"
    params = [cv2.IMWRITE_JPEG_QUALITY, quality] if ext in (".jpg", ".jpeg") else []
    ok, buf = cv2.imencode(ext, img, params)
    if not ok:
        raise ValueError("이미지 저장 실패")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    buf.tofile(str(path))


# ---------------------------------------------------------------- 검출
def detect_faces(img: np.ndarray) -> list[dict]:
    h, w = img.shape[:2]
    scale = min(1.0, 1280 / max(h, w))
    small = cv2.resize(img, (int(w * scale), int(h * scale))) if scale < 1 else img
    sh, sw = small.shape[:2]
    faces = []
    if YUNET.is_file():
        try:
            det = cv2.FaceDetectorYN.create(str(YUNET), "", (sw, sh), 0.75, 0.3, 5000)
            det.setInputSize((sw, sh))
            _, res = det.detect(small)
            for r in (res if res is not None else []):
                x, y, fw, fh, score = float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[-1])
                faces.append({"x": x / scale, "y": y / scale, "w": fw / scale, "h": fh / scale, "score": score, "kind": "face"})
            return _filter_faces(faces)
        except cv2.error:
            faces = []
    try:
        casc = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        for (x, y, fw, fh) in casc.detectMultiScale(gray, 1.1, 6, minSize=(max(24, sw // 40),) * 2):
            faces.append({"x": x / scale, "y": y / scale, "w": fw / scale, "h": fh / scale, "score": 0.5, "kind": "face"})
    except Exception:
        pass
    return _filter_faces(faces)


def _filter_faces(faces: list[dict]) -> list[dict]:
    if not faces:
        return []
    biggest = max(f["w"] * f["h"] for f in faces)
    return [f for f in faces if f["w"] * f["h"] >= biggest * 0.2]   # 배경의 작은 얼굴은 무시


def detect_saliency(img: np.ndarray) -> dict | None:
    """스펙트럼 잔차(Hou & Zhang 2007) 로 주요 영역 bbox."""
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    small = cv2.resize(gray, (64, 64))
    f = np.fft.fft2(small)
    log_amp = np.log(np.abs(f) + 1e-9)
    phase = np.angle(f)
    residual = log_amp - cv2.blur(log_amp, (3, 3))
    sal = np.abs(np.fft.ifft2(np.exp(residual + 1j * phase))) ** 2
    sal = cv2.GaussianBlur(sal.astype(np.float32), (9, 9), 2.5)
    sal = cv2.resize(sal, (w, h))
    sal = (sal - sal.min()) / (sal.max() - sal.min() + 1e-9)
    # 중앙 가중(사진가는 피사체를 가운데 근처에 둔다) — 하늘의 전선·가장자리 잡음을 누른다
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    prior = np.exp(-(((xx - w / 2) / (0.35 * w)) ** 2 + ((yy - h * 0.55) / (0.35 * h)) ** 2) / 2)
    sal = sal * prior
    thr = float(np.percentile(sal, 92))
    ys, xs = np.nonzero(sal >= thr)
    if len(xs) < 10:
        return None
    wts = sal[ys, xs]
    cx, cy = float(np.average(xs, weights=wts)), float(np.average(ys, weights=wts))
    side = 0.4 * min(w, h)
    x0 = min(max(0.0, cx - side / 2), w - side)
    y0 = min(max(0.0, cy - side / 2), h - side)
    return {"x": x0, "y": y0, "w": side, "h": side, "score": 0.3, "kind": "saliency"}


def subject_of(img: np.ndarray) -> tuple[dict | None, list[dict]]:
    faces = detect_faces(img)
    if faces:
        x0 = min(f["x"] for f in faces)
        y0 = min(f["y"] for f in faces)
        x1 = max(f["x"] + f["w"] for f in faces)
        y1 = max(f["y"] + f["h"] for f in faces)
        pad_top = (y1 - y0) * 0.35    # 머리카락·이마
        return {"x": x0, "y": max(0.0, y0 - pad_top), "w": x1 - x0, "h": (y1 - y0) + pad_top, "kind": "face"}, faces
    return detect_saliency(img), []


# ---------------------------------------------------------------- 크롭 계획
def plan_crop(img_w: int, img_h: int, box_w: int, box_h: int, subject: dict | None,
              box_top: int = 0, canvas_h: int | None = None, band=DEFAULT_BAND, max_zoom: float = 2.0) -> dict:
    """cover 크롭으로 피사체를 대역에 못 넣으면, 원본 해상도를 넘지 않는 범위에서 확대해 다시 맞춘다."""
    cover = max(box_w / img_w, box_h / img_h)
    info = _plan(img_w, img_h, box_w, box_h, subject, box_top, canvas_h, band, cover)
    if subject and info["subject_in_band"] is False:
        limit = min(max_zoom, max(1.0, 1.0 / cover))
        z = 1.1
        while z <= limit + 1e-9:
            alt = _plan(img_w, img_h, box_w, box_h, subject, box_top, canvas_h, band, cover * z)
            if alt["subject_in_band"]:
                alt["zoom"] = round(z, 2)
                return alt
            z += 0.1
    return info


def _plan(img_w, img_h, box_w, box_h, subject, box_top, canvas_h, band, scale) -> dict:
    canvas_h = canvas_h or box_h
    sw, sh = img_w * scale, img_h * scale
    band_top, band_bot = band[0] * canvas_h, band[1] * canvas_h
    if subject:
        cx = (subject["x"] + subject["w"] / 2) * scale
        top = subject["y"] * scale
        bot = (subject["y"] + subject["h"]) * scale
        target_c = (band_top + band_bot) / 2 - box_top
        if bot - top > band_bot - band_top:      # 대역보다 크면 위쪽을 맞춘다
            crop_y = top - (band_top - box_top)
        else:
            crop_y = (top + bot) / 2 - target_c
        crop_x = cx - box_w / 2
    else:
        crop_x, crop_y = (sw - box_w) / 2, (sh - box_h) * 0.35
    crop_x = float(min(max(0.0, crop_x), sw - box_w))
    crop_y = float(min(max(0.0, crop_y), sh - box_h))
    info = {"scale": scale, "zoom": 1.0, "crop": [crop_x, crop_y, box_w, box_h], "upscaled": scale > 1.0,
            "band_px": [band_top, band_bot], "subject_canvas": None, "subject_in_band": None}
    if subject:
        s_top = subject["y"] * scale - crop_y + box_top
        s_bot = (subject["y"] + subject["h"]) * scale - crop_y + box_top
        s_l = subject["x"] * scale - crop_x
        s_r = (subject["x"] + subject["w"]) * scale - crop_x
        tol = canvas_h * 0.02
        info["subject_canvas"] = [round(s_l), round(s_top), round(s_r), round(s_bot)]
        info["subject_in_band"] = bool(s_top >= band_top - tol and s_bot <= band_bot + tol
                                       and s_l >= -1 and s_r <= box_w + 1)
    return info


def apply_crop(img: np.ndarray, info: dict) -> np.ndarray:
    scale = info["scale"]
    h, w = img.shape[:2]
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
    big = cv2.resize(img, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=interp)
    x, y, bw, bh = (int(round(v)) for v in info["crop"])
    x = min(x, big.shape[1] - bw)
    y = min(y, big.shape[0] - bh)
    return big[y:y + bh, x:x + bw]


def smart_crop(src: Path, out: Path, box_w: int, box_h: int, box_top: int = 0, canvas_h: int | None = None,
               band=DEFAULT_BAND) -> dict:
    img = read_image(src)
    subject, faces = subject_of(img)
    info = plan_crop(img.shape[1], img.shape[0], box_w, box_h, subject, box_top, canvas_h, band)
    write_image(out, apply_crop(img, info))
    info.update({"source": str(src), "out": str(out), "source_size": [img.shape[1], img.shape[0]],
                 "faces": len(faces), "subject_kind": subject["kind"] if subject else None})
    return info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image", type=Path)
    ap.add_argument("--size", default="1080x1920")
    ap.add_argument("--box-top", type=int, default=0)
    ap.add_argument("--canvas-h", type=int)
    ap.add_argument("--band", default=None, help="예: 0.15,0.55")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    w, h = (int(v) for v in a.size.lower().split("x"))
    band = tuple(float(v) for v in a.band.split(",")) if a.band else DEFAULT_BAND
    info = smart_crop(a.image, a.out, w, h, a.box_top, a.canvas_h, band)
    if a.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        print(f"✔ {a.out}  얼굴 {info['faces']}개 · 피사체 {info['subject_kind'] or '없음(중앙)'}"
              + (f" · 대역 안: {'예' if info['subject_in_band'] else '아니오'}" if info['subject_in_band'] is not None else "")
              + (" · ⚠ 원본이 작아 확대됨" if info["upscaled"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
