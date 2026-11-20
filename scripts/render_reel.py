"""10초 릴스 MP4 — 0~7초 승인된 카드 정지, 7~10초 cta_follow 애니메이션.

    python render_reel.py <job_dir> [--keep-frames]

- CTA 프레임: cta_follow.html → renderSlots → window.ctaSeek(t) (document.getAnimations() 의 currentTime 지정)
  t = i / fps (i = 0 … fps*cta_duration-1) → .canvas 캡처. render_concurrency 개 페이지로 나눠 캡처.
- ffmpeg: 카드 PNG 1장을 card_until 초 loop + CTA 프레임 → 30fps, 1080×1920, H.264 High, yuv420p, CRF 18,
  -preset veryfast, +faststart, 무음 AAC
- ffprobe 로 길이·해상도·코덱·프로파일·픽셀 형식·fps·오디오를 검증한다.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import config as cfgmod  # noqa: E402
from common import paths, render  # noqa: E402
from common.rules import compose, get  # noqa: E402


def cta_inputs(c: dict, profile: dict, card_png: Path) -> tuple[dict, dict, Path | None]:
    h = profile["handle"]
    bp = profile.get("brand") or {}
    tm_slots = c["template_meta"].get("slots") or {}
    cta = c["settings"].get("default_cta") or {}
    char = cfgmod.account_asset_path(h, bp.get("character", "")) if bp.get("character") else None
    logo = cfgmod.account_asset_path(h, bp.get("logo", "")) if bp.get("logo") else None
    mark = char or logo
    bfont = cfgmod.account_asset_path(h, bp.get("font", "")) if bp.get("font") else None
    slots = {
        "cta.background": paths.file_url(card_png) + f"?v={Path(card_png).stat().st_mtime_ns}",
        "cta.pitch": cta.get("pitch", ""),
        "cta.pitch_em": cta.get("pitch_em", ""),
        "cta.logo": paths.file_url(mark) if mark else "",
        "cta.name": profile.get("display_name") or h,
        "cta.handle": f"@{h}",
        "cta.card_desc": cta.get("card_desc", ""),
        "cta.button": cta.get("button") or (tm_slots.get("cta.button") or {}).get("default", "팔로우"),
        "cta.button_done": cta.get("button_done") or (tm_slots.get("cta.button_done") or {}).get("default", "팔로잉 ✓"),
        "cta.after": cta.get("after", ""),
    }
    return slots, render.brand_with_font(c["brand"], bfont), bfont


async def capture_cta(c: dict, profile: dict, card_png: Path, times: list[float], out_dir: Path,
                      concurrency: int = 2, variant: str = "A") -> list[Path]:
    from playwright.async_api import async_playwright

    tm = c["template_meta"]
    t = tm["templates"]["cta_follow"]
    slots, brand, bfont = cta_inputs(c, profile, card_png)
    out_dir.mkdir(parents=True, exist_ok=True)
    outs = [out_dir / f"cta_{i:04d}.png" for i in range(len(times))]
    concurrency = max(1, min(int(concurrency or 1), 6, len(times)))

    async with async_playwright() as pw:
        browser = await render.launch(pw)
        try:
            async def worker(k: int):
                page = await render.open_template(browser, Path(c["templates_dir"]), t["file"], t["size"], bfont)
                await render.render_slots(page, variant, brand, slots)
                for i in range(k, len(times), concurrency):
                    await page.evaluate("(s) => window.ctaSeek(s)", times[i])
                    await page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
                    await render.capture(page, outs[i])
                await page.close()

            await asyncio.gather(*[worker(k) for k in range(concurrency)])
        finally:
            await browser.close()
    return outs


def ffmpeg_cmd(card_png: Path, frames_pattern: Path, out: Path, w: int, h: int, fps: int, card_s: float,
               total_s: float) -> list[str]:
    vf = f"scale={w}:{h}:flags=lanczos,setsar=1,format=yuv420p"
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-loop", "1", "-framerate", str(fps), "-t", f"{card_s:.3f}", "-i", str(card_png),
        "-framerate", str(fps), "-start_number", "0", "-i", str(frames_pattern),
        "-f", "lavfi", "-t", f"{total_s:.3f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-filter_complex", f"[0:v]{vf}[a];[1:v]{vf}[b];[a][b]concat=n=2:v=1:a=0,fps={fps}[v]",
        "-map", "[v]", "-map", "2:a",
        "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p", "-crf", "18", "-preset", "veryfast",
        "-r", str(fps), "-c:a", "aac", "-b:a", "128k", "-t", f"{total_s:.3f}",
        "-movflags", "+faststart", str(out),
    ]


def probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe 실패: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout)


def verify_probe(info: dict, w: int, h: int, fps: int, total_s: float) -> list[str]:
    errs = []
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if not v:
        return ["영상 스트림 없음"]
    if v.get("codec_name") != "h264":
        errs.append(f"코덱 {v.get('codec_name')} (h264 아님)")
    if (v.get("profile") or "").lower() != "high":
        errs.append(f"프로파일 {v.get('profile')} (High 아님)")
    if v.get("pix_fmt") != "yuv420p":
        errs.append(f"픽셀 형식 {v.get('pix_fmt')}")
    if (v.get("width"), v.get("height")) != (w, h):
        errs.append(f"해상도 {v.get('width')}x{v.get('height')}")
    num, _, den = (v.get("avg_frame_rate") or "0/1").partition("/")
    rate = float(num) / float(den or 1) if float(den or 1) else 0
    if abs(rate - fps) > 0.01:
        errs.append(f"fps {rate:.2f}")
    dur = float(info["format"].get("duration") or 0)
    if abs(dur - total_s) > 0.1:
        errs.append(f"길이 {dur:.2f}초 (기준 {total_s}초)")
    if not a or a.get("codec_name") != "aac":
        errs.append("AAC 오디오 트랙 없음")
    return errs


def render_reel(job: Path, keep_frames: bool = False) -> dict:
    meta = paths.read_json(job / "meta.json")
    if not meta:
        raise SystemExit(f"✘ meta.json 없음: {job}")
    card = job / "card.png"
    if not card.is_file():
        raise SystemExit("✘ card.png 가 없습니다 (render_card 먼저)")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise SystemExit("✘ ffmpeg·ffprobe 가 없습니다 (doctor 참고)")
    profile = cfgmod.load_profile(meta["handle"])
    c = compose(profile, (meta.get("rules") or {}).get("version"))
    tl = c["template_meta"].get("timeline") or {}
    ck = c["checks"]
    w = int(get(ck, "render.reel.width", 1080))
    h = int(get(ck, "render.reel.height", 1920))
    fps = int(get(ck, "render.reel.fps", tl.get("fps", 30)))
    total = float(get(ck, "render.reel.seconds", tl.get("reel_seconds", 10)))
    card_s = float(get(ck, "render.reel.card_until", (tl.get("card") or [0, 7])[1]))
    cta_s = total - card_s
    n = int(round(cta_s * fps))
    times = [i / fps for i in range(n)]
    conc = cfgmod.load_config().get("render_concurrency", 2)

    tmp = Path(tempfile.mkdtemp(prefix="cta-", dir=job))
    try:
        t0 = dt.datetime.now()
        frames = asyncio.run(capture_cta(c, profile, card, times, tmp, conc, meta.get("template") or "A"))
        shutil.copyfile(frames[-1], job / "cta_last.png")
        out = job / "reel.mp4"
        cmd = ffmpeg_cmd(card, tmp / "cta_%04d.png", out, w, h, fps, card_s, total)
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise SystemExit(f"✘ ffmpeg 실패: {r.stderr.strip()[-400:]}")
        info = probe(out)
        errs = verify_probe(info, w, h, fps, total)
        secs = (dt.datetime.now() - t0).total_seconds()
    finally:
        if keep_frames:
            print(f"· 프레임 보관: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    rep = {"file": str(out), "ok": not errs, "errors": errs, "frames_cta": n, "render_seconds": round(secs, 1),
           "probe": {"duration": float(info["format"]["duration"]), "size": [v["width"], v["height"]],
                     "codec": v["codec_name"], "profile": v.get("profile"), "pix_fmt": v.get("pix_fmt"),
                     "fps": v.get("avg_frame_rate"), "bytes": int(info["format"].get("size", 0))}}
    meta["reel"] = {**rep, "rendered_at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    meta.setdefault("stages", {})["reel"] = "done" if rep["ok"] else "failed"
    paths.write_json(job / "meta.json", meta)
    return rep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("job", type=Path)
    ap.add_argument("--keep-frames", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    rep = render_reel(a.job, a.keep_frames)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        p = rep["probe"]
        print(f"{'✔' if rep['ok'] else '✘'} 릴스: {rep['file']}")
        print(f"  {p['duration']:.2f}초 · {p['size'][0]}×{p['size'][1]} · {p['codec']} {p['profile']} · {p['pix_fmt']} · "
              f"{p['fps']} fps · {p['bytes'] / 1e6:.1f}MB · 렌더 {rep['render_seconds']}초")
        for e in rep["errors"]:
            print(f"  ✘ {e}")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
