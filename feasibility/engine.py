"""Settlement feasibility engine — evaluate_offer orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from feasibility.constants import BUFFER_DRAFT_PCT, DEFAULT_BUFFER_CENTS
from feasibility.models import (
    Client,
    CreditorRules,
    Offer,
    default_first_payment_date,
    monthly_payment_dates,
    offer_total_cents,
    program_fee_cents,
)
from feasibility.rescue import compute_increment, compute_lump
from feasibility.scoring import (
    evaluate_candidate,
    flatten_cliff,
    score_key,
)
from feasibility.shapes import compute_floors, generate_candidates
from feasibility.util import round_half_up


@dataclass
class ScheduleRow:
    date: date
    creditor_payment_cents: int
    program_fee_cents: int
    bank_fee_cents: int
    balance_cents: int


@dataclass
class FundsOption:
    amount_cents: int
    within_guardrail: bool
    reason: str
    # lump-sum only:
    date: date | None = None
    # monthly-increment only:
    num_drafts: int | None = None


@dataclass
class AdditionalFunds:
    lump_sum: FundsOption
    monthly_increment: FundsOption


@dataclass
class Result:
    feasible: bool
    # One of "even", "staircase", or "balloon" — the shape your solution produced
    # (driven by the creditor flags). None when infeasible.
    pay_shape_used: str | None = None
    schedule: list[ScheduleRow] | None = None
    additional_funds: AdditionalFunds | None = None

    def to_dict(self) -> dict:
        out: dict = {"feasible": self.feasible, "pay_shape_used": self.pay_shape_used}
        out["schedule"] = (
            [
                {
                    "date": r.date.isoformat(),
                    "creditor_payment_cents": r.creditor_payment_cents,
                    "program_fee_cents": r.program_fee_cents,
                    "bank_fee_cents": r.bank_fee_cents,
                    "balance_cents": r.balance_cents,
                }
                for r in self.schedule
            ]
            if self.schedule is not None
            else None
        )
        if self.additional_funds is None:
            out["additional_funds"] = None
        else:

            def opt(o: FundsOption) -> dict:
                d = {
                    "amount_cents": o.amount_cents,
                    "within_guardrail": o.within_guardrail,
                    "reason": o.reason,
                }
                if o.date is not None:
                    d["date"] = o.date.isoformat()
                if o.num_drafts is not None:
                    d["num_drafts"] = o.num_drafts
                return d

            out["additional_funds"] = {
                "lump_sum": opt(self.additional_funds.lump_sum),
                "monthly_increment": opt(self.additional_funds.monthly_increment),
            }
        return out


def _build_cadence(client: Client, offer: Offer) -> list[date]:
    t1 = offer.first_payment_date or default_first_payment_date(client)
    horizon = client.last_draft_date
    # Generate enough months; truncate to horizon.
    # Upper bound: months from t1 to horizon + a little slack.
    months = (horizon.year - t1.year) * 12 + (horizon.month - t1.month) + 2
    months = max(months, 1)
    dates = monthly_payment_dates(t1, months)
    return [d for d in dates if d <= horizon]


def _assemble_schedule(
    cand,
    cadence: list[date],
    placement,
    sim,
    bank_fee_cents: int,
) -> list[ScheduleRow]:
    k = cand.k
    M = len(cadence)
    last_fee_j = 0
    for j in range(1, M + 1):
        if placement.fee[j] > 0:
            last_fee_j = j
    j_last = max(k, last_fee_j)
    rows: list[ScheduleRow] = []
    for j in range(1, j_last + 1):
        p_j = cand.payments[j - 1] if j <= k else 0
        fee_j = placement.fee[j]
        if p_j == 0 and fee_j == 0:
            continue
        bf = bank_fee_cents if j <= k else 0
        t_j = cadence[j - 1]
        bal = sim.balances[t_j] - placement.Phi[j]
        rows.append(
            ScheduleRow(
                date=t_j,
                creditor_payment_cents=p_j,
                program_fee_cents=fee_j,
                bank_fee_cents=bf,
                balance_cents=bal,
            )
        )
    return rows


def _search_feasible(
    client: Client,
    candidates,
    cadence: list[date],
    bank_fee_cents: int,
    F: int,
    buffer: int,
    S: int,
    k_max: int,
    floors: list[int],
    rules: CreditorRules,
):
    """Return (cand, placement, sim) for the best feasible candidate, or None."""
    M = len(cadence)
    best = None
    best_key = None
    for cand in candidates:
        result = evaluate_candidate(
            client, cand, cadence, bank_fee_cents, F, buffer
        )
        if result is None:
            continue
        placement, sim = result
        key = score_key(cand, placement, sim, M)
        if best_key is None or key > best_key:
            best_key = key
            best = (cand, placement, sim)

    if best is None:
        return None

    cand, placement, sim = best
    cand, placement, sim = flatten_cliff(
        client,
        cand,
        cadence,
        bank_fee_cents,
        F,
        buffer,
        S,
        k_max,
        floors,
        rules,
        placement,
        sim,
    )
    return cand, placement, sim


def evaluate_offer(client: Client, offer: Offer, rules: CreditorRules) -> Result:
    """Evaluate a single offer. See ASSIGNMENT.md for the full specification."""
    S = offer_total_cents(offer)
    F = program_fee_cents(offer, rules)
    cadence = _build_cadence(client, offer)
    M = len(cadence)
    k_max = min(rules.max_payments, rules.max_terms, M) if M > 0 else 0
    floors = compute_floors(rules, max(k_max, 1))
    candidates = (
        generate_candidates(S, k_max, floors, rules) if k_max > 0 else []
    )

    buffer = max(
        DEFAULT_BUFFER_CENTS,
        round_half_up(BUFFER_DRAFT_PCT, client.draft_amount_cents),
    )

    # Tier C: try with soft buffer, fall back to 0.
    found = _search_feasible(
        client,
        candidates,
        cadence,
        rules.bank_fee_cents,
        F,
        buffer,
        S,
        k_max,
        floors,
        rules,
    )
    if found is None and buffer > 0:
        found = _search_feasible(
            client,
            candidates,
            cadence,
            rules.bank_fee_cents,
            F,
            0,
            S,
            k_max,
            floors,
            rules,
        )

    if found is not None:
        cand, placement, sim = found
        rows = _assemble_schedule(
            cand, cadence, placement, sim, rules.bank_fee_cents
        )
        return Result(
            feasible=True,
            pay_shape_used=cand.label,
            schedule=rows,
            additional_funds=None,
        )

    # Part 2 — only issued from a buffer=0 evaluation (we already are here).
    lump = compute_lump(
        client, candidates, cadence, rules.bank_fee_cents, F, S
    )
    incr = compute_increment(
        client, candidates, cadence, rules.bank_fee_cents, F
    )
    return Result(
        feasible=False,
        pay_shape_used=None,
        schedule=None,
        additional_funds=AdditionalFunds(
            lump_sum=FundsOption(
                amount_cents=lump.amount_cents,
                within_guardrail=lump.within_guardrail,
                reason=lump.reason,
                date=lump.date,
            ),
            monthly_increment=FundsOption(
                amount_cents=incr.amount_cents,
                within_guardrail=incr.within_guardrail,
                reason=incr.reason,
                num_drafts=incr.num_drafts,
            ),
        ),
    )
