---
name: sweep-backtest
description: Runs the golden-sweep options backtest and event charts in pxq_stocks — logging Quant Data ISO sweeps, reconciling vendor prices against split-adjusted bars, scoring the entry gates, backtesting the ATM-monthly strategy, and plotting price panels per signal. Use whenever asked to add sweeps, backtest the sweep strategy, score signals against the gates, or plot sweep price panels.
---

# Sweep backtest

Everything lives in [`research/sweep_events/`](../../../research/sweep_events/).
The spec being tested is
[`strategy.md`](../../../research/sweep_events/strategy.md); the evidence trail is
[`notes/entry_gate_checks.md`](../../../notes/entry_gate_checks.md).

## The pipeline

```bash
source .venv/bin/activate

# 1. bars first — Alpaca, historical lane (see data-routing)
python -m backtest fetch --symbols AAPL,MSFT --start 2025-09-19 \
    --timeframe 1Hour --session regular --out data/sweep_<tag>_1Hour.parquet
python -m backtest fetch --symbols AAPL,MSFT --start 2024-09-01 \
    --timeframe 1Day --out data/sweep_<tag>_1Day.parquet

# 2. rebuild the log after editing build_log.py
python research/sweep_events/build_log.py

# 3. score the entry gates
python research/sweep_events/run_gates.py            # --include-earnings to override

# 4. backtest the strategy
python research/sweep_events/backtest_strategy.py    # --capital --per-pos --target

# 5. charts, one panel per sweep
python research/sweep_events/plot_events.py --since 2026-06-01 --until 2026-06-30 \
    --out sweep_price_panels_june.png --title "June ISO sweeps"
```

Outputs land in `out/sweep_events/`. Everything tabular is parquet.

## Adding sweeps

Transcribe from the Quant Data **golden_sweep** tab into the `SWEEPS` list in
`build_log.py`. Earnings no longer need hand-entry: `earnings.py` reads a
vendor-sourced parquet and `build_log.py` calls `earnings.require()` first, so a
symbol with no coverage now **raises** instead of silently passing the veto.

A new symbol outside the parquet's 45 will stop the build. Refresh it by asking for
`get_earnings_calendar` pulls in 31-day windows, then
`python research/sweep_events/earnings.py <tool-results dir>`.

FOMC needs no per-symbol upkeep: `fomc.py` holds the decision calendar through 2027
and `build_log.py` applies it to every sweep. Its four columns are evidence only and
do not touch `tradeable`. Extend `DECISION_DAYS` when the Fed publishes 2028.

Record the vendor's values verbatim: `spot`, `price`, `vendor_iv`, `vendor_delta`,
`contract_volume`, `contract_oi`. Never re-derive them from bars. Vendor IV is
struck off the mid while these fill at or above the offer, so a price-implied vol
runs 1-3 points high — `gates.py` prefers the vendor greeks and keeps the back-out
alongside for comparison.

## Three checks that are not optional

**1. Vendor spot must reconcile to the bars.** Alpaca returns split-ADJUSTED
history; the vendor records the price as it traded. A stock that splits after its
sweep makes the strike and spot incomparable. CRWD split 4:1 after its 26 Jun 2026
sweep and produced a fictional **+5,368%** before this check existed.
`gates.split_factor()` corrects recognisable splits and flags anything else for
dropping. Run it on every new sweep; do not wait for a suspicious return to expose it.

**2. The chosen expiry must never precede the sweep's own.** `next_monthly()` takes
`not_before=<sweep expiry>`. Without it the rule can SHORTEN the trade — IREN was
swept for 21 Aug, was handed 17 Jul, and that accident alone turned a -100% into a
+139.7% and flipped the whole backtest positive.

**3. Indicators must use only bars that had CLOSED before the print.** An hourly bar
stamped 10:00 closes at 11:00, so a 10:32 sweep sees the 09:00 bar and no later.
`gates.evaluate()` enforces this; keep it that way.

## Known traps

| Trap | What happens |
|---|---|
| 0-DTE sweeps | whole-day `T` is zero and the greeks divide by zero; `evaluate()` measures to the 16:00 close and floors at a minute |
| Degenerate quotes | ROKU printed delta 1.000 at IV 0.05%; deep-ITM IV (MU at 368%) is meaningless — never feed these to an IV filter |
| `searchsorted` in plots | snapping forward puts the marker on the next session's open; across an overnight gap it lands nowhere near the print. Use `side="right" - 1` |
| MCP Bollinger bands | the served middle band is an EMA20, not the SMA20 §2 specifies. Rebuild as `sma(20) ± (upper − middle)` |
| Thin history | SPCX listed 12 Jun 2026, so SMA200 is NaN and the daily stack silently fails |

## Reporting

Sample size is the headline, not the return. At the time of writing: 38 sweeps,
7 earnings-vetoed, and only a third affordable at $2k per position — so the traded
subset is whatever fit the budget, not the strategy.

Always state: how many signals were **skipped as unaffordable**, how many positions
remain **open** (marked, not realised), and which one or two trades carry the result.
Two bugs once moved this backtest 118 points; a number without its reconciliation
checks means nothing.
