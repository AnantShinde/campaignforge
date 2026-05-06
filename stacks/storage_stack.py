import aws_cdk as cdk
from aws_cdk import (
    aws_s3 as s3,
    aws_dynamodb as dynamodb,
)
from constructs import Construct


class StorageStack(cdk.Stack):
    """
    Durable storage for CampaignForge.

    S3 (NFR5 — versioning; FR11 — CORS):
      - outputs: generated images, CORS-enabled for browser download

    DynamoDB (NFR6 — idempotency):
      - CampaignTable: PK campaign_id + SK product_name
      - GSI: approval_status → created_at (reviewer dashboard, sparse)
    """

    def __init__(self, scope: Construct, construct_id: str, env_name: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        _common = dict(
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        # Generated + composited images. CORS required for FR11 (browser Blob fetch).
        # Path: outputs/{campaign_id}/{product_name}/{ratio}.png
        self.outputs_bucket = s3.Bucket(
            self, "OutputsBucket",
            bucket_name=f"campaignforge-{env_name}-outputs",
            cors=[s3.CorsRule(
                allowed_methods=[s3.HttpMethods.GET],
                allowed_origins=["*"],
                allowed_headers=["*"],
                max_age=3000,
            )],
            **_common,
        )

        # PK: campaign_id, SK: product_name
        # PAY_PER_REQUEST — zero cost at idle.
        # TTL auto-expires records after 90 days.
        self.campaign_table = dynamodb.Table(
            self, "CampaignTable",
            table_name=f"campaignforge-{env_name}-campaigns",
            partition_key=dynamodb.Attribute(name="campaign_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="product_name", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            removal_policy=cdk.RemovalPolicy.RETAIN,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
        )

        # Sparse GSI — only completed campaigns (with approval_status set) appear.
        # Powers reviewer dashboard without a full table scan.
        self.campaign_table.add_global_secondary_index(
            index_name="status-created-index",
            partition_key=dynamodb.Attribute(name="approval_status", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="created_at", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        cdk.CfnOutput(self, "OutputsBucketName",
                      value=self.outputs_bucket.bucket_name,
                      export_name=f"campaignforge-{env_name}-outputs-bucket")
        cdk.CfnOutput(self, "CampaignTableName",
                      value=self.campaign_table.table_name,
                      export_name=f"campaignforge-{env_name}-campaign-table")
        cdk.CfnOutput(self, "CampaignTableArn",
                      value=self.campaign_table.table_arn,
                      export_name=f"campaignforge-{env_name}-campaign-table-arn")
