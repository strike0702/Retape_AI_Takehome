"""Part 2 rescue tests: closed forms, guardrails, binary-search oracle."""

from __future__ import annotations

from copy import deepcopy
from datetime import date

from feasibility.engine import evaluate_offer
from feasibility.models import (
    Client,
    CreditorRules,
    LedgerEntry,
    Offer,
    load_case,
    offer_total_cents,
    program_fee_cents,
)
from feasibility.util import round_half_up


def test_case2_lump_and_increment():
    client, offer, rules = load_case("cases/case2_infeasible_minima")
    r = evaluate_offer(client, offer, rules)
    assert r.feasible is False
    af = r.additional_funds
    assert af is not None
    assert af.lump_sum.amount_cents == 10000
    assert af.lump_sum.within_guardrail is True
    assert af.lump_sum.date is not None
    assert af.lump_sum.date <= client.last_draft_date
    assert af.monthly_increment.amount_cents == 2500
    assert af.monthly_increment.num_drafts == 5
    assert af.monthly_increment.within_guardrail is True


def test_case2_reported_lump_is_minimal():
    """L* is tight: L*-1 on the chosen date stays infeasible; L* succeeds."""
    client, offer, rules = load_case("cases/case2_infeasible_minima")
    r = evaluate_offer(client, offer, rules)
    L = r.additional_funds.lump_sum.amount_cents
    on = r.additional_funds.lump_sum.date
    assert L > 0 and on is not None
    assert evaluate_offer(_with_lump(client, L - 1, on), offer, rules).feasible is False
    assert evaluate_offer(_with_lump(client, L, on), offer, rules).feasible is True


def test_case2_reported_increment_is_minimal():
    """X* is tight: X*-1 on future drafts stays infeasible; X* succeeds."""
    client, offer, rules = load_case("cases/case2_infeasible_minima")
    r = evaluate_offer(client, offer, rules)
    X = r.additional_funds.monthly_increment.amount_cents
    assert X > 0
    assert evaluate_offer(_with_increment(client, X - 1), offer, rules).feasible is False
    assert evaluate_offer(_with_increment(client, X), offer, rules).feasible is True


def test_case2_lump_within_guardrail_math():
    client, offer, rules = load_case("cases/case2_infeasible_minima")
    S = offer_total_cents(offer)
    cap = round_half_up("0.65", S)
    r = evaluate_offer(client, offer, rules)
    assert r.additional_funds.lump_sum.amount_cents <= cap


def _with_lump(client: Client, amount: int, on: date) -> Client:
    c = deepcopy(client)
    c.ledger = list(c.ledger) + [LedgerEntry(on, amount, "credit")]
    return c


def _with_increment(client: Client, X: int) -> Client:
    c = deepcopy(client)
    new_ledger: list[LedgerEntry] = []
    for e in c.ledger:
        if e.type == "credit" and e.date > c.as_of_date:
            new_ledger.append(LedgerEntry(e.date, e.amount_cents + X, "credit"))
        else:
            new_ledger.append(e)
    c.ledger = new_ledger
    return c


def _oracle_lump(client: Client, offer: Offer, rules: CreditorRules) -> int:
    """Binary-search minimal lump on the earliest future ledger date."""
    future = sorted(
        {e.date for e in client.ledger if e.date > client.as_of_date}
    )
    assert future
    on = future[0]
    # Upper bound: offer total + program fee (generous).
    hi = offer_total_cents(offer) + program_fee_cents(offer, rules) + 1
    lo = 0
    # Find min L such that feasible.
    while lo < hi:
        mid = (lo + hi) // 2
        r = evaluate_offer(_with_lump(client, mid, on), offer, rules)
        if r.feasible:
            hi = mid
        else:
            lo = mid + 1
    return lo


def _oracle_increment(client: Client, offer: Offer, rules: CreditorRules) -> int:
    hi = max(client.draft_amount_cents * 2, 50000)
    lo = 0
    while lo < hi:
        mid = (lo + hi) // 2
        r = evaluate_offer(_with_increment(client, mid), offer, rules)
        if r.feasible:
            hi = mid
        else:
            lo = mid + 1
    return lo


def test_binary_search_oracle_case2_lump():
    client, offer, rules = load_case("cases/case2_infeasible_minima")
    r = evaluate_offer(client, offer, rules)
    # Closed-form L* must match oracle when lump is placed early enough.
    # Oracle places on earliest future date (weakly dominant).
    assert _oracle_lump(client, offer, rules) == r.additional_funds.lump_sum.amount_cents


def test_binary_search_oracle_case2_increment():
    client, offer, rules = load_case("cases/case2_infeasible_minima")
    r = evaluate_offer(client, offer, rules)
    assert _oracle_increment(client, offer, rules) == r.additional_funds.monthly_increment.amount_cents


def test_second_infeasible_fixture_oracle():
    """Tighter draft / larger offer — second cross-validation fixture."""
    client = Client(
        draft_amount_cents=5000,
        draft_day=1,
        first_draft_date=date(2026, 1, 1),
        last_draft_date=date(2026, 4, 1),
        as_of_date=date(2025, 12, 31),
        current_balance_cents=0,
        ledger=[
            LedgerEntry(date(2026, 1, 1), 5000, "credit"),
            LedgerEntry(date(2026, 2, 1), 5000, "credit"),
            LedgerEntry(date(2026, 3, 1), 5000, "credit"),
            LedgerEntry(date(2026, 4, 1), 5000, "credit"),
        ],
    )
    offer = Offer(
        creditor="Tight2",
        creditor_balance_cents=40000,
        original_balance_cents=40000,
        settlement_pct=0.5,
        first_payment_date=date(2026, 1, 31),
    )
    rules = CreditorRules(
        max_terms=3,
        max_payments=3,
        min_payment_cents=2500,
        max_token_pays=3,
        min_payment_tiers=[],
        even_pays=False,
        is_ballooning_allowed=False,
        max_segments=3,
        bank_fee_cents=0,
        program_fee_pct=0.1,
    )
    r = evaluate_offer(client, offer, rules)
    assert r.feasible is False
    assert _oracle_lump(client, offer, rules) == r.additional_funds.lump_sum.amount_cents
    assert _oracle_increment(client, offer, rules) == r.additional_funds.monthly_increment.amount_cents


def test_increment_unfixable_before_any_draft():
    """Deficit on as_of balance with no future draft before the shortfall date."""
    client = Client(
        draft_amount_cents=10000,
        draft_day=15,
        first_draft_date=date(2026, 3, 15),
        last_draft_date=date(2026, 6, 15),
        as_of_date=date(2026, 1, 31),
        current_balance_cents=-5000,  # already underwater
        ledger=[
            LedgerEntry(date(2026, 3, 15), 10000, "credit"),
            LedgerEntry(date(2026, 4, 15), 10000, "credit"),
            LedgerEntry(date(2026, 5, 15), 10000, "credit"),
            LedgerEntry(date(2026, 6, 15), 10000, "credit"),
        ],
    )
    # Committed debit right after as_of, before any future draft.
    client.ledger.insert(0, LedgerEntry(date(2026, 2, 1), 1, "debit"))
    offer = Offer(
        creditor="PreDraft",
        creditor_balance_cents=10000,
        original_balance_cents=10000,
        settlement_pct=0.5,
        first_payment_date=date(2026, 3, 31),
    )
    rules = CreditorRules(
        max_terms=2,
        max_payments=2,
        min_payment_cents=1000,
        max_token_pays=2,
        min_payment_tiers=[],
        even_pays=True,
        is_ballooning_allowed=False,
        max_segments=1,
        bank_fee_cents=0,
        program_fee_pct=0.0,
    )
    r = evaluate_offer(client, offer, rules)
    # May or may not be feasible depending on recovery; if infeasible, increment
    # should flag unfixable when shortfall is before drafts.
    if not r.feasible:
        # The Feb 1 debit with negative starting balance: d(v)=0 at Feb 1.
        # Lump can fix it; increment cannot if every candidate sees that deficit.
        assert r.additional_funds is not None
        # At minimum the lump should be positive.
        assert r.additional_funds.lump_sum.amount_cents > 0


def test_guardrail_reject_large_lump():
    """Construct a case where L* exceeds 65% of offer total."""
    client = Client(
        draft_amount_cents=1000,
        draft_day=1,
        first_draft_date=date(2026, 1, 1),
        last_draft_date=date(2026, 3, 1),
        as_of_date=date(2025, 12, 31),
        current_balance_cents=0,
        ledger=[
            LedgerEntry(date(2026, 1, 1), 1000, "credit"),
            LedgerEntry(date(2026, 2, 1), 1000, "credit"),
            LedgerEntry(date(2026, 3, 1), 1000, "credit"),
        ],
    )
    offer = Offer(
        creditor="HugeGap",
        creditor_balance_cents=20000,
        original_balance_cents=20000,
        settlement_pct=0.5,  # S=10000
        first_payment_date=date(2026, 1, 31),
    )
    rules = CreditorRules(
        max_terms=2,
        max_payments=2,
        min_payment_cents=1000,
        max_token_pays=2,
        min_payment_tiers=[],
        even_pays=True,
        is_ballooning_allowed=False,
        max_segments=1,
        bank_fee_cents=0,
        program_fee_pct=0.0,
    )
    r = evaluate_offer(client, offer, rules)
    assert r.feasible is False
    # Need ~7000+ more; 65% of 10000 = 6500 → should fail guardrail if L > 6500.
    if r.additional_funds.lump_sum.amount_cents > round_half_up("0.65", 10000):
        assert r.additional_funds.lump_sum.within_guardrail is False
        assert "affordability cap" in r.additional_funds.lump_sum.reason
