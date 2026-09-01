# Green API Migration Guide

This document explains how to switch from Meta WhatsApp Cloud API to Green API webhooks.

## Changes Made

The codebase has been updated to support both Meta and Green API webhooks with a simple configuration switch.

### 1. Configuration (`config.py`)
- Added `WHATSAPP_PROVIDER` environment variable (default: "meta")
- Added Green API credentials:
  - `GREENAPI_API_KEY` - Your Green API authentication key
  - `GREENAPI_INSTANCE_ID` - Your Green API instance ID
- Updated `missing_required()` to check for correct credentials based on provider

### 2. Webhook Models (`models.py`)
- Added Green API webhook models:
  - `GreenApiMessage` - Handles Green API message format
  - `GreenApiSenderData` - Extracts sender information
  - `GreenApiInstanceData` - Contains messages
  - `GreenApiWebhookPayload` - Main webhook payload
- Green API models convert messages to internal `WhatsAppMessage` format for compatibility

### 3. Webhook Handlers (`main.py`)
- Updated `GET /webhook` to handle both Meta and Green API verification
- Updated `POST /webhook` to detect provider and parse accordingly
- Added provider-aware logging with `provider=` tag in all events
- Added `_validate_webhook_signature()` to handle both signature methods
- Added `_parse_webhook_payload()` to parse based on provider type

## How to Switch to Green API

### Environment Variables
Create or update your `.env` file:

```bash
# Choose the provider
WHATSAPP_PROVIDER=greenapi

# Green API credentials
GREENAPI_API_KEY=your_api_key_here
GREENAPI_INSTANCE_ID=your_instance_id_here

# Keep OXS credentials (unchanged)
OXS_GENERAL_API_KEY=your_oxs_general_key
OXS_SERVICE_CALLS_API_KEY=your_oxs_service_calls_key

# Meta credentials are now optional (only needed if provider=meta)
META_VERIFY_TOKEN=your_meta_token
META_APP_SECRET=your_meta_secret
```

### Green API Webhook Configuration
In your Green API dashboard, set your webhook URL to:
```
POST https://your-domain.com/webhook
```

Green API will automatically send messages to this endpoint when:
- New messages arrive
- Message statuses change
- Other events occur

## How to Switch Back to Meta

Simply change the environment variable:
```bash
WHATSAPP_PROVIDER=meta
```

And ensure your Meta credentials are set:
```bash
META_VERIFY_TOKEN=your_verify_token
META_APP_SECRET=your_app_secret
```

## Testing

### Health Check
```bash
curl http://localhost:8000/health
```

Response shows missing settings if any:
```json
{
  "status": "ok",
  "configured": true,
  "missing_settings": []
}
```

### Verify Webhook (Green API)
```bash
curl http://localhost:8000/webhook
# Returns: OK
```

### Verify Webhook (Meta)
```bash
curl "http://localhost:8000/webhook?hub.mode=subscribe&hub.verify_token=your_token&hub.challenge=test"
# Returns: test
```

## Logging

All events now include a `provider=` tag to show which webhook handler processed the message:

```
INFO message.accepted message_id=msg_123 from=972501234567 name='John' chars=45 provider=greenapi
INFO message.accepted message_id=msg_124 from=972501234567 name='John' chars=50 provider=meta
```

## Message Format Differences

### Meta Format
- Messages come in nested structure under `entry[].changes[].value.messages`
- Sender info in separate `contacts` array
- Signature: HMAC-SHA256 in `X-Hub-Signature-256` header

### Green API Format
- Messages directly in `data.instanceData.messages`
- Sender info in `data.senderData`
- Sender phone in `senderJid` field (format: "972501234567@s.whatsapp.net")

Both formats are automatically converted to the internal `WhatsAppMessage` model for consistent processing.

## Troubleshooting

### Green API webhook not receiving messages
1. Check `WHATSAPP_PROVIDER=greenapi` is set
2. Verify `GREENAPI_API_KEY` and `GREENAPI_INSTANCE_ID` are correct
3. Check logs for `provider=greenapi` events
4. Verify webhook URL in Green API dashboard

### Message processing fails
1. Check logs for `provider=greenapi` or `provider=meta`
2. Verify sender phone is in correct format (11 digits for Israeli numbers: 972XXXXXXXXX)
3. Ensure OXS API keys are valid

### Performance issues
- Both providers use the same single-queue background worker
- Rate limiting remains at 10 messages per sender per 10 minutes
- Switch back to Meta if needed: just change `WHATSAPP_PROVIDER=meta`
