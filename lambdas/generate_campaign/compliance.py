import json
from config import get_openai_client, OPENAI_MODEL


def pass1_check(data: dict) -> dict:
    """
    Pre-generation text compliance (runs in parallel with copy_gen).
    Checks the brief's core message — if overall=fail, pipeline aborts
    before Nova Canvas is called (saves ~$0.48).
    """
    product  = data.get("product_name", "")
    region   = data.get("region", "global")
    language = data.get("language", "en")
    message  = data.get("message", "")

    prompt = f"""You are a legal and brand compliance auditor.

Evaluate this campaign brief for '{product}' in the '{region}' market (language: {language}).
Core Message: "{message}"

Return ONLY valid JSON:
{{
  "overall": "pass",
  "checks": [
    {{"criterion": "prohibited_claims",    "status": "pass", "note": "..."}},
    {{"criterion": "legal_disclaimer",     "status": "pass", "note": "..."}},
    {{"criterion": "brand_voice",          "status": "pass", "note": "..."}},
    {{"criterion": "cultural_sensitivity", "status": "pass", "note": "..."}},
    {{"criterion": "pii_risk",             "status": "pass", "note": "..."}}
  ],
  "flagged_terms": []
}}

Criteria:
1. prohibited_claims: no "guaranteed", "#1", "proven", "cure", "risk-free"
2. legal_disclaimer: claims modest or disclaimer present
3. brand_voice: premium, aspirational, authentic tone
4. cultural_sensitivity: appropriate for {region}
5. pii_risk: no personal data, phone numbers, URLs

overall = "fail" if ANY check is "fail". "warn" if any "warn". Otherwise "pass"."""

    client = get_openai_client()
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=512,
        temperature=0,
    )
    try:
        return json.loads(resp.choices[0].message.content)
    except Exception:
        return {"overall": "warn", "checks": [], "flagged_terms": []}


def pass2_check(data: dict, composited_images: dict, copy_result: dict) -> dict:
    """
    Post-generation vision compliance (runs after TextOverlay).
    Checks rendered composite image + final ad copy.
    overall=fail → approval_status becomes compliance_blocked.
    """
    product = data.get("product_name", "")
    region  = data.get("region", "global")

    image_url = composited_images.get("1x1", {}).get("url", "")
    ad_copy = copy_result.get("ad_copy", {})
    copy_text = (
        f"Headline: {ad_copy.get('headline', '')} | "
        f"Body: {ad_copy.get('body', '')} | "
        f"CTA: {ad_copy.get('cta', '')}"
    )

    if not image_url:
        return {"overall": "warn", "checks": [
            {"criterion": "image_available", "status": "warn",
             "note": "No rendered image URL for vision check"}
        ], "flagged_terms": []}

    prompt = f"""You are a brand compliance auditor reviewing a rendered ad for '{product}' in '{region}'.
Ad copy: {copy_text}

Return ONLY valid JSON:
{{
  "overall": "pass",
  "checks": [
    {{"criterion": "brand_colors",           "status": "pass", "note": "..."}},
    {{"criterion": "text_legibility",        "status": "pass", "note": "..."}},
    {{"criterion": "product_prominence",     "status": "pass", "note": "..."}},
    {{"criterion": "visual_appropriateness", "status": "pass", "note": "..."}}
  ],
  "flagged_terms": []
}}

overall = "fail" if ANY "fail". "warn" if any "warn". Otherwise "pass"."""

    client = get_openai_client()
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
    try:
        return json.loads(resp.choices[0].message.content)
    except Exception:
        return {"overall": "warn", "checks": [], "flagged_terms": []}
