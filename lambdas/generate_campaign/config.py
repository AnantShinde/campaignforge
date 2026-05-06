import boto3
import os

OUTPUTS_BUCKET     = os.environ["OUTPUTS_BUCKET"]
CAMPAIGN_TABLE     = os.environ["CAMPAIGN_TABLE"]
OPENAI_SECRET_ARN  = os.environ["OPENAI_SECRET_ARN"]
GOOGLE_SECRET_ARN  = os.environ["GOOGLE_SECRET_ARN"]
OPENAI_MODEL       = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
PRESIGNED_TTL      = 604800  # 7 days

s3       = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
table    = dynamodb.Table(CAMPAIGN_TABLE)

IMAGE_RATIOS = {
    "1x1":  {"width": 1024, "height": 1024, "platform": "Instagram"},
    "9x16": {"width": 720,  "height": 1280, "platform": "TikTok/Reels"},
    "16x9": {"width": 1280, "height": 720,  "platform": "YouTube"},
}

# Cached clients — fetched once per Lambda cold start
_openai_client = None
_google_client = None


def get_openai_client():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        secret = boto3.client("secretsmanager").get_secret_value(
            SecretId=OPENAI_SECRET_ARN
        )
        _openai_client = OpenAI(api_key=secret["SecretString"])
    return _openai_client


def get_google_client():
    global _google_client
    if _google_client is None:
        from google import genai
        secret = boto3.client("secretsmanager").get_secret_value(
            SecretId=GOOGLE_SECRET_ARN
        )
        _google_client = genai.Client(api_key=secret["SecretString"])
    return _google_client
