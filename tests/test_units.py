"""Unit tests: rounding, cadence, floors, shapes, fee placement, simulation."""

from __future__ import annotations

from datetime import date

from feasibility.engine import evaluate_offer
from feasibility.models import (
    Client,
    CreditorRules,
    LedgerEntry,
    end_of_month,
    is_end_of_month,
    load_case,
    monthly_payment_dates,
    offer_total_cents,
    program_fee_cents,
)
from feasibility.scoring import place_fee
from feasibility.shapes import (
    compute_floors,
    even_vector,
    generate_candidates,
)
from feasibility.simulate import simulate
from feasibility.util import round_half_up


# ---------------------------------------------------------------------------
# Rounding
# ---------------------------------------------------------------------------

def test_round_half_up_half_boundary():
    assert round_half_up(0.005, 100) == 1
    assert round_half_up("0.5", 5) == 3  # 2.5 -> 3 (not banker's 2)
    assert round_half_up(0.5, 1) == 1
    assert round_half_up(0.25, 100000) == 25000


def test_builtin_round_differs_from_half_up():
    # Python banker's rounding: round(2.5) == 2 on many platforms.
    assert round_half_up("0.5", 5) == 3
    assert round(2.5) == 2  # confirms the trap exists


# ---------------------------------------------------------------------------
# Cadence
# ---------------------------------------------------------------------------

def test_eom_preserving_cadence():
    dates = monthly_payment_dates(date(2026, 1, 31), 3)
    assert dates == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)]
    assert all(is_end_of_month(d) for d in dates)


def test_day_clamped_cadence():
    dates = monthly_payment_dates(date(2026, 1, 30), 3)
    assert dates == [date(2026, 1, 30), date(2026, 2, 28), date(2026, 3, 30)]


def test_horizon_truncates_k_max():
    client, offer, rules = load_case("cases/case1_feasible_even")
    # Horizon is 2026-07-01; first payment 2026-01-31 EOM chain.
    # Cadence dates <= Jul 1: Jan..Jun (6 dates). Jul 31 is past horizon.
    dates = monthly_payment_dates(date(2026, 1, 31), 8)
    within = [d for d in dates if d <= client.last_draft_date]
    assert within[-1] == date(2026, 6, 30)
    assert date(2026, 7, 31) not in within


def test_default_first_payment_date():
    client, offer, rules = load_case("cases/case1_feasible_even")
    offer.first_payment_date = None
    r = evaluate_offer(client, offer, rules)
    assert r.feasible is True
    assert r.schedule[0].date == end_of_month(client.first_draft_date)


# ---------------------------------------------------------------------------
# Floors / token / tiers
# ---------------------------------------------------------------------------

def test_token_pays_zero_forces_above_base():
    rules = CreditorRules(
        max_terms=3,
        max_payments=3,
        min_payment_cents=2500,
        max_token_pays=0,
        min_payment_tiers=[],
        even_pays=False,
        is_ballooning_allowed=False,
        max_segments=3,
        bank_fee_cents=0,
        program_fee_pct=0.0,
    )
    floors = compute_floors(rules, 3)
    assert floors[1] == 2501
    assert floors[2] == 2501


def test_tier_at_position_one():
    rules = CreditorRules(
        max_terms=4,
        max_payments=4,
        min_payment_cents=2500,
        max_token_pays=4,
        min_payment_tiers=[(1, 5000)],
        even_pays=False,
        is_ballooning_allowed=False,
        max_segments=2,
        bank_fee_cents=0,
        program_fee_pct=0.0,
    )
    floors = compute_floors(rules, 4)
    assert all(floors[i] == 5000 for i in range(1, 5))


def test_tier_beyond_k_inactive():
    rules = CreditorRules(
        max_terms=3,
        max_payments=3,
        min_payment_cents=2500,
        max_token_pays=3,
        min_payment_tiers=[(10, 9000)],
        even_pays=False,
        is_ballooning_allowed=False,
        max_segments=2,
        bank_fee_cents=0,
        program_fee_pct=0.0,
    )
    floors = compute_floors(rules, 3)
    assert floors[3] == 2500


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------

def test_even_remainder_on_latest():
    assert even_vector(100, 3) == (33, 33, 34)
    assert even_vector(10, 4) == (2, 2, 3, 3)


def test_even_exact_sum():
    for S in (100, 101, 99999):
        for k in (1, 2, 5, 7):
            p = even_vector(S, k)
            assert sum(p) == S
            assert list(p) == sorted(p)


def test_staircase_respects_max_segments():
    rules = CreditorRules(
        max_terms=6,
        max_payments=6,
        min_payment_cents=1000,
        max_token_pays=6,
        min_payment_tiers=[],
        even_pays=False,
        is_ballooning_allowed=False,
        max_segments=2,
        bank_fee_cents=0,
        program_fee_pct=0.0,
    )
    floors = compute_floors(rules, 6)
    cands = generate_candidates(12000, 6, floors, rules)
    for c in cands:
        assert c.label == "staircase"
        assert len(set(c.payments)) <= 2
        assert sum(c.payments) == 12000


def test_balloon_exempt_from_segments():
    rules = CreditorRules(
        max_terms=4,
        max_payments=4,
        min_payment_cents=2500,
        max_token_pays=4,
        min_payment_tiers=[],
        even_pays=False,
        is_ballooning_allowed=True,
        max_segments=1,  # would forbid multi-level staircase
        bank_fee_cents=0,
        program_fee_pct=0.0,
    )
    floors = compute_floors(rules, 4)
    cands = generate_candidates(20000, 4, floors, rules)
    balloons = [c for c in cands if c.label == "balloon"]
    assert balloons
    assert any(len(set(c.payments)) > 1 for c in balloons)


# ---------------------------------------------------------------------------
# Simulation / same-day ordering
# ---------------------------------------------------------------------------

def test_same_day_exact_zero_balance():
    client = Client(
        draft_amount_cents=20000,
        draft_day=1,
        first_draft_date=date(2026, 1, 1),
        last_draft_date=date(2026, 1, 31),
        as_of_date=date(2025, 12, 31),
        current_balance_cents=0,
        ledger=[
            LedgerEntry(date(2026, 1, 31), 20000, "credit"),
        ],
    )
    cadence = [date(2026, 1, 31)]
    # payment 8334 + fee handled separately; bank 500; credit 20000
    # Simulate payment+bank only: 20000 - 8334 - 500 = 11166 left for fee.
    sim = simulate(client, [(date(2026, 1, 31), 8334)], cadence, bank_fee_cents=500)
    assert sim.balances[date(2026, 1, 31)] == 11166
    assert sim.w[1] == 11166


def test_mid_window_committed_debit_in_w():
    """Case-3 style: debit between cadence dates must lower the window min."""
    client = Client(
        draft_amount_cents=10000,
        draft_day=1,
        first_draft_date=date(2026, 1, 1),
        last_draft_date=date(2026, 3, 1),
        as_of_date=date(2025, 12, 31),
        current_balance_cents=0,
        ledger=[
            LedgerEntry(date(2026, 1, 1), 10000, "credit"),
            LedgerEntry(date(2026, 2, 1), 10000, "credit"),
            LedgerEntry(date(2026, 2, 1), 15000, "debit"),
            LedgerEntry(date(2026, 3, 1), 10000, "credit"),
        ],
    )
    cadence = [date(2026, 1, 31), date(2026, 2, 28)]
    sim = simulate(
        client,
        [(date(2026, 1, 31), 2500), (date(2026, 2, 28), 2500)],
        cadence,
        bank_fee_cents=0,
    )
    # After Jan 31 payment: balance 7500. Feb 1: +10000 -15000 = 2500.
    # Window 1 = [Jan31, Feb28) includes Feb 1 → w[1] should be <= 2500.
    assert sim.w[1] == 2500


def test_fee_placement_suffix_min():
    # w = [_, 50, 30, 80], F=40, buffer=0
    # suffix: 50→min(50,30,80)=30; 30→min(30,80)=30; 80→80
    # Phi: min(40,30)=30, 30, min(40,80)=40
    placement = place_fee(w=[0, 50, 30, 80], pre_min=100, F=40, buffer=0)
    assert placement.feasible is True
    assert placement.Phi[1:] == [30, 30, 40]
    assert placement.fee[1:] == [30, 0, 10]


def test_fee_suffix_min_respects_later_window_cap():
    """Greedy min(remaining, w_j) can exceed a later window's capacity.

    w=[8000, 3500, 9000], F=7000: taking 7000 on day 1 conflicts with w[2]=3500.
    Suffix-min keeps day-1 cumulative at 3500 and finishes on day 3.
    """
    w = [0, 8000, 3500, 9000]
    F = 7000
    greedy_day1 = min(F, w[1])
    assert greedy_day1 == 7000
    assert greedy_day1 > w[2]

    placement = place_fee(w=w, pre_min=10000, F=F, buffer=0)
    assert placement.feasible is True
    assert placement.Phi[1:] == [3500, 3500, 7000]
    assert placement.fee[1:] == [3500, 0, 3500]


def test_fee_infeasible_when_tail_starved():
    placement = place_fee(w=[0, 100, 100, 10], pre_min=100, F=50, buffer=0)
    assert placement.feasible is False


# ---------------------------------------------------------------------------
# Independent schedule checker on provided cases
# ---------------------------------------------------------------------------

def _independent_check(case: str) -> None:
    client, offer, rules = load_case(f"cases/{case}")
    r = evaluate_offer(client, offer, rules)
    assert r.feasible is True
    assert r.schedule is not None
    S = offer_total_cents(offer)
    F = program_fee_cents(offer, rules)
    pays = [row.creditor_payment_cents for row in r.schedule if row.creditor_payment_cents > 0]
    assert sum(pays) == S
    assert list(pays) == sorted(pays)
    assert sum(row.program_fee_cents for row in r.schedule) == F
    for row in r.schedule:
        assert row.balance_cents >= 0
        assert row.date <= client.last_draft_date
    # Fee timing: no fee before first creditor payment date
    first_pay = next(row.date for row in r.schedule if row.creditor_payment_cents > 0)
    for row in r.schedule:
        if row.program_fee_cents > 0:
            assert row.date >= first_pay
        if row.creditor_payment_cents == 0:
            assert row.bank_fee_cents == 0
        else:
            assert row.bank_fee_cents == rules.bank_fee_cents
    # Token / floors
    floors = compute_floors(rules, len(pays))
    for i, p in enumerate(pays, start=1):
        assert p >= floors[i]
    token_count = sum(1 for p in pays if p == rules.min_payment_cents)
    assert token_count <= rules.max_token_pays
    # Segments
    if r.pay_shape_used == "staircase":
        assert len(set(pays)) <= rules.max_segments
    if r.pay_shape_used == "even":
        assert rules.even_pays is True


def test_independent_check_case1():
    _independent_check("case1_feasible_even")


def test_independent_check_case3():
    _independent_check("case3_balloon")


def test_independent_check_case4():
    _independent_check("case4_tiers")


def test_cliff_flattening_preserves_phi():
    """Balloon winner with slack: flattening must not change Phi* (F=0 → trivial)."""
    from feasibility.constants import ENABLE_CLIFF_FLATTENING

    assert ENABLE_CLIFF_FLATTENING is True
    client, offer, rules = load_case("cases/case3_balloon")
    r = evaluate_offer(client, offer, rules)
    assert r.feasible is True
    assert r.pay_shape_used == "balloon"
    pays = [row.creditor_payment_cents for row in r.schedule if row.creditor_payment_cents > 0]
    assert pays[-1] >= pays[-2]
    assert sum(pays) == offer_total_cents(offer)


def test_max_segments_one_with_tier():
    """max_segments=1 with an in-range tier forces flat-at-tier or skips that k."""
    rules = CreditorRules(
        max_terms=8,
        max_payments=8,
        min_payment_cents=2500,
        max_token_pays=8,
        min_payment_tiers=[(4, 5000)],
        even_pays=False,
        is_ballooning_allowed=False,
        max_segments=1,
        bank_fee_cents=0,
        program_fee_pct=0.0,
    )
    floors = compute_floors(rules, 8)
    # For k>=4, floors jump at 4 → cannot be single-valued unless all at >=5000.
    cands = generate_candidates(40000, 8, floors, rules)
    for c in cands:
        assert len(set(c.payments)) <= 1
        if c.k >= 4:
            assert all(p >= 5000 for p in c.payments)