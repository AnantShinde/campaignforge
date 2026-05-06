import boto3
import json
import os
import time

from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["CAMPAIGN_TABLE"])

ALLOWED_TRANSITIONS = {
    "pending_review":    {"approved", "rejected"},
    "approved":          {"rejected"},
    "rejected":          {"pending_review"},
    "compliance_blocked": {"pending_review"},  # requires compliance_override group
}

_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Content-Type": "application/json",
}


def _ok(body: dict) -> dict:
    return {"statusCode": 200, "headers": _CORS, "body": json.dumps(body)}


def _err(msg: str, status: int = 400) -> dict:
    return {"statusCode": status, "headers": _CORS, "body": json.dumps({"error": msg})}


def handler(event, context):
    """
    PATCH /campaigns/{id}/approval

    Body: { approval_status, reviewer_notes? }
    reviewed_by is extracted from the Cognito JWT claim — not trusted from the client.

    compliance_blocked → pending_review is only allowed for users in the
    'compliance_override' Cognito group. All other transitions are open to
    any authenticated reviewer.
    """
    path_params = event.get("pathParameters") or {}
    campaign_id = path_params.get("id")

    if not campaign_id:
        return _err("Missing campaign id in path")

    # Extract caller identity from JWT (HTTP API v2 format)
    claims = (
        event.get("requestContext", {})
             .get("authorizer", {})
             .get("jwt", {})
             .get("claims", {})
    )
    reviewed_by = claims.get("email", claims.get("cognito:username", "unknown"))
    user_groups = claims.get("cognito:groups", "")

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _err("Request body is not valid JSON")

    new_status = body.get("approval_status", "").strip()
    reviewer_notes = body.get("reviewer_notes", "")

    if not new_status:
        return _err("'approval_status' is required")

    valid_statuses = {"approved", "rejected", "pending_review"}
    if new_status not in valid_statuses:
        return _err(f"Invalid approval_status '{new_status}'. Must be one of: {sorted(valid_statuses)}")

    # Fetch current items for this campaign
    items = table.query(
        KeyConditionExpression=Key("campaign_id").eq(campaign_id)
    ).get("Items", [])

    if not items:
        return _err(f"Campaign {campaign_id} not found", status=404)

    updated = []
    for item in items:
        current_status = item.get("approval_status", "")
        allowed = ALLOWED_TRANSITIONS.get(current_status, set())

        if new_status not in allowed:
            return _err(
                f"Cannot transition from '{current_status}' to '{new_status}'",
                status=409,
            )

        # compliance_blocked → pending_review requires the override group
        if current_status == "compliance_blocked" and new_status == "pending_review":
            if "compliance_override" not in user_groups:
                return _err(
                    "Overriding a compliance_blocked campaign requires the "
                    "'compliance_override' Cognito group.",
                    status=403,
                )

        table.update_item(
            Key={
                "campaign_id": campaign_id,
                "product_name": item["product_name"],
            },
            UpdateExpression=(
                "SET approval_status = :s, reviewed_by = :rb, "
                "reviewed_at = :ra, reviewer_notes = :rn"
            ),
            ExpressionAttributeValues={
                ":s":  new_status,
                ":rb": reviewed_by,
                ":ra": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                ":rn": reviewer_notes,
            },
        )
        updated.append(item["product_name"])

    return _ok({
        "campaign_id": campaign_id,
        "approval_status": new_status,
        "reviewed_by": reviewed_by,
        "products_updated": updated,
    })
