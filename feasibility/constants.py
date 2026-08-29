"""Behavioral knobs, scoring policies, and guardrail thresholds."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

# --- Rounding / calendar conventions -------------------------------------
# All spec round(...) calls are half-up (away from zero). Implemented via
# Decimal quantize with ROUND_HALF_UP; builtin round() is forbidden for money.
# Cadence: EOM-preserving when first_payment_date is the last day of its month,
# otherwise day-of-month clamped to month length (models.monthly_payment_dates).

# --- Tier C: soft safety cushion (customer protection, two-pass) ----------
# Effective buffer = max(DEFAULT_BUFFER_CENTS, round_half_up(BUFFER_DRAFT_PCT * draft)).
# Defaults are 0 for strict assignment compliance: pass 1 == pass 2, no
# behavioral change. A nonzero buffer trades fee front-loading for cushion and
# is therefore an off-spec product knob — documented, disabled by default.
DEFAULT_BUFFER_CENTS: int = 0
BUFFER_DRAFT_PCT: Decimal = Decimal("0")  # e.g. Decimal("0.05")
OPTIONAL_SAFETY_CUSHION_CENTS: int = 2500  # reference value for README/product discussion

# --- Tier B: cliff flattening (post-pass, firm-indifferent) ----------------
ENABLE_CLIFF_FLATTENING: bool = True
MAX_CLIFF_RATIO: Fraction = Fraction(3, 1)  # target: p_k <= 3 x lower-median payment

# --- Part 2 guardrails (from ASSIGNMENT.md §8; centralized, not tunable) ---
GUARDRAIL_LUMP_SUM_PCT: Decimal = Decimal("0.65")  # L <= round_half_up(0.65 * offer_total)
GUARDRAIL_INCREMENT_PCT: Decimal = Decimal("0.40")  # X <= max(floor, round_half_up(0.40 * draft))
GUARDRAIL_INCREMENT_FLOOR_CENTS: int = 10000

# --- Determinism ------------------------------------------------------------
SHAPE_TIEBREAK_ORDER: dict[str, int] = {"even": 0, "staircase": 1, "balloon": 2}
