"""카드 렌더 + 자동 검수 → card.png / card_grid.png / card_guide.png (+ 선택: card_feed.png)

    python render_card.py <job_dir> [--variant A|B|C] [--feed] [--attempts 3]

입력: meta.json 의 hook(선택 훅), photo(원본 경로·credit), template, 계정 브랜드(로고·캐릭터·폰트·팔레트)
검수(template_meta.yaml 기준):
  - 텍스트 bbox ↔ 세이프존(상단·하단·우측 버튼 열), 프로필 그리드 크롭 영역, 요소 간 겹침
  - 대비(글자색 ↔ 실제 렌더된 배경 픽셀), 줄 넘침·줄 수·어색한 줄바꿈(외톨이 글자, 어절 중간 끊김)
실패하면 글자 크기·줄바꿈 균형·사진 밝기를 조정해 최대 3회 다시 렌더한다. 끝까지 남은 문제는 보고한다.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import io
import json
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import config as cfgmod  # noqa: E402
from common import paths, render  # noqa: E402
from common.rules import compose, get  # noqa: E402
import smart_crop  # noqa: E402

TEXT_SLOTS = ["l1", "l2", "l3"]
ALL_SLOTS = ["l1", "l2", "l3", "chip", "date", "handle", "credit", "logo"]
SLOT_SEL = {"l1": ".text .l1", "l2": ".text .l2", "l3": ".text .l3", "credit": ".credit", "chip": ".chip"}
DEFAULT_MAX_LINES = {"l1": 2, "l2": 2, "l3": 2}
DEFAULT_MIN_CONTRAST = 3.0     # 큰 글자 WCAG AA (render.min_contrast 가 있으면 그 값)


# ---------------------------------------------------------------- 입력 만들기
def card_inputs(c: dict, profile: dict, hook: dict, variant: str, credit: str = "", photo_url: str = "",
                date: dt.date | None = None) -> tuple[dict, dict, Path | None]:
    h = profile["handle"]
    brand_p = profile.get("brand") or {}
    logo = cfgmod.account_asset_path(h, brand_p.get("logo", "")) if brand_p.get("logo") else None
    char = cfgmod.account_asset_path(h, brand_p.get("character", "")) if brand_p.get("character") else None
    bfont = cfgmod.account_asset_path(h, brand_p.get("font", "")) if brand_p.get("font") else None
    d = date or dt.date.today()
    slots = {
        "l1": hook.get("l1", ""), "l2": hook.get("l2", ""), "l3": hook.get("l3", ""),
        "chip": c["chip"], "date": f"{d.month:02d}.{d.day:02d}", "handle": f"@{h}",
        "credit": "" if variant == "C" else credit,
        "photo": "" if variant == "C" else photo_url,
        "logo": paths.file_url(logo) if logo else "",
        "character": paths.file_url(char) if char else "",
        "cta.name": profile.get("display_name") or h,
    }
    return slots, render.brand_with_font(c["brand"], bfont), bfont


# ---------------------------------------------------------------- 검수
def _rgba(s: str):
    m = re.findall(r"[\d.]+", s or "")
    if len(m) < 3:
        return (255, 255, 255, 1.0)
    return (float(m[0]), float(m[1]), float(m[2]), float(m[3]) if len(m) > 3 else 1.0)


def _lum(rgb) -> float:
    def ch(v):
        v = v / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = rgb[:3]
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def _lum_arr(rgb: np.ndarray) -> np.ndarray:
    c = rgb / 255.0
    lin = np.where(c <= 0.03928, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * lin[..., 0] + 0.7152 * lin[..., 1] + 0.0722 * lin[..., 2]


def contrast_of(fg: np.ndarray, bg: np.ndarray, box, color: str) -> float | None:
    """글자 있는 렌더(fg)와 글자만 투명하게 한 렌더(bg)를 비교한다.
    글자 픽셀 위치마다 (글자색을 알파 합성한 실제 색) ↔ (그 자리 배경) 대비를 구하고 하위 10% 값을 쓴다."""
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    H, W = fg.shape[:2]
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    f = fg[y0:y1, x0:x1, :3].astype(np.float32)
    b = bg[y0:y1, x0:x1, :3].astype(np.float32)
    diff = np.abs(f - b).max(axis=2)
    core = diff >= max(24.0, float(np.percentile(diff, 97)) * 0.6)
    if core.sum() < 8:
        return None
    r, g, bb, a = _rgba(color)
    tc = np.array([r, g, bb], np.float32)
    bgp = b[core]
    eff = tc * a + bgp * (1 - a)
    tl, bl = _lum_arr(eff), _lum_arr(bgp)
    ratio = (np.maximum(tl, bl) + 0.05) / (np.minimum(tl, bl) + 0.05)
    return round(float(np.percentile(ratio, 10)), 2)


def _overlap(a, b, gap=0) -> bool:
    return not (a[2] + gap <= b[0] or b[2] + gap <= a[0] or a[3] + gap <= b[1] or b[3] + gap <= a[1])


def evaluate(data: dict, tm: dict, kind: str, img: np.ndarray, checks: dict,
             bg_img: np.ndarray | None = None) -> list[dict]:
    W, H = data["canvas"]
    slots, blocks = data["slots"], data["blocks"]
    issues: list[dict] = []

    def add(target, code, msg):
        issues.append({"target": target, "code": code, "msg": msg})

    boxes = {k: v["box"] for k, v in slots.items()}
    if ".mk" in blocks:
        boxes["logo"] = blocks[".mk"]
    if kind == "reel":
        sz = tm["safe_zones"]["reel"]
        top, bot, rb = sz["top"], H - sz["bottom"], sz["right_bottom"]
        for k, b in boxes.items():
            if b[1] < top - 0.5:
                add(k, "safe_top", f"{k}: 상단 UI 영역({top}px) 침범 (y={b[1]})")
            if b[3] > bot + 0.5:
                add(k, "safe_bottom", f"{k}: 하단 캡션 영역 침범 (bottom={b[3]} > {bot})")
            if b[2] > W - rb["width"] and b[3] > rb["y_from"] and b[1] < rb["y_to"]:
                add(k, "safe_right", f"{k}: 우측 버튼 열 침범")
        gc = (tm.get("grid_crop") or {}).get("reel")
        if gc:
            for k in TEXT_SLOTS:
                if k in boxes and (boxes[k][1] < gc["y_from"] or boxes[k][3] > gc["y_to"]):
                    add(k, "grid", f"{k}: 프로필 그리드 크롭({gc['y_from']}~{gc['y_to']}) 밖")
            gm = float(gc.get("min_margin", 0) or 0)
            if gm:   # 크롭 안에 들어오는 글자는 크롭 가장자리에서 min_margin 이상 떨어져야 그리드 썸네일에서 답답하지 않다
                for k, b in boxes.items():
                    if b[3] <= gc["y_from"] or b[1] >= gc["y_to"]:
                        continue
                    if gc["y_from"] <= b[1] < gc["y_from"] + gm - 0.5:
                        add(k, "grid_edge", f"{k}: 그리드 크롭 위 가장자리와 {b[1] - gc['y_from']:.0f}px (기준 {gm:.0f}px)")
                    elif gc["y_to"] - gm + 0.5 < b[3] <= gc["y_to"]:
                        add(k, "grid_edge", f"{k}: 그리드 크롭 아래 가장자리와 {gc['y_to'] - b[3]:.0f}px (기준 {gm:.0f}px)")
    else:
        m = tm["safe_zones"]["feed"]["margin"]
        for k, b in boxes.items():
            if b[0] < m - 0.5 or b[1] < m - 0.5 or b[2] > W - m + 0.5 or b[3] > H - m + 0.5:
                add(k, "safe_margin", f"{k}: 여백({m}px) 침범")
    if ".text" in blocks:
        for other in (".tag", ".mascot", ".credit"):
            if other in blocks and _overlap(blocks[".text"], blocks[other], 16):
                add("text", "overlap", f"본문 텍스트가 {other} 와 겹침")
    if ".credit" in blocks and ".tag" in blocks and _overlap(blocks[".credit"], blocks[".tag"], 12):
        add("credit", "overlap", "출처 표기가 칩·날짜와 겹침")

    lg = slots.get("logo")
    if lg and lg["text"].strip() and len([l for l in lg["lines"] if l["text"].strip()]) > 1:
        add("logo", "badge_wrap", f"로고 대신 넣은 글자 '{lg['text']}' 가 배지 안에서 줄바꿈됨")

    max_lines = {**DEFAULT_MAX_LINES, **(get(checks, "render.max_lines", {}) or {})}
    for k in TEXT_SLOTS:
        s = slots.get(k)
        if not s:
            continue
        lines = [l for l in s["lines"] if l["text"].strip()]
        if s.get("overflow"):
            add(k, "overflow", f"{k}: 가로 넘침")
        if len(lines) > max_lines.get(k, 2):
            add(k, "too_many_lines", f"{k}: {len(lines)}줄 (최대 {max_lines.get(k, 2)})")
        if len(lines) >= 2:
            last = lines[-1]["text"].strip()
            if len(last.replace(" ", "")) <= 2:
                add(k, "widow", f"{k}: 마지막 줄에 '{last}' 만 남음")
            t, n = s["text"], 0
            for ln in s["lines"][:-1]:
                n += len(ln["text"])
                if 0 < n < len(t) and t[n - 1] not in " ·,→/)" and t[n] not in " ·,→/(":
                    add(k, "mid_word", f"{k}: 어절 중간에서 줄이 바뀜 ('{t[max(0, n - 3):n]}|{t[n:n + 3]}')")
    minc = float(get(checks, "render.min_contrast", DEFAULT_MIN_CONTRAST))
    for k in TEXT_SLOTS + ["date", "credit"]:
        s = slots.get(k)
        if not s:
            continue
        worst = None
        for ln in s["lines"]:
            cr = contrast_of(img, bg_img if bg_img is not None else img, ln["box"], s["color"])
            if cr is not None:
                worst = cr if worst is None else min(worst, cr)
        if worst is not None and worst < minc:
            add(k, "contrast", f"{k}: 대비 {worst}:1 (기준 {minc}:1)")
    return issues


# ---------------------------------------------------------------- 보정
class Adjust:
    def __init__(self):
        self.scale = {}            # sel → 배율
        self.base = {}             # sel → 원래 px
        self.balance = set()
        self.shadow = set()
        self.opaque = set()
        self.top_scrim = 0.0
        self.top_shift = 0.0       # 칩·날짜·출처를 아래로 내리는 px (그리드 가장자리 여백)
        self.badge_fix = False
        self.photo_brightness = 1.0
        self.log: list[str] = []

    def css(self) -> str:
        out = []
        for sel, sc in self.scale.items():
            if sel in self.base:
                out.append(f"{sel}{{font-size:{self.base[sel] * sc:.1f}px !important}}")
        for sel in self.balance:
            out.append(f"{sel}{{text-wrap:balance}}")
        for sel in self.shadow:
            out.append(f"{sel}{{text-shadow:0 2px 6px rgba(0,0,0,.75),0 0 2px rgba(0,0,0,.6)}}")
        for sel in self.opaque:
            out.append(f"{sel}{{color:rgba(255,255,255,.95) !important}}")
        if self.top_shift > 0:
            out.append(f".tag,.credit{{transform:translateY({self.top_shift:.0f}px)}}")
        if self.badge_fix:
            out.append(".mk{width:auto !important;min-width:48px;padding:0 8px;white-space:nowrap;font-size:13px !important}")
        if self.top_scrim > 0:
            out.append(".photo{position:absolute}.photo::after{content:'';position:absolute;left:0;right:0;top:0;"
                       f"height:520px;background:linear-gradient(rgba(0,0,0,{self.top_scrim:.2f}),rgba(0,0,0,0))}}")
        if self.photo_brightness < 1:
            out.append(f".photo{{filter:brightness({self.photo_brightness:.2f})}}")
        return "\n".join(out)

    def plan(self, issues: list[dict], data: dict, variant: str) -> bool:
        for k in TEXT_SLOTS + ["credit"]:
            s = data["slots"].get(k)
            sel = SLOT_SEL[k]
            if s and sel not in self.base:
                self.base[sel] = s["fontSize"]
        changed = False

        def shrink(k, f, why):
            nonlocal changed
            sel = SLOT_SEL.get(k)
            if not sel or sel not in self.base:
                return
            if self.scale.get(sel, 1.0) * f < 0.6:
                return
            self.scale[sel] = self.scale.get(sel, 1.0) * f
            self.log.append(f"{k} 글자 {round((1 - f) * 100)}% 축소 ({why})")
            changed = True

        codes = {(i["target"], i["code"]) for i in issues}
        block_level = any(c in ("safe_top", "grid", "grid_edge", "overlap", "safe_margin") and t in TEXT_SLOTS + ["text"]
                          for t, c in codes)
        if block_level:
            for k in TEXT_SLOTS:
                shrink(k, 0.9, "세이프존·겹침")
        done = set()
        for t, c in codes:
            if t in TEXT_SLOTS and c in ("overflow", "too_many_lines", "safe_right", "safe_bottom") and t not in done \
                    and not block_level:
                shrink(t, 0.9, c)
                done.add(t)
            if t in TEXT_SLOTS and c in ("widow", "mid_word"):
                sel = SLOT_SEL[t]
                if sel not in self.balance:
                    self.balance.add(sel)
                    self.log.append(f"{t} 줄바꿈 균형 맞춤")
                    changed = True
                elif t not in done:
                    shrink(t, 0.94, c)
                    done.add(t)
            if t == "credit" and c == "overlap":
                shrink("credit", 0.85, "겹침")
        tm_gc = None
        for i in issues:
            if i["code"] == "grid_edge" and i["target"] in ("chip", "date", "credit") and self.top_shift < 80:
                tm_gc = True
        if tm_gc:
            self.top_shift += 20
            self.log.append(f"칩·날짜·출처 {self.top_shift:.0f}px 아래로 (그리드 가장자리 여백)")
            changed = True
        if ("logo", "badge_wrap") in codes and not self.badge_fix:
            self.badge_fix = True
            self.log.append("로고 대체 글자 한 줄로 (배지 줄바꿈)")
            changed = True
        for t, c in codes:
            if c == "contrast" and t in ("date", "credit"):
                sel = ".date" if t == "date" else ".credit"
                if sel not in self.shadow:
                    self.shadow.add(sel)
                    self.log.append(f"{t} 글자 그림자 추가 (대비)")
                    changed = True
                elif sel not in self.opaque:
                    self.opaque.add(sel)
                    self.log.append(f"{t} 글자 불투명하게 (대비)")
                    changed = True
                elif variant in ("A", "B") and self.top_scrim < 0.6:
                    self.top_scrim = round(self.top_scrim + 0.3, 2)
                    self.log.append(f"사진 상단 어둡게 {self.top_scrim} (칩·출처 대비)")
                    changed = True
        if any(c == "contrast" and t in TEXT_SLOTS for t, c in codes) and variant in ("A", "B")                 and self.photo_brightness > 0.55:
            self.photo_brightness = round(self.photo_brightness * 0.8, 2)
            self.log.append(f"사진 밝기 {self.photo_brightness} 로 낮춤 (대비)")
            changed = True
        return changed


MEASURE_ON = """() => { const s = document.createElement('style'); s.id = 'ia-measure';
  s.textContent = '[data-slot]{color:transparent !important}'; document.head.append(s); }"""
MEASURE_OFF = "() => document.getElementById('ia-measure').remove()"

GUIDE_JS = r"""
(boxes) => {
  document.body.classList.add('show-guides');
  const cv = document.querySelector('.canvas');
  cv.classList.add('show-guides');
  for (const [label, b, color] of boxes) {
    const d = document.createElement('div');
    d.className = 'ia-box';
    d.style.cssText = `position:absolute;left:${b[0]}px;top:${b[1]}px;width:${b[2]-b[0]}px;height:${b[3]-b[1]}px;` +
      `outline:3px solid ${color};z-index:99;pointer-events:none`;
    const t = document.createElement('span');
    t.textContent = label;
    t.style.cssText = `position:absolute;left:0;top:-30px;font:700 22px sans-serif;color:#fff;background:${color};padding:1px 6px`;
    d.append(t); cv.append(d);
  }
}
"""


async def _render_one(browser, c, tm, kind, variant, slots, brand, bfont, photo_src, out_dir: Path,
                      checks, attempts: int, adjust_enabled: bool) -> dict:
    tname = "reel_9x16" if kind == "reel" else "feed_4x5"
    t = tm["templates"][tname]
    page = await render.open_template(browser, Path(c["templates_dir"]), t["file"], t["size"], bfont)
    crop_info = None
    slots = dict(slots)
    if variant != "C" and photo_src:
        await render.render_slots(page, variant, brand, {**slots, "photo": ""})
        pb = (await render.collect(page, []))["blocks"].get(".photo")
        if pb:
            bw, bh = int(round(pb[2] - pb[0])), int(round(pb[3] - pb[1]))
            band = get(checks, "image.subject_band", list(smart_crop.DEFAULT_BAND))
            crop_out = out_dir / f"photo_{kind}.jpg"
            crop_info = smart_crop.smart_crop(Path(photo_src), crop_out, bw, bh, int(round(pb[1])), t["size"][1], tuple(band))
            slots["photo"] = paths.file_url(crop_out) + f"?v={crop_out.stat().st_mtime_ns}"
    adj = Adjust()
    history = []
    issues: list[dict] = []
    png = b""
    data = {}
    for attempt in range(1, attempts + 2):
        await render.set_adjust_css(page, adj.css())
        await render.render_slots(page, variant, brand, slots)
        data = await render.collect(page, ALL_SLOTS)
        png = await page.locator(".canvas").first.screenshot()
        img = np.array(Image.open(io.BytesIO(png)).convert("RGB"))
        await page.evaluate(MEASURE_ON)
        bg_png = await page.locator(".canvas").first.screenshot()
        await page.evaluate(MEASURE_OFF)
        bg_img = np.array(Image.open(io.BytesIO(bg_png)).convert("RGB"))
        issues = evaluate(data, tm, kind, img, checks, bg_img)
        history.append({"attempt": attempt, "issues": [i["msg"] for i in issues]})
        if not issues or not adjust_enabled or attempt > attempts:
            break
        if not adj.plan(issues, data, variant):
            break
    name = "card.png" if kind == "reel" else "card_feed.png"
    (out_dir / name).write_bytes(png)
    files = {"card": str(out_dir / name)}
    if kind == "reel":
        gc = (tm.get("grid_crop") or {}).get("reel")
        if gc:
            Image.open(io.BytesIO(png)).crop((0, gc["y_from"], t["size"][0], gc["y_to"])).save(out_dir / "card_grid.png")
            files["grid"] = str(out_dir / "card_grid.png")
        boxes = [[k, v["box"], "#00E5FF"] for k, v in data["slots"].items()]
        if crop_info and crop_info.get("subject_canvas"):
            boxes.append(["피사체", crop_info["subject_canvas"], "#7CFF4F"])
            bt, bb = crop_info["band_px"]
            boxes.append(["피사체 대역", [4, bt, t["size"][0] - 4, bb], "#FFB800"])
        await page.evaluate(GUIDE_JS, boxes)
        await render.capture(page, out_dir / "card_guide.png")
        files["guide"] = str(out_dir / "card_guide.png")
    await page.close()
    return {"kind": kind, "variant": variant, "ok": not issues, "issues": issues, "attempts": history,
            "adjustments": adj.log, "adjust_css": adj.css(), "crop": crop_info, "files": files}


async def render_cards(c, profile, hook, variant, out_dir: Path, photo_src: str | None = None, credit: str = "",
                       feed: bool = False, attempts: int = 3, adjust_enabled: bool = True,
                       date: dt.date | None = None) -> dict:
    from playwright.async_api import async_playwright

    tm, checks = c["template_meta"], c["checks"]
    slots, brand, bfont = card_inputs(c, profile, hook, variant, credit, "", date)
    out_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await render.launch(pw)
        try:
            kinds = ["reel"] + (["feed"] if feed else [])
            results = await asyncio.gather(*[
                _render_one(browser, c, tm, k, variant, slots, brand, bfont, photo_src, out_dir, checks,
                            attempts, adjust_enabled) for k in kinds])
        finally:
            await browser.close()
    return {"rules_version": c["version"], "variant": variant, "brand": brand,
            "results": {r["kind"]: r for r in results}, "ok": all(r["ok"] for r in results)}


def pick_variant(meta: dict, c: dict, cli: str | None) -> str:
    v = cli or meta.get("template") or "A"            # 기본은 A(마커형). 이용자가 고르면 그 템플릿
    if v != "C" and not (meta.get("photo") or {}).get("path"):
        return "C"
    return v


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("job", type=Path)
    ap.add_argument("--variant", choices=["A", "B", "C"])
    ap.add_argument("--feed", action="store_true", help="피드 4:5 표지도 렌더")
    ap.add_argument("--attempts", type=int, default=3)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    meta = paths.read_json(a.job / "meta.json")
    if not meta:
        print(f"✘ meta.json 없음: {a.job}")
        return 2
    if not meta.get("hook"):
        print("✘ 선택된 훅이 없습니다 (job.py select-hook 먼저)")
        return 2
    profile = cfgmod.load_profile(meta["handle"])
    c = compose(profile, (meta.get("rules") or {}).get("version"))
    variant = pick_variant(meta, c, a.variant)
    photo = dict(meta.get("photo") or {})
    if photo.get("path") and not Path(photo["path"]).is_file():
        # 데이터 폴더를 옮겼거나 상대 경로로 저장된 경우 — 작업 폴더 안의 원본 사진으로
        local = sorted(a.job.glob("photo_original.*"))
        if local:
            photo["path"] = str(local[0])
    date = dt.date.fromisoformat(a.job.parent.parent.name) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", a.job.parent.parent.name) else None
    rep = asyncio.run(render_cards(c, profile, meta["hook"], variant, a.job, photo.get("path") if variant != "C" else None,
                                   photo.get("credit", ""), a.feed, a.attempts, True, date))
    meta["template"] = variant
    meta["card"] = {"variant": variant, "brand": rep["brand"], "ok": rep["ok"],
                    "results": {k: {kk: v[kk] for kk in ("ok", "issues", "adjustments", "files", "crop")}
                                for k, v in rep["results"].items()},
                    "rendered_at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    meta.setdefault("stages", {})["card"] = "waiting_user"
    paths.write_json(a.job / "meta.json", meta)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        for k, r in rep["results"].items():
            print(f"{'✔' if r['ok'] else '⚠'} {k} 카드 (variant {variant}) — 시도 {len(r['attempts'])}회")
            for adj in r["adjustments"]:
                print(f"  · 보정: {adj}")
            for i in r["issues"]:
                print(f"  ✘ {i['msg']}")
            for name, f in r["files"].items():
                print(f"  {name}: {f}")
            if r.get("crop"):
                cr = r["crop"]
                print(f"  사진 크롭: 얼굴 {cr['faces']}개, 피사체 {cr['subject_kind'] or '중앙'}"
                      + ("" if cr["subject_in_band"] is None else f", 대역 안 {'예' if cr['subject_in_band'] else '아니오'}")
                      + (" · ⚠ 원본 해상도 부족(확대됨)" if cr["upscaled"] else ""))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
