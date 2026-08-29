"""Suffix-min fee placement, lexicographic scoring, Tier B cliff flattening."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from fractions import Fraction

from feasibility.constants import (
    ENABLE_CLIFF_FLATTENING,
    MAX_CLIFF_RATIO,
    SHAPE_TIEBREAK_ORDER,
)
from feasibility.models import Client, CreditorRules
from feasibility.shapes import Candidate, is_statically_valid
from feasibility.simulate import SimulationResult, simulate


@dataclass(frozen=True)
class FeePlacement:
    """1-indexed Phi and fee arrays; feasible flag under buffer c."""

    Phi: list[int]  # Phi[0]=0; Phi[j] cumulative by t_j
    fee: list[int]  # fee[j] collected on t_j
    feasible: bool


def place_fee(
    w: list[int],
    pre_min: int,
    F: int,
    buffer: int = 0,
) -> FeePlacement:
    """Pointwise-maximal feasible cumulative fee via suffix minima.

    Phi[j] = min(F, suffix_min(w[j..M]) - buffer).
    Feasible iff pre_min >= buffer, min(w) >= buffer, and w[M] - buffer >= F.
    """
    M = len(w) - 1  # w is 1-indexed
    if M == 0:
        return FeePlacement(Phi=[0], fee=[0], feasible=F == 0 and pre_min >= buffer)

    # Right-to-left suffix mins
    suffix = [0] * (M + 1)
    suffix[M] = w[M]
    for j in range(M - 1, 0, -1):
        suffix[j] = min(w[j], suffix[j + 1])

    Phi = [0] * (M + 1)
    fee = [0] * (M + 1)
    for j in range(1, M + 1):
        Phi[j] = min(F, suffix[j] - buffer)
        if Phi[j] < 0:
            Phi[j] = 0
        fee[j] = Phi[j] - Phi[j - 1]

    min_w = min(w[1:]) if M >= 1 else 10**18
    feasible = pre_min >= buffer and min_w >= buffer and (w[M] - buffer >= F)
    # When feasible, Phi[M] must equal F (suffix[M]-buffer = w[M]-buffer >= F).
    if feasible and Phi[M] != F:
        feasible = False
    return FeePlacement(Phi=Phi, fee=fee, feasible=feasible)


def median_low(payments: tuple[int, ...]) -> int:
    k = len(payments)
    return sorted(payments)[(k - 1) // 2]


def cliff_ratio(payments: tuple[int, ...]) -> Fraction:
    med = max(1, median_low(payments))
    return Fraction(payments[-1], med)


def score_key(
    cand: Candidate,
    placement: FeePlacement,
    sim: SimulationResult,
    M: int,
) -> tuple:
    """Sort key: maximize this under Python's lexicographic tuple order.

    Components are signed so that max() / reverse sort picks the winner:
      1. Phi vector (maximize)
      2. -k (minimize k)
      3. min_slack, total_slack (maximize)
      4. -cliff (minimize)
      5. -shape_order (minimize)
      6. -payments (lexicographic minimize via negated tuple)
    """
    Phi = tuple(placement.Phi[1 : M + 1])
    k = cand.k
    w = sim.w
    min_slack = min(sim.pre_min, min(w[j] - placement.Phi[j] for j in range(1, M + 1)))
    total_slack = sum(w[j] - placement.Phi[j] for j in range(1, M + 1))
    cliff = cliff_ratio(cand.payments)
    shape_ord = SHAPE_TIEBREAK_ORDER[cand.label]
    # Negate payments elementwise for lex-min via max
    neg_p = tuple(-x for x in cand.payments)
    return (Phi, -k, min_slack, total_slack, -cliff, -shape_ord, neg_p)


def evaluate_candidate(
    client: Client,
    cand: Candidate,
    cadence: list[date],
    bank_fee_cents: int,
    F: int,
    buffer: int,
) -> tuple[FeePlacement, SimulationResult] | None:
    """Simulate + place fee. Returns None if infeasible under buffer."""
    payments = list(zip(cadence[: cand.k], cand.payments))
    sim = simulate(client, payments, cadence, bank_fee_cents)
    placement = place_fee(sim.w, sim.pre_min, F, buffer)
    if not placement.feasible:
        return None
    # Also require all raw balances >= buffer (place_fee checks window mins /
    # pre_min; balances are end-of-day which equal intraday mins under credits-first).
    if any(b < buffer for b in sim.balances.values()):
        return None
    return placement, sim


def flatten_cliff(
    client: Client,
    cand: Candidate,
    cadence: list[date],
    bank_fee_cents: int,
    F: int,
    buffer: int,
    S: int,
    k_max: int,
    floors: list[int],
    rules: CreditorRules,
    placement: FeePlacement,
    sim: SimulationResult,
) -> tuple[Candidate, FeePlacement, SimulationResult]:
    """Tier B: transfer delta from p_k to p_{k-1} without changing Phi*."""
    if not ENABLE_CLIFF_FLATTENING:
        return cand, placement, sim
    k = cand.k
    if k < 2:
        return cand, placement, sim
    if cliff_ratio(cand.payments) <= MAX_CLIFF_RATIO:
        return cand, placement, sim

    p = list(cand.payments)
    med = median_low(cand.payments)
    # ceil(MAX_CLIFF_RATIO * med)
    target = (MAX_CLIFF_RATIO.numerator * med + MAX_CLIFF_RATIO.denominator - 1) // (
        MAX_CLIFF_RATIO.denominator
    )

    d_slack = sim.w[k - 1] - placement.Phi[k - 1]
    d_order = (p[k - 1] - p[k - 2] - 1) // 2
    d_need = p[k - 1] - target
    delta = max(0, min(d_slack, d_order, d_need))
    if delta <= 0:
        return cand, placement, sim

    new_p = list(p)
    new_p[k - 2] += delta
    new_p[k - 1] -= delta
    new_payments = tuple(new_p)
    new_label = cand.label  # preserve balloon label (p_k still > p_{k-1})
    if new_payments[-1] <= new_payments[-2]:
        return cand, placement, sim

    if not is_statically_valid(k, new_payments, S, k_max, floors, rules, new_label):
        return cand, placement, sim

    new_cand = Candidate(k=k, payments=new_payments, label=new_label)
    result = evaluate_candidate(client, new_cand, cadence, bank_fee_cents, F, buffer)
    if result is None:
        return cand, placement, sim
    new_placement, new_sim = result

    # Assert Phi* unchanged; otherwise revert.
    if new_placement.Phi != placement.Phi:
        return cand, placement, sim

    return new_cand, new_placement, new_sim
