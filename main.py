"""OXS WhatsApp Bridge — FastAPI application.

Supports two inbound providers, selected with WHATSAPP_PROVIDER:
  meta      — Meta WhatsApp Cloud API webhooks (X-Hub-Signature-256, fail closed)
  greenapi  — Green API webhooks (Authorization header must match
              GREENAPI_WEBHOOK_TOKEN / the instance's webhookUrlToken, fail closed)

Flow per incoming WhatsApp message:
  1. webhook.received      — payload parsed & signature-verified (fail closed)
  2. message.accepted      — sender phone + text extracted, enqueued
  3. match.*               — tenant located via OXS buildings/tenants (General key)
  4. service_call.created  — POST /service-calls with the Service Calls key

Meta expects a fast 200 on the webhook and retries on timeouts, so OXS work
runs on a single background worker fed by an asyncio.Queue — the handler only
enqueues and returns, so slow OXS calls never block Meta's keep-alive
connections, and OXS access is naturally serialized. Message IDs are
deduplicated in memory (Meta re-delivers on retry); a message whose processing
FAILS is un-marked so Meta's redelivery gets a second chance.
"""

import asyncio
import hashlib
import hmac
import logging
import re
import time
from collections import OrderedDict
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import ValidationError

from config import get_settings
from message_classifier import classify_message
from models import GreenApiWebhookPayload, WebhookPayload, WhatsAppMessage
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
    """Meta webhook subscription handshake, or a plain liveness OK for Green API."""
    if settings.whatsapp_provider == "greenapi":
        # Green API has no GET handshake; answer OK for dashboards/health checks.
        return PlainTextResponse("OK")
    if (
        hub_mode == "subscribe"
        and settings.meta_verify_token
        and hmac.compare_digest(
            hub_verify_token.encode("utf-8", "replace"),
            settings.meta_verify_token.encode("utf-8", "replace"),
        )
    ):
        log.info("event=webhook.verified")
        return PlainTextResponse(hub_challenge)
    log.warning("event=webhook.verify_rejected mode=%r", hub_mode)
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook")
async def receive_webhook(request: Request) -> dict:
    content_length = request.headers.get("content-length", "")
    if content_length.isdigit() and int(content_length) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large")
    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large")

    if settings.whatsapp_provider == "greenapi":
        outcome = _greenapi_authorized(request)
        if outcome == "forbidden":
            log.warning("event=webhook.bad_token provider=greenapi")
            raise HTTPException(status_code=403, detail="Invalid webhook token")
        if outcome == "ignored":
            # Fail closed, same reasoning as the Meta path below.
            log.warning(
                "event=webhook.unsigned_ignored provider=greenapi "
                "hint=set_GREENAPI_WEBHOOK_TOKEN (and set the instance's "
                "webhookUrlToken to the same value), or "
                "ALLOW_UNSIGNED_WEBHOOKS=true for local development only"
            )
            return {"status": "ignored_unsigned"}
    elif settings.meta_app_secret:
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not _valid_signature(raw, signature, settings.meta_app_secret):
            log.warning("event=webhook.bad_signature")
            raise HTTPException(status_code=403, detail="Invalid signature")
    elif not settings.allow_unsigned_webhooks:
        # Fail closed: without META_APP_SECRET anyone could forge messages and
        # open bogus service calls. ACK (so Meta stops retrying) but do nothing.
        log.warning(
            "event=webhook.unsigned_ignored hint=set_META_APP_SECRET "
            "(or ALLOW_UNSIGNED_WEBHOOKS=true for local development only)"
        )
        return {"status": "ignored_unsigned"}

    payload = _parse_payload(raw)
    if payload is None:
        # 200 on malformed payloads: providers would otherwise retry them forever.
        return {"status": "ignored"}

    if settings.whatsapp_provider == "greenapi":
        expected_instance = settings.greenapi_numeric_instance_id()
        if expected_instance and str(payload.instanceData.idInstance) != expected_instance:
            log.warning(
                "event=webhook.wrong_instance got=%s", payload.instanceData.idInstance
            )
            return {"status": "ignored"}

    accepted = 0
    for message, sender_name in payload.incoming_messages():
        if accepted >= _MAX_MESSAGES_PER_POST:
            log.warning("event=webhook.message_cap cap=%d", _MAX_MESSAGES_PER_POST)
            break
        if not message.id or not message.from_number:
            log.warning("event=message.skipped reason=missing_id_or_sender")
            continue
        if _already_processed(message.id):
            log.info("event=message.duplicate message_id=%s", message.id)
            continue

        text = _sanitize_text(message.body_text())
        if not text:
            log.info(
                "event=message.skipped reason=no_text type=%s message_id=%s",
                message.type, message.id,
            )
            continue
        classification = classify_message(text)
        if classification.category != "maintenance_request":
            log.info(
                "event=message.skipped reason=not_maintenance category=%s message_id=%s",
                classification.category, message.id,
            )
            continue
        if _sender_over_limit(message.from_number):
            # Stays marked as seen on purpose — a flood shouldn't come back.
            log.warning(
                "event=message.sender_rate_limited from=%s message_id=%s",
                message.from_number, message.id,
            )
            continue

        try:
            request.app.state.queue.put_nowait((message, sender_name, text))
        except asyncio.QueueFull:
            _forget_message(message.id)  # let Meta redeliver once there's room
            log.warning("event=message.queue_full message_id=%s", message.id)
            continue
        log.info(
            "event=message.accepted message_id=%s from=%s name=%r chars=%d",
            message.id, message.from_number, sender_name, len(text),
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
        "event=flow.done message_id=%s building=%r apartment_id=%s service_call_id=%s",
        message.id, match.building_name, match.apartment_id, call_id,
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


def _greenapi_authorized(request: Request) -> str:
    """'ok' | 'ignored' (no token configured) | 'forbidden' (token mismatch).

    Green API sends the instance's webhookUrlToken in the Authorization header;
    depending on version it arrives raw or prefixed with Bearer/Basic.
    """
    token = settings.greenapi_webhook_token
    if not token:
        return "ok" if settings.allow_unsigned_webhooks else "ignored"
    header = request.headers.get("Authorization", "")
    expected = token.encode()
    candidates = (header, header.removeprefix("Bearer "), header.removeprefix("Basic "))
    for candidate in candidates:
        if hmac.compare_digest(candidate.encode("utf-8", "replace"), expected):
            return "ok"
    return "forbidden"


def _parse_payload(raw: bytes) -> GreenApiWebhookPayload | WebhookPayload | None:
    model = (
        GreenApiWebhookPayload
        if settings.whatsapp_provider == "greenapi"
        else WebhookPayload
    )
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        log.warning(
            "event=webhook.invalid_payload provider=%s error=%s",
            settings.whatsapp_provider,
            str(exc).replace("\n", " | ")[:300],
        )
        return None


def _valid_signature(raw_body: bytes, header_value: str, app_secret: str) -> bool:
    if not header_value.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest().encode()
    provided = header_value[len("sha256="):].encode("utf-8", "replace")
    return hmac.compare_digest(provided, expected)
