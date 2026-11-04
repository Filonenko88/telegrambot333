from __future__ import annotations


def validate_age(text: str) -> int | None:
    """Validate an age from 18 through 99 inclusive."""
    if not isinstance(text, str):
        return None

    value = text.strip()
    if not value or not value.isascii() or not value.isdecimal():
        return None

    # Avoid needless conversion of arbitrarily long user input.
    if len(value) > 2:
        return None

    try:
        age = int(value, 10)
    except (ValueError, OverflowError):
        return None

    return age if 18 <= age <= 99 else None


def validate_price(text: str) -> int | None:
    """Validate a positive price up to 1,000,000 inclusive."""
    if not isinstance(text, str):
        return None

    value = text.strip()
    if not value or not value.isascii() or not value.isdecimal():
        return None

    # Seven digits are enough to represent the maximum allowed price.
    if len(value) > 7:
        return None

    try:
        price = int(value, 10)
    except (ValueError, OverflowError):
        return None

    return price if 0 < price <= 1_000_000 else None


def validate_text(
    text: str,
    min_len: int = 2,
    max_len: int = 500,
) -> str | None:
    """Trim and validate text length; return None for invalid input/config."""
    if not isinstance(text, str):
        return None
    if (
        isinstance(min_len, bool)
        or not isinstance(min_len, int)
        or isinstance(max_len, bool)
        or not isinstance(max_len, int)
        or min_len < 0
        or max_len < min_len
    ):
        return None

    value = text.strip()
    return value if min_len <= len(value) <= max_len else None
