import boto3
import json
import os

OPENAI_SECRET_ARN = os.environ["OPENAI_SECRET_ARN"]
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

# Cached per Lambda execution context — fetched once on cold start
_openai_client = None


def _get_client():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        secret = boto3.client("secretsmanager").get_secret_value(
            SecretId=OPENAI_SECRET_ARN
        )
        _openai_client = OpenAI(api_key=secret["SecretString"])
    return _openai_client


def handler(event, context):
    """
    State: Pass1Compliance (Parallel branch 1) and Pass2Compliance (sequential)
    Two-pass GPT-4o mini compliance gate.

    Pass 1 — pre-generation text check (mode=pre):
      Checks the campaign brief's core message and audience targeting.
      Runs in parallel with CopyGen — does NOT block on generated copy.
      If overall=fail  → Step Functions Choice aborts BEFORE Nova Canvas.
      Saves $0.48 per hard-failing campaign.

    Pass 2 — post-generation vision check (mode=post):
      Checks the rendered composite image + final ad copy.
      Catches visual brand violations invisible from the brief alone.
      overall=fail → approval_status written as compliance_blocked (not pending_review).
      Human override required (Cognito compliance_override group).

    Input:  { mode: "pre"|"post", data: { ...full state... } }
    Output: { overall: "pass"|"warn"|"fail", checks: [...], flagged_terms: [] }
    """
    mode = event.get("mode", "pre")
    data = event.get("data", event)

    if mode == "pre":
        return _pass1_text_check(data)
    return _pass2_vision_check(data)


def _pass1_text_check(data: dict) -> dict:
    product = data.get("product_name", "")
    region = data.get("region", "global")
    language = data.get("language", "en")
    message = data.get("message", "")

    prompt = f"""You are a legal and brand compliance auditor for global advertising.

Evaluate this campaign brief for '{product}' targeting the '{region}' market (language: {language}).

Core Message: "{message}"

Run exactly these 5 checks and return ONLY valid JSON — no explanation, no markdown:
{{
  "overall": "pass",
  "checks": [
    {{"criterion": "prohibited_claims", "status": "pass", "note": "one sentence"}},
    {{"criterion": "legal_disclaimer",  "status": "pass", "note": "one sentence"}},
    {{"criterion": "brand_voice",       "status": "pass", "note": "one sentence"}},
    {{"criterion": "cultural_sensitivity", "status": "pass", "note": "one sentence"}},
    {{"criterion": "pii_risk",          "status": "pass", "note": "one sentence"}}
  ],
  "flagged_terms": []
}}

Criteria:
1. prohibited_claims: No unsubstantiated superlatives (guaranteed, #1, proven, cure, risk-free, clinically proven)
2. legal_disclaimer: Claims modest enough, or disclaimer present
3. brand_voice: Premium, aspirational, authentic tone
4. cultural_sensitivity: Appropriate and respectful for {region}
5. pii_risk: No personal data, phone numbers, email addresses, or URLs

overall = "fail" if ANY check is "fail". overall = "warn" if ANY check is "warn". Otherwise "pass"."""

    client = _get_client()
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=512,
        temperature=0,
    )

    return json.loads(resp.choices[0].message.content)


def _pass2_vision_check(data: dict) -> dict:
    product = data.get("product_name", "")
    region = data.get("region", "global")

    # Extract parallel results: [0] = CopyGen, [1] = Pass1 result
    parallel = data.get("parallel_results", [])
    ad_copy = parallel[0].get("ad_copy", {}) if parallel else {}
    copy_text = (
        f"Headline: {ad_copy.get('headline', '')} | "
        f"Body: {ad_copy.get('body', '')} | "
        f"CTA: {ad_copy.get('cta', '')}"
    )

    # Use composited 1x1 image for visual inspection
    composited = data.get("composited_images", data.get("images", {}))
    image_url = composited.get("1x1", {}).get("url", "")

    if not image_url:
        return {
            "overall": "warn",
            "checks": [{
                "criterion": "image_available",
                "status": "warn",
                "note": "No rendered image URL available for vision check",
            }],
            "flagged_terms": [],
        }

    prompt = f"""You are a brand compliance auditor reviewing a rendered advertising image for '{product}' in the '{region}' market.

Ad Copy on image: {copy_text}

Evaluate the image for these 4 criteria and return ONLY valid JSON:
{{
  "overall": "pass",
  "checks": [
    {{"criterion": "brand_colors",           "status": "pass", "note": "one sentence"}},
    {{"criterion": "text_legibility",        "status": "pass", "note": "one sentence"}},
    {{"criterion": "product_prominence",     "status": "pass", "note": "one sentence"}},
    {{"criterion": "visual_appropriateness", "status": "pass", "note": "one sentence"}}
  ],
  "flagged_terms": []
}}

overall = "fail" if ANY check is "fail". overall = "warn" if ANY check is "warn". Otherwise "pass"."""

    client = _get_client()
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": image_url}},
                {"type": "text", "text": prompt},
            ],
        }],
        response_format={"type": "json_object"},
        max_tokens=512,
        temperature=0,
    )

    return json.loads(resp.choices[0].message.content)
