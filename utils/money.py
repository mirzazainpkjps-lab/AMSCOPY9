"""Exact, currency-safe helpers.

SQLite has no native fixed-point decimal type.  Financial models therefore keep
an integer minor-unit (cents/paisa) mirror as the authoritative value while the
legacy ``REAL`` column remains available to old reports and imports.  All new
accounting mutations use these helpers and synchronise both representations.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

MONEY_QUANTUM = Decimal("0.01")
MINOR_FACTOR = Decimal("100")


class MoneyValueError(ValueError):
    """Raised when a submitted money value is missing, invalid, or non-finite."""


def decimal_money(value, *, field: str = "Amount") -> Decimal:
    """Return a finite two-decimal ``Decimal`` using commercial half-up rounding."""
    if value is None or (isinstance(value, str) and not value.strip()):
        value = "0"
    try:
        result = Decimal(str(value).strip()).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError, AttributeError) as exc:
        raise MoneyValueError(f"{field} must be a valid number.") from exc
    if not result.is_finite():
        raise MoneyValueError(f"{field} must be a finite number.")
    if result == Decimal("-0.00"):
        return Decimal("0.00")
    return result


def to_minor(value, *, field: str = "Amount") -> int:
    """Convert a currency value to exact integer minor units (paisa/cents)."""
    return int(decimal_money(value, field=field) * MINOR_FACTOR)


def from_minor(value) -> Decimal:
    """Convert integer minor units to a two-decimal ``Decimal``."""
    try:
        minor = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise MoneyValueError("Stored minor-unit amount is invalid.") from exc
    return (Decimal(minor) / MINOR_FACTOR).quantize(MONEY_QUANTUM)


def money_float(value) -> float:
    """Legacy/UI representation after exact decimal normalisation."""
    return float(decimal_money(value))


def sync_money_fields(obj, value_attr: str, minor_attr: str) -> None:
    """Synchronise a model's legacy REAL and authoritative integer fields.

    Normal application writes generally set the legacy value attribute.  If it
    is absent but a minor-unit value exists (for imported/newer data), the minor
    value is used instead.
    """
    if not hasattr(obj, value_attr) or not hasattr(obj, minor_attr):
        return
    value = getattr(obj, value_attr, None)
    minor = getattr(obj, minor_attr, None)
    if value is None and minor is not None:
        exact = from_minor(minor)
        setattr(obj, value_attr, float(exact))
        setattr(obj, minor_attr, int(minor))
        return
    exact_minor = to_minor(value or 0, field=value_attr.replace("_", " ").title())
    setattr(obj, minor_attr, exact_minor)
    setattr(obj, value_attr, float(from_minor(exact_minor)))
