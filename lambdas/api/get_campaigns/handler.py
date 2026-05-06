import boto3
import json
import os

from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")

table = dynamodb.Table(os.environ["CAMPAIGN_TABLE"])
OUTPUTS_BUCKET = os.environ["OUTPUTS_BUCKET"]
PRESIGNED_TTL = 604800  # 7 days

_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Content-Type": "application/json",
}


def _ok(body, status: int = 200) -> dict:
    return {"statusCode": status, "headers": _CORS, "body": json.dumps(body, default=str)}


def _err(msg: str, status: int = 400) -> dict:
    return {"statusCode": status, "headers": _CORS, "body": json.dumps({"error": msg})}


def _refresh_urls(item: dict) -> dict:
    """Re-generates presigned URLs so they never expire between poll cycles."""
    images = item.get("images", {})
    if not images:
        return item

    refreshed = {}
    for ratio, meta in images.items():
        if not isinstance(meta, dict):
            refreshed[ratio] = meta
            continue
        s3_key = meta.get("s3_key", "")
        url = meta.get("url", "")
        if s3_key:
            try:
                url = s3.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": OUTPUTS_BUCKET, "Key": s3_key},
                    ExpiresIn=PRESIGNED_TTL,
                )
            except Exception as exc:
                print(f"Could not refresh presigned URL for {s3_key}: {exc}")
        refreshed[ratio] = {**meta, "url": url}

    return {**item, "images": refreshed}


def _shape(item: dict) -> dict:
    """
    Normalises a DynamoDB item into the API response shape.
    Detects pipeline state from field presence:
      no approval_status → "queued"  (SFN pipeline still running)
      approval_status present → "complete"
    """
    if "approval_status" not in item:
        return {
            "campaign_id": item["campaign_id"],
            "product_name": item.get("product_name", ""),
            "status": "queued",
            "queued_at": item.get("queued_at", ""),
        }

    return {
        "campaign_id": item["campaign_id"],
        "product_name": item.get("product_name", ""),
        "status": "complete",
        "approval_status": item.get("approval_status"),
        "region": item.get("region"),
        "audience": item.get("audience"),
        "message": item.get("message"),
        "language": item.get("language"),
        "strategy": item.get("strategy"),
        "ad_copy": item.get("ad_copy", []),
        "image_prompt": item.get("image_prompt"),
        "images": item.get("images", {}),
        "compliance_pass1": item.get("compliance_pass1", {}),
        "compliance_pass2": item.get("compliance_pass2", {}),
        "created_at": item.get("created_at"),
        "reviewed_by": item.get("reviewed_by"),
        "reviewer_notes": item.get("reviewer_notes"),
        "reviewed_at": item.get("reviewed_at"),
    }


def handler(event, context):
    """
    GET /campaigns        — list all campaigns (no URL refresh, for history view)
    GET /campaigns/{id}   — single campaign with refreshed presigned URLs

    Frontend polls GET /campaigns/{id} every 3s.
    "queued"   → Step Functions pipeline still running
    "complete" → PersistAssets has written the full blueprint
    """
    path_params = event.get("pathParameters") or {}
    campaign_id = path_params.get("id")

    if campaign_id:
        resp = table.query(
            KeyConditionExpression=Key("campaign_id").eq(campaign_id)
        )
        items = resp.get("Items", [])

        if not items:
            return _err(f"Campaign {campaign_id} not found", status=404)

        shaped = [_shape(_refresh_urls(item)) for item in items]
        return _ok({"campaign_id": campaign_id, "blueprints": shaped})

    # List all — scan with projection (no URL refresh for list view)
    resp = table.scan(
        ProjectionExpression=(
            "campaign_id, product_name, #r, audience, approval_status, "
            "created_at, queued_at"
        ),
        ExpressionAttributeNames={"#r": "region"},
    )
    items = sorted(
        resp.get("Items", []),
        key=lambda x: x.get("created_at") or x.get("queued_at") or "",
        reverse=True,
    )
    return _ok([_shape(item) for item in items])
