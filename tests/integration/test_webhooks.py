"""The two webhooks, against the deployed system.

Both were written with every adapter mocked, and both broke the first time a real request
reached them: post_call returned 500 because it passed a keyword the adapter forwards
straight to boto, and conversation_init had no integration coverage at all.

Mocked tests cannot see a seam. These are the seams.

    AWS_PROFILE=voice-agent-admin uv run pytest tests/integration/test_webhooks.py -v
"""

import hashlib
import hmac
import json
import subprocess
import time
import uuid

import httpx
import pytest

TIMEOUT = 20.0
KNOWN_NUMBER = "+41 91 604 77 31"
KNOWN_CUSTOMER = "446019693775"


def _shell(*command: str) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


@pytest.fixture(scope="module")
def endpoint() -> str:
    url = _shell("terraform", "-chdir=infra/terraform", "output", "-raw", "api_endpoint")
    if not url.startswith("https://"):
        pytest.skip("no deployed stack")
    return url.rstrip("/")


@pytest.fixture(scope="module")
def webhook_secret() -> str:
    secret = _shell(
        "aws",
        "secretsmanager",
        "get-secret-value",
        "--secret-id",
        "voice-agent/elevenlabs/webhook-secret",
        "--query",
        "SecretString",
        "--output",
        "text",
    )
    if not secret:
        pytest.skip("no webhook secret")
    return secret


def _post_call(
    endpoint: str, secret: str, payload: dict, *, age: int = 0, digest: str | None = None
):
    body = json.dumps(payload)
    timestamp = int(time.time()) - age
    signature = (
        digest
        or hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()
    )
    return httpx.post(
        f"{endpoint}/webhooks/post-call",
        headers={
            "ElevenLabs-Signature": f"t={timestamp},v0={signature}",
            "content-type": "application/json",
        },
        content=body,
        timeout=TIMEOUT,
    )


class TestConversationInitiation:
    def test_a_known_number_is_greeted_in_its_language(self, endpoint):
        response = httpx.post(
            f"{endpoint}/webhooks/conversation-initiation",
            json={"caller_id": KNOWN_NUMBER},
            timeout=TIMEOUT,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["dynamic_variables"]["greeting_language"] == "it"
        assert body["conversation_config_override"]["agent"]["language"] == "it"

    def test_the_candidate_is_the_company_not_the_person(self, endpoint):
        body = httpx.post(
            f"{endpoint}/webhooks/conversation-initiation",
            json={"caller_id": KNOWN_NUMBER},
            timeout=TIMEOUT,
        ).json()
        assert body["dynamic_variables"]["secret__candidate_customer_id"] == KNOWN_CUSTOMER

    def test_an_unknown_number_falls_back_without_a_candidate(self, endpoint):
        body = httpx.post(
            f"{endpoint}/webhooks/conversation-initiation",
            json={"caller_id": "+41 99 999 99 99"},
            timeout=TIMEOUT,
        ).json()
        assert body["dynamic_variables"]["greeting_language"] == "de"
        assert body["dynamic_variables"]["secret__candidate_customer_id"] is None

    def test_it_answers_even_when_the_request_is_nonsense(self, endpoint):
        """A caller hearing the wrong language is an annoyance. A caller hearing nothing is a
        lost call, so this must never fail."""
        response = httpx.post(
            f"{endpoint}/webhooks/conversation-initiation",
            content="not json",
            headers={"content-type": "application/json"},
            timeout=TIMEOUT,
        )
        assert response.status_code == 200
        assert response.json()["dynamic_variables"]["greeting_language"] == "de"


class TestPostCall:
    def test_a_signed_delivery_is_processed(self, endpoint, webhook_secret):
        """The one that returned 500. Every mocked test passed, because the adapter that
        rejected the call was the adapter the tests replaced."""
        payload = {
            "conversation_id": f"itest_{uuid.uuid4().hex[:10]}",
            "customer_id": KNOWN_CUSTOMER,
            "metadata": {"call_duration_secs": 42, "language": "it"},
            "analysis": {"transcript_summary": "Integration probe."},
            "transcript": [],
        }
        response = _post_call(endpoint, webhook_secret, payload)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "OK"
        # Every step ran. A partial list means one of them raised.
        assert set(body["completed"]) == {
            "transcript",
            "metrics",
            "summary",
            "preferences",
            "transfer",
        }

    def test_a_forged_signature_is_rejected(self, endpoint, webhook_secret):
        payload = {"conversation_id": f"itest_{uuid.uuid4().hex[:10]}"}
        response = _post_call(endpoint, webhook_secret, payload, digest="deadbeef")
        assert response.status_code == 401

    def test_an_old_signature_is_rejected(self, endpoint, webhook_secret):
        """Valid, and captured an hour ago. Still a replay."""
        payload = {"conversation_id": f"itest_{uuid.uuid4().hex[:10]}"}
        assert _post_call(endpoint, webhook_secret, payload, age=3700).status_code == 401

    def test_a_duplicate_delivery_does_nothing_twice(self, endpoint, webhook_secret):
        payload = {
            "conversation_id": f"itest_{uuid.uuid4().hex[:10]}",
            "customer_id": KNOWN_CUSTOMER,
            "metadata": {"call_duration_secs": 10, "language": "en"},
            "analysis": {"transcript_summary": "Duplicate probe."},
        }
        assert _post_call(endpoint, webhook_secret, payload).json()["status"] == "OK"
        assert _post_call(endpoint, webhook_secret, payload).json()["status"] == "DUPLICATE"

    def test_the_transcript_reaches_s3_and_only_its_key_reaches_the_table(
        self, endpoint, webhook_secret
    ):
        """The transcript expires in ninety days; the record of it lives for ten years."""
        conversation_id = f"itest_{uuid.uuid4().hex[:10]}"
        _post_call(
            endpoint,
            webhook_secret,
            {
                "conversation_id": conversation_id,
                "customer_id": KNOWN_CUSTOMER,
                "metadata": {"call_duration_secs": 5, "language": "en"},
                "analysis": {"transcript_summary": "Key probe."},
                "transcript": [{"role": "agent", "message": "hello"}],
            },
        )

        raw = _shell(
            "aws",
            "dynamodb",
            "get-item",
            "--table-name",
            "voice-agent-conversations",
            "--key",
            json.dumps({"conversation_id": {"S": conversation_id}}),
            "--output",
            "json",
        )
        item = json.loads(raw)["Item"]
        assert item["transcript_s3_key"]["S"].endswith(f"{conversation_id}.json")
        assert "hello" not in json.dumps(item)
