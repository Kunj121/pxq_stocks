# stopping — can the exit be learned?

**Status: no, and fitting makes it worse than doing nothing.** A one-parameter policy
fitted leave-one-expiry-cycle-out turned an in-sample gain into an out-of-sample loss in
3 of 4 folds. Fitted on the training cycles and run on the test cycles it returns
**-15.8%** against **-14.8%** for holding to expiry — paired CI **[-2.7%, -0.0%]**, i.e.
significantly *worse*. The Longstaff-Schwartz engine this work was meant to build was not
built, because the gate that licenses it fails.

The one arm that beats holding with a confidence interval clear of zero is an **unfitted**
+30% take-profit: **+7.5%** on test, 66% win rate, **+22.4%** against hold, CI
**[+2.1%, +39.5%]**.

## What was being attempted

Exiting an option position is an optimal-stopping problem, so the textbook instrument is
Longstaff-Schwartz: at each decision date regress the realised continuation value on basis
functions of the state, and exit where the immediate value exceeds the fitted continuation.
A machine-learned continuation value was to run beside it as a comparison.

## 1. The harness

[`stopping.py`](stopping.py). Every exit is a policy passed to one walker, so the decision
grid and the trading cost are arguments rather than assumptions.

| Rule | Grade |
|---|---|
| Every arm evaluated on the identical grid | mechanical — `exit_rules.simulate` hard-codes `for i in range(1, len(mark))`, so a daily policy could not be compared with a 15-minute one at all |
| `evaluate(grid="fine", cost_frac=0)` reproduces `exit_rules.simulate` exactly | **definitional** — `check_reconciles()` asserts max abs difference across all 305 trades; it comes out at **0.00e+00**, which preserves the dashboard reconciliation in [`jump_sweeps.md`](jump_sweeps.md) §10 |
| A 2% half-spread on every early exit | **evidenced** — `swept_spread` median $0.45 against `entry_price` median $11.12. Expiry settles at intrinsic and pays nothing, so costing exits at zero is a systematic ~2pp gift to every early-exit rule, on effects that are themselves ~5pp |
| Train/test split by **expiry cycle**, not entry date | **definitional** — splitting on `confirm_ts <= 2026-07-18` leaves 13 training trades holding Aug/Sep expiries whose bars run through the entire test window. Expiry-aligned is 171 / 134 and nothing crosses |
| Paired cluster CI on the *difference*, resampled by ticker | inference — overlapping CIs on two levels answer a different question, and MU + NVDA alone are 29% of the book |

## 2. Gate 0 — the test that stops the project

Fit the single best take-profit on three expiry cycles, evaluate on the fourth. One free
parameter, daily grid, no cost.

```
held out     n    fitted TP   in-sample     oos    oos hold   vs hold
2026-06-18   66        60%       +0.064    -0.137    -0.134    -0.003
2026-07-17  105        30%       +0.057    -0.051    +0.126    -0.176
2026-08-21   69       110%       -0.024    -0.054    -0.143    +0.089
2026-09-18   65        60%       +0.043    -0.064    -0.154    +0.091

in-sample gain -> out-of-sample loss:  3 / 4
mean out-of-sample vs hold:            -0.000
```

The fitted parameter is not stable — 60%, 30%, 110%, 60% — and the mean edge over holding
is exactly nothing. **A continuation-value regression over five or eight state variables has
strictly more capacity to produce this pattern with a prettier in-sample number.** That is
the whole argument for stopping here.

## 3. Why the regimes defeat it

There are four monthly expiry cycles in this data, and they disagree about the answer.

| cycle | n | median entry IV | hold-to-expiry | best flat TP |
|---|---|---|---|---|
| 2026-06-18 | 66 | 0.95 | -13.4% | 30% |
| 2026-07-17 | 105 | 0.91 | **+12.6%** | **200%** |
| 2026-08-21 | 69 | 0.60 | -14.3% | 30% |
| 2026-09-18 | 65 | 0.56 | -15.4% | 30% |

July is the only cycle where holding pays and the only one where a high target wins. It is
also 105 of the 171 training trades — so a policy fitted on train learns July, and the test
cycles are not July. The train-fitted target comes out at **290%** on the daily grid, which
is "never take profit" in disguise.

The drift the synthetic-path generator would have learned reverses sign out of sample:

```
                 train            test
BULL       -0.87% /session   +0.24% /session
BEAR       +0.66% /session   -0.10% /session
```

A bootstrap built on training returns teaches "dump calls, hold puts". The test period says
the opposite for both.

## 4. The arms

Test set, n=134, 2% cost, against a perfect-foresight oracle of +77.9% (daily) / +97.1%
(fine). "% orc" is the share of that headroom captured.

```
DAILY grid        train-fitted TP 290%   ·   boundary a=1.3 b=-0.3
  arm                             mean   win   %orc   vs hold          95% CI
  hold to expiry                -14.8%   28%   -19%     +0.0%
  flat TP fitted on train       -15.8%   28%   -20%     -1.0%   [-2.7%, -0.0%]
  Tier 0: flat +30, unfitted     +7.5%   66%    10%    +22.4%   [+2.1%, +39.5%]
  Tier 1: ret >= 1.3 -0.3*tau    -9.1%   34%   -12%     +5.8%   [-7.8%, +15.0%]
  adaptive 70/20 (incumbent)     +1.8%   56%     2%    +16.7%   [-3.9%, +33.7%]
  flat TP fitted on TEST         +7.5%   66%    10%    +22.4%   [+2.1%, +39.5%]   upper bound

FINE grid         train-fitted TP 150%   ·   boundary a=1.5 b=+0.2
  hold to expiry                -14.8%   28%   -15%     +0.0%
  flat TP fitted on train       -12.7%   32%   -13%     +2.1%   [-9.5%, +10.8%]
  Tier 0: flat +30, unfitted     +2.5%   73%     3%    +17.3%   [-2.6%, +34.7%]
  Tier 1: ret >= 1.5 +0.2*tau   -11.8%   31%   -12%     +3.1%   [-7.4%, +12.5%]
  adaptive 70/20 (incumbent)     -0.1%   63%    -0%    +14.7%   [-5.1%, +33.2%]
  flat TP fitted on TEST         +4.6%   58%     5%    +19.4%   [+1.0%, +36.0%]   upper bound
```

Three readings:

- **Fitting costs money.** One free parameter, honestly fitted, underperforms holding.
  Two parameters (Tier 1) land on a near-never-exit boundary and also fail to clear zero.
- **The unfitted prior wins.** +30% was chosen from the economics — you are long a decaying
  asset whose entry IV exceeds trailing realised vol on 62% of trades — not from an argmax.
  It is the only non-oracle arm whose CI excludes zero.
- **Nothing captures much.** Tier 0 takes 10% of the available headroom. The oracle is
  +77.9%; every implementable arm lives between -20% and +10% of it.

**The honest asterisk on Tier 0:** the test-optimal target *is* 30%, so Tier 0 and the
test-fitted upper bound are the same number on the daily grid. The prior happened to be
exactly right here. It was also the best target in 3 of the 4 cycles independently, which is
why it is a defensible prior rather than a coincidence — but it is not an out-of-sample
victory over a tuned alternative, because on this test set it *is* the tuned alternative.

## 5. Stability

Refitting the target with the two dominant tickers removed:

```
drop none       n=171   best TP 290%   train mean +1.6%
drop MU         n=138   best TP 290%   train mean -6.0%
drop NVDA       n=148   best TP 290%   train mean +9.9%
drop MU, NVDA   n=115   best TP 290%   train mean +3.3%
```

The argmax is pinned at the top of the search range either way, but the level swings across
a 16-point range on two tickers. The grid choice moves Tier 0 from +7.5% to +2.5% — the same
order of magnitude as the entire spread between arms.

## 6. What this is NOT

- **Not a demonstration that LSMC is wrong for this problem.** It is the correct formalism.
  The obstruction is the data: four regimes, 171 training trades, 29% of them in two tickers.
- **Not a claim that exits do not matter.** Tier 0 beats holding by 22 points. The claim is
  narrower: the *level* cannot be learned from this sample, so it should be set by argument.
- **Not a validated mark.** The option path is Black-Scholes off a frozen `entry_iv`; only
  the underlying bars are real. That modelling error is unmeasured and is the largest
  remaining uncertainty — see §8.

## 7. What would change the verdict

- Gate 0 passing on a longer sample — eight or more expiry cycles rather than four.
- The train-fitted target landing near 30% instead of 290%, which would mean the training
  slice had stopped being dominated by one anomalous cycle.
- A drift estimate that keeps its sign between adjacent cycles.
- Tier 1 beating Tier 0 by more than the paired CI width, which would mean the time
  dimension carries information the flat target misses.

## 8. Honest accounting

**The gate was run before the model, which is the only reason this is short.** Building the
LSMC first would have produced an in-sample number, and §2 says exactly what that number
would have been worth.

**scikit-learn was not added.** The plan gated the ML arm behind Gate 0 and Gate 0 failed;
spending a dependency on a curated `requirements.txt` for an arm whose only plausible
contribution is a better in-sample fit is not a trade worth making. If you want it run
anyway, say so — the harness takes any policy, so it is a small addition.

**The modelled mark is unvalidated mid-path.** `build_paths` reprices with frozen IV and
zero vega, so post-sweep IV crush is invisible to every policy here. Terminal values are
clean (`settle` against `mark[-1]`: median 0.0000, p5 -0.008, p95 +0.015), but the interior
is not checked against traded option bars. Until it is, all of §4 describes policies acting
on a model rather than on prices.

**Sample.** 305 simulable trades, 171 train / 134 test, 70 tickers, four expiry cycles, one
market regime. The paired CI on the best arm spans +2.1% to +39.5% — an order of magnitude.
That is the resolution this dataset offers; any claimed edge narrower than about 20 points
is unresolvable here.

**Recommended next action: keep the unfitted +30% target, and spend the next effort on
validating the mark against traded option bars rather than on a better learner.** A policy
learned on a mispriced path is precise about the wrong thing.

## Running it

```bash
source .venv/bin/activate
python research/sweep_events/stopping.py     # reconciliation, Gate 0, both grids, all arms
```

`compare(paths, trades, bars, train, test, grid, cost_frac)` returns the arm table; any
`f(state, k) -> bool` factory is a policy, so a fitted rule drops straight in.
