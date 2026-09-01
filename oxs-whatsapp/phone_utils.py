"""Phone-number normalization and phone-like value extraction.

WhatsApp delivers Israeli senders as e.g. "972501234567"; OXS tenant records may
hold "050-123-4567", "+972 50 123 4567", "0501234567" and so on. Both sides are
reduced to a canonical core (digits, country code and leading zeros stripped) so
they can be compared directly.
"""

import re
from typing import Any, Iterator

_DIGITS = re.compile(r"\d+")

# Keys in OXS records that plausibly hold a phone number (English + Hebrew).
_PHONE_KEY_HINTS = ("phone", "mobile", "cell", "tel", "טלפון", "נייד", "פלאפון")

# A canonical Israeli core is 8-9 digits (mobile 50xxxxxxx, landline 3xxxxxxx).
_MIN_CORE_LEN = 8
_MAX_CORE_LEN = 10


def normalize_phone(raw: Any) -> str:
    """Reduce any phone representation to its canonical digit core.

    "+972 50-123 4567" -> "501234567"
    "0501234567"       -> "501234567"
    "972501234567"     -> "501234567"
    Returns "" when the value doesn't look like a phone number at all.
    """
    if raw is None:
        return ""
    digits = "".join(_DIGITS.findall(str(raw)))
    if digits.startswith("00"):
        digits = digits[2:]
    # Treat 972 as a country code only when enough digits remain for a full
    # core. A Sharon-area landline stored without its leading zero (97212345
    # for 09-7212345) must not have its prefix eaten.
    if digits.startswith("972") and len(digits) >= 11:
        digits = digits[3:]
    digits = digits.lstrip("0")
    if not (_MIN_CORE_LEN <= len(digits) <= _MAX_CORE_LEN):
        return ""
    return digits


def phones_match(a: Any, b: Any) -> bool:
    core_a, core_b = normalize_phone(a), normalize_phone(b)
    return bool(core_a) and core_a == core_b


def plausible_israeli_core(core: str) -> bool:
    """Mobile/VoIP 05x/07x -> 9-digit core starting 5/7; landline -> 8 digits
    starting with an Israeli area digit. Used to reject IDs/dates/counters."""
    return (len(core) == 9 and core[:1] in ("5", "7")) or (
        len(core) == 8 and core[:1] in ("2", "3", "4", "8", "9")
    )


def iter_phone_candidates(record: Any) -> Iterator[str]:
    """Yield phone-like strings from an OXS record of unknown exact schema.

    When the record has phone-hinted keys (any nesting), only their values are
    yielded. Only when NO hinted key exists at all do we fall back to scanning
    every value — and then only values shaped like a real Israeli number
    (see plausible_israeli_core), so national IDs (9 digits, rarely starting
    5/7) and most numeric ids/dates don't misroute a service call to the
    wrong tenant. The residual risk is documented in the README; the hinted
    path is the expected one once the real OXS schema is confirmed.
    """
    if _has_hinted_key(record):
        yield from _walk(record, hinted_only=True)
        return
    for value in _walk(record, hinted_only=False):
        if plausible_israeli_core(normalize_phone(value)):
            yield value


def _has_hinted_key(node: Any) -> bool:
    if isinstance(node, dict):
        for key, value in node.items():
            key_l = str(key).lower()
            if any(h in key_l for h in _PHONE_KEY_HINTS):
                return True
            if _has_hinted_key(value):
                return True
    elif isinstance(node, (list, tuple)):
        return any(_has_hinted_key(item) for item in node)
    return False


def _walk(node: Any, hinted_only: bool, under_hint: bool = False) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            key_l = str(key).lower()
            is_hint = any(h in key_l for h in _PHONE_KEY_HINTS)
            yield from _walk(value, hinted_only, under_hint or is_hint)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _walk(item, hinted_only, under_hint)
    elif isinstance(node, (str, int)):
        if hinted_only and not under_hint:
            return
        if normalize_phone(node):
            yield str(node)
