import io
import os

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

from config import s3, OUTPUTS_BUCKET, PRESIGNED_TTL

FONT_PATH = os.path.join(os.path.dirname(__file__), "assets", "RobotoSlab-Bold.ttf")


def overlay(images: dict, headline: str) -> dict:
    """
    Reads each raw image from S3, composites the headline at the bottom,
    writes it back, and refreshes the presigned URL.
    Returns the same dict with updated URLs and composited=True.
    """
    result = {}
    for ratio, meta in images.items():
        s3_key = meta.get("s3_key", "")
        if not s3_key or not headline or not PIL_AVAILABLE:
            result[ratio] = meta
            continue

        try:
            raw = s3.get_object(Bucket=OUTPUTS_BUCKET, Key=s3_key)["Body"].read()
            composited = _composite(raw, headline)
            s3.put_object(Bucket=OUTPUTS_BUCKET, Key=s3_key,
                          Body=composited, ContentType="image/png")
            url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": OUTPUTS_BUCKET, "Key": s3_key},
                ExpiresIn=PRESIGNED_TTL,
            )
            result[ratio] = {**meta, "url": url, "composited": True}
        except Exception as exc:
            print(f"TextOverlay failed for {ratio}: {exc}")
            result[ratio] = meta

    return result


def _composite(image_bytes: bytes, headline: str) -> bytes:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    w, h = img.size
    font_size = max(36, min(w, h) // 12)

    try:
        font = (ImageFont.truetype(FONT_PATH, font_size)
                if os.path.exists(FONT_PATH) else ImageFont.load_default())
    except (IOError, OSError):
        font = ImageFont.load_default()

    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Word-wrap
    words, lines, current = headline.split(), [], []
    for word in words:
        test = " ".join(current + [word])
        if draw.textbbox((0, 0), test, font=font)[2] <= int(w * 0.88):
            current.append(word)
        else:
            if current:
                lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))

    line_h = font_size + int(font_size * 0.2)
    bar_h  = line_h * len(lines) + int(font_size * 0.8)
    bar_top = h - bar_h - int(h * 0.04)

    draw.rectangle([(0, bar_top), (w, bar_top + bar_h)], fill=(0, 0, 0, 185))

    y = bar_top + int(font_size * 0.4)
    for line in lines:
        lw = draw.textbbox((0, 0), line, font=font)[2]
        x = (w - lw) // 2
        draw.text((x + 2, y + 2), line, font=font, fill=(0, 0, 0, 150))
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
        y += line_h

    buf = io.BytesIO()
    Image.alpha_composite(img, overlay).convert("RGB").save(buf, format="PNG")
    return buf.getvalue()
