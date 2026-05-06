import boto3
import os
import time

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["CAMPAIGN_TABLE"])

TTL_DAYS = 90


def handler(event, context):
    """
    State: PersistAssets (final state before Succeed)
    Writes the completed campaign blueprint to DynamoDB.

    approval_status logic:
      - Pass 2 compliance overall=fail  → "compliance_blocked"
        Human reviewer must use the Override Compliance button (Cognito group gate).
      - Everything else                 → "pending_review"

    Input:  full accumulated state
    Output: { success: True, approval_status: "..." }
    """
    campaign_id = event["campaign_id"]
    product_name = event["product_name"]

    parallel_results = event.get("parallel_results", [])
    copy_result = parallel_results[0] if parallel_results else {}
    compliance_pass1 = parallel_results[1] if len(parallel_results) > 1 else {}
    compliance_pass2 = event.get("compliance_pass2", {})

    approval_status = (
        "compliance_blocked"
        if compliance_pass2.get("overall") == "fail"
        else "pending_review"
    )

    item = {
        "campaign_id": campaign_id,
        "product_name": product_name,
        "region": event.get("region", ""),
        "audience": event.get("audience", ""),
        "message": event.get("message", ""),
        "language": event.get("language", "en"),
        "brand_context": event.get("brand_context", ""),
        "ad_copy": [
            {
                "lang": event.get("language", "en"),
                **copy_result.get("ad_copy", {}),
            }
        ],
        "image_prompt": copy_result.get("image_prompt", ""),
        "images": event.get("composited_images", event.get("images", {})),
        "compliance_pass1": compliance_pass1,
        "compliance_pass2": compliance_pass2,
        "approval_status": approval_status,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ttl": int(time.time()) + (TTL_DAYS * 24 * 60 * 60),
    }

    table.put_item(Item=item)

    return {"success": True, "approval_status": approval_status}
