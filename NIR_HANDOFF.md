# Green API bridge — status & handoff (for Nir)

_Last updated: 2026-09-01_

## TL;DR — LIVE IN PRODUCTION ✅

The Green API flow is **fully live** on the Azure deployment. On 2026-09-01 (~22:49) the
complete end-to-end ran in production: incoming message from tenant `0547878258` →
webhook auth → maintenance classification → OXS tenant match →
**real service call created: `6a972c3060320be872f60991`** (check your OXS dashboard).

Your Render app (`whatsapptoosx.onrender.com`) had a payload-format bug that made it
**silently drop every real WhatsApp message** (details below); the Green API webhook now
points at the fixed Azure deployment, so Render no longer receives anything and can be
shut down.

## What runs where

| Thing | Value |
|---|---|
| Production app (fixed code) | `https://oxs-whatsapp-72520.azurewebsites.net` (Azure, `rg-oxs-whatsapp`) |
| Health check | `https://oxs-whatsapp-72520.azurewebsites.net/health` |
| Webhook endpoint | `https://oxs-whatsapp-72520.azurewebsites.net/webhook` |
| Green API instance | idInstance `710722725742`, linked to `972544446045` (your business number) |
| Green API webhook points at | **the Azure app** (switched 2026-09-01, `webhookUrlToken` armed) |
| Code | This repo — your WhatsappToOsx changes were merged with fixes (see below) |

## The two bugs found in WhatsappToOsx

**1. Webhook models don't match Green API's real format (messages silently dropped).**
Green API POSTs one flat notification object per message:

```json
{
  "typeWebhook": "incomingMessageReceived",
  "instanceData": { "idInstance": 710722725742, "wid": "...", "typeInstance": "whatsapp" },
  "timestamp": 1788285600,
  "idMessage": "...",
  "senderData": { "chatId": "972...@c.us", "sender": "972...@c.us", "senderName": "..." },
  "messageData": { "typeMessage": "textMessage", "textMessageData": { "textMessage": "..." } }
}
```

Your models expect `data.instanceData.messages[]` with a `senderJid` field — that shape
never arrives, so every real notification parses to zero messages. **Verified live:**
POSTing the real format above to `whatsapptoosx.onrender.com/webhook` returns
`{"status":"received","accepted":0}`. Your `live_webhook_e2e_test.py` shows a false
"pass" because it posts a *Meta*-shaped payload and only checks for HTTP 200.

**2. The Green API path accepted unsigned webhooks (fail open).** Anyone who found the
URL could forge messages. The merged code fails closed instead: it requires the
`Authorization` header to match `GREENAPI_WEBHOOK_TOKEN`, which must equal the
instance's `webhookUrlToken` setting.

Also merged from your fork: the `WHATSAPP_PROVIDER` switch, the Hebrew maintenance-keyword
classifier (a message must contain a maintenance term — a plain "שלום" opens no ticket),
and the `id`/`_id` fallback for the OXS service-call response.

## Env naming note (heads-up)

In your email/setup, `GREENAPI_API_KEY` holds the **numeric idInstance** and
`GREENAPI_INSTANCE_ID` holds the **long apiTokenInstance** — swapped relative to Green
API's own docs. The merged config keeps your naming and auto-detects the numeric one,
so nothing breaks either way.

## Validation already done (2026-09-01)

- Green API instance `710722725742` is **authorized** and linked to `972544446045@c.us`.
- Azure app health: OK; `GET /webhook` → `OK`.
- Real-format POST to Azure **without** token → `403` (fail closed works).
- Real-format POST **with** token → `{"accepted": 1}` — parsed, classified, queued.
- Local smoke suites: 41/41 checks green across both providers (`python smoke_test.py`).

## Go-live steps — ALL DONE 2026-09-01

1. ~~OXS API keys~~ — set in the Azure app settings; verified live (`GET /buildings` → 200,
   6 buildings). ✅
2. ~~Flip the Green API webhook~~ — instance `710722725742` now has
   `webhookUrl = https://oxs-whatsapp-72520.azurewebsites.net/webhook` and
   `webhookUrlToken` armed (equals the app's `GREENAPI_WEBHOOK_TOKEN`; read it in the
   Azure app settings — deliberately not written here). ✅
3. ~~End-to-end test~~ — production run created OXS service call
   **`6a972c3060320be872f60991`** for tenant `0547878258`
   (building `6980aa650d8e2c743ec5bc7d`). ✅

**Your test now:** WhatsApp a maintenance-style message (e.g. "יש נזילה בחניון") from
`0547878258` to `0544446045` and watch it appear in OXS within seconds. Note the
classifier gate: a message with no maintenance keyword (e.g. just "שלום") deliberately
opens no ticket. Logs: `az webapp log tail -g rg-oxs-whatsapp -n oxs-whatsapp-72520`.

## Security to-dos on your side

- **Rotate the Green API apiTokenInstance** after testing — it was sent in plain email.
- Your Render deployment can be deleted after the flip (it will stop receiving webhooks
  anyway), or updated with this repo's code if you want it as a second environment.
