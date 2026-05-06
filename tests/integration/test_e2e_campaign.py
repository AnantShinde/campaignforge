"""
End-to-end integration test — requires all 5 CDK stacks deployed.
Submits one brief, polls until complete, asserts 3 images were generated.

Prerequisites:
  - All stacks deployed (./scripts/deploy.sh dev)
  - OpenAI key injected (./scripts/inject_openai_key.sh dev)
  - Bedrock model access enabled (Nova Canvas + Titan Embed)
  - KB synced (./scripts/sync_kb.sh dev)

Run:
  AWS_PROFILE=campaignforge pytest tests/integration/test_e2e_campaign.py -v -s

Cost per run: ~$0.09 (1 image via quick_test _ratios filter)
Full 3-image run: set CAMPAIGNFORGE_ALL_RATIOS=1 → ~$0.49
"""
import json
import os
import time

import boto3
import pytest
import requests

# ── Config from environment / stack outputs ────────────────────────────────────

ENV         = os.getenv("CAMPAIGNFORGE_ENV", "dev")
AWS_PROFILE = os.getenv("AWS_PROFILE", "campaignforge")
AWS_REGION  = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
ALL_RATIOS  = os.getenv("CAMPAIGNFORGE_ALL_RATIOS", "0") == "1"
TIMEOUT_S   = int(os.getenv("CAMPAIGNFORGE_TIMEOUT", "360"))
POLL_S      = 5


def _cf_output(stack_name: str, key_fragment: str) -> str:
    session = boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    cf = session.client("cloudformation")
    resp = cf.describe_stacks(StackName=stack_name)
    outputs = resp["Stacks"][0].get("Outputs", [])
    for o in outputs:
        if key_fragment.lower() in o["OutputKey"].lower():
            return o["OutputValue"]
    return ""


def _get_jwt(user_pool_id: str, client_id: str) -> str:
    session = boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    idp = session.client("cognito-idp")

    test_email = f"e2e-test@campaignforge.internal"
    test_pass  = "E2eTest@2025!"

    try:
        idp.admin_create_user(
            UserPoolId=user_pool_id,
            Username=test_email,
            TemporaryPassword=test_pass,
            MessageAction="SUPPRESS",
        )
    except idp.exceptions.UsernameExistsException:
        pass

    idp.admin_set_user_password(
        UserPoolId=user_pool_id,
        Username=test_email,
        Password=test_pass,
        Permanent=True,
    )

    auth = idp.initiate_auth(
        AuthFlow="USER_PASSWORD_AUTH",
        ClientId=client_id,
        AuthParameters={"USERNAME": test_email, "PASSWORD": test_pass},
    )
    return auth["AuthenticationResult"]["IdToken"]


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def api_url():
    url = _cf_output(f"CampaignForge-{ENV}-Api", "ApiUrl")
    if not url:
        pytest.skip(f"CampaignForge-{ENV}-Api not deployed or ApiUrl output missing")
    return url.rstrip("/")


@pytest.fixture(scope="module")
def jwt(api_url):
    pool_id   = _cf_output(f"CampaignForge-{ENV}-Api", "UserPoolId")
    client_id = _cf_output(f"CampaignForge-{ENV}-Api", "WebClientId")
    if not pool_id or not client_id:
        pytest.skip("Cognito outputs missing from Api stack")
    return _get_jwt(pool_id, client_id)


@pytest.fixture(scope="module")
def auth_headers(jwt):
    return {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestOneBriefThreeImages:
    """
    Submit one campaign brief and verify the full pipeline produces
    3 images (one per ratio: 1×1, 9×16, 16×9).
    """

    @pytest.fixture(scope="class")
    def campaign(self, api_url, auth_headers):
        """Submit brief → poll until complete → return blueprint."""
        brief = {
            "product_name": "Ergo Desk Pro",
            "region": "united states",
            "audience": "professionals aged 25-45",
            "message": "Work smarter, perform better",
            "language": "en",
        }
        # Limit to 1 image in quick mode, all 3 in full mode
        if not ALL_RATIOS:
            brief["_ratios"] = "1x1,9x16,16x9"  # all 3, but cheap quick path for demo

        resp = requests.post(f"{api_url}/brief", json=brief, headers=auth_headers)
        assert resp.status_code == 202, f"Submission failed: {resp.text}"

        campaign_id = resp.json()["campaign_id"]
        print(f"\n  Submitted campaign: {campaign_id}")

        # Poll until complete
        start = time.time()
        while True:
            elapsed = time.time() - start
            assert elapsed < TIMEOUT_S, f"Timed out after {elapsed:.0f}s"

            poll = requests.get(f"{api_url}/campaigns/{campaign_id}", headers=auth_headers)
            assert poll.status_code == 200

            blueprints = poll.json().get("blueprints", [])
            assert blueprints, "No blueprints in response"

            bp = blueprints[0]
            status = bp.get("status")
            print(f"  [{elapsed:.0f}s] status={status} approval={bp.get('approval_status','')}", end="\r")

            if status == "complete":
                print(f"\n  Complete in {elapsed:.0f}s")
                return bp

            assert status != "failed", f"Campaign failed: {bp.get('failure_reason')}"
            time.sleep(POLL_S)

    # ── Image assertions ────────────────────────────────────────────────────

    def test_three_images_generated(self, campaign):
        images = campaign.get("images", {})
        assert len(images) == 3, \
            f"Expected 3 images, got {len(images)}: {list(images.keys())}"

    def test_all_ratio_keys_present(self, campaign):
        images = campaign.get("images", {})
        assert "1x1"  in images, "Missing 1×1 (Instagram)"
        assert "9x16" in images, "Missing 9×16 (TikTok/Reels)"
        assert "16x9" in images, "Missing 16×9 (YouTube)"

    def test_each_image_has_presigned_url(self, campaign):
        images = campaign.get("images", {})
        for ratio, meta in images.items():
            assert isinstance(meta, dict), f"{ratio}: meta is not a dict"
            url = meta.get("url", "")
            assert url.startswith("https://"), \
                f"{ratio}: missing or invalid presigned URL: '{url}'"

    def test_images_are_downloadable(self, campaign):
        """Presigned URLs must return actual PNG data (FR11)."""
        images = campaign.get("images", {})
        for ratio, meta in images.items():
            url = meta.get("url", "")
            if not url:
                continue
            r = requests.get(url, timeout=30)
            assert r.status_code == 200, \
                f"{ratio}: download returned HTTP {r.status_code}"
            assert r.headers.get("Content-Type", "").startswith("image/"), \
                f"{ratio}: Content-Type is not image/*: {r.headers.get('Content-Type')}"
            assert len(r.content) > 1000, \
                f"{ratio}: image too small ({len(r.content)} bytes) — likely empty"

    # ── Ad copy assertions ──────────────────────────────────────────────────

    def test_ad_copy_generated(self, campaign):
        ad_copy = campaign.get("ad_copy", [])
        assert ad_copy, "No ad copy in blueprint"
        copy = ad_copy[0]
        assert copy.get("headline"), "Missing headline"
        assert copy.get("body"),     "Missing body"
        assert copy.get("cta"),      "Missing CTA"

    def test_ad_copy_is_english(self, campaign):
        ad_copy = campaign.get("ad_copy", [{}])
        assert ad_copy[0].get("lang") == "en"

    # ── Compliance assertions ───────────────────────────────────────────────

    def test_pass1_compliance_ran(self, campaign):
        p1 = campaign.get("compliance_pass1", {})
        assert p1, "compliance_pass1 missing from blueprint"
        assert p1.get("overall") in {"pass", "warn", "fail"}, \
            f"Invalid Pass 1 overall: {p1.get('overall')}"
        assert len(p1.get("checks", [])) > 0, "Pass 1 has no checks"

    def test_pass2_compliance_ran(self, campaign):
        p2 = campaign.get("compliance_pass2", {})
        assert p2, "compliance_pass2 missing from blueprint"
        assert p2.get("overall") in {"pass", "warn", "fail"}, \
            f"Invalid Pass 2 overall: {p2.get('overall')}"

    def test_campaign_not_compliance_blocked(self, campaign):
        status = campaign.get("approval_status")
        assert status != "compliance_blocked", \
            "Campaign hit a hard compliance fail — check compliance_pass2 in the blueprint"

    # ── Approval workflow assertion ─────────────────────────────────────────

    def test_approval_status_is_pending_review(self, campaign):
        """FR7: campaigns must be reviewed and approved before being considered final."""
        assert campaign.get("approval_status") == "pending_review", \
            f"Expected pending_review, got: {campaign.get('approval_status')}"

    # ── Persistence assertions ──────────────────────────────────────────────

    def test_campaign_retrievable_from_api(self, campaign, api_url, auth_headers):
        """Campaign must be readable after completion (FR4 — assets saved)."""
        campaign_id = campaign["campaign_id"]
        resp = requests.get(f"{api_url}/campaigns/{campaign_id}", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["campaign_id"] == campaign_id

    def test_campaign_appears_in_list(self, campaign, api_url, auth_headers):
        campaign_id = campaign["campaign_id"]
        resp = requests.get(f"{api_url}/campaigns", headers=auth_headers)
        assert resp.status_code == 200
        ids = [c.get("campaign_id") for c in resp.json()]
        assert campaign_id in ids, "Campaign not found in GET /campaigns list"
