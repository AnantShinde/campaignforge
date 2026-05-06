import boto3
import json
import os
import re

OPENAI_SECRET_ARN = os.environ["OPENAI_SECRET_ARN"]
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

# Cached per Lambda execution context
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
    State: CopyGen  (Parallel branch 0)
    Generates localised ad copy and image prompt using GPT-4o mini.
    The KB-retrieved brand_context is injected into the prompt so the
    model executes against a constrained, well-scoped brief.

    Input:  full state — uses campaign fields + $.brand_context
    Output: { ad_copy: { headline, body, cta }, image_prompt }
            → becomes $.parallel_results[0] via Parallel result_path
    """
    product = event.get("product_name", "")
    region = event.get("region", "global")
    audience = event.get("audience", "general")
    message = event.get("message", "")
    language = event.get("language", "en")
    brand_context = event.get("brand_context", "")

    prompt = f"""You are a senior advertising copywriter. Use the brand context below to write a localized ad campaign.

Brand Context:
{brand_context}

Campaign Brief:
- Product: {product}
- Region: {region}
- Target Audience: {audience}
- Core Message: {message}
- Output Language: {language}

Return ONLY valid JSON — no markdown, no explanation:
{{
  "ad_copy": {{
    "headline": "...",
    "body": "...",
    "cta": "..."
  }},
  "image_prompt": "..."
}}

Rules:
- headline, body, cta MUST be in language code '{language}'
- image_prompt MUST be in English (for Nova Canvas)
- image_prompt: premium commercial photograph — product, setting, lighting, mood, 4K editorial style"""

    client = _get_client()
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=1024,
        temperature=0.7,
    )

    return json.loads(response.choices[0].message.content)
