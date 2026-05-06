"""
CDK assertion tests — verify CloudFormation templates for all 5 stacks.
No AWS deployment or credentials required. Runs against synthesised templates.

Run:  pytest tests/unit/test_firefly_campaign_stack.py -v
"""
import os
import aws_cdk as cdk
import aws_cdk.assertions as assertions
import pytest

from stacks.secrets_stack import SecretsStack
from stacks.storage_stack import StorageStack
from stacks.ai_stack import AiStack
from stacks.pipeline_stack import PipelineStack
from stacks.api_stack import ApiStack


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def app():
    return cdk.App()

@pytest.fixture(scope="module")
def env():
    return cdk.Environment(account="123456789012", region="us-east-1")

@pytest.fixture(scope="module")
def secrets(app, env):
    return SecretsStack(app, "Test-Secrets", env_name="test", env=env)

@pytest.fixture(scope="module")
def storage(app, env):
    return StorageStack(app, "Test-Storage", env_name="test", env=env)

@pytest.fixture(scope="module")
def ai(app, env, storage):
    return AiStack(app, "Test-Ai", env_name="test",
                   rag_docs_bucket=storage.rag_docs_bucket, env=env)

@pytest.fixture(scope="module")
def pipeline(app, env, storage, ai, secrets):
    return PipelineStack(
        app, "Test-Pipeline", env_name="test",
        campaign_table=storage.campaign_table,
        assets_input_bucket=storage.assets_input_bucket,
        outputs_bucket=storage.outputs_bucket,
        knowledge_base_id=ai.knowledge_base.attr_knowledge_base_id,
        guardrail_id=ai.guardrail.attr_guardrail_id,
        openai_secret=secrets.openai_api_key_secret,
        env=env,
    )

@pytest.fixture(scope="module")
def api(app, env, storage, pipeline):
    return ApiStack(
        app, "Test-Api", env_name="test",
        campaign_table=storage.campaign_table,
        outputs_bucket=storage.outputs_bucket,
        campaign_queue=pipeline.campaign_queue,
        env=env,
    )


# ── SecretsStack ──────────────────────────────────────────────────────────────

class TestSecretsStack:
    def test_secret_created(self, secrets):
        t = assertions.Template.from_stack(secrets)
        t.resource_count_is("AWS::SecretsManager::Secret", 1)

    def test_secret_name(self, secrets):
        t = assertions.Template.from_stack(secrets)
        t.has_resource_properties("AWS::SecretsManager::Secret", {
            "Name": "campaignforge/test/openai-api-key"
        })

    def test_secret_retained_on_delete(self, secrets):
        t = assertions.Template.from_stack(secrets)
        t.has_resource("AWS::SecretsManager::Secret", {
            "DeletionPolicy": "Retain",
            "UpdateReplacePolicy": "Retain",
        })


# ── StorageStack ──────────────────────────────────────────────────────────────

class TestStorageStack:
    def test_three_s3_buckets(self, storage):
        t = assertions.Template.from_stack(storage)
        t.resource_count_is("AWS::S3::Bucket", 3)

    def test_all_buckets_versioned(self, storage):
        """NFR5: All generated assets must be durable, S3 with versioning enabled."""
        t = assertions.Template.from_stack(storage)
        buckets = t.find_resources("AWS::S3::Bucket")
        for logical_id, bucket in buckets.items():
            props = bucket.get("Properties", {})
            versioning = props.get("VersioningConfiguration", {})
            assert versioning.get("Status") == "Enabled", \
                f"Bucket {logical_id} missing versioning (NFR5)"

    def test_outputs_bucket_has_cors(self, storage):
        """FR11: Programmatic download via S3 CORS and Blob fetching."""
        t = assertions.Template.from_stack(storage)
        t.has_resource_properties("AWS::S3::Bucket", {
            "CorsConfiguration": assertions.Match.object_like({
                "CorsRules": assertions.Match.array_with([
                    assertions.Match.object_like({"AllowedMethods": ["GET"]})
                ])
            })
        })

    def test_dynamodb_table_created(self, storage):
        t = assertions.Template.from_stack(storage)
        t.resource_count_is("AWS::DynamoDB::Table", 1)

    def test_dynamodb_pay_per_request(self, storage):
        t = assertions.Template.from_stack(storage)
        t.has_resource_properties("AWS::DynamoDB::Table", {
            "BillingMode": "PAY_PER_REQUEST"
        })

    def test_dynamodb_ttl_enabled(self, storage):
        t = assertions.Template.from_stack(storage)
        t.has_resource_properties("AWS::DynamoDB::Table", {
            "TimeToLiveSpecification": {
                "AttributeName": "ttl",
                "Enabled": True,
            }
        })

    def test_dynamodb_composite_key(self, storage):
        """PK: campaign_id, SK: product_name — allows querying all products per campaign."""
        t = assertions.Template.from_stack(storage)
        t.has_resource_properties("AWS::DynamoDB::Table", {
            "KeySchema": assertions.Match.array_with([
                {"AttributeName": "campaign_id", "KeyType": "HASH"},
                {"AttributeName": "product_name", "KeyType": "RANGE"},
            ])
        })

    def test_dynamodb_gsi_for_reviewer_dashboard(self, storage):
        """Sparse GSI: approval_status → created_at (powers reviewer dashboard)."""
        t = assertions.Template.from_stack(storage)
        t.has_resource_properties("AWS::DynamoDB::Table", {
            "GlobalSecondaryIndexes": assertions.Match.array_with([
                assertions.Match.object_like({
                    "IndexName": "status-created-index",
                    "KeySchema": assertions.Match.array_with([
                        {"AttributeName": "approval_status", "KeyType": "HASH"},
                        {"AttributeName": "created_at",      "KeyType": "RANGE"},
                    ])
                })
            ])
        })


# ── AiStack ───────────────────────────────────────────────────────────────────

class TestAiStack:
    def test_opensearch_collection_created(self, ai):
        t = assertions.Template.from_stack(ai)
        t.resource_count_is("AWS::OpenSearchServerless::Collection", 1)

    def test_collection_is_vectorsearch(self, ai):
        t = assertions.Template.from_stack(ai)
        t.has_resource_properties("AWS::OpenSearchServerless::Collection", {
            "Type": "VECTORSEARCH"
        })

    def test_knowledge_base_created(self, ai):
        t = assertions.Template.from_stack(ai)
        t.resource_count_is("AWS::Bedrock::KnowledgeBase", 1)

    def test_knowledge_base_uses_titan_embed_v2(self, ai):
        t = assertions.Template.from_stack(ai)
        t.has_resource_properties("AWS::Bedrock::KnowledgeBase", {
            "KnowledgeBaseConfiguration": {
                "Type": "VECTOR",
                "VectorKnowledgeBaseConfiguration": assertions.Match.object_like({
                    "EmbeddingModelArn": assertions.Match.string_like_regexp(
                        ".*titan-embed-text-v2:0"
                    )
                })
            }
        })

    def test_two_data_sources(self, ai):
        """One KB, two S3 data sources: brand-guidelines/ + regional-trends/."""
        t = assertions.Template.from_stack(ai)
        t.resource_count_is("AWS::Bedrock::DataSource", 2)

    def test_guardrail_created(self, ai):
        t = assertions.Template.from_stack(ai)
        t.resource_count_is("AWS::Bedrock::Guardrail", 1)

    def test_guardrail_blocks_hate_at_high(self, ai):
        t = assertions.Template.from_stack(ai)
        t.has_resource_properties("AWS::Bedrock::Guardrail", {
            "ContentPolicyConfig": assertions.Match.object_like({
                "FiltersConfig": assertions.Match.array_with([
                    assertions.Match.object_like({
                        "Type": "HATE",
                        "InputStrength": "HIGH",
                        "OutputStrength": "HIGH",
                    })
                ])
            })
        })

    def test_guardrail_blocked_word_guaranteed(self, ai):
        t = assertions.Template.from_stack(ai)
        t.has_resource_properties("AWS::Bedrock::Guardrail", {
            "WordPolicyConfig": assertions.Match.object_like({
                "WordsConfig": assertions.Match.array_with([
                    {"Text": "guaranteed"}
                ])
            })
        })


# ── PipelineStack ─────────────────────────────────────────────────────────────

class TestPipelineStack:
    def test_sqs_queue_created(self, pipeline):
        """NFR6: SQS retries must not duplicate work or double-charge Bedrock."""
        t = assertions.Template.from_stack(pipeline)
        # Main queue + DLQ = 2
        t.resource_count_is("AWS::SQS::Queue", 2)

    def test_sqs_visibility_timeout_exceeds_sfn_max(self, pipeline):
        """Visibility timeout (360s) > Express SFN max (300s) — prevents re-delivery mid-execution."""
        t = assertions.Template.from_stack(pipeline)
        t.has_resource_properties("AWS::SQS::Queue", {
            "VisibilityTimeout": 360
        })

    def test_sqs_has_dlq(self, pipeline):
        t = assertions.Template.from_stack(pipeline)
        t.has_resource_properties("AWS::SQS::Queue", {
            "RedrivePolicy": assertions.Match.object_like({
                "maxReceiveCount": 3
            })
        })

    def test_step_functions_is_express(self, pipeline):
        """Express Workflows for sub-5-minute generation (NFR2)."""
        t = assertions.Template.from_stack(pipeline)
        t.has_resource_properties("AWS::StepFunctions::StateMachine", {
            "StateMachineType": "EXPRESS"
        })

    def test_step_functions_timeout_5_minutes(self, pipeline):
        t = assertions.Template.from_stack(pipeline)
        t.has_resource_properties("AWS::StepFunctions::StateMachine", {
            "TimeoutSeconds": 300
        })

    def test_eventbridge_pipe_created(self, pipeline):
        t = assertions.Template.from_stack(pipeline)
        t.resource_count_is("AWS::Pipes::Pipe", 1)

    def test_pipe_is_fire_and_forget(self, pipeline):
        t = assertions.Template.from_stack(pipeline)
        t.has_resource_properties("AWS::Pipes::Pipe", {
            "TargetParameters": assertions.Match.object_like({
                "StepFunctionStateMachineParameters": {
                    "InvocationType": "FIRE_AND_FORGET"
                }
            })
        })

    def test_image_gen_lambda_has_1024mb(self, pipeline):
        """Image gen Lambda needs 1024 MB for concurrent base64 decode + S3 upload."""
        t = assertions.Template.from_stack(pipeline)
        t.has_resource_properties("AWS::Lambda::Function", {
            "MemorySize": 1024,
            "Timeout": 180,
        })

    def test_nova_canvas_model_id(self, pipeline):
        """Verify correct available Nova Canvas model ID is used (not v2 which requires access)."""
        t = assertions.Template.from_stack(pipeline)
        t.has_resource_properties("AWS::IAM::Policy", {
            "PolicyDocument": assertions.Match.object_like({
                "Statement": assertions.Match.array_with([
                    assertions.Match.object_like({
                        "Action": "bedrock:InvokeModel",
                        "Resource": assertions.Match.string_like_regexp(
                            ".*nova-canvas-v1:0"
                        )
                    })
                ])
            })
        })


# ── ApiStack ──────────────────────────────────────────────────────────────────

class TestApiStack:
    def test_http_api_created(self, api):
        t = assertions.Template.from_stack(api)
        t.resource_count_is("AWS::ApiGatewayV2::Api", 1)

    def test_cognito_user_pool_created(self, api):
        t = assertions.Template.from_stack(api)
        t.resource_count_is("AWS::Cognito::UserPool", 1)

    def test_self_sign_up_disabled(self, api):
        t = assertions.Template.from_stack(api)
        t.has_resource_properties("AWS::Cognito::UserPool", {
            "AdminCreateUserConfig": {
                "AllowAdminCreateUserOnly": True
            }
        })

    def test_compliance_override_group_created(self, api):
        t = assertions.Template.from_stack(api)
        t.has_resource_properties("AWS::Cognito::UserPoolGroup", {
            "GroupName": "compliance_override"
        })

    def test_four_api_lambdas(self, api):
        t = assertions.Template.from_stack(api)
        fns = t.find_resources("AWS::Lambda::Function")
        # 4 API Lambdas + any CDK custom resource Lambdas
        assert len(fns) >= 4

    def test_six_api_routes(self, api):
        t = assertions.Template.from_stack(api)
        t.resource_count_is("AWS::ApiGatewayV2::Route", 6)
