import base64
import concurrent.futures
import json
import re

from config import s3, OUTPUTS_BUCKET, IMAGE_RATIOS, PRESIGNED_TTL, get_google_client

IMAGEN_MODEL  = "imagen-4.0-fast-generate-001"
ASPECT_RATIOS = {"1x1": "1:1", "9x16": "9:16", "16x9": "16:9"}


def _slugify(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9\-_]", "-", text.strip()).strip("-")


def _generate_one(ratio: str, image_prompt: str,
                  campaign_id: str, product_name: str) -> tuple[str, dict]:
    from google.genai import types

    client = get_google_client()
    resp = client.models.generate_images(
        model=IMAGEN_MODEL,
        prompt=image_prompt,
        config=types.GenerateImagesConfig(
            number_of_images=1,
            aspect_ratio=ASPECT_RATIOS[ratio],
        ),
    )
    image_bytes = resp.generated_images[0].image.image_bytes

    s3_key = f"outputs/{campaign_id}/{_slugify(product_name)}/{ratio}.png"
    s3.put_object(Bucket=OUTPUTS_BUCKET, Key=s3_key,
                  Body=image_bytes, ContentType="image/png")
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": OUTPUTS_BUCKET, "Key": s3_key},
        ExpiresIn=PRESIGNED_TTL,
    )
    return ratio, {
        "s3_key": s3_key, "url": url,
        "ratio": ratio, "platform": IMAGE_RATIOS[ratio]["platform"],
        "mode": "imagen4",
    }


def generate(image_prompt: str, data: dict) -> dict:
    """
    Generates all active ratio images concurrently using Imagen 4 Fast.
    Aspect ratios map exactly: 1:1, 9:16, 16:9 — no approximation.
    _ratios field limits to a subset (used by quick_test.sh).
    """
    campaign_id  = data["campaign_id"]
    product_name = data["product_name"]

    ratios_filter = data.get("_ratios", "")
    allowed = set(ratios_filter.split(",")) if ratios_filter else set(IMAGE_RATIOS.keys())
    active  = [r for r in IMAGE_RATIOS if r in allowed]

    images = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(active)) as pool:
        futures = {
            pool.submit(_generate_one, ratio, image_prompt, campaign_id, product_name): ratio
            for ratio in active
        }
        for future in concurrent.futures.as_completed(futures):
            ratio, result = future.result()
            images[ratio] = result

    return images
