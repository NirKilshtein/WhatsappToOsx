"""End-to-end smoke test with a mocked OXS API. Run: python smoke_test.py"""

import hashlib
import hmac
import json
import os
import time
from collections import Counter

os.environ.update(
    OXS_GENERAL_API_KEY="general-key-test",
    OXS_SERVICE_CALLS_API_KEY="service-key-test",
    META_VERIFY_TOKEN="verify-token-test",
    META_APP_SECRET="app-secret-test",
)

import httpx
from fastapi.testclient import TestClient

import main  # noqa: E402  (env must be set before import)

captured_service_calls: list[dict] = []
seen_keys: dict[str, str] = {}
call_counts: Counter = Counter()


def oxs_mock(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    seen_keys[path] = request.headers.get("x-api-key", "")
    call_counts[path] += 1
    if path.endswith("/buildings"):
        return httpx.Response(200, json={"data": [
            {"id": 1, "name": "בניין הרצל 10", "isActive": True},
            {"id": 2, "name": "בניין ישן", "isActive": False},
        ]})
    if path.endswith("/buildings/1/tenants"):
        return httpx.Response(200, json=[
            {"tenantId": 7, "fullName": "דנה לוי", "apartment": {"id": 42},
             "phones": {"mobile": "050-123 4567"}},
            {"tenantId": 8, "fullName": "אחר", "apartment": {"id": 43},
             "phones": {"mobile": "052-999 8888"}},
        ])
    if path.endswith("/service-calls"):
        captured_service_calls.append(json.loads(request.content))
        return httpx.Response(201, json={"id": 999})
    return httpx.Response(404, json={"error": f"unmocked {path}"})


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(b"app-secret-test", body, hashlib.sha256).hexdigest()


def wa_payload(message_id: str, text: str, sender: str = "972501234567") -> bytes:
    return json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"id": "E1", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "9725550000", "phone_number_id": "P1"},
            "contacts": [{"profile": {"name": "Dana"}, "wa_id": sender}],
            "messages": [{"from": sender, "id": message_id,
                          "timestamp": "1725000000", "type": "text",
                          "text": {"body": text}}],
        }}]}],
    }).encode()


failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def wait_until(cond, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def queue_drained() -> bool:
    # Private attr, but this is a test: unfinished == 0 means the worker is idle.
    return main.app.state.queue._unfinished_tasks == 0  # noqa: SLF001


with TestClient(main.app) as client:
    # Swap the real OXS transport for the mock.
    main.app.state.oxs._http = httpx.AsyncClient(
        base_url="https://api.oxs.co.il/api/external/v1",
        transport=httpx.MockTransport(oxs_mock),
    )

    r = client.get("/health")
    check("health ok + configured", r.status_code == 200 and r.json()["configured"] is True, r.text)

    r = client.get("/webhook", params={
        "hub.mode": "subscribe", "hub.verify_token": "verify-token-test",
        "hub.challenge": "12345"})
    check("meta verification echoes challenge", r.status_code == 200 and r.text == "12345", r.text)

    r = client.get("/webhook", params={
        "hub.mode": "subscribe", "hub.verify_token": "WRONG", "hub.challenge": "x"})
    check("wrong verify token rejected", r.status_code == 403, str(r.status_code))

    r = client.get("/webhook", params={
        "hub.mode": "subscribe", "hub.verify_token": "wröng—token",
        "hub.challenge": "x"})
    check("non-ascii verify token rejected (no 500)", r.status_code == 403, str(r.status_code))

    body = wa_payload("wamid.001", "יש נזילה בחניון")
    r = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": "sha256=bad"})
    check("bad signature rejected", r.status_code == 403, str(r.status_code))

    r = client.post("/webhook", content=body)
    check("missing signature rejected", r.status_code == 403, str(r.status_code))

    r = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body)})
    check("valid message accepted", r.status_code == 200 and r.json()["accepted"] == 1, r.text)
    check("service call created (async worker)",
          wait_until(lambda: len(captured_service_calls) == 1), str(captured_service_calls))
    if captured_service_calls:
        sc = captured_service_calls[0]
        check("buildingId=1", sc.get("buildingId") == 1, str(sc))
        check("apartmentId=42 (nested extraction)", sc.get("apartmentId") == 42, str(sc))
        desc = sc.get("description", "")
        check("description carries text + reporter",
              "נזילה" in desc and "דנה לוי" in desc, desc)
        check("attribution line comes first (anti-spoof)",
              desc.startswith("— נפתח אוטומטית"), desc[:60])
    check("general key used for reads",
          seen_keys.get("/api/external/v1/buildings") == "general-key-test", str(seen_keys))
    check("service key used for create",
          seen_keys.get("/api/external/v1/service-calls") == "service-key-test", str(seen_keys))

    r = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body)})
    check("duplicate message deduped",
          r.json()["accepted"] == 0 and len(captured_service_calls) == 1, r.text)

    body2 = wa_payload("wamid.002", "אין חשמל בלובי")
    r = client.post("/webhook", content=body2, headers={"X-Hub-Signature-256": sign(body2)})
    check("second message (cached match) creates 2nd call",
          r.status_code == 200 and wait_until(lambda: len(captured_service_calls) == 2), r.text)

    long_text = "מים בכל הבניין " * 300  # ~4500 chars
    body3 = wa_payload("wamid.003", long_text)
    r = client.post("/webhook", content=body3, headers={"X-Hub-Signature-256": sign(body3)})
    check("long message creates 3rd call",
          r.status_code == 200 and wait_until(lambda: len(captured_service_calls) == 3), r.text)
    if len(captured_service_calls) >= 3:
        desc3 = captured_service_calls[2].get("description", "")
        check("long text truncated with marker",
              "[קוצר]" in desc3 and len(desc3) < 1800, f"len={len(desc3)}")

    unknown = wa_payload("wamid.004", "שלום", sender="972539999999")
    r = client.post("/webhook", content=unknown, headers={"X-Hub-Signature-256": sign(unknown)})
    wait_until(queue_drained)
    check("unknown sender: accepted, no service call",
          r.status_code == 200 and len(captured_service_calls) == 3, r.text)

    counts_before = dict(call_counts)
    unknown2 = wa_payload("wamid.005", "עוד הודעה", sender="972539999999")
    r = client.post("/webhook", content=unknown2, headers={"X-Hub-Signature-256": sign(unknown2)})
    wait_until(queue_drained)
    check("unknown sender negative-cached (no extra OXS calls)",
          dict(call_counts) == counts_before,
          f"before={counts_before} after={dict(call_counts)}")

    check("buildings fetched exactly once (directory cache)",
          call_counts["/api/external/v1/buildings"] == 1, str(dict(call_counts)))
    check("tenants fetched exactly once (directory cache)",
          call_counts["/api/external/v1/buildings/1/tenants"] == 1, str(dict(call_counts)))

    status_event = json.dumps({"object": "whatsapp_business_account", "entry": [
        {"id": "E1", "changes": [{"field": "messages", "value": {
            "statuses": [{"id": "wamid.001", "status": "delivered"}]}}]}]}).encode()
    r = client.post("/webhook", content=status_event,
                    headers={"X-Hub-Signature-256": sign(status_event)})
    check("status-only event ignored gracefully",
          r.status_code == 200 and r.json()["accepted"] == 0, r.text)

    garbage = b'{"not": "a real payload"'
    r = client.post("/webhook", content=garbage,
                    headers={"X-Hub-Signature-256": sign(garbage)})
    check("malformed payload -> 200 ignored", r.status_code == 200, r.text)

    big = b"x" * (300 * 1024)
    r = client.post("/webhook", content=big, headers={"X-Hub-Signature-256": sign(big)})
    check("oversized body -> 413", r.status_code == 413, str(r.status_code))

print()
if failures:
    print(f"SMOKE FAILED: {len(failures)} failing check(s): {failures}")
    raise SystemExit(1)
print("SMOKE PASSED: all checks green")
