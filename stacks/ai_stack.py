import json
import aws_cdk as cdk
from aws_cdk import (
    aws_iam as iam,
    aws_opensearchserverless as aoss,
    aws_bedrock as bedrock,
    aws_s3 as s3,
)
from constructs import Construct


class AiStack(cdk.Stack):
    """
    Provisions all AI/ML infrastructure for CampaignForge.

    Resources:
      - IAM role         : Bedrock Knowledge Base service role
      - OpenSearch AOSS  : VECTORSEARCH collection (brand guidelines + regional trends)
      - Bedrock KB       : one KB, two S3 data sources, Titan Embed v2 (1024-dim)
      - Bedrock Guardrail: model-layer safety gate applied to Claude Haiku (layer 1 of 2)

    Compliance layer architecture:
      Layer 1 — Bedrock Guardrail (this stack)
        Deterministic, model-level blocking of toxic content and prompt attacks.
        Applied at every Claude Haiku invocation before copy reaches GPT-4o mini.
      Layer 2 — GPT-4o mini (ComplianceChecker Lambda, pipeline_stack)
        Marketing-specific two-pass audit: text pre-generation + vision post-generation.

    Token note:
      The AOSS data access policy embeds the KB role ARN, which is a CDK token at
      synth time. Stack.to_json_string() is used (not json.dumps) so CDK resolves
      the token to a proper CloudFormation intrinsic in the synthesised template.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        env_name: str,
        rag_docs_bucket: s3.Bucket,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Short prefix reused across all resource names.
        # AOSS policy names are capped at 32 characters — keep this in mind.
        _pfx = f"campaignforge-{env_name}"   # e.g. "campaignforge-dev" (17 chars)

        # ------------------------------------------------------------------ #
        # IAM Role — Bedrock Knowledge Base service principal
        #
        # Created before the AOSS access policy so its ARN (a CloudFormation
        # token) can be embedded via to_json_string() in the policy document.
        # ------------------------------------------------------------------ #
        self.kb_role = iam.Role(
            self,
            "BedrockKbRole",
            role_name=f"{_pfx}-bedrock-kb-role",
            assumed_by=iam.ServicePrincipal(
                "bedrock.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/*"
                        )
                    },
                },
            ),
            description="Bedrock Knowledge Base service role for CampaignForge",
        )

        # Embed: vectorise document chunks with Titan Embed Text v2
        self.kb_role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockEmbed",
                actions=["bedrock:InvokeModel"],
                resources=[
                    f"arn:aws:bedrock:{self.region}::foundation-model/amazon.titan-embed-text-v2:0"
                ],
            )
        )

        # S3: read brand guidelines and regional trend documents
        self.kb_role.add_to_policy(
            iam.PolicyStatement(
                sid="RagDocsRead",
                actions=["s3:GetObject", "s3:ListBucket"],
                resources=[
                    rag_docs_bucket.bucket_arn,
                    f"{rag_docs_bucket.bucket_arn}/*",
                ],
            )
        )

        # ------------------------------------------------------------------ #
        # OpenSearch Serverless — VECTORSEARCH collection
        #
        # Three policies must exist before the collection is created:
        #   1. Encryption  — at-rest encryption using AWS-owned key
        #   2. Network     — public endpoint (required for Bedrock managed service)
        #   3. Data access — grants KB role CRUD rights on indices
        #
        # AOSS name limits: collection and policy names ≤ 32 chars, lowercase.
        #   _pfx = "campaignforge-dev" (17)
        #   collection : "{_pfx}-kb"       → 20 chars ✓
        #   enc policy : "{_pfx}-kb-enc"   → 24 chars ✓
        #   net policy : "{_pfx}-kb-net"   → 24 chars ✓
        #   data policy: "{_pfx}-kb-data"  → 25 chars ✓
        # ------------------------------------------------------------------ #
        encryption_policy = aoss.CfnSecurityPolicy(
            self,
            "KbEncryptionPolicy",
            name=f"{_pfx}-kb-enc",
            type="encryption",
            policy=json.dumps({
                "Rules": [{
                    "ResourceType": "collection",
                    "Resource": [f"collection/{_pfx}-kb"],
                }],
                "AWSOwnedKey": True,
            }),
        )

        # Bedrock's regional service endpoints reach OpenSearch over the public
        # endpoint. AllowFromPublic is required at POC scale; lock to a VPC
        # endpoint in production if the Bedrock service supports it in-region.
        network_policy = aoss.CfnSecurityPolicy(
            self,
            "KbNetworkPolicy",
            name=f"{_pfx}-kb-net",
            type="network",
            policy=json.dumps([{
                "Description": "Public access for Bedrock Knowledge Base service",
                "Rules": [
                    {
                        "ResourceType": "collection",
                        "Resource": [f"collection/{_pfx}-kb"],
                    },
                    {
                        "ResourceType": "dashboard",
                        "Resource": [f"collection/{_pfx}-kb"],
                    },
                ],
                "AllowFromPublic": True,
            }]),
        )

        self.collection = aoss.CfnCollection(
            self,
            "KbCollection",
            name=f"{_pfx}-kb",
            type="VECTORSEARCH",
            description=(
                "CampaignForge vector store — brand guidelines and regional trends"
            ),
        )
        self.collection.add_dependency(encryption_policy)
        self.collection.add_dependency(network_policy)

        # Data access policy — grants the KB role index-level CRUD.
        #
        # self.kb_role.role_arn is a CDK token; json.dumps() would serialise it
        # as a literal placeholder string. Stack.to_json_string() resolves it to
        # the correct CloudFormation {"Fn::GetAtt": [...]} intrinsic instead.
        access_policy_doc = self.to_json_string([{
            "Description": "Bedrock KB service role — index and collection access",
            "Rules": [
                {
                    "ResourceType": "index",
                    "Resource": [f"index/{_pfx}-kb/*"],
                    "Permission": [
                        "aoss:CreateIndex",
                        "aoss:DeleteIndex",
                        "aoss:UpdateIndex",
                        "aoss:DescribeIndex",
                        "aoss:ReadDocument",
                        "aoss:WriteDocument",
                    ],
                },
                {
                    "ResourceType": "collection",
                    "Resource": [f"collection/{_pfx}-kb"],
                    "Permission": [
                        "aoss:DescribeCollectionItems",
                        "aoss:CreateCollectionItems",
                        "aoss:DeleteCollectionItems",
                    ],
                },
            ],
            "Principal": [self.kb_role.role_arn],
        }])

        access_policy = aoss.CfnAccessPolicy(
            self,
            "KbAccessPolicy",
            name=f"{_pfx}-kb-data",
            type="data",
            policy=access_policy_doc,
        )
        access_policy.add_dependency(self.collection)

        # Allow the KB role to call the AOSS collection API
        self.kb_role.add_to_policy(
            iam.PolicyStatement(
                sid="OpenSearchAccess",
                actions=["aoss:APIAccessAll"],
                resources=[self.collection.attr_arn],
            )
        )

        # ------------------------------------------------------------------ #
        # Bedrock Knowledge Base
        #
        # One KB — two data sources (brand-guidelines/ + regional-trends/).
        # A single vector search retrieves chunks from both S3 prefixes
        # simultaneously; Bedrock ranks all results by cosine similarity.
        # No manual result merging required.
        #
        # Embeddings : Titan Embed Text v2, 1024 dimensions.
        # Bedrock auto-creates the vector index in the AOSS collection on
        # the first StartIngestionJob. The index name and field mapping
        # declared here must match what Bedrock writes.
        # ------------------------------------------------------------------ #
        self.knowledge_base = bedrock.CfnKnowledgeBase(
            self,
            "CampaignKnowledgeBase",
            name=f"{_pfx}-kb",
            role_arn=self.kb_role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                    embedding_model_arn=(
                        f"arn:aws:bedrock:{self.region}::foundation-model/"
                        "amazon.titan-embed-text-v2:0"
                    ),
                ),
            ),
            storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                type="OPENSEARCH_SERVERLESS",
                opensearch_serverless_configuration=bedrock.CfnKnowledgeBase.OpenSearchServerlessConfigurationProperty(
                    collection_arn=self.collection.attr_arn,
                    vector_index_name=f"{_pfx}-index",
                    field_mapping=bedrock.CfnKnowledgeBase.OpenSearchServerlessFieldMappingProperty(
                        vector_field="embedding",
                        text_field="text",
                        metadata_field="metadata",
                    ),
                ),
            ),
        )
        self.knowledge_base.add_dependency(access_policy)

        # Chunking: FIXED_SIZE 300 tokens / 20 % overlap.
        # 300 tokens ≈ one guideline section — large enough for context,
        # small enough to stay relevant in top-k retrieval.
        _chunking = bedrock.CfnDataSource.VectorIngestionConfigurationProperty(
            chunking_configuration=bedrock.CfnDataSource.ChunkingConfigurationProperty(
                chunking_strategy="FIXED_SIZE",
                fixed_size_chunking_configuration=bedrock.CfnDataSource.FixedSizeChunkingConfigurationProperty(
                    max_tokens=300,
                    overlap_percentage=20,
                ),
            ),
        )

        # Data Source 1 — Brand Guidelines
        # Brand voice, color palette, tone rules, logo usage, forbidden elements.
        brand_ds = bedrock.CfnDataSource(
            self,
            "BrandGuidelinesDataSource",
            knowledge_base_id=self.knowledge_base.attr_knowledge_base_id,
            name="brand-guidelines",
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="S3",
                s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                    bucket_arn=rag_docs_bucket.bucket_arn,
                    inclusion_prefixes=["brand-guidelines/"],
                ),
            ),
            vector_ingestion_configuration=_chunking,
        )
        brand_ds.add_dependency(self.knowledge_base)

        # Data Source 2 — Regional Trends
        # Per-market consumer insights, cultural nuances, seasonal context.
        regional_ds = bedrock.CfnDataSource(
            self,
            "RegionalTrendsDataSource",
            knowledge_base_id=self.knowledge_base.attr_knowledge_base_id,
            name="regional-trends",
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="S3",
                s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                    bucket_arn=rag_docs_bucket.bucket_arn,
                    inclusion_prefixes=["regional-trends/"],
                ),
            ),
            vector_ingestion_configuration=_chunking,
        )
        regional_ds.add_dependency(self.knowledge_base)

        # ------------------------------------------------------------------ #
        # Bedrock Guardrail — compliance layer 1 (model-level, deterministic)
        #
        # Applied to every Claude Haiku InvokeModel call via guardrailIdentifier
        # + guardrailVersion parameters. Blocks at the model layer before output
        # reaches the GPT-4o mini compliance check (layer 2).
        #
        # Safety filters (HIGH): blocks toxic / hateful / violent content.
        # PROMPT_ATTACK (input HIGH, output NONE): blocks jailbreak attempts.
        # Topic policy: denies competitor promotion and political content.
        # Word policy : blocks prohibited advertising superlatives + PROFANITY.
        # ------------------------------------------------------------------ #
        self.guardrail = bedrock.CfnGuardrail(
            self,
            "CampaignGuardrail",
            name=f"{_pfx}-guardrail",
            description=(
                "CampaignForge brand and legal safety guardrail — "
                "applied to Claude Haiku (compliance layer 1 of 2)"
            ),
            blocked_input_messaging=(
                "This input violates CampaignForge brand or legal policies "
                "and cannot be processed."
            ),
            blocked_outputs_messaging=(
                "This output has been blocked to protect brand integrity "
                "and legal compliance."
            ),
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="HATE",
                        input_strength="HIGH",
                        output_strength="HIGH",
                    ),
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="VIOLENCE",
                        input_strength="HIGH",
                        output_strength="HIGH",
                    ),
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="INSULTS",
                        input_strength="HIGH",
                        output_strength="HIGH",
                    ),
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="SEXUAL",
                        input_strength="HIGH",
                        output_strength="HIGH",
                    ),
                    # PROMPT_ATTACK is input-only — output_strength must be NONE
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="PROMPT_ATTACK",
                        input_strength="HIGH",
                        output_strength="NONE",
                    ),
                ],
            ),
            topic_policy_config=bedrock.CfnGuardrail.TopicPolicyConfigProperty(
                topics_config=[
                    bedrock.CfnGuardrail.TopicConfigProperty(
                        name="CompetitorPromotion",
                        definition=(
                            "Any content that promotes, positively mentions, or "
                            "favorably compares competitor products or brands."
                        ),
                        examples=[
                            "Our product is better than Brand X",
                            "Unlike our competitors",
                            "Leading the market over rival Y",
                        ],
                        type="DENY",
                    ),
                    bedrock.CfnGuardrail.TopicConfigProperty(
                        name="PoliticalContent",
                        definition=(
                            "Political opinions, party endorsements, or content "
                            "that could be perceived as politically divisive."
                        ),
                        examples=[
                            "Vote for this candidate",
                            "Support this political party",
                        ],
                        type="DENY",
                    ),
                ],
            ),
            word_policy_config=bedrock.CfnGuardrail.WordPolicyConfigProperty(
                managed_word_lists_config=[
                    bedrock.CfnGuardrail.ManagedWordsConfigProperty(type="PROFANITY"),
                ],
                words_config=[
                    bedrock.CfnGuardrail.WordConfigProperty(text="guaranteed"),
                    bedrock.CfnGuardrail.WordConfigProperty(text="proven"),
                    bedrock.CfnGuardrail.WordConfigProperty(text="cure"),
                    bedrock.CfnGuardrail.WordConfigProperty(text="risk-free"),
                    bedrock.CfnGuardrail.WordConfigProperty(text="clinically proven"),
                    bedrock.CfnGuardrail.WordConfigProperty(text="scientifically proven"),
                    bedrock.CfnGuardrail.WordConfigProperty(text="number one"),
                    bedrock.CfnGuardrail.WordConfigProperty(text="#1"),
                ],
            ),
        )

        # ------------------------------------------------------------------ #
        # Outputs — consumed by pipeline_stack and api_stack
        # ------------------------------------------------------------------ #
        cdk.CfnOutput(
            self, "KnowledgeBaseId",
            value=self.knowledge_base.attr_knowledge_base_id,
            export_name=f"campaignforge-{env_name}-kb-id",
        )
        cdk.CfnOutput(
            self, "GuardrailId",
            value=self.guardrail.attr_guardrail_id,
            export_name=f"campaignforge-{env_name}-guardrail-id",
        )
        cdk.CfnOutput(
            self, "GuardrailArn",
            value=self.guardrail.attr_guardrail_arn,
            export_name=f"campaignforge-{env_name}-guardrail-arn",
        )
        cdk.CfnOutput(
            self, "OpenSearchCollectionArn",
            value=self.collection.attr_arn,
            export_name=f"campaignforge-{env_name}-aoss-arn",
        )
        cdk.CfnOutput(
            self, "BedrockKbRoleArn",
            value=self.kb_role.role_arn,
            export_name=f"campaignforge-{env_name}-kb-role-arn",
        )
