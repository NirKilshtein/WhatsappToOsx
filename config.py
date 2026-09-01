"""Application settings, loaded from environment variables (.env supported).

On Azure App Service these come from Configuration > Application settings,
so the same code runs locally and in the cloud with no changes.
"""

import logging
import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()

_log = logging.getLogger("config")


def _secret_env(name: str) -> str:
    """Read a secret setting; the deploy script's CHANGE_ME placeholder counts
    as not set, so the app fails closed until real values are pasted in."""
    value = os.getenv(name, "")
    return "" if value == "CHANGE_ME" else value


def _int_env(name: str, default: int) -> int:
    """Parse an int env var defensively — a typo in Azure app settings must not
    crash-loop the container."""
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        _log.warning("event=config.bad_int name=%s value=%r using_default=%d", name, raw, default)
        return default


class Settings:
    def __init__(self) -> None:
        # --- WhatsApp Provider Selection ---
        # Choose between "meta" (Meta Cloud API) or "greenapi" (Green API)
        self.whatsapp_provider: str = os.getenv("WHATSAPP_PROVIDER", "meta").lower()
        if self.whatsapp_provider not in ("meta", "greenapi"):
            _log.warning("event=config.bad_provider value=%r using_default=meta", self.whatsapp_provider)
            self.whatsapp_provider = "meta"

        # --- OXS Management API ---
        self.oxs_base_url: str = os.getenv(
            "OXS_BASE_URL", "https://api.oxs.co.il/api/external/v1"
        ).rstrip("/")
        # General module key (read-only): GET /buildings, GET /buildings/{id}/tenants
        self.oxs_general_key: str = _secret_env("OXS_GENERAL_API_KEY")
        # Service Calls module key (full control): POST /service-calls
        self.oxs_service_calls_key: str = _secret_env("OXS_SERVICE_CALLS_API_KEY")

        # --- Meta WhatsApp Cloud API webhook ---
        # The token you type into the Meta App dashboard when subscribing the webhook.
        self.meta_verify_token: str = _secret_env("META_VERIFY_TOKEN")
        # Meta App secret; every POST is checked against X-Hub-Signature-256.
        # Without it the app ACKs webhooks but refuses to process them (fail closed),
        # unless ALLOW_UNSIGNED_WEBHOOKS=true (local development only).
        self.meta_app_secret: str = _secret_env("META_APP_SECRET")
        self.allow_unsigned_webhooks: bool = os.getenv(
            "ALLOW_UNSIGNED_WEBHOOKS", ""
        ).lower() in ("1", "true", "yes")

        # --- Green API webhook ---
        # API key for authenticating Green API webhooks
        self.greenapi_api_key: str = _secret_env("GREENAPI_API_KEY")
        # Instance ID for Green API
        self.greenapi_instance_id: str = _secret_env("GREENAPI_INSTANCE_ID")

        # --- Behaviour tuning ---
        # Client-side ceiling below the documented OXS limit of 60 req/min.
        self.oxs_rate_limit_per_minute: int = _int_env("OXS_RATE_LIMIT_PER_MINUTE", 55)
        # How long the buildings/tenants directory and resolved phone matches stay cached.
        self.cache_ttl_seconds: int = _int_env("CACHE_TTL_SECONDS", 600)
        self.log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
        if not isinstance(getattr(logging, self.log_level, None), int):
            _log.warning("event=config.bad_log_level value=%r using_default=INFO", self.log_level)
            self.log_level = "INFO"

    def missing_required(self) -> list[str]:
        """Names of settings that must be filled before real traffic can be served
        (CHANGE_ME placeholders already count as unset via _secret_env)."""
        missing = []
        if not self.oxs_general_key:
            missing.append("OXS_GENERAL_API_KEY")
        if not self.oxs_service_calls_key:
            missing.append("OXS_SERVICE_CALLS_API_KEY")
        
        if self.whatsapp_provider == "meta":
            if not self.meta_verify_token:
                missing.append("META_VERIFY_TOKEN")
            if not self.meta_app_secret and not self.allow_unsigned_webhooks:
                missing.append("META_APP_SECRET")
        elif self.whatsapp_provider == "greenapi":
            if not self.greenapi_api_key:
                missing.append("GREENAPI_API_KEY")
            if not self.greenapi_instance_id:
                missing.append("GREENAPI_INSTANCE_ID")
        
        return missing


@lru_cache
def get_settings() -> Settings:
    return Settings()
