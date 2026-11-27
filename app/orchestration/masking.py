"""Input masking (PII desensitization) applied to user input before persistence/context."""
from __future__ import annotations

import re

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ID_CARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_LONG_DIGITS = re.compile(r"(?<!\d)\d{16,19}(?!\d)")


def mask_input(text: str) -> str:
    """Replace PII with stable placeholders. Order matters: specific patterns first."""
    masked = _EMAIL.sub("[MASKED_EMAIL]", text)
    masked = _ID_CARD.sub("[MASKED_ID_CARD]", masked)
    masked = _PHONE.sub("[MASKED_PHONE]", masked)
    masked = _LONG_DIGITS.sub("[MASKED_NUMBER]", masked)
    return masked
