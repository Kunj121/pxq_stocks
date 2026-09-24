# sweep_events — ISO sweep gate scoring

Forward-test log for options order-flow sweeps and the entry gates from
[`papers/intraday_signal.md`](../../papers/intraday_signal.md).

```bash
source .venv/bin/activate
python research/sweep_events/build_log.py     # seed / rebuild sweeps.parquet
python research/sweep_events/run_gates.py     # score every sweep -> out/sweep_events/
python research/sweep_events/plot_events.py   # price panels around each signal
```

| File | What it is |
|---|---|
| `sweeps.parquet` | the log — one row per ISO sweep. **Append here.** |
| `gates.py` | hourly indicators, Black-Scholes delta, and the nine gate tests |
| `run_gates.py` | scores the log, compares gate subsets, writes `gate_scores.parquet` |
| `plot_events.py` | price panels, one per sweep |
| `fomc.py` | FOMC decision days, 2025-2027 — the macro event calendar |
| `earnings.py` | earnings report dates, vendor-sourced — replaces the hand-typed dict |
| `fetch_flow.py` | caches every ISO sweep print over a date range to `data/sweep_flow/` |
| `directional.py` | direction and confirmation features for the directional-sweep strategy |
| `data_recipes.ipynb` | **every pull, with runnable examples — start here** |
| `directional_sweeps.ipynb` | builds and stress-tests the signal |
| `backtest_directional.py` | prices it against real traded option bars (~15 min pull) |
| `directional_backtest.ipynb` | the return test, with the out-of-sample split |
| `strategy.md` | the first spec — direction-agnostic, holds to expiry |
| `jump_sweeps.md` | the second spec — bearish sweeps, adaptive exit, sells into the move |
| `build_dashboard_trades.py` | builds `dashboard_trades.parquet` — the 56-column log every study reads |
| `stopping.py` / `stopping.md` | grid- and cost-aware exit harness; why the exit cannot be learned here |

## Causality

Every indicator uses only bars that had **closed** before the sweep printed — an
hourly bar stamped 10:00 closes at 11:00, so a 10:32 sweep sees the 09:00 bar and
no later. `spot` and `price` come from the vendor's own print, never re-derived
from bars.

Two things the framework needs that the Robinhood MCP cannot serve are computed
here instead: **`+DI`/`-DI`** (the MCP's `adx` returns only the scalar) and
**`RVOL_TOD`** (cumulative volume vs the median at the same clock hour over the
prior 20 sessions). The Bollinger bands are centred on **SMA20** as §2 specifies,
not the EMA20 centre the MCP returns.

## Where the data comes from

[`data_recipes.ipynb`](data_recipes.ipynb) is the working reference for all five
families — sweeps, golden sweeps, ticker history, earnings, FOMC — every cell runnable.
The pull functions live in [`research/quantdata/feeds.py`](../quantdata/feeds.py):

```python
import feeds as F
F.sweeps("2026-09-21", premium_min=1e6, tickers=["NVDA"], sides=["ASK", "ABOVE_ASK"])
F.golden_sweeps("2026-09-21")                      # vendor badge AND ISO
F.stock_bars("NVDA", "2026-09-21")                 # unadjusted — matches strikes
F.option_bars("TSLA", "2026-10-16", 360, "CALL", "2026-09-21")
F.alpaca_bars(["NVDA"], "2026-06-01", "2026-09-21")  # split-adjusted, bulk
```

**`isGoldenSweep` is not the ISO screen.** On 2026-09-21 at a $1M floor the vendor
flagged 121 prints against 13 for ISO + SWEEP, overlapping on only 9. Of the 112
golden-only prints, 70 are `AUTO` — ordinary electronic fills — and 42 are multi-leg
`COB`. The badge is a loose unusual-activity marker; `ISO` is a statement about how the
order routed, which is what the strategy's premise actually needs.

## Directional sweeps

A second strategy under test, separate from the log above: ISO + SWEEP, premium
>= $1M, delta 0.30-0.70, **single names only**, confirmed against the ticker's other
flow. [`directional_sweeps.ipynb`](directional_sweeps.ipynb) has the evidence; two
findings shape it.

**Direction is `contractType` x `tradeSideCode`.** Buying calls or selling puts is
bullish, selling calls or buying puts is bearish. Call/put alone inverts the majority
of prints. `MID_MARKET` has no aggressor and so no direction — dropped, not guessed.

**Confirmation is one-sidedness, not volume.** A raw count of prior sweeps correlates
+0.41 with merely being an ETF, and the confound survives excluding them (MU, TSLA and
NVDA average ~10 prior sweeps against a median of 3). The count is also near-blind to
direction: prior flow agrees more than it disagrees only 53% of the time. The ratio
`(bull premium - bear premium) / total premium` correlates -0.03 with the count,
and strong alignment beats strong opposition about 2:1.

ETFs and indices are excluded by default — 38% of all prints. A QQQ or BITX sweep is
hedging, not a view on a company.

SNDK is excluded by name: one contract costs a median $15,544, 1.6x the next worst.
That screen is a stopgap — it moves mean alignment not at all (+0.18 either way,
30 triggers of 441), while a `max_contract_cost` cap of $5k lifts it to +0.25 by also
removing MU (65 triggers at $9.0k), ARM and COHR. Prefer the cost cap once a position
size is fixed; the lift still needs an out-of-sample test before it is believed.

Everything is point-in-time: features are built only from prints that had already
happened when the trigger fired.

### Pricing

`backtest_directional.py` takes the ATM contract on the next listed monthly, enters at
the **first traded option print at or after the sweep**, and exits at **intrinsic value
on expiry day**. Exit is intrinsic rather than a final option bar because a bar exists
only where the contract traded, and a worthless option stops trading before expiry —
marking it off its last print would flatter every loser. Earnings-in-window and
FOMC-decision-day triggers are skipped before anything is priced.

**Expiries are discovered, not computed.** June 2026's third Friday is the 19th —
Juneteenth — so the listed monthly is Thursday the 18th, which carried 1,311 prints
against fewer than 100 on neighbouring dates. `backtest_strategy.third_friday()`
returns the 19th regardless; because that backtest prices with Black-Scholes it never
noticed it was modelling a contract that never existed. `D.monthly_expiries()` takes
the most-traded listed expiry within three days of each third Friday instead.

## Earnings dates

`earnings.py` reads `data/earnings_dates.parquet` — 223 reports across all 45 log
symbols, 2025-10-09 to 2026-12-16, sourced from Robinhood's `get_earnings_calendar`.
It supplies `date`, `timing` (already the `am`/`pm` the log encodes) and `verified`,
which separates a confirmed date from a projection.

`build_log.py` calls `earnings.require()` before scoring. That is the whole point:
the hand-typed `EARNINGS` dict's worst property was that **an unlisted symbol passed
the veto in silence**, and absence is now an exception instead.

Against the dict it replaces, **170 of 173 entries matched exactly on date and
timing.** The three that differ — CAR 2026-10-27, LITE 2026-11-05, POET 2026-11-12 —
are all `verified=False` future dates where neither source is authoritative yet. No
confirmed date disagreed, and the veto changed on **zero of 118 sweeps**, so the
backtest is unmoved by the swap.

### Refreshing it

This is the one calendar that cannot regenerate itself. The MCP tool is callable from
Claude, not from Python, and answers market-wide (~6,000 records per 31-day window),
so a refresh is: ask Claude to re-pull `get_earnings_calendar` in 31-day windows
across the span, then

```bash
python research/sweep_events/earnings.py <session tool-results dir>
```

which folds the dumps into the parquet. Quant Data serves no earnings dates, and the
Massive endpoints that do — `/benzinga/v1/earnings`, `/tmx/v1/corporate-events` —
are not entitled on the current plan.

## FOMC decision days

`fomc.py` carries every scheduled FOMC decision from 2025 through 2027, sourced from
federalreserve.gov. Each meeting runs two days and **only the second is an event** —
the statement lands at 14:00 ET — so day one is an ordinary session and is not
recorded. `python research/sweep_events/fomc.py` writes `data/fomc_days.parquet`;
`build_log.py` imports it and adds four columns to every sweep:

| Column | What it is |
|---|---|
| `fomc_in_window` | first decision date strictly after the print and at or before expiry |
| `fomc_count` | how many decisions the holding period spans — a monthly can straddle two |
| `swept_on_fomc` | did the print itself land on a decision day |
| `swept_pre_statement` | on a decision day, was it before 14:00 — `None` off-day |

These are **evidence, not a veto.** `tradeable` is unchanged. The earnings veto earns
its place on n=2 of its own; nothing comparable exists for FOMC yet, and the log now
carries what is needed to find out.

Two things the columns already say. **47% of tradeable sweeps (25 of 53) span a
decision** — close to unavoidable, because §3's third-Friday monthly rule implies a
30-45 day hold and the FOMC meets every six weeks. And the exposure **clusters**:
nine of those 25 straddle 2026-06-17 alone. Unlike earnings, an FOMC day is common
across every symbol at once, so those nine are one macro bet wearing nine position
tickets — a concentration the log's position count hides.

## What this cannot tell you

Neither Alpaca nor Robinhood serves historical sweep prints, so the log only
grows forward. **Log every sweep, including the ones you don't trade** — without
the rejects there is no way to tell whether a gate added edge or only cut
winners. `taken` and `exit_note` exist for that.

Sample size as of the seed: six sweeps, three resolved. Nothing here is
significant yet; it is a scaffold for accumulating evidence, not evidence.

**The forward-only constraint above is now obsolete for sweeps.** Quant Data serves at
least twelve months of historical ISO sweeps and per-contract option bars — see
[`research/quantdata/`](../quantdata/). It does not serve earnings or FOMC dates, so
those calendars stay local.
