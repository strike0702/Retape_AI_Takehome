# Settlement Feasibility & Fee Engine

Given a client's escrow account, a settlement offer and a creditor's rules,
decide whether the offer is affordable (and schedule it, collecting our fee as
early as allowed) or if not compute the minimum extra funding needed.

Full problem statement is in `[ASSIGNMENT.md](./ASSIGNMENT.md)`.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

## Run

Evaluate a single case (prints the `Result` as JSON):

```bash
python run.py cases/case1_feasible_even
```

Run the full test suite:

```bash
pytest -q
```

Or with the module form (same suite):

```bash
python -m pytest -q
```



## Layout

```
feasibility/
├── constants.py   # buffer / cliff / guardrail knobs
├── models.py      # data models, loaders, date helpers
├── util.py        # round_half_up, ceil_div
├── simulate.py    # credits-before-debits ledger (no program fee)
├── shapes.py      # floors + even / balloon / staircase candidates
├── scoring.py     # suffix-min fee placement, scorer, cliff flatten
├── rescue.py      # Part 2 lump / increment minima
└── engine.py      # evaluate_offer + Result shape
```

The engine is stdlib only. `pytest` is the sole test dependency.

---



## Approach

I broke the problem into two pieces that look entangled but aren't. Choose the
creditor payment amounts, then decide when to take the program fee. Once I had
a fixed payment vector, fee timing turned out to have a closed form, so the
whole search collapsed to enumerating legal payment vectors and checking each
one.

The pipeline in `engine.py` is:

1. Build the monthly cadence from the first payment date through the client's
  horizon. Let `M` be how many of those dates fall on or before the horizon.
   Cap the installment count at
   `k ≤ min(max_payments, max_terms, M)`, where `k` is the number of creditor
   payments and the two maxima come from the creditor rules.
2. Generate the legal payment vectors for whatever shape families the flags
  allow, validate the static constraints (floors, non-decreasing, segment
   caps, exact sum) and dedupe.
3. For each candidate, run the ledger simulation **without** the program fee,
  then place the fee with the suffix-min rule below.
4. Rank the feasible ones by fee timing first, then the client-protection
  tie-breaks. Optionally cliff-flatten the winner afterward.
5. If nothing works at buffer zero, compute the Part 2 minima over that same
  candidate set with closed forms. Those amounts are cross-checked by
   binary-search oracles in the tests.

I considered an ILP and a DP and put both aside. On the shipped cases the space
is small. `max_payments` / `max_terms` top out at 12 (case 4 and the
assignment's sample rules). `M` is just the monthly cadence through each
client's horizon and `k_max = min(max_payments, max_terms, M)` stays in that
range. The structured generators produce on the order of hundreds of candidates
per case, not an exponential blow-up. A general-purpose solver would be heavier
than that and a lexicographic fee objective is painful to encode with MIP
weight stacks. A DP over remaining cents is worse. The sum axis is in the
millions while the position axis is only about `k` wide. The subproblem that
actually looked like DP (fee placement) has the closed form instead.

### Fee placement

The key observation was that fee and payments don't need a joint search. Fix
the creditor payments, simulate everything else with fee set to zero and the
earliest legal fee schedule falls out of the window minima.

I simulate committed credits and debits, the candidate creditor payments and
the bank fees, with program fee = 0. Write `B(v)` for the end-of-day escrow
balance on calendar date `v`.

Cadence dates are `t_1 < t_2 < … < t_M`. For each index `j` I define the window
minimum

```
w_j = min { B(v) | t_j ≤ v < t_{j+1} }
```

with the last window running from `t_M` through the remaining simulated dates.
So `w_j` is the lowest balance anywhere in the stretch that starts at payment
date `t_j` and ends just before the next cadence date.

Let `F` be the total program fee in cents (half-up of
`program_fee_pct × original_balance_cents`). The unique pointwise-maximal
feasible cumulative fee is

```
Φ*_j = min(F, min_{l ≥ j} w_l)
```

`Φ*_j` is how much of the fee I am allowed to have collected by `t_j` without
overdrawing any later window. The installment on `t_j` is
`fee_j = Φ*_j - Φ*_{j-1}` (with `Φ*_0 = 0`).

A candidate is fee-feasible when every simulated balance stays non-negative and
the last window can cover the whole fee (`w_M ≥ F`). That is what lets me
enumerate payment vectors independently of fee placement.

One thing I had to be careful about. Taking `min(remaining fee, w_j)` on each
date without the suffix is not the same. That greedy rule can lock in early
cumulative fee that a later window cannot support. Example with window minima
`w = [8000, 3500, 9000]` cents and `F = 7000`. A greedy take of 7000 on day one
conflicts with `w_2 = 3500`. The closed form keeps day-one cumulative fee at
3500 and finishes on day three. Covered by
`test_fee_suffix_min_respects_later_window_cap`.

### Shapes

I treated creditor flags as filters on what is legal, not as a hard-coded
choice of schedule. The `pay_shape_used` label comes from the winning vector
plus the flags, not from which helper emitted the list.


| Flag                    | What I generate                                                                 | `pay_shape_used`                                                              |
| ----------------------- | ------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| `even_pays`             | Exact even split of the offer total with remainder cents on the latest payments | `"even"`                                                                      |
| `is_ballooning_allowed` | Floors-pinned balloon **and** the staircase family                              | `"balloon"` if `k ≥ 2` and the last payment steps up, otherwise `"staircase"` |
| neither                 | Staircase with at most `max_segments` distinct levels                           | `"staircase"`                                                                 |


**Even pays.** Split the offer total `S` into `k` nearly equal installments.
Every payment gets `S // k` cents. The leftover `S % k` cents are added as
`+1` on each of the **latest** `S % k` payments so the vector stays
non-decreasing. Example with `S = 10003` and `k = 4`. Base `2500`, remainder `3`
gives `[2500, 2501, 2501, 2501]`.

**Balloon.** Pin payments `1 … k-1` at their position floors and put whatever
is left on the final payment, as long as that balloon is at least as large as
the prior payment and its own floor. Example with floors
`[2500, 2500, 2500, 2500]`, `S = 17500` and `k = 4` gives
`[2500, 2500, 2500, 10000]`.

**Floors, token pays, tiers.** Floors apply to every installment, including
balloons. They combine the base minimum, the token-pay rule and any tier
step-ups. Token pays look like a count (“at most `max_token_pays` payments may
equal the base minimum”), but payments are non-decreasing, so those
base-minimum payments have to be a prefix. From position
`max_token_pays + 1` onward I raise the floor to at least `base + 1`. Example
with base `2500`, `max_token_pays = 2`, no tiers and `k = 4`. Floors are
`[2500, 2500, 2501, 2501]`.

**Staircase.** Split the `k` payments into at most `max_segments` level groups
(same amount inside a group, strictly increasing across groups). Early groups
sit on their floor minimums. For the last two groups I need integer levels
`v_a` and `v_b` such that

```
c_a · v_a + c_b · v_b = R
```

where `c_a` and `c_b` are the two group lengths and `R` is whatever of the
offer total is left after pinning earlier groups. That is a small linear
Diophantine scan. Try feasible `v_a` values and keep those where
`R - c_a · v_a` divides evenly by `c_b` and `v_b` clears its floor and the
required step-up above `v_a`. Example with `k = 4`, `max_segments = 2`, floors
all `2500`, `S = 12000` and groups of sizes `(2, 2)`. Then `R = 12000` and one
solution is `v_a = 2500`, `v_b = 3500`, which gives `[2500, 2500, 3500, 3500]`.

I also always emit a nearly flat (quasi-even) schedule using the same remainder
rule as even pays. The reason is practical. The most extreme balloon can leave
too little cash in the final window to collect the fee (`w_M < F`) while a
flatter schedule would still work. Skipping that extreme would call some offers
infeasible for the wrong reason. `max_segments` applies to staircases only.
Balloons are exempt.

### Scoring and client protection

The assignment's primary goal is to collect the fee as early as possible. I
read that as lexicographic order on the cumulative fee vector
`(Φ*_1, …, Φ*_M)` and I never move that objective.

Front-loading the fee is good for the firm and hard on the client at the
margins. It drains early cushion. Each extra payment burns a bank fee out of
their escrow and a big final balloon puts settlement risk on one late day
*after* we have already been paid. The end goal of the client-protection layer
is this. Whenever the fee objective does not force a choice, pick the schedule
that leaves the client with more cash resilience and less concentration risk
without delaying a single cent of fee.

That still leaves a lot of ties. When two candidates agree on `Φ*`, I spend the
remaining freedom on the client.

1. Prefer fewer payments `k`. Each payment burns a bank fee out of escrow and
  case 1 has several values of `k` that tie on fee timing.
2. Prefer more post-fee slack in the windows (min slack, then total slack) so a
  late debit or a slightly thin draft is less likely to bounce the account.
3. Prefer a smaller cliff ratio `p_k / median(p)`, where `p_1, …, p_k` are the
  creditor payment amounts in cents and `median(p)` is their lower median so
   we are not needlessly concentrating the settlement on one huge final hit.
4. Prefer a fixed shape order (`even` before `staircase` before `balloon`),
  then a lexicographic payment vector so the choice is bit-stable.

Cliff flattening is a post-pass on purpose. Once the winner and its `Φ*` are
fixed, residual window slack sometimes lets me move cents from the final
payment `p_k` into `p_{k-1}` without changing the fee profile on any date. Same
fee timing for the firm. A milder last step for the client. That is why it
belongs after scoring, not inside it. The move is capped by `MAX_CLIFF_RATIO`
in `constants.py` (default final payment at most about 3× the lower-median
payment) and `ENABLE_CLIFF_FLATTENING` turns it off.

There is also an optional soft safety cushion. It is a two-pass search that
first prefers candidates keeping a configured buffer above zero, then falls
back to buffer zero if none exist. I left the defaults at zero
(`DEFAULT_BUFFER_CENTS = 0`, `BUFFER_DRAFT_PCT = 0`) because a nonzero buffer
delays fee collection. That felt like a product knob, not something to enable
quietly for the assignment.

### Part 2

When no schedule is feasible, I still have the candidate set from Part 1.
Adding money never makes those candidates harder to pay, so the minimum lump
sum `L*` and the minimum uniform draft increment `X*` are well-defined over
that set. I compute both with closed forms in `rescue.py` and I keep
binary-search oracles in `tests/test_rescue.py` that have to agree. I wanted
the closed form for clarity (and so the minimum has an explanation) and the
oracle so a ceiling-division bug cannot silently pass.

`L*` is pinned by the binding constraint. I report the **latest** calendar date
that still achieves that same amount. Same ask, more time for the client to
raise the funds (same-day credits land before debits). Guardrails only set
`within_guardrail` and a reason. I still report the true minimum. If no amount
of money can produce a legal split inside the horizon, `amount_cents` is `0`
with an explanation rather than inventing a number.

---



## Assumptions

- **Offer balance.** I renamed the offer field to `creditor_balance_cents` to
match the spec and keep it distinct from the client's SDA balance
(`Client.current_balance_cents`). The loader still accepts the older JSON key
`current_balance_cents` so the shipped cases load unchanged.
- **Rounding.** Money percentages go through `Decimal` half-up. I do not use
builtin banker's `round()` for offer totals or program fees.
- **Drafts.** Ledger credits are the drafts. I never synthesize them from
`draft_day`. In Part 2, `N` (reported as `num_drafts`) counts every future
credit after `as_of_date`.
- **Schedule rows.** I emit a row only for cadence dates that carry a creditor
payment and/or a positive fee installment. Fee-only dates do not get a bank
fee.
- **Single payment.** When `k = 1` and even-pays is false, I label it
`"staircase"`. Balloon needs `k ≥ 2` and a step-up on the final payment.
- **Same-day ordering.** Credits before debits. Closing balance must be ≥ 0.
Exact zero is allowed.



## Limitations

- **Candidate family is structured, not exhaustive.** I generate floor-pinned  
prefixes, a Diophantine band on the last two staircase levels, balloons when  
allowed and the quasi-even extreme. That covers the schedules I need for the  
shipped cases and the fee-starvation trap, but I have not proven it hits every  
legal non-decreasing vector under weird floor patterns.
- **Soft cushion defaults off.** The optional buffer above zero
(`DEFAULT_BUFFER_CENTS` / `BUFFER_DRAFT_PCT` in `constants.py`) delays fee
collection whenever it binds. I keep both at zero for assignment-faithful
runs. Turning them on is an explicit product choice.
- **Part 2 minima are over the same candidate family.** `L`* and `X*` are
minimal among the vectors I enumerate, not among every conceivable legal
payment list the creditor rules might allow outside that family.
- **Runtime is fine for this problem size.** Each simulation is linear in the
ledger size plus `M`. On the provided cases the full `pytest` suite finishes
in well under a second. A multi-year horizon with a much larger
`max_payments` would want a tighter generator loop.



## Tests

On top of the shipped smoke and case tests I added coverage for rounding,
cadence construction, floors, shape generation, ledger ordering, fee placement
(including the greedy versus suffix-min conflict above), independent schedule
re-checks on the feasible fixtures, Part 2 closed-form versus binary-search
oracles and strict minimality checks on case 2 (`L* - 1` and `X* - 1` stay
infeasible).