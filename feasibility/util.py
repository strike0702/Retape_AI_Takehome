"""Shared utilities: half-up rounding and ceil division."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def round_half_up(pct: float | Decimal | str, cents: int) -> int:
    """Round ``pct * cents`` half-away-from-zero to the nearest integer.

    Uses Decimal so ``0.5`` stays exact. Builtin ``round()`` is banker's
    rounding and must never be used for money.
    """
    return int(
        (Decimal(str(pct)) * Decimal(cents)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def ceil_div(a: int, b: int) -> int:
    """Ceiling of a/b for positive a and positive b."""
    return (a + b - 1) // b
