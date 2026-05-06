import json
import re
from config import get_openai_client, OPENAI_MODEL

BRAND_CONTEXT = """
## Brand Voice & Tone
- Premium, aspirational, authentic. Confident but never arrogant.
- No superlatives: no "best ever", "guaranteed", "#1", "proven to".
- Inclusive language — resonates across cultures.

## Compliance Hard Rules
- No unsubstantiated health or performance claims.
- No competitor mentions (direct or implied).
- All performance claims must be hedged ("designed to", "helps you", "built for").

## Copy Structure
- Headline: short, benefit-led, max 8 words.
- Body: one core idea, two sentences max.
- CTA: action verb + value ("Shop the Pro", "Explore the range").
"""


def generate(data: dict) -> dict:
    """
    Generates localised ad copy and image prompt using GPT-4o mini.
    Returns: { ad_copy: { headline, body, cta }, image_prompt }
    """
    product  = data.get("product_name", "")
    region   = data.get("region", "global")
    audience = data.get("audience", "general")
    message  = data.get("message", "")
    language = data.get("language", "en")

    prompt = f"""You are a senior advertising copywriter. Use the brand guidelines below.

Brand Guidelines:
{BRAND_CONTEXT}

Campaign Brief:
- Product: {product}
- Region: {region}
- Target Audience: {audience}
- Core Message: {message}
- Output Language: {language}

Return ONLY valid JSON — no markdown, no explanation:
{{
  "ad_copy": {{"headline": "...", "body": "...", "cta": "..."}},
  "image_prompt": "..."
}}

Rules:
- headline, body, cta MUST be in language code '{language}'
- image_prompt MUST be in English
- image_prompt: premium commercial photograph — product, setting, lighting, mood, 4K editorial style"""

    client = get_openai_client()
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=1024,
        temperature=0.7,
    )
    raw = response.choices[0].message.content
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    return json.loads(raw.strip())
