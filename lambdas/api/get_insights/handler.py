import boto3
import json
import os
from collections import Counter, defaultdict

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["CAMPAIGN_TABLE"])

NOVA_CANVAS_COST_PER_IMAGE = 0.08
IMAGES_PER_CAMPAIGN = 6
HAIKU_COST_PER_CAMPAIGN = 0.001
GPT_MINI_COST_PER_CAMPAIGN = 0.005

_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Content-Type": "application/json",
}


def handler(event, context):
    """
    GET /insights

    Performs a full DynamoDB Scan with server-side aggregation.
    Performant at POC scale (~500 campaigns/month = ~1000 items).
    When volume exceeds ~10K items, migrate to Firehose → Athena.

    Returns:
      total_campaigns, top_regions, avg_cost_usd, total_cost_usd,
      compliance_pass_rate, avg_generation_time_ms, most_flagged_terms,
      approval_breakdown
    """
    # Scan all items
    items = []
    resp = table.scan()
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))

    total = len(items)

    if total == 0:
        return {
            "statusCode": 200,
            "headers": _CORS,
            "body": json.dumps({
                "total_campaigns": 0,
                "top_regions": [],
                "avg_cost_usd": 0,
                "total_cost_usd": 0,
                "compliance_pass_rate": 0,
                "avg_generation_time_ms": 0,
                "most_flagged_terms": [],
                "approval_breakdown": {},
            }),
        }

    # Regional breakdown
    region_counts = Counter(
        item.get("region", "unknown").lower()
        for item in items
        if item.get("region")
    )
    top_regions = [
        {"region": r, "count": c}
        for r, c in region_counts.most_common(10)
    ]

    # Cost — use stored value if present, otherwise estimate
    total_cost = 0.0
    for item in items:
        stored = item.get("estimated_cost_usd")
        if stored:
            total_cost += float(stored)
        elif item.get("approval_status"):
            # Estimate from fixed model costs
            total_cost += (
                NOVA_CANVAS_COST_PER_IMAGE * IMAGES_PER_CAMPAIGN
                + HAIKU_COST_PER_CAMPAIGN
                + GPT_MINI_COST_PER_CAMPAIGN
            )

    avg_cost = round(total_cost / total, 4) if total > 0 else 0

    # Compliance pass rate (Pass 1 — pre-generation check)
    completed = [i for i in items if i.get("approval_status")]
    pass1_results = [
        i.get("compliance_pass1", {}).get("overall", "unknown")
        for i in completed
    ]
    pass_count = sum(1 for r in pass1_results if r == "pass")
    compliance_pass_rate = round(pass_count / len(completed), 4) if completed else 0

    # Most flagged terms across all compliance checks
    flagged_counter = Counter()
    for item in items:
        for check_key in ("compliance_pass1", "compliance_pass2"):
            terms = item.get(check_key, {}).get("flagged_terms", [])
            for t in terms:
                if t:
                    flagged_counter[t.lower()] += 1
    most_flagged = [
        {"term": t, "count": c}
        for t, c in flagged_counter.most_common(10)
    ]

    # Approval breakdown
    approval_counts = Counter(
        item.get("approval_status", "queued") for item in items
    )

    body = {
        "total_campaigns": total,
        "top_regions": top_regions,
        "avg_cost_usd": avg_cost,
        "total_cost_usd": round(total_cost, 2),
        "compliance_pass_rate": compliance_pass_rate,
        "avg_generation_time_ms": 0,   # placeholder — add created_at → reviewed_at delta in v2
        "most_flagged_terms": most_flagged,
        "approval_breakdown": dict(approval_counts),
    }

    return {
        "statusCode": 200,
        "headers": _CORS,
        "body": json.dumps(body, default=str),
    }
