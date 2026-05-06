#!/usr/bin/env python3
import os
import aws_cdk as cdk
import aws_cdk.aws_secretsmanager as secretsmanager
from stacks.secrets_stack import SecretsStack
from stacks.storage_stack import StorageStack
from stacks.ai_stack import AiStack      # kept for Option B (KB + AOSS)
from stacks.pipeline_stack import PipelineStack
from stacks.api_stack import ApiStack

app = cdk.App()

env_name = app.node.try_get_context("env") or "dev"

# Set to "true" to deploy the Ai stack (OpenSearch + Bedrock KB).
# Option A (default): skip Ai stack — brief_enrich uses hardcoded brand context.
# Option B: set DEPLOY_AI_STACK=true to enable live RAG retrieval.
deploy_ai = os.getenv("DEPLOY_AI_STACK", "false").lower() == "true"

aws_env = cdk.Environment(
    account=os.getenv("CDK_DEFAULT_ACCOUNT"),
    region=os.getenv("CDK_DEFAULT_REGION", "us-east-1"),
)

secrets_stack = SecretsStack(
    app,
    f"CampaignForge-{env_name}-Secrets",
    env_name=env_name,
    env=aws_env,
    description=f"[{env_name}] CampaignForge — Secrets Manager (OpenAI key)",
)

storage_stack = StorageStack(
    app,
    f"CampaignForge-{env_name}-Storage",
    env_name=env_name,
    env=aws_env,
    description=f"[{env_name}] CampaignForge — S3 (3 buckets) + DynamoDB",
)

# Ai stack — only instantiated when DEPLOY_AI_STACK=true
knowledge_base_id = ""
guardrail_id = ""

if deploy_ai:
    ai_stack = AiStack(
        app,
        f"CampaignForge-{env_name}-Ai",
        env_name=env_name,
        rag_docs_bucket=storage_stack.rag_docs_bucket,
        env=aws_env,
        description=f"[{env_name}] CampaignForge — OpenSearch Serverless + Bedrock KB + Guardrail",
    )
    ai_stack.add_dependency(storage_stack)
    knowledge_base_id = ai_stack.knowledge_base.attr_knowledge_base_id
    guardrail_id = ai_stack.guardrail.attr_guardrail_id

# Import the Google secret that was created directly in Secrets Manager
google_secret = secretsmanager.Secret.from_secret_name_v2(
    storage_stack, "GoogleApiKeySecret",
    "campaignforge/dev/google-api-key",
)

pipeline_stack = PipelineStack(
    app,
    f"CampaignForge-{env_name}-Pipeline",
    env_name=env_name,
    campaign_table=storage_stack.campaign_table,
    outputs_bucket=storage_stack.outputs_bucket,
    openai_secret=secrets_stack.openai_api_key_secret,
    google_secret=google_secret,
    knowledge_base_id=knowledge_base_id,
    guardrail_id=guardrail_id,
    env=aws_env,
    description=f"[{env_name}] CampaignForge — SQS + EventBridge Pipe + Step Functions",
)
pipeline_stack.add_dependency(storage_stack)

api_stack = ApiStack(
    app,
    f"CampaignForge-{env_name}-Api",
    env_name=env_name,
    campaign_table=storage_stack.campaign_table,
    outputs_bucket=storage_stack.outputs_bucket,
    campaign_queue=pipeline_stack.campaign_queue,
    env=aws_env,
    description=f"[{env_name}] CampaignForge — HTTP API v2 + Cognito + Lambda",
)
api_stack.add_dependency(pipeline_stack)

app.synth()
