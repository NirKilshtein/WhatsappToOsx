"""Send a realistic mocked WhatsApp webhook to the live deployed endpoint.

Usage:
  1) Set WEBHOOK_URL and META_APP_SECRET in your environment or .env
  2) Run:
       python live_webhook_e2e_test.py

This posts a payload shaped like Meta's WhatsApp webhook so the app's real
webhook code path runs end-to-end without needing the WhatsApp dashboard.
"""

import hashlib
import hmac
import json
import os
import uuid
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

DEFAULT_WEBHOOK_URL = "https://whatsapptoosx.onrender.com/webhook"
DEFAULT_TEST_FROM = "0547878258"


def build_mock_whatsapp_payload() -> dict[str, Any]:
    sender = os.getenv("MOCK_WHATSAPP_FROM", DEFAULT_TEST_FROM)
    message_id = os.getenv("MOCK_MESSAGE_ID", f"wamid.mock_{uuid.uuid4().hex[:12]}")
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "9999",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15551234567",
                                "phone_number_id": "123456789",
                            },
                            "contacts": [
                                {
                                    "wa_id": sender,
                                    "profile": {"name": "Dana"},
                                }
                            ],
                            "messages": [
                                {
                                    "from": sender,
                                    "id": message_id,
                                    "timestamp": "1712345678",
                                    "type": "text",
                                    "text": {"body": "Hello from the mock e2e test using 0547878258"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def sign_payload(payload: str, app_secret: str) -> str:
    digest = hmac.new(app_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def main() -> int:
    webhook_url = os.getenv("WEBHOOK_URL", DEFAULT_WEBHOOK_URL)
    app_secret = os.getenv("META_APP_SECRET", "").strip()

    if not webhook_url:
        raise SystemExit("WEBHOOK_URL is not set.")
    if not app_secret:
        raise SystemExit(
            "META_APP_SECRET is not set. Add it to your environment or .env before sending to the live webhook."
        )

    health_url = webhook_url.rsplit("/", 1)[0] + "/health"
    print(f"[1/2] Checking health: {health_url}")
    try:
        health = requests.get(health_url, timeout=20)
        print(f"health status: {health.status_code}")
        print(health.text[:300])
    except Exception as exc:  # pragma: no cover - network issue is part of runtime validation
        print(f"health check failed: {exc}")

    payload = build_mock_whatsapp_payload()
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    signature = sign_payload(body, app_secret)

    print(f"\n[2/2] Posting mocked WhatsApp payload to: {webhook_url}")
    print(f"Signature header: {signature[:20]}...")
    response = requests.post(
        webhook_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": signature,
        },
        timeout=30,
    )

    print(f"status: {response.status_code}")
    print(response.text[:1000])

    if response.status_code == 200:
        print("\n✅ Live webhook accepted the mock payload.")
        return 0

    print("\n❌ Webhook rejected the mock payload.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
