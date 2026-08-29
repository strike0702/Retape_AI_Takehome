"""Floor function, payment-shape generators, static validator, labeling."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Iterator

from feasibility.models import CreditorRules


@dataclass(frozen=True)
class Candidate:
    k: int
    payments: tuple[int, ...]
    label: str  # "even" | "staircase" | "balloon"


def compute_floors(rules: CreditorRules, k_max: int) -> list[int]:
    """1-indexed floors[1..k_max]. floors[0] unused."""
    floors = [0] * (k_max + 1)
    for i in range(1, k_max + 1):
        tier = 0
        for frm, m in rules.min_payment_tiers:
            if frm <= i:
                tier = max(tier, m)
        token_bump = rules.min_payment_cents + 1 if i > rules.max_token_pays else 0
        floors[i] = max(rules.min_payment_cents, tier, token_bump)
    return floors


def label_shape(
    payments: tuple[int, ...],
    rules: CreditorRules,
) -> str:
    """Label by rule, not by generating family (case 3 needs balloon for dual generators)."""
    if rules.even_pays:
        return "even"
    k = len(payments)
    if rules.is_ballooning_allowed and k >= 2 and payments[-1] > payments[-2]:
        return "balloon"
    return "staircase"


def even_vector(S: int, k: int) -> tuple[int, ...]:
    q, r = divmod(S, k)
    return tuple([q] * (k - r) + [q + 1] * r)


def _compositions(k: int, m: int) -> Iterator[tuple[int, ...]]:
    """Positive compositions of k into exactly m parts."""
    if m == 1:
        yield (k,)
        return
    # Place m-1 bars among k-1 gaps between k units.
    for cuts in combinations(range(1, k), m - 1):
        parts: list[int] = []
        prev = 0
        for c in cuts:
            parts.append(c - prev)
            prev = c
        parts.append(k - prev)
        yield tuple(parts)


def _segment_lower_bounds(floors: list[int], counts: tuple[int, ...]) -> list[int]:
    """Strictly increasing segment lower bounds from floors at each segment's last position."""
    lbs: list[int] = []
    pos = 0
    prev_lb = 0
    for i, c in enumerate(counts):
        pos += c
        floor_at = floors[pos]
        if i == 0:
            lb = floor_at
        else:
            lb = max(floor_at, prev_lb + 1)
        lbs.append(lb)
        prev_lb = lb
    return lbs


def generate_candidates(
    S: int,
    k_max: int,
    floors: list[int],
    rules: CreditorRules,
) -> list[Candidate]:
    """Enumerate even / balloon / staircase candidates for k in 1..k_max."""
    seen: set[tuple[int, tuple[int, ...]]] = set()
    out: list[Candidate] = []

    def emit(k: int, p: tuple[int, ...]) -> None:
        key = (k, p)
        if key in seen:
            return
        seen.add(key)
        label = label_shape(p, rules)
        if is_statically_valid(k, p, S, k_max, floors, rules, label):
            out.append(Candidate(k=k, payments=p, label=label))

    for k in range(1, k_max + 1):
        if rules.even_pays:
            p = even_vector(S, k)
            if p[0] >= floors[1]:  # fast-path; validator re-checks
                emit(k, p)
            continue

        # Staircase family
        s = rules.max_segments
        for m in range(1, min(s, k) + 1):
            for counts in _compositions(k, m):
                lbs = _segment_lower_bounds(floors, counts)
                if m == 1:
                    if S % k == 0 and S // k >= lbs[0]:
                        emit(k, tuple([S // k] * k))
                    continue
                # Pin v_1..v_{m-2} = lb; solve last two segments.
                R = S - sum(counts[i] * lbs[i] for i in range(m - 2))
                c_a, c_b = counts[m - 2], counts[m - 1]
                lb_a, lb_b = lbs[m - 2], lbs[m - 1]
                for v_a in range(lb_a, lb_a + c_b):
                    rem = R - c_a * v_a
                    if rem < 0 or rem % c_b != 0:
                        continue
                    v_b = rem // c_b
                    if v_b < max(lb_b, v_a + 1):
                        continue
                    levels = list(lbs[: m - 2]) + [v_a, v_b]
                    vec: list[int] = []
                    for cnt, lvl in zip(counts, levels):
                        vec.extend([lvl] * cnt)
                    emit(k, tuple(vec))

        # Quasi-even extreme (max-flat) — covers suffix-min starvation trap.
        emit(k, even_vector(S, k))

        # Balloon family
        if rules.is_ballooning_allowed:
            if k == 1:
                if S >= floors[1]:
                    emit(k, (S,))
            else:
                prefix = [floors[i] for i in range(1, k)]
                residual = S - sum(prefix)
                if residual >= max(floors[k], prefix[-1]):
                    emit(k, tuple(prefix + [residual]))

    return out


def is_statically_valid(
    k: int,
    payments: tuple[int, ...],
    S: int,
    k_max: int,
    floors: list[int],
    rules: CreditorRules,
    label: str | None = None,
) -> bool:
    """Money-independent legality checks (also the Part 2 candidate filter)."""
    if not (1 <= k <= k_max) or len(payments) != k:
        return False
    if sum(payments) != S:
        return False
    for i in range(k - 1):
        if payments[i] > payments[i + 1]:
            return False
    for i in range(1, k + 1):
        if payments[i - 1] < floors[i]:
            return False
    token_count = sum(1 for p in payments if p == rules.min_payment_cents)
    if token_count > rules.max_token_pays:
        return False

    if label is None:
        label = label_shape(payments, rules)

    if label == "even":
        return payments == even_vector(S, k)
    if label == "staircase":
        return len(set(payments)) <= rules.max_segments
    # balloon: exempt from max_segments
    return True
