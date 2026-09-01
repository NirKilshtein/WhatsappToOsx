"""
ARCHITECTURE SUMMARY - OXS WhatsApp Bridge Complete Flow

The application integrates with Meta WhatsApp Cloud API and processes messages end-to-end:

1. WEBHOOK RECEPTION (main.py - receive_webhook)
   - Meta sends POST /webhook with WhatsApp message payload
   - Verifies signature using META_APP_SECRET (if configured)
   - Parses message using WebhookPayload model
   - Extracts sender phone, sender name, message text

2. MESSAGE QUEUEING (main.py - receive_webhook)
   - Messages are validated (no missing data, not spam, not duplicate)
   - Rate-limited per sender (10 messages per 10-minute window)
   - Enqueued to asyncio.Queue with (message, sender_name, text)
   - Returns 200 immediately to Meta (doesn't wait for OXS processing)

3. BACKGROUND WORKER (main.py - _worker)
   - Single async consumer processes messages one at a time
   - Serializes OXS API access so no connection pool exhaustion
   - Calls process_message() for each queued message

4. TENANT LOOKUP (main.py - process_message → oxs_service.py - find_tenant_by_phone)
   - Normalizes sender phone to canonical core (e.g., "050-123-4567" → "501234567")
   - Checks phone cache (positive and negative, TTL: 10 min / 5 min)
   
   IF NOT CACHED:
   a) Fetches all active buildings from OXS API (cached 10 min)
   b) For each building:
      - Gets tenants list (cached per building 10 min)
      - Scans each tenant's "phone" field
      - Normalizes tenant phone and compares to sender
      - Uses intelligent phone extraction (iter_phone_candidates)
        that looks for phone-hinted keys in the tenant record
   c) Returns TenantMatch when found (building_id, tenant_id, apartment_id, names)
   d) Caches negative results (no tenant found) for 5 minutes

5. SERVICE CALL CREATION (main.py - process_message → oxs.create_service_call)
   - Builds description with sender name and message text
   - Hebrew attribution: "— נפתח אוטומטית מהודעת וואטסאפ מאת {name}"
   - POST to /service-calls with:
     {
       "buildingId": "{tenant's building ID}",
       "apartmentId": "{apartment number}",
       "description": "{formatted description}"
     }
   - Returns 200 with service call details (_id, taskNumber, status)

6. ERROR HANDLING
   - 401 Unauthorized → API key invalid/expired (logged as flow.*_error)
   - 403 Forbidden → Wrong key for module (logged as flow.*_error)
   - Unknown sender → Logs warning, NO service call created (silently dropped)
   - Network error → Message unmarked, Meta retries with backoff
   - Processing error → Message unmarked, Meta retries up to 6 times

CACHING STRATEGY
- Buildings: 10 minutes (shared across all senders)
- Tenants per building: 10 minutes (shared across all senders)
- Phone matches: 10 minutes (positive cache)
- Phone mismatches: 5 minutes (negative cache, to avoid repeated scans)
- Memory limits: Max 5000 phone cache entries, 500 building-tenant pairs

API CREDENTIALS REQUIRED
✓ OXS_GENERAL_API_KEY       - For GET /buildings and GET /buildings/{id}/tenants
✓ OXS_SERVICE_CALLS_API_KEY - For POST /service-calls
✓ META_VERIFY_TOKEN          - For webhook subscription handshake
✓ META_APP_SECRET            - For webhook signature verification

RATE LIMITING
- OXS: Client-side sliding window, 60 req/min (per API docs)
- Meta: 10 messages per sender per 10 minutes (anti-spam)
- Both limits are gracefully handled with backoff

IDEMPOTENCY
- Messages deduplicated by ID (Meta re-delivers on timeout)
- Tenant lookup is stateless (repeatable)
- Service call creation: READ timeout allowed (no blind retry), will log error instead
"""

import asyncio
import logging

log = logging.getLogger("bridge")

# Showing the integration:
# 1. WebhookPayload.incoming_messages() extracts messages from Meta
# 2. Messages are queued with phone number and text
# 3. _worker() processes queue via process_message()
# 4. process_message() calls oxs.find_tenant_by_phone()
# 5. OxsClient handles all building/tenant/phone logic
# 6. TenantMatch returned is used to create service call
# 7. Unknown senders are logged but message stays deduped (not retried)

print(__doc__)
