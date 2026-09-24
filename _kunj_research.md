# Notebooks to test

Four notebooks, in the order they build on each other. All are already executed, so
outputs are visible without running anything — but every one re-runs offline from the
local parquet caches except where noted.

```bash
source .venv/bin/activate
jupyter lab                      # start the kernel from the REPO ROOT, not from the
                                 # notebook's own directory — the cells resolve
                                 # imports via Path.cwd()
```

---

## 1. `research/quantdata/explore_api.ipynb`
**What the Quant Data feed actually serves.** 32 cells.

The map: how the API is reached from Python, all 31 tools by family, the 48-column
order-flow record, how far back history runs, and what the feed does *not* serve.
Ends with a gap analysis of what is still missing to backtest honestly.

Read this first if you want to know what data exists. Hits the live API on every run
(~20 calls, a few seconds).

---

## 2. `research/sweep_events/data_recipes.ipynb`
**Every pull you need, with runnable examples.** 31 cells. ← *start here for testing*

The working reference for all five data families:

| Family | Functions |
|---|---|
| Sweeps | `F.sweeps()`, `F.all_prints()`, `D.load()` |
| Golden sweeps | `F.golden_sweeps()` |
| Ticker history | `F.stock_bars()`, `F.alpaca_bars()`, `F.option_bars()` |
| Earnings | `E.frame()`, `E.next_in_window()`, `E.require()` |
| FOMC | `fomc.frame()`, `fomc.is_decision_day()`, `fomc.next_in_window()` |

Section 2 is the one to read closely: **`isGoldenSweep` is not the ISO screen.** At a
$1M floor on 2026-09-21 the vendor flagged 121 prints against 13 for ISO + SWEEP,
overlapping on 9. Seventy of the golden-only prints are `AUTO` (ordinary electronic
fills) and 42 are multi-leg `COB`. Worth deciding which population the strategy means.

Hits the live API. Section 3 also calls Alpaca.

---

## 3. `research/sweep_events/directional_sweeps.ipynb`
**Signal construction and stress-testing.** 29 cells. Runs offline.

Builds the trigger and tests your confirmation rule *before* any returns are involved.
Knobs are in one cell near the top — `TRIGGER_PREMIUM`, `DELTA_BAND`, `LOOKBACK`,
`EXCLUDE_FUNDS`, `EXCLUDE_TICKERS`, `MAX_COST` — change them and re-run from there.

What it establishes:
- direction is `contractType` × `tradeSideCode`, not call/put alone
- a raw count of prior sweeps is a **liquidity** measure, not a conviction one
- ETFs/indices (38% of prints) and SNDK are excluded, and why a cost cap beats a name

---

## 4. `research/sweep_events/directional_backtest.ipynb`
**The return test.** 21 cells. Runs offline.

Prices 305 positions from real traded option bars — entry at the first print at or
after the sweep, exit at intrinsic on expiry. Your earnings and FOMC skips are applied
before anything is priced. Includes the out-of-sample split.

**The result is negative.** Baseline −5.4% mean, 35% win rate, and no version of the
confirmation filter improves it out of sample. Read section 5 before drawing
conclusions — no stops, no targets, no costs charged, and one market regime.

---

## Caches these read

Already built; nothing needs re-pulling to test.

| Path | What |
|---|---|
| `data/sweep_flow/` | 81 sessions, 25,610 ISO sweep prints (Jun 1 – Sep 21 2026) |
| `data/directional_trades.parquet` | 441 triggers, 305 priced |
| `data/earnings_dates.parquet` | 27,465 reports, 5,788 symbols |
| `data/fomc_days.parquet` | 24 decision days, 2025–2027 |

To extend the flow cache (incremental — skips sessions already held):

```bash
python research/sweep_events/fetch_flow.py 2026-03-01 2026-05-31
python research/sweep_events/backtest_directional.py     # ~15 min, re-prices everything
```

---

## Known limits

- **Option bars start when a contract lists**, not 12 months back. Flow history runs a
  year; per-contract bars only exist from each contract's listing, so an option-priced
  test cannot reach as far back as the flow can.
- **Expiries are discovered, not computed.** June 2026's third Friday is the 19th —
  Juneteenth — so the monthly was the 18th. `backtest_strategy.third_friday()` still
  returns the 19th and, because it prices with Black-Scholes, silently models a
  contract that never existed.
- **`backtest_strategy.py` and `build_log.py` are the older strategy**, unrelated to
  these four notebooks apart from sharing the earnings and FOMC calendars.
