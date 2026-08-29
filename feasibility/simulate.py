"""Chronological ledger simulator with credits-before-debits ordering."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from feasibility.models import Client


@dataclass(frozen=True)
class SimulationResult:
    """Balances after committed ledger + creditor payments + bank fees (no program fee).

    ``balances`` maps every evaluated date to end-of-day balance.
    ``w`` is 1-indexed by cadence: ``w[j]`` = min balance on ``[t_j, t_{j+1})``.
    ``pre_min`` is the min balance on dates strictly before ``t_1`` (or +inf if none).
    """

    balances: dict[date, int]
    ordered_dates: list[date]
    w: list[int]  # 1-indexed; w[0] unused
    pre_min: int


def simulate(
    client: Client,
    payments: list[tuple[date, int]],
    cadence: list[date],
    bank_fee_cents: int,
) -> SimulationResult:
    """Simulate the ledger without program fees.

    ``payments`` is ``(date, creditor_payment_cents)`` for the k payment dates.
    Bank fees are added on each payment-carrying date.
    """
    # Collect credits/debits per date from future committed ledger entries.
    credits: dict[date, int] = {}
    debits: dict[date, int] = {}

    for entry in client.ledger:
        if entry.date <= client.as_of_date:
            continue
        if entry.type == "credit":
            credits[entry.date] = credits.get(entry.date, 0) + entry.amount_cents
        else:
            debits[entry.date] = debits.get(entry.date, 0) + entry.amount_cents

    for d, p in payments:
        debits[d] = debits.get(d, 0) + p + bank_fee_cents

    event_dates = set(credits) | set(debits) | set(cadence)
    ordered = sorted(event_dates)

    balance = client.current_balance_cents
    balances: dict[date, int] = {}
    for d in ordered:
        balance += credits.get(d, 0)
        balance -= debits.get(d, 0)
        balances[d] = balance

    if not cadence:
        return SimulationResult(
            balances=balances,
            ordered_dates=ordered,
            w=[0],
            pre_min=min(balances.values()) if balances else 10**18,
        )

    t1 = cadence[0]
    pre_vals = [balances[d] for d in ordered if d < t1]
    pre_min = min(pre_vals) if pre_vals else 10**18

    M = len(cadence)
    w = [0] * (M + 1)
    for j in range(1, M + 1):
        left = cadence[j - 1]
        right = cadence[j] if j < M else None  # [t_j, t_{j+1})
        window_vals = [
            balances[d]
            for d in ordered
            if d >= left and (right is None or d < right)
        ]
        if not window_vals:
            prior = [balances[d] for d in ordered if d <= left]
            window_vals = [prior[-1] if prior else client.current_balance_cents]
        w[j] = min(window_vals)

    return SimulationResult(
        balances=balances,
        ordered_dates=ordered,
        w=w,
        pre_min=pre_min,
    )
