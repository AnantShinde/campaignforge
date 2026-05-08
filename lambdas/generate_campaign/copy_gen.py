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
- Headline   : exactly 1 word — powerful, evocative, uppercase
- Subheadline: exactly 6 words — benefit-focused, supports the headline
- Body       : one core idea, two sentences max
- CTA        : action verb + value ("Shop the Pro", "Explore the range")
"""


def generate(data: dict) -> dict:
    """
    Generates localised ad copy using GPT-4o mini.

    Returns:
      {
        ad_copy: {
          headline:    "RISE"            ← exactly 1 word, uppercase
          subheadline: "Work smarter on your terms"  ← exactly 6 words
          body:        "..."
          cta:         "..."
        },
        image_prompt: "..."
      }
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
  "ad_copy": {{
    "headline":    "WORD",
    "subheadline": "Exactly six words here as subhead",
    "body":        "Two sentence body copy.",
    "cta":         "Action phrase"
  }},
  "image_prompt": "English-language image description for Imagen 4"
}}

STRICT RULES:
- headline    : EXACTLY 1 word, written in UPPERCASE, in language '{language}'
- subheadline : EXACTLY 6 words, sentence case, in language '{language}'
- body        : 1-2 sentences in language '{language}'
- cta         : short action phrase in language '{language}'
- image_prompt: ALWAYS in English, premium commercial photograph style, 4K editorial"""

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
