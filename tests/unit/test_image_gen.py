"""
Unit tests for image_gen Lambda handler.
Mocks all external calls (Bedrock, S3) — no AWS deployment needed.

Verifies:
  - One brief → 3 images (1×1, 9×16, 16×9)
  - IMAGE_VARIATION mode used when product reference photo exists
  - TEXT_IMAGE fallback when no reference photo
  - _ratios filter limits generation (used by quick_test.sh)
  - Retry logic handles transient Bedrock throttling

Run:  pytest tests/unit/test_image_gen.py -v
"""
import base64
import json
import os
import sys
from io import BytesIO
from unittest.mock import MagicMock, patch, call

import pytest

# Inject env vars + region before importing the handler
os.environ["ASSETS_BUCKET"]        = "test-assets-bucket"
os.environ["OUTPUTS_BUCKET"]       = "test-outputs-bucket"
os.environ["MODEL_ID"]             = "amazon.nova-canvas-v1:0"
os.environ["AWS_DEFAULT_REGION"]   = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"]    = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../lambdas/pipeline"))
from image_gen.handler import handler, IMAGE_RATIOS


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fake_png_b64() -> str:
    """Minimal valid 1×1 white PNG as base64."""
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00"
        b"\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18"
        b"\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    return base64.b64encode(png).decode()

def _bedrock_response(b64: str) -> dict:
    body = json.dumps({"images": [b64]}).encode()
    return {"body": BytesIO(body)}

def _make_event(ratios: str = "") -> dict:
    return {
        "campaign_id": "test-campaign-001",
        "product_name": "Ergo Desk Pro",
        "region": "united states",
        "audience": "professionals",
        "message": "Work smarter",
        "language": "en",
        "parallel_results": [
            {
                "image_prompt": "Premium ergonomic desk, studio lighting, 4K",
                "ad_copy": {"headline": "Work smarter", "body": "...", "cta": "Shop now"},
            },
            {"overall": "pass", "checks": []},
        ],
        **({"_ratios": ratios} if ratios else {}),
    }


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestOneBriefThreeImages:
    """Core requirement: one brief → exactly 3 images (1×1, 9×16, 16×9)."""

    @patch("image_gen.handler.s3")
    @patch("image_gen.handler.bedrock")
    def test_returns_three_ratios(self, mock_bedrock, mock_s3):
        mock_s3.list_objects_v2.return_value = {}          # no reference photo
        mock_s3.put_object.return_value = {}
        mock_s3.generate_presigned_url.return_value = "https://s3.example.com/img.png"
        mock_bedrock.invoke_model.side_effect = lambda **kw: _bedrock_response(_fake_png_b64())

        result = handler(_make_event(), {})

        assert set(result.keys()) == {"1x1", "9x16", "16x9"}, \
            f"Expected 3 ratios, got: {list(result.keys())}"

    @patch("image_gen.handler.s3")
    @patch("image_gen.handler.bedrock")
    def test_each_image_has_required_fields(self, mock_bedrock, mock_s3):
        mock_s3.list_objects_v2.return_value = {}
        mock_s3.put_object.return_value = {}
        mock_s3.generate_presigned_url.return_value = "https://s3.example.com/img.png"
        mock_bedrock.invoke_model.side_effect = lambda **kw: _bedrock_response(_fake_png_b64())

        result = handler(_make_event(), {})

        for ratio, meta in result.items():
            assert "s3_key" in meta,  f"{ratio}: missing s3_key"
            assert "url"    in meta,  f"{ratio}: missing url"
            assert "ratio"  in meta,  f"{ratio}: missing ratio"
            assert meta["url"].startswith("https://"), f"{ratio}: url not https"

    @patch("image_gen.handler.s3")
    @patch("image_gen.handler.bedrock")
    def test_bedrock_called_three_times(self, mock_bedrock, mock_s3):
        """One Bedrock call per ratio — concurrent via ThreadPoolExecutor."""
        mock_s3.list_objects_v2.return_value = {}
        mock_s3.put_object.return_value = {}
        mock_s3.generate_presigned_url.return_value = "https://s3.example.com/img.png"
        mock_bedrock.invoke_model.side_effect = lambda **kw: _bedrock_response(_fake_png_b64())

        handler(_make_event(), {})

        assert mock_bedrock.invoke_model.call_count == 3, \
            f"Expected 3 Bedrock calls, got {mock_bedrock.invoke_model.call_count}"

    @patch("image_gen.handler.s3")
    @patch("image_gen.handler.bedrock")
    def test_correct_dimensions_per_ratio(self, mock_bedrock, mock_s3):
        mock_s3.list_objects_v2.return_value = {}
        mock_s3.put_object.return_value = {}
        mock_s3.generate_presigned_url.return_value = "https://s3.example.com/img.png"

        captured_bodies = []
        def capture(**kwargs):
            captured_bodies.append(json.loads(kwargs["body"]))
            return _bedrock_response(_fake_png_b64())
        mock_bedrock.invoke_model.side_effect = capture

        handler(_make_event(), {})

        dims_seen = set()
        for body in captured_bodies:
            cfg = body.get("imageGenerationConfig", {})
            dims_seen.add((cfg.get("width"), cfg.get("height")))

        expected = {(1024, 1024), (720, 1280), (1280, 720)}
        assert dims_seen == expected, \
            f"Wrong dimensions generated: {dims_seen}"

    @patch("image_gen.handler.s3")
    @patch("image_gen.handler.bedrock")
    def test_s3_key_format(self, mock_bedrock, mock_s3):
        """Output path must follow FR4: outputs/{campaign_id}/{product}/{ratio}.png"""
        mock_s3.list_objects_v2.return_value = {}
        mock_s3.put_object.return_value = {}
        mock_s3.generate_presigned_url.return_value = "https://s3.example.com/img.png"
        mock_bedrock.invoke_model.side_effect = lambda **kw: _bedrock_response(_fake_png_b64())

        result = handler(_make_event(), {})

        for ratio, meta in result.items():
            key = meta["s3_key"]
            assert key.startswith("outputs/test-campaign-001/"), \
                f"{ratio}: s3_key doesn't start with outputs/campaign_id/"
            assert key.endswith(".png"), f"{ratio}: s3_key doesn't end with .png"


class TestImageVariationMode:
    """IMAGE_VARIATION used when product reference photo exists in assets-input."""

    @patch("image_gen.handler.s3")
    @patch("image_gen.handler.bedrock")
    def test_uses_image_variation_when_ref_exists(self, mock_bedrock, mock_s3):
        # Reference photo found
        mock_s3.list_objects_v2.return_value = {
            "Contents": [{"Key": "products/ergo-desk-pro/hero.jpg"}]
        }
        mock_s3.get_object.return_value = {
            "Body": BytesIO(base64.b64decode(_fake_png_b64()))
        }
        mock_s3.put_object.return_value = {}
        mock_s3.generate_presigned_url.return_value = "https://s3.example.com/img.png"
        mock_bedrock.invoke_model.side_effect = lambda **kw: _bedrock_response(_fake_png_b64())

        result = handler(_make_event(), {})

        # Verify IMAGE_VARIATION task type was used
        calls = mock_bedrock.invoke_model.call_args_list
        for c in calls:
            body = json.loads(c.kwargs["body"])
            assert body.get("taskType") == "IMAGE_VARIATION", \
                "Expected IMAGE_VARIATION when reference photo exists"

        # All 3 images should be tagged as variation mode
        for ratio, meta in result.items():
            assert meta.get("mode") == "variation", \
                f"{ratio}: expected mode='variation'"

    @patch("image_gen.handler.s3")
    @patch("image_gen.handler.bedrock")
    def test_falls_back_to_text_image_when_no_ref(self, mock_bedrock, mock_s3):
        mock_s3.list_objects_v2.return_value = {}  # no reference photo
        mock_s3.put_object.return_value = {}
        mock_s3.generate_presigned_url.return_value = "https://s3.example.com/img.png"
        mock_bedrock.invoke_model.side_effect = lambda **kw: _bedrock_response(_fake_png_b64())

        result = handler(_make_event(), {})

        calls = mock_bedrock.invoke_model.call_args_list
        for c in calls:
            body = json.loads(c.kwargs["body"])
            assert body.get("taskType") == "TEXT_IMAGE", \
                "Expected TEXT_IMAGE fallback when no reference photo"

        for ratio, meta in result.items():
            assert meta.get("mode") == "text", \
                f"{ratio}: expected mode='text'"


class TestRatioFilter:
    """_ratios field limits generation — used by quick_test.sh to save cost."""

    @patch("image_gen.handler.s3")
    @patch("image_gen.handler.bedrock")
    def test_single_ratio_filter(self, mock_bedrock, mock_s3):
        mock_s3.list_objects_v2.return_value = {}
        mock_s3.put_object.return_value = {}
        mock_s3.generate_presigned_url.return_value = "https://s3.example.com/img.png"
        mock_bedrock.invoke_model.side_effect = lambda **kw: _bedrock_response(_fake_png_b64())

        result = handler(_make_event(ratios="1x1"), {})

        assert set(result.keys()) == {"1x1"}, \
            f"Expected only 1x1, got {list(result.keys())}"
        assert mock_bedrock.invoke_model.call_count == 1

    @patch("image_gen.handler.s3")
    @patch("image_gen.handler.bedrock")
    def test_two_ratio_filter(self, mock_bedrock, mock_s3):
        mock_s3.list_objects_v2.return_value = {}
        mock_s3.put_object.return_value = {}
        mock_s3.generate_presigned_url.return_value = "https://s3.example.com/img.png"
        mock_bedrock.invoke_model.side_effect = lambda **kw: _bedrock_response(_fake_png_b64())

        result = handler(_make_event(ratios="1x1,16x9"), {})

        assert set(result.keys()) == {"1x1", "16x9"}
        assert mock_bedrock.invoke_model.call_count == 2

    @patch("image_gen.handler.s3")
    @patch("image_gen.handler.bedrock")
    def test_no_filter_generates_all_three(self, mock_bedrock, mock_s3):
        mock_s3.list_objects_v2.return_value = {}
        mock_s3.put_object.return_value = {}
        mock_s3.generate_presigned_url.return_value = "https://s3.example.com/img.png"
        mock_bedrock.invoke_model.side_effect = lambda **kw: _bedrock_response(_fake_png_b64())

        result = handler(_make_event(), {})

        assert len(result) == 3
        assert mock_bedrock.invoke_model.call_count == 3


class TestRetryLogic:
    """3-attempt retry with exponential backoff on transient Bedrock failures."""

    @patch("image_gen.handler.time.sleep")  # skip actual sleep in tests
    @patch("image_gen.handler.s3")
    @patch("image_gen.handler.bedrock")
    def test_succeeds_after_one_transient_failure(self, mock_bedrock, mock_s3, mock_sleep):
        mock_s3.list_objects_v2.return_value = {}
        mock_s3.put_object.return_value = {}
        mock_s3.generate_presigned_url.return_value = "https://s3.example.com/img.png"

        # First call fails, second succeeds — for every ratio
        call_counts = {"n": 0}
        def side_effect(**kwargs):
            call_counts["n"] += 1
            if call_counts["n"] % 2 == 1:
                raise Exception("ThrottlingException")
            return _bedrock_response(_fake_png_b64())

        mock_bedrock.invoke_model.side_effect = side_effect

        result = handler(_make_event(), {})

        assert len(result) == 3, "Should recover from transient failures"

    @patch("image_gen.handler.time.sleep")
    @patch("image_gen.handler.s3")
    @patch("image_gen.handler.bedrock")
    def test_raises_after_three_failures(self, mock_bedrock, mock_s3, mock_sleep):
        mock_s3.list_objects_v2.return_value = {}
        mock_bedrock.invoke_model.side_effect = Exception("ThrottlingException")

        with pytest.raises(Exception, match="ThrottlingException"):
            handler(_make_event(ratios="1x1"), {})

        assert mock_bedrock.invoke_model.call_count == 3
