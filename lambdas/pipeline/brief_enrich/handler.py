"""
BriefEnrich — injects brand context into the pipeline state.

Currently uses hardcoded brand guidelines (Option A).
To enable live RAG retrieval from Bedrock Knowledge Base (Option B),
set KNOWLEDGE_BASE_ID env var and uncomment the _retrieve_from_kb() call.
"""
import os

KNOWLEDGE_BASE_ID = os.environ.get("KNOWLEDGE_BASE_ID", "")

HARDCODED_BRAND_CONTEXT = """
## Brand Voice & Tone
- Premium, aspirational, and authentic. Confident but never arrogant.
- Speak to ambition: our customers invest in quality because they invest in themselves.
- Avoid hollow superlatives: no "best ever", "guaranteed", "#1", "proven to".
- Inclusive language — resonate across cultures without stereotyping.

## Visual Identity
- Palette: deep navy, clean white, warm gold accents.
- Imagery: cinematic lighting, real-world context, high production value.
- Product must be clearly visible and well-lit. No cluttered backgrounds.

## Compliance Hard Rules
- No unsubstantiated health or performance claims.
- No competitor mentions, direct or implied ("unlike Brand X", "better than others").
- No political, religious, or divisive content.
- All performance claims must be hedged ("designed to", "helps you", "built for").

## Regional Guidance
- LATAM (Brazil, Mexico): family and community values drive purchase. Mobile-first.
  Portuguese/Spanish must feel native, not translated. Football culture resonates.
- USA: authenticity and brand purpose matter. Benefit-focused copy > lifestyle abstraction.
  FTC compliance required — no unsubstantiated claims.
- Germany: precision, engineering quality, and reliability are key purchase drivers.
  Formal register preferred. Data privacy messaging adds trust.
- Japan: harmony, craftsmanship, and attention to detail. Indirect language appropriate.

## Copy Structure
- Headline: short, punchy, benefit-led. Max 8 words.
- Body: one core idea, two sentences max. Focus on the "so what" for the customer.
- CTA: action verb + value ("Shop the Pro", "Start your setup", "Explore the range").
"""


def _retrieve_from_kb(product: str, region: str, audience: str, message: str) -> str:
    """RAG retrieval via Bedrock KB — requires KNOWLEDGE_BASE_ID to be set."""
    import boto3
    bedrock_agent_runtime = boto3.client("bedrock-agent-runtime")
    query = (
        f"Brand guidelines and regional marketing trends for {product} "
        f"targeting {audience} in the {region} market. Campaign message: {message}"
    )
    response = bedrock_agent_runtime.retrieve(
        knowledgeBaseId=KNOWLEDGE_BASE_ID,
        retrievalQuery={"text": query},
        retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": 5}},
    )
    chunks = [r["content"]["text"] for r in response.get("retrievalResults", [])]
    return "\n\n---\n\n".join(chunks) if chunks else HARDCODED_BRAND_CONTEXT


def handler(event, context):
    """
    State: BriefEnrich
    Returns brand context injected into $.brand_context via result_path.

    Option A (current): returns hardcoded brand guidelines — no AWS cost, no AOSS dependency.
    Option B (future):  set KNOWLEDGE_BASE_ID to enable live RAG retrieval.
    """
    if KNOWLEDGE_BASE_ID:
        return _retrieve_from_kb(
            product=event.get("product_name", ""),
            region=event.get("region", "global"),
            audience=event.get("audience", "general"),
            message=event.get("message", ""),
        )

    return HARDCODED_BRAND_CONTEXT
