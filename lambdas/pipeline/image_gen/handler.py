import base64
import boto3
import concurrent.futures
import json
import os
import random
import re
import time

s3 = boto3.client("s3")
bedrock = boto3.client("bedrock-runtime")

ASSETS_BUCKET = os.environ["ASSETS_BUCKET"]
OUTPUTS_BUCKET = os.environ["OUTPUTS_BUCKET"]
MODEL_ID = os.environ.get("MODEL_ID", "amazon.nova-canvas-v1:0")
PRESIGNED_TTL = 604800  # 7 days

IMAGE_RATIOS = {
    "1x1":  {"width": 1024, "height": 1024, "platform": "Instagram"},
    "9x16": {"width": 720,  "height": 1280, "platform": "TikTok/Reels"},
    "16x9": {"width": 1280, "height": 720,  "platform": "YouTube"},
}


def _slugify(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9\-_]", "-", text.strip()).strip("-")


def _get_reference_image_b64(product_name: str) -> str | None:
    """Fetches the first product photo from assets-input as base64 for IMAGE_VARIATION."""
    prefix = f"products/{_slugify(product_name)}/"
    objs = s3.list_objects_v2(Bucket=ASSETS_BUCKET, Prefix=prefix)
    if "Contents" not in objs:
        return None
    key = objs["Contents"][0]["Key"]
    obj = s3.get_object(Bucket=ASSETS_BUCKET, Key=key)
    return base64.b64encode(obj["Body"].read()).decode()


def _generate_one(
    ratio: str,
    dims: dict,
    image_prompt: str,
    product_name: str,
    campaign_id: str,
    ref_b64: str | None,
) -> tuple[str, dict]:
    """
    Generates one image at the given ratio.
    Uses IMAGE_VARIATION (product photo seed) when a reference exists,
    falls back to TEXT_IMAGE otherwise.
    Retries 3× with exponential backoff + jitter for throttling.
    """
    if ref_b64:
        body = json.dumps({
            "taskType": "IMAGE_VARIATION",
            "imageVariationParams": {
                "text": image_prompt,
                "images": [ref_b64],
                "similarityStrength": 0.7,
            },
            "imageGenerationConfig": {
                "numberOfImages": 1,
                "width": dims["width"],
                "height": dims["height"],
                "cfgScale": 8.0,
                "quality": "standard",
            },
        })
        mode = "variation"
    else:
        body = json.dumps({
            "taskType": "TEXT_IMAGE",
            "textToImageParams": {"text": image_prompt},
            "imageGenerationConfig": {
                "numberOfImages": 1,
                "width": dims["width"],
                "height": dims["height"],
                "cfgScale": 8.0,
                "quality": "standard",
            },
        })
        mode = "text"

    for attempt in range(3):
        try:
            time.sleep((2 ** attempt) + random.uniform(0, 1))
            resp = bedrock.invoke_model(
                modelId=MODEL_ID,
                body=body,
                contentType="application/json",
                accept="application/json",
            )
            result = json.loads(resp["body"].read())
            image_bytes = base64.b64decode(result["images"][0])

            s3_key = f"outputs/{campaign_id}/{_slugify(product_name)}/{ratio}.png"
            s3.put_object(
                Bucket=OUTPUTS_BUCKET,
                Key=s3_key,
                Body=image_bytes,
                ContentType="image/png",
            )
            url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": OUTPUTS_BUCKET, "Key": s3_key},
                ExpiresIn=PRESIGNED_TTL,
            )
            return ratio, {
                "s3_key": s3_key,
                "url": url,
                "ratio": ratio,
                "platform": dims["platform"],
                "mode": mode,
            }
        except Exception as exc:
            if attempt == 2:
                raise exc

    raise RuntimeError(f"Image generation failed for ratio {ratio} after 3 attempts")


def handler(event, context):
    """
    State: ImageGen
    Generates all 3 aspect ratio images concurrently via ThreadPoolExecutor.

    IMAGE_VARIATION mode: product reference photo from assets-input used as
    structure/color seed — outputs are brand-anchored by design.
    TEXT_IMAGE fallback: when no reference photo found in assets-input.

    Input:  full state — uses $.campaign_id, $.product_name, $.parallel_results[0].image_prompt
    Output: { "1x1": {...}, "9x16": {...}, "16x9": {...} }  →  written to $.images
    """
    campaign_id = event["campaign_id"]
    product_name = event["product_name"]

    parallel_results = event.get("parallel_results", [])
    image_prompt = (
        parallel_results[0].get("image_prompt", f"Professional product photo of {product_name}")
        if parallel_results
        else f"Professional product photo of {product_name}"
    )

    # _ratios: optional comma-separated allowlist e.g. "1x1" or "1x1,9x16"
    # Used by quick_test.sh to limit generation to a single ratio ($0.08 vs $0.24)
    ratios_filter = event.get("_ratios", "")
    allowed = set(ratios_filter.split(",")) if ratios_filter else set(IMAGE_RATIOS.keys())
    active_ratios = {k: v for k, v in IMAGE_RATIOS.items() if k in allowed}

    ref_b64 = _get_reference_image_b64(product_name)

    images = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(active_ratios)) as pool:
        futures = {
            pool.submit(_generate_one, ratio, dims, image_prompt, product_name, campaign_id, ref_b64): ratio
            for ratio, dims in active_ratios.items()
        }
        for future in concurrent.futures.as_completed(futures):
            ratio, result = future.result()
            images[ratio] = result

    return images
