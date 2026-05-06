import boto3
import json
import os
import time
import uuid

dynamodb = boto3.resource("dynamodb")
sqs = boto3.client("sqs")

table = dynamodb.Table(os.environ["CAMPAIGN_TABLE"])
QUEUE_URL = os.environ["QUEUE_URL"]
TTL_DAYS = 90

REQUIRED = {"product_name", "region", "audience", "message", "language"}

_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Content-Type": "application/json",
}


def _ok(body: dict, status: int = 200) -> dict:
    return {"statusCode": status, "headers": _CORS, "body": json.dumps(body)}


def _err(msg: str, status: int = 400) -> dict:
    return {"statusCode": status, "headers": _CORS, "body": json.dumps({"error": msg})}


def _validate(brief: dict) -> str | None:
    missing = REQUIRED - brief.keys()
    if missing:
        return f"Missing required fields: {sorted(missing)}"
    return None


def _submit_one(brief: dict) -> str:
    """Writes one campaign record to DynamoDB and enqueues it."""
    campaign_id = str(uuid.uuid4())
    ttl = int(time.time()) + (TTL_DAYS * 24 * 60 * 60)

    table.put_item(Item={
        "campaign_id": campaign_id,
        "product_name": brief["product_name"],
        "region": brief["region"],
        "audience": brief["audience"],
        "message": brief["message"],
        "language": brief["language"],
        "queued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ttl": ttl,
    })

    sqs.send_message(
        QueueUrl=QUEUE_URL,
        MessageBody=json.dumps({
            "campaign_id": campaign_id,
            **{k: brief[k] for k in REQUIRED},
        }),
    )

    return campaign_id


def handler(event, context):
    """
    POST /brief       — single campaign brief
    POST /brief/batch — array of up to 50 briefs

    HTTP API v2 payload format 2.0:
      event["body"]       raw JSON string
      event["routeKey"]   "POST /brief" or "POST /brief/batch"

    Returns 202 Accepted immediately; generation runs asynchronously via
    SQS → EventBridge Pipe → Step Functions Express Workflow.
    """
    route = event.get("routeKey", "")
    raw = event.get("body") or "{}"

    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return _err("Request body is not valid JSON")

    # ---- batch endpoint -------------------------------------------------- #
    if "/batch" in route:
        briefs = body.get("briefs", [])
        if not isinstance(briefs, list) or not briefs:
            return _err("'briefs' must be a non-empty array")
        if len(briefs) > 50:
            return _err("Batch size exceeds maximum of 50 briefs per request")

        for i, b in enumerate(briefs):
            err = _validate(b)
            if err:
                return _err(f"Brief at index {i}: {err}")

        campaign_ids = [_submit_one(b) for b in briefs]

        return _ok(
            {"batch_id": str(uuid.uuid4()), "campaign_ids": campaign_ids, "count": len(campaign_ids)},
            status=202,
        )

    # ---- single endpoint ------------------------------------------------- #
    err = _validate(body)
    if err:
        return _err(err)

    campaign_id = _submit_one(body)

    return _ok(
        {
            "campaign_id": campaign_id,
            "product_name": body["product_name"],
            "status": "queued",
        },
        status=202,
    )
