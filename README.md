# OXS WhatsApp Bridge

Turns incoming WhatsApp messages into OXS service calls:

```
WhatsApp (Meta Cloud API webhook)
        │  POST /webhook
        ▼
FastAPI app ──► GET /buildings ───────────────┐  (General key, read-only)
        │       GET /buildings/{id}/tenants ──┤  match sender's phone
        │                                     ▼
        └─────► POST /service-calls  (Service Calls key, full control)
```

## Files

| File | Purpose |
|---|---|
| `main.py` | FastAPI app: webhook verification (GET) + message intake (POST), queue + background worker |
| `oxs_service.py` | Async `httpx` client for OXS: two keys, 60 req/min throttle, 401/403/429/5xx handling, tenant matching + caching |
| `models.py` | Pydantic models for the Meta webhook payload; internal result types |
| `phone_utils.py` | Israeli phone normalization (`+972 50…` / `050…` / `972…` → one canonical core) |
| `config.py` | Settings from environment / `.env` |
| `deploy-azure.ps1` | One-command Azure deployment (Free tier) |

## Run locally

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env    # fill in the keys
.venv\Scripts\python -m uvicorn main:app --reload
```

`GET http://localhost:8000/health` shows whether all required settings are present.

Without `META_APP_SECRET` the app **acknowledges webhooks but refuses to process
them** (anyone could forge them otherwise). For local experiments only, set
`ALLOW_UNSIGNED_WEBHOOKS=true` in `.env`. Never set it on Azure.

Mocked end-to-end test (no real OXS/Meta needed): `.venv\Scripts\python smoke_test.py`

## Deploy to Azure ($0/month)

```powershell
az login          # if needed
.\deploy-azure.ps1
```

Creates (all inside resource group `rg-oxs-whatsapp`, isolated from everything else):
Free (F1) Linux App Service plan + web app, HTTPS-only, FTP and basic-auth
publishing disabled, container logging on, placeholder settings, and deploys the
code. Re-run the script to ship code updates. The script refuses to run against
the wrong Azure account (see `-ExpectedAccount`).

### Entering the API keys

Three values start as `CHANGE_ME` and must be filled in:

| Setting | Where it comes from |
|---|---|
| `OXS_GENERAL_API_KEY` | OXS — General module key (read-only) |
| `OXS_SERVICE_CALLS_API_KEY` | OXS — Service Calls module key (full control) |
| `META_APP_SECRET` | Meta developer dashboard → App settings → Basic → App secret |

Portal: App Service → **Settings → Environment variables** → edit each → Apply
(app restarts itself). Or CLI:

```powershell
az webapp config appsettings set -g rg-oxs-whatsapp -n <app-name> --settings `
  OXS_GENERAL_API_KEY="<general key>" `
  OXS_SERVICE_CALLS_API_KEY="<service calls key>" `
  META_APP_SECRET="<meta app secret>"
```

`https://<app-name>.azurewebsites.net/health` returns `"configured": true` once done.

### Connecting the Meta WhatsApp webhook

1. In the [Meta developer dashboard](https://developers.facebook.com/apps): your app →
   **WhatsApp → Configuration → Webhook**.
2. Callback URL: `https://<app-name>.azurewebsites.net/webhook`
3. Verify token: the value the deploy script printed (also visible in the app settings
   as `META_VERIFY_TOKEN`).
4. **Important (Free tier):** the app sleeps when idle and a cold start takes
   1–2 minutes — open the `/health` URL first and **wait until it actually
   returns JSON**, then immediately click *Verify and save*.
5. Subscribe to the **messages** webhook field.

**Free-tier timing:** the first message after a quiet period may hit a sleeping
app; Meta retries with backoff, so it arrives a few minutes late (the dedupe
logic absorbs the re-deliveries — no duplicate service calls). If that bothers
your friend, either point a free pinger (cron-job.org / UptimeRobot, 5-minute
interval) at `/health` to keep the app warm — still $0 — or upgrade to B1.

## Error handling & hardening built in

- **401** from OXS → logged as `flow.*_error` (invalid/expired key).
- **403** → wrong key for the module — e.g. General key used where Service Calls
  full-control is needed.
- **429** → honors `Retry-After` (clamped to 60s) with retries; a client-side
  sliding-window limiter (default 55/min) stays under OXS's 60 req/min in the
  first place.
- **5xx / network errors** → exponential-backoff retries — but **never** a blind
  retry of `POST /service-calls`: an ambiguous failure (read timeout, gateway
  5xx) might mean the call *was* created, so it's logged loudly for manual
  verification instead of risking duplicate tickets.
- Webhook signatures (`X-Hub-Signature-256`) are **required** — unsigned
  traffic is acknowledged and dropped, so forged messages can't open bogus
  service calls.
- Meta webhook retries → in-memory message-ID dedupe (no double service calls);
  a message whose processing *failed* is un-marked so Meta's retry gets a
  second chance.
- Malformed webhook payloads → acknowledged with 200 and ignored (otherwise Meta
  retries them forever).
- Phone formats → `+972 50-123-4567`, `0501234567`, `972501234567` all match.
- The buildings/tenants directory is cached (10 min default) and unknown phones
  are negative-cached (5 min), so strangers messaging the line can't burn the
  OXS rate budget; per-sender and per-request caps + a 256KB body limit bound
  abuse of the public endpoint.
- Messages are processed by a single background worker off an in-memory queue —
  slow OXS calls never block Meta's delivery connections. (In-memory means an
  app restart drops queued-but-unprocessed messages; Meta's retries cover most
  of that window. Fine for this scale; a persistent queue is the upgrade path.)

Logs: App Service → **Log stream** (or `az webapp log tail -g rg-oxs-whatsapp -n <app-name>`;
the deploy script already enabled container logging). Every step logs a
structured `event=...` line.

## Assumption to verify against the OXS docs

The exact JSON field names of the OXS buildings/tenants/service-calls payloads are
not published in this repo. The client is tolerant (accepts `data`/`items` wrappers,
several id/phone/apartment key spellings, and falls back to scanning any phone-like
value in a tenant record), and `POST /service-calls` sends
`{buildingId, apartmentId, description}`. If OXS expects different field names,
adjust the `*_KEYS` tuples at the top of `oxs_service.py` and the body in
`create_service_call` — one place each.

## Costs

| Setup | Monthly | Notes |
|---|---|---|
| **F1 Free tier (deployed by default)** | **$0** | Sleeps after ~20 min idle → first message after a quiet period is slow (~30s). 60 CPU-min/day, plenty for a building's traffic. |
| B1 Basic (if cold starts annoy) | ~$13 | Always-on, one command to upgrade (see deploy script header). |
| Meta WhatsApp Cloud API | $0 | Receiving messages is free; replying within the 24h service window is free too. |
| OXS API | $0 | Included with the OXS subscription. |
