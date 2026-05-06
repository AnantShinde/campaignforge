import aws_cdk as cdk
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct


class SecretsStack(cdk.Stack):
    """
    Provisions Secrets Manager secrets consumed by downstream stacks.

    Secrets created:
      - openai_api_key: placeholder value; must be updated manually after deploy
        via the AWS Console or CLI before the compliance Lambdas can run.

    The secret ARN is exposed as a stack output and as a property so that
    the ApiStack can grant its Lambda functions read access without creating
    a circular dependency.
    """

    def __init__(self, scope: Construct, construct_id: str, env_name: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self._env_name = env_name

        # ------------------------------------------------------------------ #
        # OpenAI API key
        # Stored as a plain string secret. The compliance Lambda fetches this
        # once per cold start and caches it in the execution context — never
        # in an environment variable.
        #
        # After deploy, update the value with:
        #   aws secretsmanager put-secret-value \
        #     --secret-id <SecretArn> \
        #     --secret-string "sk-..."
        # ------------------------------------------------------------------ #
        self.openai_api_key_secret = secretsmanager.Secret(
            self,
            "OpenAIApiKey",
            secret_name=f"campaignforge/{env_name}/openai-api-key",
            description="OpenAI API key for GPT-4o mini compliance checks (Pass 1 text + Pass 2 vision)",
            # Placeholder — the actual key must be injected post-deploy.
            # generate_secret_string would create a random value not usable as
            # an API key, so we use a string secret with a clear placeholder.
            secret_string_value=cdk.SecretValue.unsafe_plain_text("REPLACE_ME"),
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        # ------------------------------------------------------------------ #
        # Outputs
        # ------------------------------------------------------------------ #
        cdk.CfnOutput(
            self,
            "OpenAIApiKeySecretArn",
            value=self.openai_api_key_secret.secret_arn,
            description="ARN of the OpenAI API key secret — update the value before deploying Lambdas",
            export_name=f"campaignforge-{env_name}-openai-secret-arn",
        )

        cdk.CfnOutput(
            self,
            "OpenAIApiKeySecretName",
            value=self.openai_api_key_secret.secret_name,
            description="Name of the OpenAI API key secret",
            export_name=f"campaignforge-{env_name}-openai-secret-name",
        )
