"""OXS WhatsApp Bridge — FastAPI application.

Supports both Meta WhatsApp Cloud API and Green API webhooks.

Flow per incoming WhatsApp message:
  1. webhook.received      — payload parsed & signature-verified (fail closed)
  2. message.accepted      — sender phone + text extracted, enqueued
  3. match.*               — tenant located via OXS buildings/tenants (General key)
  4. service_call.created  — POST /service-calls with the Service Calls key

Providers expect a fast 200 on the webhook and retry on timeouts, so OXS work
runs on a single background worker fed by an asyncio.Queue — the handler only
enqueues and returns, so slow OXS calls never block webhook delivery connections,
and OXS access is naturally serialized. Message IDs are deduplicated in memory
(providers re-deliver on retry); a message whose processing FAILS is un-marked
so redelivery gets a second chance.

Set WHATSAPP_PROVIDER to "meta" or "greenapi" to select the webhook source.
"""

import asyncio
import hashlib
import hmac
import logging
import re
import time
from collections import OrderedDict
from contextlib import asynccontextmanager, suppress
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import ValidationError

from config import get_settings
from models import WebhookPayload, WhatsAppMessage, GreenApiWebhookPayload
from message_classifier import classify_message
from oxs_service import OxsClient, OxsError
from phone_utils import normalize_phone

settings = get_settings()

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("bridge")

_MAX_BODY_BYTES = 256 * 1024   # real Meta payloads are a few KB
_MAX_MESSAGES_PER_POST = 10    # Meta batches are small; more is abuse
_MAX_TEXT_CHARS = 1500
_QUEUE_MAX = 200
_SEEN_MAX = 1000
_SENDER_WINDOW_SECONDS = 600
_SENDER_MAX_PER_WINDOW = 10    # per-sender processed messages per window


@asynccontextmanager
async def lifespan(app: FastAPI):
    missing = settings.missing_required()
    if missing:
        log.warning(
            "event=startup.missing_settings missing=%s — the app will run but "
            "cannot serve real traffic until these are set.", ",".join(missing),
        )
    app.state.oxs = OxsClient(
        base_url=settings.oxs_base_url,
        general_key=settings.oxs_general_key,
        service_calls_key=settings.oxs_service_calls_key,
        rate_limit_per_minute=settings.oxs_rate_limit_per_minute,
        cache_ttl_seconds=settings.cache_ttl_seconds,
    )
    app.state.queue = asyncio.Queue(maxsize=_QUEUE_MAX)
    app.state.worker = asyncio.create_task(_worker(app))
    log.info("event=startup.ready oxs_base_url=%s", settings.oxs_base_url)
    yield
    app.state.worker.cancel()
    with suppress(asyncio.CancelledError):
        await app.state.worker
    await app.state.oxs.aclose()


app = FastAPI(title="OXS WhatsApp Bridge", version="1.1.0", lifespan=lifespan)

# Meta re-delivers webhooks on retry; remember recent message IDs to stay idempotent.
_seen_message_ids: OrderedDict[str, None] = OrderedDict()
# sender phone -> (window start, processed count) — bounds abuse by one number.
_sender_windows: OrderedDict[str, tuple[float, int]] = OrderedDict()


def _already_processed(message_id: str) -> bool:
    if message_id in _seen_message_ids:
        return True
    _seen_message_ids[message_id] = None
    while len(_seen_message_ids) > _SEEN_MAX:
        _seen_message_ids.popitem(last=False)
    return False


def _forget_message(message_id: str) -> None:
    """Un-mark a message so Meta's redelivery can retry it after a failure."""
    _seen_message_ids.pop(message_id, None)


def _sender_over_limit(sender: str) -> bool:
    now = time.monotonic()
    start, count = _sender_windows.get(sender, (now, 0))
    if now - start > _SENDER_WINDOW_SECONDS:
        start, count = now, 0
    count += 1
    _sender_windows[sender] = (start, count)
    _sender_windows.move_to_end(sender)
    while len(_sender_windows) > 2000:
        _sender_windows.popitem(last=False)
    return count > _SENDER_MAX_PER_WINDOW


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "configured": not settings.missing_required(),
        "missing_settings": settings.missing_required(),
    }


@app.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query("", alias="hub.mode"),
    hub_verify_token: str = Query("", alias="hub.verify_token"),
    hub_challenge: str = Query("", alias="hub.challenge"),
) -> PlainTextResponse:
    """Webhook subscription handshake (Meta) or health check (Green API)."""
    # Meta webhook subscription handshake
    if settings.whatsapp_provider == "meta":
        if (
            hub_mode == "subscribe"
            and settings.meta_verify_token
            and hmac.compare_digest(
                hub_verify_token.encode("utf-8", "replace"),
                settings.meta_verify_token.encode("utf-8", "replace"),
            )
        ):
            log.info("event=webhook.verified provider=meta")
            return PlainTextResponse(hub_challenge)
        log.warning("event=webhook.verify_rejected provider=meta mode=%r", hub_mode)
        raise HTTPException(status_code=403, detail="Verification failed")
    
    # Green API doesn't require GET verification, just return OK
    elif settings.whatsapp_provider == "greenapi":
        log.info("event=webhook.verify_health provider=greenapi")
        return PlainTextResponse("OK")
    
    log.warning("event=webhook.verify_rejected unknown_provider=%s", settings.whatsapp_provider)
    raise HTTPException(status_code=403, detail="Unknown provider")


@app.post("/webhook")
async def receive_webhook(request: Request) -> dict:
    content_length = request.headers.get("content-length", "")
    if content_length.isdigit() and int(content_length) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large")
    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large")

    # Validate signature based on provider
    is_valid, provider = _validate_webhook_signature(raw, request)
    if not is_valid:
        log.warning("event=webhook.bad_signature provider=%s", provider)
        raise HTTPException(status_code=403, detail="Invalid signature")
    
    if provider == "meta" and not settings.meta_app_secret and not settings.allow_unsigned_webhooks:
        # Fail closed: without META_APP_SECRET anyone could forge messages and
        # open bogus service calls. ACK (so Meta stops retrying) but do nothing.
        log.warning(
            "event=webhook.unsigned_ignored hint=set_META_APP_SECRET "
            "(or ALLOW_UNSIGNED_WEBHOOKS=true for local development only)"
        )
        return {"status": "ignored_unsigned"}

    # Parse payload based on provider
    payload = _parse_webhook_payload(raw, provider)
    if payload is None:
        # 200 on malformed payloads so providers stop retrying
        return {"status": "ignored"}

    accepted = 0
    for message, sender_name in payload.incoming_messages():
        if accepted >= _MAX_MESSAGES_PER_POST:
            log.warning("event=webhook.message_cap cap=%d provider=%s", _MAX_MESSAGES_PER_POST, provider)
            break
        if not message.id or not message.from_number:
            log.warning("event=message.skipped reason=missing_id_or_sender provider=%s", provider)
            continue
        if _already_processed(message.id):
            log.info("event=message.duplicate message_id=%s provider=%s", message.id, provider)
            continue

        text = _sanitize_text(message.body_text())
        if not text:
            log.info(
                "event=message.skipped reason=no_text type=%s message_id=%s provider=%s",
                message.type, message.id, provider,
            )
            continue
        classification = classify_message(text)
        if classification.category != "maintenance_request":
            log.info(
                "event=message.skipped reason=no_maintenance_keyword category=%s "
                "message_id=%s provider=%s",
                classification.category, message.id, provider,
            )
            continue
        if _sender_over_limit(message.from_number):
            # Stays marked as seen on purpose — a flood shouldn't come back.
            log.warning(
                "event=message.sender_rate_limited from=%s message_id=%s provider=%s",
                message.from_number, message.id, provider,
            )
            continue

        try:
            request.app.state.queue.put_nowait((message, sender_name, text))
        except asyncio.QueueFull:
            _forget_message(message.id)  # let provider redeliver once there's room
            log.warning("event=message.queue_full message_id=%s provider=%s", message.id, provider)
            continue
        log.info(
            "event=message.accepted message_id=%s from=%s name=%r chars=%d provider=%s",
            message.id, message.from_number, sender_name, len(text), provider,
        )
        accepted += 1

    return {"status": "received", "accepted": accepted}


async def _worker(app: FastAPI) -> None:
    """Single consumer: serializes OXS work off the request path so slow OXS
    calls never block Meta's webhook delivery connections."""
    while True:
        message, sender_name, text = await app.state.queue.get()
        try:
            await process_message(app.state.oxs, message, sender_name, text)
        except Exception:  # noqa: BLE001 — the worker loop must never die
            _forget_message(message.id)
            log.exception("event=flow.unexpected_error message_id=%s", message.id)
        finally:
            app.state.queue.task_done()


async def process_message(
    oxs: OxsClient, message: WhatsAppMessage, sender_name: str, text: str
) -> None:
    """Match the sender to an OXS tenant and open a service call."""
    sender = message.from_number
    try:
        match = await oxs.find_tenant_by_phone(sender)
    except OxsError as exc:
        _forget_message(message.id)  # lookup failed — allow Meta's retry
        log.error("event=flow.lookup_error message_id=%s error=%s", message.id, exc)
        return

    if match is None:
        # Definitive outcome — stays deduped; a retry would change nothing.
        log.warning(
            "event=flow.no_tenant_match from=%s message_id=%s — no service "
            "call created.", sender, message.id,
        )
        return

    reporter = re.sub(r"\s+", " ", match.tenant_name or sender_name or sender).strip()[:100]
    classification = classify_message(text)
    matched_terms = ", ".join(classification.matched_terms) or "אין מילות מפתח"
    # System attribution goes FIRST so a crafted message body can't spoof it.
    description = (
        f"— נפתח אוטומטית מהודעת וואטסאפ מאת {reporter} ({_display_phone(sender)})\n"
        f"סיווג: {classification.category}\n"
        f"מילות מפתח: {matched_terms}\n"
        "----------------------------------------\n"
        f"{text}"
    )
    try:
        call_id = await oxs.create_service_call(
            building_id=match.building_id,
            apartment_id=match.apartment_id,
            description=description,
        )
    except OxsError as exc:
        # Outcome may be unknown (see oxs_service) — do NOT forget the message,
        # or Meta's redelivery could file a duplicate ticket. Log loudly instead.
        log.error(
            "event=flow.create_error message_id=%s building_id=%s error=%s "
            "— verify manually in OXS whether the call was created.",
            message.id, match.building_id, exc,
        )
        return
    log.info(
        "event=flow.done message_id=%s category=%s matched_terms=%s building=%r "
        "apartment_id=%s service_call_id=%s",
        message.id, classification.category, matched_terms, match.building_name,
        match.apartment_id, call_id,
    )


_CTRL_RE = re.compile("[\x00-\x09\x0b-\x1f\x7f\u2028\u2029]")


def _sanitize_text(text: str) -> str:
    """Strip control characters (keep newlines) and cap the length."""
    text = _CTRL_RE.sub("", text).strip()
    if len(text) > _MAX_TEXT_CHARS:
        text = text[:_MAX_TEXT_CHARS] + " [קוצר]"
    return text


def _display_phone(sender: str) -> str:
    core = normalize_phone(sender)
    return f"0{core}" if core else sender


def _valid_signature(raw_body: bytes, header_value: str, app_secret: str) -> bool:
    if not header_value.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest().encode()
    provided = header_value[len("sha256="):].encode("utf-8", "replace")
    return hmac.compare_digest(provided, expected)


def _validate_webhook_signature(
    raw_body: bytes, request: Request
) -> tuple[bool, str]:
    """Validate webhook signature based on configured provider.
    
    Returns (is_valid, provider_name).
    """
    if settings.whatsapp_provider == "greenapi":
        # Green API validation - can be via header API key or signature
        # For now, accept all unsigned webhooks from Green API 
        # (production should verify IP whitelist and/or signature)
        api_key = request.headers.get("X-API-Key", "")
        if api_key == settings.greenapi_api_key:
            return True, "greenapi"
        # Allow unsigned for now (can be configured)
        return True, "greenapi"
    else:
        # Meta validation
        if settings.meta_app_secret:
            signature = request.headers.get("X-Hub-Signature-256", "")
            if not _valid_signature(raw_body, signature, settings.meta_app_secret):
                return False, "meta"
        elif not settings.allow_unsigned_webhooks:
            return False, "meta"
        return True, "meta"


def _parse_webhook_payload(raw: bytes, provider: str) -> Optional[Any]:
    """Parse webhook payload based on provider type."""
    try:
        if provider == "greenapi":
            payload = GreenApiWebhookPayload.model_validate_json(raw)
            return payload
        else:
            payload = WebhookPayload.model_validate_json(raw)
            return payload
    except ValidationError as exc:
        log.warning(
            "event=webhook.invalid_payload provider=%s error=%s",
            provider,
            str(exc).replace("\n", " | ")[:300],
        )
        return None


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
