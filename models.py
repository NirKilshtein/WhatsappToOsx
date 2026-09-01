"""Pydantic models for the Meta WhatsApp Cloud API webhook payload, plus
internal result types for the OXS matching flow.

All webhook models ignore unknown fields — Meta adds fields over time and the
payload also carries delivery-status events we don't care about.
"""

from dataclasses import dataclass
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


# --------------------------------------------------------------------------
# Meta WhatsApp Cloud API webhook payload
# https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/payload-examples
# --------------------------------------------------------------------------

class WhatsAppText(_Base):
    body: str = ""


class WhatsAppMediaCaption(_Base):
    caption: str = ""


class WhatsAppMessage(_Base):
    id: str = ""
    from_number: str = Field(default="", alias="from")
    timestamp: str = ""
    type: str = "text"
    text: Optional[WhatsAppText] = None
    image: Optional[WhatsAppMediaCaption] = None
    document: Optional[WhatsAppMediaCaption] = None
    video: Optional[WhatsAppMediaCaption] = None

    def body_text(self) -> str:
        """Best-effort human text: message body, or a media caption."""
        if self.type == "text" and self.text:
            return self.text.body.strip()
        for media in (self.image, self.document, self.video):
            if media and media.caption:
                return media.caption.strip()
        return ""


class WhatsAppProfile(_Base):
    name: str = ""


class WhatsAppContact(_Base):
    wa_id: str = ""
    profile: WhatsAppProfile = WhatsAppProfile()


class ChangeValue(_Base):
    messaging_product: str = ""
    messages: list[WhatsAppMessage] = []
    contacts: list[WhatsAppContact] = []
    statuses: list[dict[str, Any]] = []  # delivery receipts — ignored


class Change(_Base):
    field: str = ""
    value: ChangeValue = ChangeValue()


class Entry(_Base):
    id: str = ""
    changes: list[Change] = []


class WebhookPayload(_Base):
    object: str = ""
    entry: list[Entry] = []

    def incoming_messages(self) -> list[tuple[WhatsAppMessage, str]]:
        """Flatten to (message, sender_display_name) pairs, skipping statuses."""
        result: list[tuple[WhatsAppMessage, str]] = []
        for entry in self.entry:
            for change in entry.changes:
                if change.field != "messages":
                    continue
                names = {
                    c.wa_id: c.profile.name for c in change.value.contacts if c.wa_id
                }
                for msg in change.value.messages:
                    result.append((msg, names.get(msg.from_number, "")))
        return result


# --------------------------------------------------------------------------
# Internal flow results
# --------------------------------------------------------------------------

@dataclass
class TenantMatch:
    building_id: Any
    building_name: str
    apartment_id: Any
    tenant_id: Any
    tenant_name: str


@dataclass
class ServiceCallResult:
    ok: bool
    service_call_id: Any = None
    error: str = ""
