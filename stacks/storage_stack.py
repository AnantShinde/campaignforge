import aws_cdk as cdk
from aws_cdk import (
    aws_s3 as s3,
    aws_dynamodb as dynamodb,
)
from constructs import Construct


class StorageStack(cdk.Stack):
    """
    Provisions all durable storage for CampaignForge.

    S3 buckets (NFR5 — versioning required; FR11 — S3 CORS required):
      - rag-docs      : brand guidelines + regional trends, read by Bedrock KB
      - assets-input  : product reference photos, used by Nova Canvas IMAGE_VARIATION
      - outputs       : generated + composited images, CORS-enabled for browser download

    DynamoDB (NFR6 — idempotency; FR8 — analytics source):
      - CampaignTable : PK campaign_id + SK product_name
      - GSI           : approval_status → created_at (reviewer dashboard, sparse)
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        env_name: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self._env_name = env_name

        # ------------------------------------------------------------------ #
        # S3 — shared settings
        # ------------------------------------------------------------------ #
        _common_bucket_props = dict(
            versioned=True,                          # NFR5
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        # ------------------------------------------------------------------ #
        # Bucket 1: RAG docs
        # Brand guidelines + regional trends uploaded here; Bedrock KB syncs
        # from this bucket into OpenSearch Serverless via StartIngestionJob.
        # ------------------------------------------------------------------ #
        self.rag_docs_bucket = s3.Bucket(
            self,
            "RagDocsBucket",
            bucket_name=f"campaignforge-{env_name}-ragdocs",
            **_common_bucket_props,
        )

        # ------------------------------------------------------------------ #
        # Bucket 2: Asset input
        # Product reference photos uploaded by the creative team (or seeded
        # via CDK bootstrap). Lambda ImageGen reads from here for
        # IMAGE_VARIATION conditioning and graceful fallback.
        # ------------------------------------------------------------------ #
        self.assets_input_bucket = s3.Bucket(
            self,
            "AssetsInputBucket",
            bucket_name=f"campaignforge-{env_name}-assetsinput",
            **_common_bucket_props,
        )

        # ------------------------------------------------------------------ #
        # Bucket 3: Outputs
        # Generated and text-composited PNGs written by the Step Functions
        # PersistAssets state. CORS is required for FR11 (browser Blob fetch).
        # Path: /outputs/{campaign_id}/{product_name}/{ratio}.png
        # ------------------------------------------------------------------ #
        self.outputs_bucket = s3.Bucket(
            self,
            "OutputsBucket",
            bucket_name=f"campaignforge-{env_name}-outputs",
            cors=[
                s3.CorsRule(
                    allowed_methods=[s3.HttpMethods.GET],
                    allowed_origins=["*"],   # tighten to CloudFront domain in prod
                    allowed_headers=["*"],
                    max_age=3000,
                )
            ],
            **_common_bucket_props,
        )

        # ------------------------------------------------------------------ #
        # DynamoDB — CampaignTable
        #
        # Primary key:  campaign_id (PK, String) + product_name (SK, String)
        # Rationale: one campaign covers multiple products. campaign_id as PK
        # lets us Query all products in one call. product_name as SK lets us
        # target a specific product. No scan needed for either pattern.
        #
        # Billing: PAY_PER_REQUEST — zero cost at idle, no capacity planning.
        # TTL: auto-expire records after 90 days via the `ttl` attribute.
        # ------------------------------------------------------------------ #
        self.campaign_table = dynamodb.Table(
            self,
            "CampaignTable",
            table_name=f"campaignforge-{env_name}-campaigns",
            partition_key=dynamodb.Attribute(
                name="campaign_id",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="product_name",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            removal_policy=cdk.RemovalPolicy.RETAIN,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
        )

        # ------------------------------------------------------------------ #
        # GSI — status-created-index
        #
        # Hash:  approval_status (String)
        # Sort:  created_at      (String, ISO-8601 sorts lexicographically)
        #
        # Purpose: power the reviewer dashboard
        #   Query WHERE approval_status = "pending_review"
        #   ORDER BY created_at DESC LIMIT 20
        # without a full table scan.
        #
        # Sparse by design: items without approval_status (still inside the
        # Step Functions workflow) are automatically excluded — only completed
        # campaigns appear in this index.
        # ------------------------------------------------------------------ #
        self.campaign_table.add_global_secondary_index(
            index_name="status-created-index",
            partition_key=dynamodb.Attribute(
                name="approval_status",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="created_at",
                type=dynamodb.AttributeType.STRING,
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # ------------------------------------------------------------------ #
        # Outputs
        # ------------------------------------------------------------------ #
        cdk.CfnOutput(self, "RagDocsBucketName",
                       value=self.rag_docs_bucket.bucket_name,
                       export_name=f"campaignforge-{env_name}-ragdocs-bucket")

        cdk.CfnOutput(self, "AssetsInputBucketName",
                       value=self.assets_input_bucket.bucket_name,
                       export_name=f"campaignforge-{env_name}-assetsinput-bucket")

        cdk.CfnOutput(self, "OutputsBucketName",
                       value=self.outputs_bucket.bucket_name,
                       export_name=f"campaignforge-{env_name}-outputs-bucket")

        cdk.CfnOutput(self, "CampaignTableName",
                       value=self.campaign_table.table_name,
                       export_name=f"campaignforge-{env_name}-campaign-table")

        cdk.CfnOutput(self, "CampaignTableArn",
                       value=self.campaign_table.table_arn,
                       export_name=f"campaignforge-{env_name}-campaign-table-arn")
