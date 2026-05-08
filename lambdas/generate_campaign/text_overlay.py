"""
text_overlay.py
---------------
Composites headline + subheadline onto generated images.

Layout (bottom of image):
  ┌────────────────────────────────────┐
  │                                    │  ← dark semi-transparent bar
  │          HEADLINE                  │  ← 8–15% of height (target 12%)
  │      six word subheadline          │  ← 3–6% of height  (target 4%)
  │                                    │
  └────────────────────────────────────┘

Font selection by language:
  Japanese / Korean / Chinese → NotoSansJP-Bold.ttf  (CJK glyph support)
  All other languages         → RobotoSlab-Bold.ttf
"""
import io
import os

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

from config import s3, OUTPUTS_BUCKET, PRESIGNED_TTL

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")

CJK_LANGUAGES = {"ja", "ko", "zh", "zh-hans", "zh-hant", "zh-cn", "zh-tw"}

FONTS = {
    "cjk":     os.path.join(ASSETS_DIR, "NotoSansJP-Bold.ttf"),
    "default": os.path.join(ASSETS_DIR, "RobotoSlab-Bold.ttf"),
}


# ── Font helpers ───────────────────────────────────────────────────────────────

def _pick_font(language: str, size: int) -> "ImageFont.FreeTypeFont":
    lang = language.lower().split("-")[0]
    path = FONTS["cjk"] if lang in CJK_LANGUAGES else FONTS["default"]
    if not os.path.exists(path):
        path = next((p for p in FONTS.values() if os.path.exists(p)), None)
    try:
        return ImageFont.truetype(path, size) if path else ImageFont.load_default()
    except (IOError, OSError):
        return ImageFont.load_default()


def _headline_size(h: int) -> int:
    """8–15% of image height, target 12%."""
    return max(int(h * 0.08), min(int(h * 0.12), int(h * 0.15)))


def _subheadline_size(h: int) -> int:
    """3–6% of image height, target 4%."""
    return max(int(h * 0.03), min(int(h * 0.04), int(h * 0.06)))


def _text_width(draw, text, font) -> int:
    return draw.textbbox((0, 0), text, font=font)[2]


def _stroke_text(draw, x, y, text, font, fill=(255, 255, 255, 255),
                 stroke_fill=(0, 0, 0, 220), stroke_width=2):
    """Draws text with a solid stroke for legibility over any background."""
    for dx in range(-stroke_width, stroke_width + 1):
        for dy in range(-stroke_width, stroke_width + 1):
            if dx != 0 or dy != 0:
                draw.text((x + dx, y + dy), text, font=font, fill=stroke_fill)
    draw.text((x, y), text, font=font, fill=fill)


# ── Core compositing ───────────────────────────────────────────────────────────

def _composite(image_bytes: bytes, headline: str, subheadline: str,
               language: str) -> bytes:
    """
    Renders headline + subheadline onto the image.

    Headline    : 1 word,  12% of height, uppercase, centred
    Subheadline : 6 words,  4% of height, sentence case, centred
    """
    if not PIL_AVAILABLE:
        return image_bytes

    texts = [t for t in [headline, subheadline] if t and t.strip()]
    if not texts:
        return image_bytes

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        w, h = img.size

        hl_size  = _headline_size(h)
        sub_size = _subheadline_size(h)

        hl_font  = _pick_font(language, hl_size)
        sub_font = _pick_font(language, sub_size)

        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw    = ImageDraw.Draw(overlay)

        # Row heights
        hl_row_h   = hl_size  + int(hl_size  * 0.20)
        sub_row_h  = sub_size + int(sub_size * 0.20)

        # Vertical padding inside the bar
        v_pad = int(hl_size * 0.35)

        # Total bar height
        n_rows  = (1 if headline else 0) + (1 if subheadline else 0)
        bar_h   = hl_row_h + (sub_row_h if subheadline else 0) + v_pad * 2
        bar_top = h - bar_h - int(h * 0.025)

        # Draw bar
        draw.rectangle([(0, bar_top), (w, bar_top + bar_h)], fill=(0, 0, 0, 210))

        y = bar_top + v_pad

        # Headline (1 word, large)
        if headline:
            hl_text = headline.upper()
            x = (w - _text_width(draw, hl_text, hl_font)) // 2
            _stroke_text(draw, x, y, hl_text, hl_font, stroke_width=2)
            y += hl_row_h

        # Subheadline (6 words, smaller)
        if subheadline:
            x = (w - _text_width(draw, subheadline, sub_font)) // 2
            # If subheadline is too wide, reduce font size down to 3% floor
            while x < int(w * 0.05) and sub_size > int(h * 0.03):
                sub_size -= max(1, int(sub_size * 0.1))
                sub_font = _pick_font(language, sub_size)
                x = (w - _text_width(draw, subheadline, sub_font)) // 2
            _stroke_text(draw, x, y, subheadline, sub_font,
                         fill=(220, 220, 220, 255), stroke_width=1)

        buf = io.BytesIO()
        Image.alpha_composite(img, overlay).convert("RGB").save(buf, format="PNG")
        return buf.getvalue()

    except Exception as exc:
        print(f"TextOverlay failed: {exc}")
        return image_bytes


# ── Public API ─────────────────────────────────────────────────────────────────

def overlay(images: dict, headline: str, language: str = "en",
            subheadline: str = "") -> dict:
    """
    Reads each ratio image from S3, composites headline + subheadline, writes back.
    """
    result = {}
    for ratio, meta in images.items():
        s3_key = meta.get("s3_key", "") if isinstance(meta, dict) else ""
        if not s3_key or not headline or not PIL_AVAILABLE:
            result[ratio] = meta
            continue

        try:
            raw   = s3.get_object(Bucket=OUTPUTS_BUCKET, Key=s3_key)["Body"].read()
            final = _composite(raw, headline, subheadline, language)

            s3.put_object(Bucket=OUTPUTS_BUCKET, Key=s3_key,
                          Body=final, ContentType="image/png")
            url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": OUTPUTS_BUCKET, "Key": s3_key},
                ExpiresIn=PRESIGNED_TTL,
            )
            result[ratio] = {**meta, "url": url, "composited": True}

        except Exception as exc:
            print(f"TextOverlay {ratio} failed: {exc}")
            result[ratio] = meta

    return result
