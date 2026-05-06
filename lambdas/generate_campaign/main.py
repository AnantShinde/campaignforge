"""
GenerateCampaign Lambda
-----------------------
Triggered by SQS (max concurrency: 10).

For each campaign brief it does, in order:

  1. CopyGen + Pass1Compliance  → run in parallel (ThreadPoolExecutor)
     - CopyGen:     GPT-4o mini writes localised ad copy + image prompt
     - Pass1:       GPT-4o mini checks the brief text for legal/brand violations
                    If overall="fail" → write compliance_blocked and stop.
                    This saves ~$0.48 by skipping Nova Canvas on bad briefs.

  2. ImageGen  → Nova Canvas generates 3 aspect-ratio images concurrently
                 (IMAGE_VARIATION when a product photo exists, TEXT_IMAGE otherwise)

  3. TextOverlay → Pillow composites the localised headline onto each image

  4. Pass2Compliance → GPT-4o mini vision reviews the rendered image + copy
                       overall="fail" → approval_status = "compliance_blocked"
                       otherwise      → approval_status = "pending_review"

  5. PersistAssets → writes the full blueprint to DynamoDB
"""

import concurrent.futures
import json
import time

from config import table
import copy_gen, compliance, image_gen, text_overlay


# ── Entry point ───────────────────────────────────────────────────────────────

def handler(event, context):
    for record in event.get("Records", []):
        data = json.loads(record["body"])
        _process(data)
    return {"statusCode": 200}


# ── Pipeline ──────────────────────────────────────────────────────────────────

def _process(data: dict) -> None:
    campaign_id  = data["campaign_id"]
    product_name = data["product_name"]

    try:
        # Step 1 — parallel: copy generation and pre-gen compliance check
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            copy_future = pool.submit(copy_gen.generate, data)
            p1_future   = pool.submit(compliance.pass1_check, data)
            copy_result = copy_future.result()
            p1_result   = p1_future.result()

        # Abort if the brief itself fails compliance — no image generation cost
        if p1_result.get("overall") == "fail":
            _save(campaign_id, product_name, data,
                  copy_result, {}, p1_result, {},
                  approval_status="compliance_blocked")
            return

        # Step 2 — generate images (3 ratios concurrently via ThreadPoolExecutor)
        images = image_gen.generate(copy_result["image_prompt"], data)

        # Step 3 — composite localised headline onto every image
        headline = copy_result.get("ad_copy", {}).get("headline", "")
        composited = text_overlay.overlay(images, headline)

        # Step 4 — vision compliance on the rendered output
        p2_result = compliance.pass2_check(data, composited, copy_result)

        # Step 5 — save blueprint; hard pass-2 fail → compliance_blocked
        approval = (
            "compliance_blocked"
            if p2_result.get("overall") == "fail"
            else "pending_review"
        )
        _save(campaign_id, product_name, data,
              copy_result, composited, p1_result, p2_result,
              approval_status=approval)

    except Exception as exc:
        print(f"[ERROR] Campaign {campaign_id}/{product_name}: {exc}")
        _save(campaign_id, product_name, data,
              {}, {}, {}, {},
              approval_status="failed",
              failure_reason=str(exc))


# ── Persistence ───────────────────────────────────────────────────────────────

def _save(campaign_id, product_name, data,
          copy_result, images, p1, p2,
          approval_status, failure_reason=""):
    ad_copy = copy_result.get("ad_copy", {})
    table.put_item(Item={
        "campaign_id":     campaign_id,
        "product_name":    product_name,
        "region":          data.get("region", ""),
        "audience":        data.get("audience", ""),
        "message":         data.get("message", ""),
        "language":        data.get("language", "en"),
        "ad_copy":         [{"lang": data.get("language", "en"), **ad_copy}],
        "image_prompt":    copy_result.get("image_prompt", ""),
        "images":          images,
        "compliance_pass1": p1,
        "compliance_pass2": p2,
        "approval_status": approval_status,
        "failure_reason":  failure_reason,
        "created_at":      time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ttl":             int(time.time()) + (90 * 24 * 60 * 60),
    })
