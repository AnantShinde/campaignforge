import aws_cdk as cdk
from aws_cdk import (
    aws_apigatewayv2 as apigw,
    aws_apigatewayv2_authorizers as authorizers,
    aws_apigatewayv2_integrations as integrations,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_sqs as sqs,
)
from constructs import Construct

LAMBDA_RUNTIME = lambda_.Runtime.PYTHON_3_12


def _api_lambda(
    scope: Construct,
    construct_id: str,
    code_path: str,
    env: dict,
    timeout_seconds: int = 10,
    memory_mb: int = 256,
) -> lambda_.Function:
    """Helper: creates a Lambda function for an API Gateway route."""
    log_group = logs.LogGroup(
        scope,
        f"{construct_id}LogGroup",
        retention=logs.RetentionDays.ONE_MONTH,
        removal_policy=cdk.RemovalPolicy.DESTROY,
    )
    return lambda_.Function(
        scope,
        construct_id,
        runtime=LAMBDA_RUNTIME,
        handler="handler.handler",
        code=lambda_.Code.from_asset(code_path),
        environment=env,
        timeout=cdk.Duration.seconds(timeout_seconds),
        memory_size=memory_mb,
        architecture=lambda_.Architecture.X86_64,
        log_group=log_group,
    )


class ApiStack(cdk.Stack):
    """
    Provisions the public-facing API layer for CampaignForge.

    Components:
      Cognito User Pool  — email sign-in, JWT tokens for HTTP API v2
      HTTP API v2        — CORS enabled, JWT authorizer on all routes
      Lambda × 4        — SubmitBrief, GetCampaigns, UpdateApproval, GetInsights

    API Routes:
      POST  /brief                       → SubmitBrief
      POST  /brief/batch                 → SubmitBrief
      GET   /campaigns                   → GetCampaigns
      GET   /campaigns/{id}              → GetCampaigns
      PATCH /campaigns/{id}/approval     → UpdateApproval
      GET   /insights                    → GetInsights

    Auth:
      All routes protected by Cognito JWT (Authorization: Bearer <id_token>).
      Cognito group 'compliance_override' required to unblock compliance_blocked
      campaigns — enforced in the UpdateApproval Lambda, not at the API layer.

    Why HTTP API v2 over REST v1:
      HTTP v2 natively supports JWT authorizers without a Lambda authorizer.
      Cost: $1.00/M vs REST v1's $3.50/M. At POC scale the difference is ~$0.25/month.
      reviewed_by is extracted from JWT claims server-side — never trusted from client.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        env_name: str,
        campaign_table: dynamodb.Table,
        outputs_bucket: s3.Bucket,
        campaign_queue: sqs.Queue,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        _pfx = f"campaignforge-{env_name}"

        # ------------------------------------------------------------------ #
        # Cognito User Pool
        # ------------------------------------------------------------------ #
        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=f"{_pfx}-users",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True),
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_uppercase=True,
                require_digits=True,
                require_symbols=False,
            ),
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        # App client — no secret (browser/CLI compatible).
        # USER_PASSWORD_AUTH enabled so the test script can authenticate via CLI.
        # In production, switch to USER_SRP_AUTH only.
        self.user_pool_client = self.user_pool.add_client(
            "WebClient",
            user_pool_client_name=f"{_pfx}-web-client",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(
                user_password=True,
                user_srp=True,
            ),
            supported_identity_providers=[
                cognito.UserPoolClientIdentityProvider.COGNITO
            ],
        )

        # Group for compliance override — members can unblock compliance_blocked
        # campaigns via PATCH /campaigns/{id}/approval.
        # The Lambda enforces this; the API layer does not distinguish groups.
        cognito.CfnUserPoolGroup(
            self,
            "ComplianceOverrideGroup",
            user_pool_id=self.user_pool.user_pool_id,
            group_name="compliance_override",
            description="Members can override compliance_blocked campaigns",
        )

        # ------------------------------------------------------------------ #
        # Lambda functions
        # ------------------------------------------------------------------ #
        submit_brief_fn = _api_lambda(
            self, "SubmitBriefFn",
            code_path="lambdas/api/submit_brief",
            env={
                "CAMPAIGN_TABLE": campaign_table.table_name,
                "QUEUE_URL": campaign_queue.queue_url,
            },
        )
        campaign_table.grant_write_data(submit_brief_fn)
        campaign_queue.grant_send_messages(submit_brief_fn)

        get_campaigns_fn = _api_lambda(
            self, "GetCampaignsFn",
            code_path="lambdas/api/get_campaigns",
            env={
                "CAMPAIGN_TABLE": campaign_table.table_name,
                "OUTPUTS_BUCKET": outputs_bucket.bucket_name,
            },
            timeout_seconds=10,
            memory_mb=256,
        )
        campaign_table.grant_read_data(get_campaigns_fn)
        outputs_bucket.grant_read(get_campaigns_fn)
        # Allow generating presigned URLs for the outputs bucket
        get_campaigns_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[f"{outputs_bucket.bucket_arn}/*"],
            )
        )

        update_approval_fn = _api_lambda(
            self, "UpdateApprovalFn",
            code_path="lambdas/api/update_approval",
            env={"CAMPAIGN_TABLE": campaign_table.table_name},
        )
        campaign_table.grant_read_write_data(update_approval_fn)

        get_insights_fn = _api_lambda(
            self, "GetInsightsFn",
            code_path="lambdas/api/get_insights",
            env={"CAMPAIGN_TABLE": campaign_table.table_name},
            timeout_seconds=30,
        )
        campaign_table.grant_read_data(get_insights_fn)

        # ------------------------------------------------------------------ #
        # HTTP API v2
        # ------------------------------------------------------------------ #
        self.http_api = apigw.HttpApi(
            self,
            "HttpApi",
            api_name=f"{_pfx}-api",
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[
                    apigw.CorsHttpMethod.GET,
                    apigw.CorsHttpMethod.POST,
                    apigw.CorsHttpMethod.PATCH,
                    apigw.CorsHttpMethod.OPTIONS,
                ],
                allow_headers=["Authorization", "Content-Type"],
                max_age=cdk.Duration.hours(1),
            ),
        )

        # JWT authorizer — validates Cognito Id tokens automatically
        jwt_authorizer = authorizers.HttpUserPoolAuthorizer(
            "CognitoAuthorizer",
            self.user_pool,
            user_pool_clients=[self.user_pool_client],
        )

        # Convenience: reusable integration + authorizer builders
        def _integration(fn: lambda_.Function, name: str) -> integrations.HttpLambdaIntegration:
            return integrations.HttpLambdaIntegration(name, fn)

        _auth = {"authorizer": jwt_authorizer}

        # ── Routes ──────────────────────────────────────────────────────── #
        # POST /brief — single campaign brief submission
        self.http_api.add_routes(
            path="/brief",
            methods=[apigw.HttpMethod.POST],
            integration=_integration(submit_brief_fn, "SubmitBriefIntegration"),
            **_auth,
        )

        # POST /brief/batch — up to 50 briefs
        self.http_api.add_routes(
            path="/brief/batch",
            methods=[apigw.HttpMethod.POST],
            integration=_integration(submit_brief_fn, "SubmitBriefBatchIntegration"),
            **_auth,
        )

        # GET /campaigns — history list
        self.http_api.add_routes(
            path="/campaigns",
            methods=[apigw.HttpMethod.GET],
            integration=_integration(get_campaigns_fn, "GetCampaignsIntegration"),
            **_auth,
        )

        # GET /campaigns/{id} — poll for single campaign (every 3s by frontend)
        self.http_api.add_routes(
            path="/campaigns/{id}",
            methods=[apigw.HttpMethod.GET],
            integration=_integration(get_campaigns_fn, "GetCampaignByIdIntegration"),
            **_auth,
        )

        # PATCH /campaigns/{id}/approval — approve / reject / override
        self.http_api.add_routes(
            path="/campaigns/{id}/approval",
            methods=[apigw.HttpMethod.PATCH],
            integration=_integration(update_approval_fn, "UpdateApprovalIntegration"),
            **_auth,
        )

        # GET /insights — analytics dashboard
        self.http_api.add_routes(
            path="/insights",
            methods=[apigw.HttpMethod.GET],
            integration=_integration(get_insights_fn, "GetInsightsIntegration"),
            **_auth,
        )

        # ------------------------------------------------------------------ #
        # Outputs — consumed by test_campaign.sh and the frontend .env
        # ------------------------------------------------------------------ #
        cdk.CfnOutput(
            self, "ApiUrl",
            value=self.http_api.api_endpoint,
            export_name=f"campaignforge-{env_name}-api-url",
            description="HTTP API v2 base URL — use as VITE_API_URL in frontend .env",
        )
        cdk.CfnOutput(
            self, "UserPoolId",
            value=self.user_pool.user_pool_id,
            export_name=f"campaignforge-{env_name}-user-pool-id",
        )
        cdk.CfnOutput(
            self, "WebClientId",
            value=self.user_pool_client.user_pool_client_id,
            export_name=f"campaignforge-{env_name}-web-client-id",
            description="Cognito app client ID — use as VITE_COGNITO_CLIENT_ID in frontend .env",
        )
