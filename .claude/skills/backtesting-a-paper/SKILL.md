---
name: backtesting-a-paper
description: Method for replicating a quantitative finance paper end to end in pxq_stocks — extracting the strategy spec from the PDF, mapping its data requirements onto what Alpaca can actually serve, building a causal backtest, and reporting the replication honestly against the paper's own tables. Use whenever asked to read, replicate, backtest, or reproduce a trading paper, a strategy from a PDF, or published results.
---

# Backtesting a paper

Replicating a paper is not "code the strategy and see if the number matches." It is
four separable jobs, and conflating them is how replications go wrong.

```
1. SPEC      what exactly does the paper claim, in falsifiable terms
2. DATA      what does it need, what can we serve, what is the gap worth
3. ENGINE    a causal simulation of the spec
4. VERDICT   our numbers vs theirs, with the gap explained rather than buried
```

Worked example in this repo: [`research/momentum_orb/`](../../../research/momentum_orb/)
— the 5-minute ORB paper (SSRN 4729284). Read
[`momentum_orb.md`](../../../research/momentum_orb/momentum_orb.md) for what the
output of step 1–2 should look like.

---

## 1 — Extract the spec

```bash
pdftotext -layout papers/<paper>.pdf /tmp/paper.txt   # -layout preserves tables
```

Read the whole thing. Then write the summary markdown **before** writing any code. It
must pin down:

- **The decision rule**, in the order a trader would execute it. Entry trigger, direction
  rule, stop, target, exit. If you cannot draw it as a timeline, you do not have it yet.
- **The filters**, numbered, with the exact thresholds.
- **Position sizing and leverage** — these change the equity curve by more than most
  signal choices.
- **Costs** — commission model, and whether results are gross or net.
- **The numbers to hit**, transcribed into a table. Total return, Sharpe, MDD, hit
  ratio, alpha/beta, per-period breakdowns.

### Find the mechanism claim

Most papers have one headline number (an equity curve) and one **mechanism claim** (a
cross-sectional relationship that explains *why*). In the ORB paper the headline is
"1,637% total return" and the mechanism is Figure 4: average PnL rises monotonically
with Relative Volume.

**Target the mechanism first.** It needs far less data, it is far more portable across
universes, and if it fails to replicate the headline number is meaningless anyway. A
replication that reproduces the mechanism on a small universe and misses the headline
is a *success with a stated scope limit*. One that reproduces neither is a failure, and
one that "reproduces" the headline while missing the mechanism is almost certainly a bug.

---

## 2 — Map data requirements onto reality

Build the requirements table before fetching anything. Columns: what is needed, at what
granularity, why, and which Alpaca call serves it.

Then write down what you **cannot** get. In this repo the recurring gaps are:

| Gap | Where it bites |
|---|---|
| **Universe enumeration.** `load_universe()` is *today's* tradable list. | You cannot reconstruct the listed universe of 2016. Any "all US stocks" paper is unreplicable at full breadth. Note: bars for delisted tickers *are* served if you already know the ticker — the bias is in discovery, not retrieval. |
| **History start.** Alpaca equities begin **2016-01-04**. | A paper starting in 2004 gets truncated. Say so and run the window you have. |
| **Feed.** `sip` is full tape; `iex` is ~2% of volume. | Volume-based signals are meaningless on `iex`. Check `python -m backtest check` first. |
| **Tick data.** Minute bars are the floor. | Any intrabar fill (stop hit, limit touch) is an assumption, not a measurement. |
| **Corporate actions / fundamentals / news.** Not available. | Catalysts must be proxied. Say what the proxy is. |

### Adjustment is a real decision, not a default

`adjustment="all"` restates history for every later split and dividend. If the paper
filters on **price levels** (`price > $5`) or **dollar ranges** (`ATR > $0.50`), adjusted
data silently changes which rows pass the filter. Papers using vendor intraday feeds
(IQFeed, Polygon) are usually **unadjusted** — use `adjustment="raw"` and keep daily and
intraday consistent with each other. Use adjusted data for the *benchmark*, which is a
return series.

### Size the download before starting it

```
rows ≈ symbols × trading_days × bars_per_day     (390 for 1Min regular session)
```

Benchmark one symbol-year first and extrapolate. Roughly 18k bars/sec against Alpaca and
~24 bytes/row on disk. Anything over ~20 minutes goes in the background
(`run_in_background: true`) while you write the spec doc and the engine.

---

## 3 — Build the engine

### Separate what is size-independent from what is not

The structural trick that makes intraday replications tractable:

> A trade's outcome **in units of R** — entry, stop, exit, R multiple — usually depends
> only on the signal and the price path. It does **not** depend on position size.

So split the simulation:

- **Stage 1**, one symbol at a time, streaming: produce one row per symbol-day with the
  trade outcome in R. Never hold the whole panel of minute bars in memory.
- **Stage 2**, on the resulting ~10⁵-row table: filters, cross-sectional ranking, sizing,
  leverage, costs, compounding.

This makes 70M bars run in bounded memory, and lets you re-run every sizing and filter
variant in seconds without re-touching the bars.

**Simulate every day in stage 1, including days the filters reject.** The mechanism
claim lives in the rejected trades — you cannot plot PnL-vs-signal if you only kept the
trades the strategy takes.

### Causality is the thing that will get you

Every lookback must use data strictly before the decision. In practice:

- `.shift(1)` every daily-frequency feature (ATR, average volume) before it meets a
  trade date. An unshifted ATR leaks the trade day's own range into that day's stop.
- Rolling windows for a "previous N days" average must exclude today:
  `s.shift(1).rolling(N)`.
- Never rank or normalise using full-sample statistics.
- A symbol's first N sessions legitimately have no signal. Do not backfill them.

### Intrabar ambiguity: check the pessimistic reading is even coherent

With a tight stop, entry and stop are often touched **inside the same bar**, and OHLC
cannot order them. The reflex is "assume the worst and move on." That reflex is wrong
often enough to be dangerous.

In the ORB replication, "assume the stop hit first" charged every long with its entry
bar's **low** — but that low frequently printed *before* the bar traded up through the
range high and filled the order. The position did not exist yet. That is not
conservatism, it is counting price action against a position that had not been opened,
and it was worth **1,700 percentage points** of total return.

The rule:

> Split the bar's ambiguity into what OHLC can **prove** and what it merely permits.
> Charge the provable part; bracket the rest.

For a stop-entry with a protective stop, every entry bar sorts into exactly three cases,
and only the third is a guess:

| Case | Test (long) | Verdict |
|---|---|---|
| **provably stopped** | bar closes below the stop, or the fill was at the bar's open | charge it |
| **provably safe** | bar *opens* below the stop — price rose from under it to the trigger, so the low predates the fill | never charge it, under any policy |
| **ambiguous** | opened between stop and trigger, dipped through the stop, closed back above | the bracket |

Getting the second row wrong is the trap, and it is easy to hit twice: a naive
"pessimistic" run that charges *every* entry-bar excursion re-imports the same bug into
the bound that was supposed to check it. Size the ambiguous set explicitly, run it both
ways, and report a range rather than a point estimate.

If one assumption moves the headline by more than the effect you are testing, it is not
a sensitivity — it *is* the result, and the paper's number cannot be confirmed or denied
until it is pinned down.

### The same trap in operational parameters

This is not confined to fill logic. Any parameter chosen for "safety margin" deserves the
same treatment, because a margin has a price and the price is rarely zero.

Going live with the ORB, the entry job was scheduled two minutes after the opening range
closed — padding for data-feed lag, which sounded prudent. Measuring the cost afterwards:

    trigger window   share of trades   mean R
    09:35-09:36           44.1%        +0.291
    09:36-09:37           11.0%        +0.234
    after 09:50           18.6%        +0.066

By the scheduled time, 55% of breakouts had already fired, and the early ones carried
**73.5% of total profit**. The two-minute cushion was quietly forfeiting most of the
edge — and a resting stop order that arrives after price has crossed its trigger is not a
stop order, it is a market order at whatever price has already run to.

The fix was not a better guess at the delay. It was to fire at the earliest legitimate
moment and **block until the data is actually there**, so the wait is as short as reality
allows rather than as long as caution imagines.

> Before accepting any safety margin — in a fill rule, a schedule, a lookback, a
> threshold — compute what it costs. "Conservative" is a claim about a number, and until
> you have the number it is only a feeling.

Other fill assumptions worth stating: a stop order whose bar *opens* through the trigger
fills at the open, not the trigger; exits at the last bar's close, not a closing auction
print.

---

## 4 — Report the verdict

Put the paper's number next to yours, in the same table, always.

| | Paper | This replication |
|---|---|---|
| headline metric | ... | ... |
| mechanism metric | ... | ... |

Then explain each gap with a **direction and a cause**, not an apology. "Our universe is
100 names, so the top-20 filter is a 20th-percentile cut where the paper's was a
0.3rd-percentile cut; the selected trades' median RelVol is X vs the paper's implied Y,
so returns should be lower and are" is a result. "Results differ due to data
limitations" is not.

### Checks that catch real bugs

- **Per-trade win rate** against the paper's per-stock tables. ORB's ~16% matched the
  paper's 17–27% immediately and confirmed the entry/stop logic before any equity curve
  existed.
- **Trade count** in the right order of magnitude.
- **Sign and shape of the mechanism** before its magnitude.
- **Turn the signal off.** The strategy with its key filter removed should degrade to
  roughly nothing. If the null variant also prints a great Sharpe, the bug is in the
  engine, not the signal.
- **Fat right tail intact.** Trend-following intraday strategies make their money in a
  handful of trades. A max R of ~1 means the exit logic is truncating winners.

### Always close with

1. **Assumptions** — numbered, each with which direction it biases results.
2. **Pros and cons** of the strategy as a thing to actually trade.
3. **What live deployment would require** — data, infrastructure, capital, approvals.
   Never place a live order off a backtest without explicit human confirmation; the
   backtest lane and the execution lane do not touch
   ([`data-routing`](../data-routing/SKILL.md),
   [`execution/PROTOCOL.md`](../../../execution/PROTOCOL.md)).

---

## Layout

```
papers/<paper>.pdf                     the source
research/<strategy>/
    <strategy>.md                      spec + data requirements + gaps
    universe.py, select_universe.py    universe, chosen by rule and frozen to disk
    fetch_*.py                         background downloader, resumable
    <strategy>.py                      the engine (stage 1 + stage 2)
    metrics.py                         statistics matching the paper's table
    run_backtest.py                    produces every artefact
    <strategy>.ipynb                   the exploration and the charts
out/<strategy>/*.parquet               results — parquet, per parquet-output
```

Freeze the universe to a CSV rather than recomputing it. A universe that silently
changes between runs makes every comparison meaningless.
