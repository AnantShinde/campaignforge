import aws_cdk as cdk
from aws_cdk import (
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_lambda_event_sources as event_sources,
    aws_logs as logs,
    aws_s3 as s3,
    aws_secretsmanager as secretsmanager,
    aws_sqs as sqs,
)
from constructs import Construct

LAMBDA_RUNTIME = lambda_.Runtime.PYTHON_3_12


class PipelineStack(cdk.Stack):
    """
    Asynchronous campaign generation pipeline (simplified architecture).

    Components:
      SQS        — standard queue (NFR6: idempotent retries) + DLQ
      Lambda     — one GenerateCampaign function, SQS event source mapping

    Pipeline flow (inside the Lambda):
      1. CopyGen + Pass1Compliance  → parallel via ThreadPoolExecutor
      2. ImageGen                   → Nova Canvas (3 ratios concurrently)
      3. TextOverlay                → Pillow
      4. Pass2Compliance            → GPT-4o mini vision
      5. PersistAssets              → DynamoDB write

    SQS event source mapping uses maxConcurrency=10 to cap concurrent
    Lambda executions and avoid Bedrock throttling (NFR6 idempotency).

    Idempotency:
      Visibility timeout (360s) > Lambda timeout (300s) + buffer.
      SQS retries up to 3× before moving to DLQ.
      Lambda checks DynamoDB on start — if status != queued, exits cleanly.

    option_b_note:
      knowledge_base_id and guardrail_id are kept as optional params.
      Set knowledge_base_id to enable live RAG retrieval (Option B).
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        env_name: str,
        campaign_table: dynamodb.Table,
        outputs_bucket: s3.Bucket,
        openai_secret: secretsmanager.Secret,
        google_secret: secretsmanager.Secret,
        knowledge_base_id: str = "",
        guardrail_id: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        _pfx = f"campaignforge-{env_name}"

        # ── SQS (NFR6) ────────────────────────────────────────────────────── #
        dlq = sqs.Queue(
            self, "CampaignGenDLQ",
            queue_name=f"{_pfx}-campaign-gen-dlq",
            retention_period=cdk.Duration.days(14),
        )

        self.campaign_queue = sqs.Queue(
            self, "CampaignGenQueue",
            queue_name=f"{_pfx}-campaign-gen",
            visibility_timeout=cdk.Duration.seconds(360),
            retention_period=cdk.Duration.days(1),
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=dlq),
        )

        # ── GenerateCampaign Lambda ───────────────────────────────────────── #
        log_group = logs.LogGroup(
            self, "GenerateCampaignLogGroup",
            log_group_name=f"/aws/lambda/{_pfx}-generate-campaign",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        self.generate_fn = lambda_.Function(
            self, "GenerateCampaignFn",
            function_name=f"{_pfx}-generate-campaign",
            runtime=LAMBDA_RUNTIME,
            handler="main.handler",
            code=lambda_.Code.from_asset("lambdas/generate_campaign"),
            timeout=cdk.Duration.seconds(300),
            memory_size=1024,
            architecture=lambda_.Architecture.X86_64,
            log_group=log_group,
            environment={
                "OUTPUTS_BUCKET":    outputs_bucket.bucket_name,
                "CAMPAIGN_TABLE":    campaign_table.table_name,
                "OPENAI_SECRET_ARN": openai_secret.secret_arn,
                "GOOGLE_SECRET_ARN": google_secret.secret_arn,
                "OPENAI_MODEL":      "gpt-4o-mini",
                "KNOWLEDGE_BASE_ID": knowledge_base_id,
            },
        )

        # ── IAM permissions ───────────────────────────────────────────────── #
        outputs_bucket.grant_read_write(self.generate_fn)
        campaign_table.grant_read_write_data(self.generate_fn)
        openai_secret.grant_read(self.generate_fn)
        google_secret.grant_read(self.generate_fn)

        if knowledge_base_id:
            self.generate_fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["bedrock:Retrieve"],
                    resources=[
                        f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/{knowledge_base_id}"
                    ],
                )
            )

        # ── SQS → Lambda (event source mapping, maxConcurrency=10) ───────── #
        self.generate_fn.add_event_source(
            event_sources.SqsEventSource(
                self.campaign_queue,
                batch_size=1,
                max_concurrency=10,
            )
        )

        # ── Outputs ───────────────────────────────────────────────────────── #
        cdk.CfnOutput(self, "CampaignQueueUrl",
                      value=self.campaign_queue.queue_url,
                      export_name=f"campaignforge-{env_name}-queue-url")
        cdk.CfnOutput(self, "GenerateFnArn",
                      value=self.generate_fn.function_arn,
                      export_name=f"campaignforge-{env_name}-generate-fn-arn")
