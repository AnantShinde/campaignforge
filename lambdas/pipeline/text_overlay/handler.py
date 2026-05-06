import boto3
import io
import os
import re

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

s3 = boto3.client("s3")
OUTPUTS_BUCKET = os.environ["OUTPUTS_BUCKET"]
PRESIGNED_TTL = 604800  # 7 days
FONT_PATH = os.path.join(os.path.dirname(__file__), "assets", "RobotoSlab-Bold.ttf")


def _wrap_text(draw: "ImageDraw.Draw", text: str, font, max_width: int) -> list[str]:
    words = text.split()
    lines, current = [], []
    for word in words:
        test = " ".join(current + [word])
        w = draw.textbbox((0, 0), test, font=font)[2]
        if w <= max_width:
            current.append(word)
        else:
            if current:
                lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))
    return lines


def _composite(image_bytes: bytes, headline: str) -> bytes:
    """
    Renders the localized headline onto the image with a semi-transparent
    dark bar at the bottom. Gracefully returns the original bytes if
    Pillow is unavailable or compositing fails.
    """
    if not PIL_AVAILABLE or not headline:
        return image_bytes

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        w, h = img.size
        font_size = max(36, min(w, h) // 12)

        try:
            font = ImageFont.truetype(FONT_PATH, font_size) if os.path.exists(FONT_PATH) else ImageFont.load_default()
        except (IOError, OSError):
            font = ImageFont.load_default()

        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        lines = _wrap_text(draw, headline, font, int(w * 0.88))
        line_h = font_size + int(font_size * 0.2)
        bar_h = line_h * len(lines) + int(font_size * 0.8)
        bar_top = h - bar_h - int(h * 0.04)

        draw.rectangle([(0, bar_top), (w, bar_top + bar_h)], fill=(0, 0, 0, 185))

        y = bar_top + int(font_size * 0.4)
        for line in lines:
            line_w = draw.textbbox((0, 0), line, font=font)[2]
            x = (w - line_w) // 2
            draw.text((x + 2, y + 2), line, font=font, fill=(0, 0, 0, 150))
            draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
            y += line_h

        buf = io.BytesIO()
        Image.alpha_composite(img, overlay).convert("RGB").save(buf, format="PNG")
        return buf.getvalue()

    except Exception as exc:
        print(f"Text overlay failed, returning original: {exc}")
        return image_bytes


def handler(event, context):
    """
    State: TextOverlay
    Reads each ratio image from S3, composites the localized headline,
    and writes the result back to the same S3 key.

    Input:  full state — uses $.images and $.parallel_results[0].ad_copy.headline
    Output: { "1x1": {...}, "9x16": {...}, "16x9": {...} }  →  written to $.composited_images
    """
    images = event.get("images", {})
    parallel_results = event.get("parallel_results", [])
    headline = (
        parallel_results[0].get("ad_copy", {}).get("headline", "")
        if parallel_results
        else ""
    )

    composited = {}
    for ratio, meta in images.items():
        s3_key = meta.get("s3_key", "")
        if not s3_key:
            composited[ratio] = meta
            continue

        raw = s3.get_object(Bucket=OUTPUTS_BUCKET, Key=s3_key)["Body"].read()
        final_bytes = _composite(raw, headline)

        s3.put_object(
            Bucket=OUTPUTS_BUCKET,
            Key=s3_key,
            Body=final_bytes,
            ContentType="image/png",
        )
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": OUTPUTS_BUCKET, "Key": s3_key},
            ExpiresIn=PRESIGNED_TTL,
        )
        composited[ratio] = {**meta, "url": url, "composited": True}

    return composited
