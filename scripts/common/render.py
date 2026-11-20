"""Playwright 렌더 공통 — CLIENT_SPEC 8.3 render_contract 를 따른다.

1. 뷰포트 = 템플릿 크기, deviceScaleFactor 1
2. HTML(file://) → await window.renderSlots({variant, brand, slots})
3. body[data-ready="1"] 대기
4. .canvas 만 캡처

폰트는 번들에 없으므로 클라이언트가 @font-face 를 주입한다(플러그인 assets/fonts, 계정 assets/fonts).
"""
from __future__ import annotations

from pathlib import Path

from . import paths

PRETENDARD = paths.FONTS_DIR / "PretendardVariable.woff2"
BRAND_FONT_FAMILY = "IABrandFont"

COLLECT_JS = r"""
(keys) => {
  const cv = document.querySelector('.canvas').getBoundingClientRect();
  const rel = b => [b.left - cv.left, b.top - cv.top, b.right - cv.left, b.bottom - cv.top].map(v => Math.round(v * 10) / 10);
  const visible = el => el && !el.closest('[data-empty]') && getComputedStyle(el).display !== 'none'
                        && getComputedStyle(el).visibility !== 'hidden';
  const lines = el => {
    // 글자마다 위치를 재서 줄로 묶는다
    const out = [];
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      for (let i = 0; i < node.data.length; i++) {
        const r = document.createRange(); r.setStart(node, i); r.setEnd(node, i + 1);
        const rs = [...r.getClientRects()].filter(x => x.width > 0 || x.height > 0);
        if (!rs.length) continue;
        const b = rs[0];
        const ch = node.data[i];
        let ln = out.find(l => Math.abs(l.cy - (b.top + b.bottom) / 2) < b.height * 0.5);
        if (!ln) { ln = {cy: (b.top + b.bottom) / 2, text: '', box: [b.left, b.top, b.right, b.bottom]}; out.push(ln); }
        ln.text += ch;
        ln.box = [Math.min(ln.box[0], b.left), Math.min(ln.box[1], b.top), Math.max(ln.box[2], b.right), Math.max(ln.box[3], b.bottom)];
      }
    }
    out.sort((a, b) => a.cy - b.cy);
    return out.map(l => ({text: l.text, box: rel({left: l.box[0], top: l.box[1], right: l.box[2], bottom: l.box[3]})}));
  };
  const res = {canvas: [cv.width, cv.height], slots: {}, blocks: {}};
  for (const key of keys) {
    const el = document.querySelector(`[data-slot="${key}"]`);
    if (!visible(el)) continue;
    const cs = getComputedStyle(el);
    const host = key === 'l2' ? el : el;
    res.slots[key] = {
      text: el.textContent, box: rel(el.getBoundingClientRect()), lines: lines(el),
      color: cs.color, fontSize: parseFloat(cs.fontSize),
      overflow: host.scrollWidth > host.clientWidth + 1 && cs.display !== 'inline',
    };
  }
  for (const sel of ['.text', '.tag', '.mascot', '.photo', '.handle', '.credit', '.mk']) {
    const el = document.querySelector(sel);
    if (visible(el)) res.blocks[sel] = rel(el.getBoundingClientRect());
  }
  return res;
}
"""


def font_css(brand_font: Path | None = None) -> str:
    css = (f"@font-face{{font-family:'Pretendard';src:url('{paths.file_url(PRETENDARD)}') format('woff2');"
           "font-weight:45 920;font-style:normal;font-display:block;}\n")
    if brand_font and brand_font.is_file():
        fmt = {".woff2": "woff2", ".woff": "woff", ".otf": "opentype", ".ttf": "truetype"}.get(brand_font.suffix.lower(), "truetype")
        css += (f"@font-face{{font-family:'{BRAND_FONT_FAMILY}';src:url('{paths.file_url(brand_font)}') format('{fmt}');"
                "font-weight:100 900;font-display:block;}\n")
    return css


def brand_with_font(brand: dict, brand_font: Path | None) -> dict:
    b = dict(brand)
    if brand_font and brand_font.is_file():
        b["font"] = f"'{BRAND_FONT_FAMILY}','Pretendard','Noto Sans KR',sans-serif"
    return b


async def launch(pw):
    return await pw.chromium.launch(args=["--font-render-hinting=none", "--disable-lcd-text"])


async def open_template(browser, templates_dir: Path, file: str, size, brand_font: Path | None = None):
    page = await browser.new_page(viewport={"width": int(size[0]), "height": int(size[1])}, device_scale_factor=1)
    await page.goto(paths.file_url(Path(templates_dir) / file))
    await page.add_style_tag(content=font_css(brand_font))
    await page.evaluate("""() => { const s = document.createElement('style'); s.id = 'ia-adjust';
        document.head.append(s); }""")   # 자동 보정 CSS 자리
    await page.evaluate("""async (fam) => { for (const w of [500,600,700,800,900]) {
        try { await document.fonts.load(`${w} 40px ${fam}`, '가A1'); } catch (e) {} } }""",
                        f"'{BRAND_FONT_FAMILY}'" if brand_font and brand_font.is_file() else "Pretendard")
    return page


async def set_adjust_css(page, css: str) -> None:
    await page.evaluate("(css) => { document.getElementById('ia-adjust').textContent = css; }", css)


async def render_slots(page, variant: str, brand: dict, slots: dict) -> None:
    await page.evaluate("() => document.body.removeAttribute('data-ready')")
    await page.evaluate("(d) => window.renderSlots(d)", {"variant": variant, "brand": brand, "slots": slots})
    await page.wait_for_selector('body[data-ready="1"]', state="attached", timeout=30000)


async def capture(page, out: Path) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    await page.locator(".canvas").first.screenshot(path=str(out), animations="allow", omit_background=False)
    return out


async def collect(page, keys) -> dict:
    return await page.evaluate(COLLECT_JS, list(keys))
