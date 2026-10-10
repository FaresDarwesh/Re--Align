"""Generate every brand asset for re-align from one source of truth.

The mark: two chevrons that sit out of line and snap onto a shared axis —
"re-align" drawn rather than written. The left chevron is ink, the right one
is the accent colour; where they meet is the product.

Everything is drawn programmatically (no vision, no external assets) and the
palette lives here so the SVGs, the PNGs and the CSS can never drift apart.
"""
from __future__ import annotations

import pathlib

from PIL import Image, ImageDraw

STATIC = pathlib.Path(__file__).resolve().parent.parent / "realign" / "web" / "static"

INK = (17, 18, 20)
ACCENT = (196, 62, 20)          # vermillion — the single accent
ACCENT_SOFT = (253, 240, 234)
PAPER = (252, 252, 250)

# ------------------------------------------------------------------ the mark

MARK_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" fill="none">
  <path class="ra-l" d="M13.4 7.2 6.6 14l6.8 6.8" stroke="#111214" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>
  <path class="ra-r" d="M18.6 7.2 25.4 14l-6.8 6.8" stroke="#C43E14" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>
  <circle class="ra-dot" cx="16" cy="14" r="2.0" fill="#C43E14"/>
</svg>
"""


def mark_png(size: int, path: pathlib.Path, *, ink=INK, accent=ACCENT, bg=None) -> None:
    """Rasterise the mark at `size` (crisp: drawn directly, not upscaled)."""
    pad = max(2, round(size * 0.10))
    img = Image.new("RGBA", (size, size), bg or (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    w = size - pad * 2
    s = w / 32.0

    def pt(x: float, y: float) -> tuple[float, float]:
        return (pad + x * s, pad + y * s)

    lw = max(1.6, 2.6 * s)
    # left chevron  ‹
    d.line([pt(13.4, 7.2), pt(6.6, 14.0), pt(13.4, 20.8)], fill=ink + (255,), width=round(lw), joint="curve")
    # right chevron ›
    d.line([pt(18.6, 7.2), pt(25.4, 14.0), pt(18.6, 20.8)], fill=accent + (255,), width=round(lw), joint="curve")
    # meeting point
    r = max(1.2, 2.0 * s)
    d.ellipse([pt(16, 14)[0] - r, pt(16, 14)[1] - r, pt(16, 14)[0] + r, pt(16, 14)[1] + r], fill=accent + (255,))

    img.save(path, "PNG", optimize=True)


def wordmark_png(path: pathlib.Path, *, height: int = 96, accent: bool = True) -> None:
    """`re-align` set in the display face, printed flat — no gradients."""
    from PIL import ImageFont

    scale = 6  # supersample, then downscale = clean edges without a TTF hinting pass
    text = "re-align"
    # Inter is self-hosted as woff2; Pil needs a TTF. Fall back to a solid
    # geometric construction so the asset never depends on a download.
    font = None
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if pathlib.Path(candidate).exists():
            font = ImageFont.truetype(candidate, 62 * scale // 6)
            break
    if font is None:
        font = ImageFont.load_default()

    tmp = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    td = ImageDraw.Draw(tmp)
    box = td.textbbox((0, 0), text, font=font)
    w = box[2] - box[0]
    h = box[3] - box[1]

    W = (w + 40 * scale // 6) * 1
    H = h + 30 * scale // 6
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.text((20 * scale // 6 - box[0], 15 * scale // 6 - box[1]), text, font=font, fill=INK + (255,))
    if accent:
        # colour the hyphen
        hx = d.textbbox((0, 0), "re-", font=font)[2] - box[0] + 20 * scale // 6
        hy = 15 * scale // 6
        d.line([(hx - 6 * scale // 6, hy + h * 0.52), (hx + 8 * scale // 6, hy + h * 0.52)],
               fill=ACCENT + (255,), width=max(2, 5 * scale // 6))
    img = img.resize((max(1, W // (scale // 6)), max(1, H // (scale // 6))), Image.LANCZOS)
    img.save(path, "PNG", optimize=True)


def og_image(path: pathlib.Path) -> None:
    """1200x630 social card: paper, the mark, the wordmark, one line of copy."""
    W, H = 1200, 630
    img = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(img)

    # hairline frame — editorial, not gradient
    d.rectangle([40, 40, W - 41, H - 41], outline=(228, 226, 220), width=2)

    # accent rule
    d.rectangle([88, 150, 148, 158], fill=ACCENT)

    # big mark, drawn large
    def pt(x: float, y: float, ox: float, oy: float, s: float) -> tuple[float, float]:
        return (ox + x * s, oy + y * s)

    s, ox, oy = 4.2, 88, 210
    lw = max(3, round(2.6 * s))
    d.line([pt(13.4, 7.2, ox, oy, s), pt(6.6, 14, ox, oy, s), pt(13.4, 20.8, ox, oy, s)],
           fill=INK, width=lw, joint="curve")
    d.line([pt(18.6, 7.2, ox, oy, s), pt(25.4, 14, ox, oy, s), pt(18.6, 20.8, ox, oy, s)],
           fill=ACCENT, width=lw, joint="curve")
    r = 2.0 * s
    cx, cy = pt(16, 14, ox, oy, s)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ACCENT)
    d.line([pt(13.4, 7.2, ox, oy, s), pt(6.6, 14, ox, oy, s)], fill=INK, width=lw)

    # headline
    from PIL import ImageFont

    font = None
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if pathlib.Path(candidate).exists():
            font = ImageFont.truetype(candidate, 68)
            break
    if font is None:
        font = ImageFont.load_default()

    d.text((250, 236), "re-align", font=font, fill=INK)
    d.text((252, 330), "The agent operating system", font=font, fill=(107, 107, 112))
    d.text((252, 404), "for your company", font=font, fill=(107, 107, 112))

    d.rectangle([88, H - 130, W - 88, H - 129], fill=(228, 226, 220))
    d.text((88, H - 108), "Nothing ships without your approval.", font=font, fill=INK)

    img.save(path, "PNG", optimize=True)


def main() -> None:
    (STATIC / "logo.svg").write_text(MARK_SVG, encoding="utf-8")
    (STATIC / "favicon.svg").write_text(MARK_SVG, encoding="utf-8")

    mark_png(512, STATIC / "logo-mark.png")
    mark_png(64, STATIC / "favicon.png", bg=PAPER + (255,))
    mark_png(32, STATIC / "favicon-32.png", bg=PAPER + (255,))
    mark_png(180, STATIC / "apple-touch-icon.png", bg=PAPER + (255,))
    wordmark_png(STATIC / "logo.png")
    wordmark_png(STATIC / "logo-light.png", accent=False)
    og_image(STATIC / "og-image.png")

    (STATIC / "manifest.webmanifest").write_text(
        """{
  "name": "re-align",
  "short_name": "re-align",
  "description": "نظام تشغيل الوكلاء الأذكياء للشركات — re-align",
  "lang": "ar",
  "dir": "rtl",
  "start_url": "/app",
  "scope": "/",
  "display": "standalone",
  "background_color": "#FCFCFA",
  "theme_color": "#FCFCFA",
  "icons": [
    { "src": "/static/apple-touch-icon.png", "sizes": "180x180", "type": "image/png", "purpose": "any" },
    { "src": "/static/favicon.png", "sizes": "64x64", "type": "image/png", "purpose": "any" },
    { "src": "/static/favicon-32.png", "sizes": "32x32", "type": "image/png", "purpose": "any" },
    { "src": "/static/logo-mark.png", "sizes": "512x512", "type": "image/png", "purpose": "any" },
    { "src": "/static/logo-mark.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable" }
  ]
}
""",
        encoding="utf-8",
    )
    print("brand assets written to", STATIC)


if __name__ == "__main__":
    main()
