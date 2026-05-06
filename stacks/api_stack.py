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


def _api_lambda(scope, construct_id, code_path, env, timeout_seconds=10, memory_mb=256):
    log_group = logs.LogGroup(
        scope, f"{construct_id}LogGroup",
        retention=logs.RetentionDays.ONE_MONTH,
        removal_policy=cdk.RemovalPolicy.DESTROY,
    )
    return lambda_.Function(
        scope, construct_id,
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
    Public-facing API layer for CampaignForge.

    Components:
      Cognito User Pool — email sign-in, JWT tokens
      HTTP API v2       — CORS enabled, JWT authorizer on all routes
      Lambda × 2        — SubmitBrief, GetCampaigns

    Routes:
      POST /brief           → SubmitBrief  (single brief)
      POST /brief/batch     → SubmitBrief  (up to 50 briefs)
      GET  /campaigns       → GetCampaigns (history list)
      GET  /campaigns/{id}  → GetCampaigns (poll for status + assets)
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

        # ── Cognito ───────────────────────────────────────────────────────── #
        self.user_pool = cognito.UserPool(
            self, "UserPool",
            user_pool_name=f"{_pfx}-users",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True),
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=8, require_uppercase=True,
                require_digits=True, require_symbols=False,
            ),
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        self.user_pool_client = self.user_pool.add_client(
            "WebClient",
            user_pool_client_name=f"{_pfx}-web-client",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
        )

        # ── Lambdas ───────────────────────────────────────────────────────── #
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
        )
        campaign_table.grant_read_data(get_campaigns_fn)
        outputs_bucket.grant_read(get_campaigns_fn)
        get_campaigns_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[f"{outputs_bucket.bucket_arn}/*"],
            )
        )

        # ── HTTP API v2 ───────────────────────────────────────────────────── #
        self.http_api = apigw.HttpApi(
            self, "HttpApi",
            api_name=f"{_pfx}-api",
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[
                    apigw.CorsHttpMethod.GET,
                    apigw.CorsHttpMethod.POST,
                    apigw.CorsHttpMethod.OPTIONS,
                ],
                allow_headers=["Authorization", "Content-Type"],
                max_age=cdk.Duration.hours(1),
            ),
        )

        jwt_authorizer = authorizers.HttpUserPoolAuthorizer(
            "CognitoAuthorizer", self.user_pool,
            user_pool_clients=[self.user_pool_client],
        )

        def _route(path, method, fn, name):
            self.http_api.add_routes(
                path=path, methods=[method],
                integration=integrations.HttpLambdaIntegration(name, fn),
                authorizer=jwt_authorizer,
            )

        _route("/brief",        apigw.HttpMethod.POST, submit_brief_fn,  "SubmitBriefIntegration")
        _route("/brief/batch",  apigw.HttpMethod.POST, submit_brief_fn,  "SubmitBriefBatchIntegration")
        _route("/campaigns",    apigw.HttpMethod.GET,  get_campaigns_fn, "GetCampaignsIntegration")
        _route("/campaigns/{id}", apigw.HttpMethod.GET, get_campaigns_fn, "GetCampaignByIdIntegration")

        # ── Outputs ───────────────────────────────────────────────────────── #
        cdk.CfnOutput(self, "ApiUrl",
                      value=self.http_api.api_endpoint,
                      export_name=f"campaignforge-{env_name}-api-url")
        cdk.CfnOutput(self, "UserPoolId",
                      value=self.user_pool.user_pool_id,
                      export_name=f"campaignforge-{env_name}-user-pool-id")
        cdk.CfnOutput(self, "WebClientId",
                      value=self.user_pool_client.user_pool_client_id,
                      export_name=f"campaignforge-{env_name}-web-client-id")
