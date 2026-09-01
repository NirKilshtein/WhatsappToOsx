"""Async client for the OXS Management external API (api.oxs.co.il).

Two separate keys are used, both sent via the x-api-key header:
  * General module key (read-only)      — GET /buildings, GET /buildings/{id}/tenants
  * Service Calls module key (full)     — POST /service-calls

Error semantics handled here:
  401 -> OxsAuthError   (invalid / expired key)
  403 -> OxsScopeError  (key doesn't cover this module)
  429 -> retried honoring Retry-After (clamped to 60s); a client-side
         sliding-window limiter keeps us under the documented 60 req/min
         in the first place.
  5xx / network errors -> retried with exponential backoff, but ONLY for
         idempotent requests. POST /service-calls is never blind-retried:
         a read timeout or gateway 5xx may mean the call WAS created, and
         retrying would file duplicate tickets. Only connect-phase failures
         (request provably never sent) are retried for POSTs.

Budget protection: the buildings list and per-building tenant lists are cached
for CACHE_TTL_SECONDS, and phone lookups are cached both positively and
negatively — so a stranger messaging the line repeatedly costs zero OXS calls
within the negative-cache window instead of a full buildings scan each time.

The OXS external API schema for buildings/tenants isn't pinned down here, so
list unwrapping and field extraction are deliberately tolerant (see
_as_list / _extract). Adjust the *_KEYS tuples if the real payload differs;
an unrecognized response shape is logged at ERROR with its top-level keys.
"""

import asyncio
import logging
import math
import time
from collections import OrderedDict, deque
from typing import Any, Optional

import httpx

from models import TenantMatch
from phone_utils import iter_phone_candidates, normalize_phone

log = logging.getLogger("oxs")

_PHONE_CACHE_MAX = 5000
_TENANTS_CACHE_MAX = 500
_NEGATIVE_TTL_CAP = 300  # unmatched phones re-checked after at most 5 minutes


class OxsError(Exception):
    """Generic OXS API failure."""


class OxsAuthError(OxsError):
    """401 — API key is invalid or expired."""


class OxsScopeError(OxsError):
    """403 — API key doesn't have access to this module/scope."""


class OxsRateLimitError(OxsError):
    """429 — rate limit still exceeded after retries."""


class _SlidingWindowLimiter:
    """Allow at most `limit` calls in any 60-second window (client side)."""

    def __init__(self, limit: int) -> None:
        self._limit = max(1, limit)
        self._stamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._stamps and now - self._stamps[0] > 60:
                    self._stamps.popleft()
                if len(self._stamps) < self._limit:
                    self._stamps.append(now)
                    return
                wait = 60 - (now - self._stamps[0]) + 0.05
                log.info("event=oxs.throttle wait_s=%.1f", wait)
                await asyncio.sleep(wait)


# Field-name candidates for the tolerant extractors.
_ID_KEYS = ("id", "_id", "buildingId", "building_id")
_NAME_KEYS = ("name", "buildingName", "title", "address", "displayName")
_ACTIVE_KEYS = ("isActive", "is_active", "active", "enabled")
_APARTMENT_KEYS = ("apartmentId", "apartment_id", "apartmentID", "aptId", "unitId", "unit_id")
_APARTMENT_NESTED = ("apartment", "unit")
_TENANT_ID_KEYS = ("id", "_id", "tenantId", "tenant_id")
_TENANT_NAME_KEYS = ("name", "fullName", "full_name", "firstName", "displayName")


def _as_list(payload: Any) -> list[dict[str, Any]]:
    """Accept either a bare JSON array or {data|items|results|buildings|tenants: [...]}."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items", "results", "buildings", "tenants"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def _shape_of(payload: Any) -> str:
    if isinstance(payload, dict):
        return "dict:" + ",".join(str(k) for k in list(payload)[:8])
    return type(payload).__name__


def _extract(record: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


def _is_active(record: dict[str, Any]) -> bool:
    """Treat a building as active unless it explicitly says otherwise."""
    for key in _ACTIVE_KEYS:
        if key in record:
            return bool(record[key])
    status = record.get("status")
    if isinstance(status, str) and status.lower() in ("inactive", "archived", "deleted"):
        return False
    return True


def _extract_apartment_id(tenant: dict[str, Any]) -> Any:
    direct = _extract(tenant, _APARTMENT_KEYS)
    if direct is not None:
        return direct
    for key in _APARTMENT_NESTED:
        nested = tenant.get(key)
        if isinstance(nested, dict):
            nested_id = _extract(nested, _ID_KEYS + ("number",))
            if nested_id is not None:
                return nested_id
        elif nested not in (None, ""):
            return nested
    return None


class OxsClient:
    def __init__(
        self,
        base_url: str,
        general_key: str,
        service_calls_key: str,
        rate_limit_per_minute: int = 55,
        cache_ttl_seconds: int = 600,
    ) -> None:
        self._general_key = general_key
        self._service_calls_key = service_calls_key
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(20.0, connect=10.0),
            headers={"Accept": "application/json"},
        )
        self._limiter = _SlidingWindowLimiter(rate_limit_per_minute)
        self._cache_ttl = max(0, cache_ttl_seconds)
        self._negative_ttl = min(_NEGATIVE_TTL_CAP, self._cache_ttl) or _NEGATIVE_TTL_CAP
        # phone core -> (expiry, TenantMatch | None)  — None = cached "no match"
        self._phone_cache: OrderedDict[str, tuple[float, Optional[TenantMatch]]] = OrderedDict()
        self._buildings_cache: Optional[tuple[float, list[dict[str, Any]]]] = None
        # building_id -> (expiry, tenants)
        self._tenants_cache: OrderedDict[Any, tuple[float, list[dict[str, Any]]]] = OrderedDict()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------
    # Low-level request with error mapping, throttling and retries
    # ------------------------------------------------------------------
    async def _request(
        self,
        method: str,
        path: str,
        api_key: str,
        json_body: Optional[dict[str, Any]] = None,
        max_attempts: int = 4,
        idempotent: bool = True,
    ) -> Any:
        last_error: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            await self._limiter.acquire()
            try:
                resp = await self._http.request(
                    method, path, headers={"x-api-key": api_key}, json=json_body
                )
            except httpx.RequestError as exc:
                # Connect-phase failures mean the request never reached OXS —
                # always safe to retry. Anything later (read timeout etc.) has
                # an unknown outcome and must not be retried for writes.
                sent_maybe = not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
                if sent_maybe and not idempotent:
                    raise OxsError(
                        f"{method} {path} outcome unknown ({exc.__class__.__name__}) — "
                        "not retrying a non-idempotent request to avoid duplicates."
                    ) from exc
                last_error = exc
                log.warning(
                    "event=oxs.network_error method=%s path=%s attempt=%d error=%s",
                    method, path, attempt, exc,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(2 ** attempt)
                continue

            if resp.status_code == 401:
                raise OxsAuthError(
                    f"OXS rejected the API key (401) on {method} {path} — "
                    "key is invalid or expired."
                )
            if resp.status_code == 403:
                raise OxsScopeError(
                    f"OXS refused {method} {path} (403) — the key used doesn't "
                    "have access to this module (check General vs Service Calls key)."
                )
            if resp.status_code == 429:
                # 429 = rejected before execution, so retrying is safe for POST too.
                retry_after = _retry_after_seconds(resp, default=2.0 * attempt)
                log.warning(
                    "event=oxs.rate_limited path=%s attempt=%d retry_after_s=%.1f",
                    path, attempt, retry_after,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(retry_after)
                    continue
                raise OxsRateLimitError(
                    f"OXS rate limit (60 req/min) still exceeded after "
                    f"{max_attempts} attempts on {method} {path}."
                )
            if resp.status_code >= 500:
                if not idempotent:
                    raise OxsError(
                        f"OXS server error {resp.status_code} on {method} {path} — "
                        "outcome unknown, not retrying a non-idempotent request."
                    )
                log.warning(
                    "event=oxs.server_error path=%s status=%d attempt=%d",
                    path, resp.status_code, attempt,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise OxsError(
                    f"OXS server error {resp.status_code} on {method} {path} "
                    f"after {max_attempts} attempts."
                )
            if resp.status_code >= 400:
                raise OxsError(
                    f"OXS {resp.status_code} on {method} {path}: {resp.text[:500]}"
                )

            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError as exc:
                raise OxsError(
                    f"OXS returned non-JSON body on {method} {path}: {resp.text[:200]}"
                ) from exc

        raise OxsError(f"OXS unreachable on {method} {path}: {last_error}")

    # ------------------------------------------------------------------
    # General module (read-only key) — responses cached for the TTL
    # ------------------------------------------------------------------
    async def get_buildings(self, active_only: bool = True) -> list[dict[str, Any]]:
        now = time.monotonic()
        if self._buildings_cache and self._buildings_cache[0] > now:
            buildings = self._buildings_cache[1]
        else:
            payload = await self._request("GET", "/buildings", self._general_key)
            buildings = _as_list(payload)
            if payload and not buildings:
                log.error(
                    "event=oxs.unrecognized_buildings_shape shape=%s — adjust "
                    "_as_list in oxs_service.py to the real OXS response format.",
                    _shape_of(payload),
                )
            self._buildings_cache = (now + self._cache_ttl, buildings)
            log.info("event=oxs.buildings_fetched count=%d", len(buildings))
        if active_only:
            buildings = [b for b in buildings if _is_active(b)]
        return buildings

    async def get_tenants(self, building_id: Any) -> list[dict[str, Any]]:
        now = time.monotonic()
        cached = self._tenants_cache.get(building_id)
        if cached and cached[0] > now:
            return cached[1]
        payload = await self._request(
            "GET", f"/buildings/{building_id}/tenants", self._general_key
        )
        tenants = _as_list(payload)
        if payload and not tenants:
            log.error(
                "event=oxs.unrecognized_tenants_shape building_id=%s shape=%s",
                building_id, _shape_of(payload),
            )
        self._tenants_cache[building_id] = (now + self._cache_ttl, tenants)
        self._tenants_cache.move_to_end(building_id)
        while len(self._tenants_cache) > _TENANTS_CACHE_MAX:
            self._tenants_cache.popitem(last=False)
        log.info(
            "event=oxs.tenants_fetched building_id=%s count=%d", building_id, len(tenants)
        )
        return tenants

    # ------------------------------------------------------------------
    # Tenant matching (positive + negative caching)
    # ------------------------------------------------------------------
    async def find_tenant_by_phone(self, sender_phone: str) -> Optional[TenantMatch]:
        core = normalize_phone(sender_phone)
        if not core:
            log.warning("event=match.bad_phone raw=%r", sender_phone)
            return None

        cached = self._phone_cache.get(core)
        if cached and cached[0] > time.monotonic():
            log.info(
                "event=match.cache_hit phone_core=%s found=%s", core, cached[1] is not None
            )
            return cached[1]

        buildings = await self.get_buildings(active_only=True)
        for building in buildings:
            building_id = _extract(building, _ID_KEYS)
            if building_id is None:
                log.warning("event=match.building_without_id keys=%s", list(building)[:8])
                continue
            building_name = str(_extract(building, _NAME_KEYS) or building_id)

            tenants = await self.get_tenants(building_id)
            for tenant in tenants:
                if not self._tenant_phone_matches(tenant, core):
                    continue
                match = TenantMatch(
                    building_id=building_id,
                    building_name=building_name,
                    apartment_id=_extract_apartment_id(tenant),
                    tenant_id=_extract(tenant, _TENANT_ID_KEYS),
                    tenant_name=str(_extract(tenant, _TENANT_NAME_KEYS) or ""),
                )
                log.info(
                    "event=match.found phone_core=%s building_id=%s apartment_id=%s tenant_id=%s",
                    core, match.building_id, match.apartment_id, match.tenant_id,
                )
                self._cache_phone(core, match, self._cache_ttl)
                return match

        log.warning(
            "event=match.not_found phone_core=%s buildings_scanned=%d negative_cached_s=%d",
            core, len(buildings), self._negative_ttl,
        )
        self._cache_phone(core, None, self._negative_ttl)
        return None

    def _cache_phone(self, core: str, match: Optional[TenantMatch], ttl: float) -> None:
        self._phone_cache[core] = (time.monotonic() + ttl, match)
        self._phone_cache.move_to_end(core)
        while len(self._phone_cache) > _PHONE_CACHE_MAX:
            self._phone_cache.popitem(last=False)

    @staticmethod
    def _tenant_phone_matches(tenant: dict[str, Any], target_core: str) -> bool:
        for candidate in iter_phone_candidates(tenant):
            if normalize_phone(candidate) == target_core:
                return True
        return False

    # ------------------------------------------------------------------
    # Service Calls module (full-control key)
    # ------------------------------------------------------------------
    async def create_service_call(
        self,
        building_id: Any,
        apartment_id: Any,
        description: str,
    ) -> Any:
        body: dict[str, Any] = {
            "buildingId": building_id,
            "description": description,
        }
        if apartment_id is not None:
            body["apartmentId"] = apartment_id
        payload = await self._request(
            "POST", "/service-calls", self._service_calls_key,
            json_body=body, idempotent=False,
        )
        call_id = payload.get("id", payload.get("_id")) if isinstance(payload, dict) else None
        if call_id is None and isinstance(payload, dict):
            nested = payload.get("data")
            if isinstance(nested, dict):
                call_id = nested.get("id", nested.get("_id"))
        log.info(
            "event=oxs.service_call_created building_id=%s apartment_id=%s call_id=%s",
            building_id, apartment_id, call_id,
        )
        return call_id


def _retry_after_seconds(resp: httpx.Response, default: float) -> float:
    value = resp.headers.get("Retry-After", "")
    try:
        seconds = float(value)
    except ValueError:
        seconds = default
    if not math.isfinite(seconds):
        seconds = default
    return min(60.0, max(0.5, seconds))
