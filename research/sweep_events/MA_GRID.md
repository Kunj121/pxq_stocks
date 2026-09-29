# Does a moving-average crossover add anything to the sweep signal?

*95-cell grid over EMA(fast) × SMA(slow) × cross-recency on the 1,107 simulable ISO
trades. Reproduce with `python research/sweep_events/ma_grid.py --boot 2000 --perm 1000`
(~12 min). Writes `out/sweep_events/ma_grid.parquet`.*

**Verdict: no positive synergy, and one significant negative one.** The classic golden
cross is a direction confound and evaporates when you control for it. What survives the
grid correction is the opposite of the thesis: a sweep firing just after a *fresh cross in
its own direction* is the worst subset in the log.

---

## 1. Design

For each trade, the EMA/SMA state is read from daily split-adjusted closes (`sip_all`)
**shifted one session**, so a sweep printing during session *S* sees bars through *S−1*
only. The script asserts this rather than trusting it.

A trade is **aligned** when the crossover agrees with its direction — BULL wants EMA above
SMA, BEAR below. `fresh` additionally requires the cross to have happened within *N*
sessions. The comparator is every other simulable trade.

Grid: fast ∈ {5, 8, 10, 12, 20, 26, 50} × slow ∈ {20, 50, 100, 200} (fast < slow) ×
fresh ∈ {state-only, 5, 10, 20} = 95 cells, re-run on the 296 badged trades.

Three corrections, all of them load-bearing:

- **Direction stratification.** Alignment is defined against the trade's own direction, so
  in a trending tape the aligned arm fills with BULL and the unaligned arm with BEAR. BULL
  returned +29% and BEAR −13% unconditionally, so a raw difference recovers *that* and
  calls it a crossover effect.
- **Cluster bootstrap by ticker.** NVDA, TSLA and MU are a third of the log.
- **Family-wise permutation.** Each trade keeps its ticker and return but is re-dated to a
  random session of the same ticker, decoupling the crossover state from the sweep while
  preserving each name's own state distribution. The statistic is **max |t| across all 95
  cells** — what a grid search actually reports. 1,000 draws.

---

## 2. The golden cross is a direction confound

The one cell that looked good on the raw statistic:

| EMA50 / SMA100 ... 50/200, state only | n | mean |
|---|---|---|
| aligned | 591 | +22.8% |
| other | 507 | −7.3% |
| **raw difference** | | **+30.1%, t = 3.21** |

The aligned arm is **83% BULL**; the unaligned arm is **83% BEAR**. Holding direction
fixed:

| within direction | aligned | other | diff |
|---|---|---|---|
| BULL | +29.4% (n=492) | +26.6% (n=86) | +2.8%, t = 0.12 |
| BEAR | −10.0% (n=99) | −14.2% (n=421) | +4.2%, t = 0.31 |

**Stratified: +3.4%, t = 0.25.** The clustered CI straddles zero either way
([−0.25, +0.84]). A 30-point effect became three points of nothing — the grid found
Simpson's paradox, not an edge.

---

## 3. What actually survives the correction — and it is negative

Best cell by stratified |t|, **EMA26 / SMA100, cross within 5 sessions**:

| | n | mean | median | win |
|---|---|---|---|---|
| aligned with a fresh cross | 70 | **−38.7%** | −99.4% | 25.7% |
| everything else | 1,033 | +12.3% | −74.3% | 37.3% |

- stratified difference **−58.5%, t = −4.76**
- cluster-bootstrapped CI **[−0.95, −0.15]** — excludes zero
- ex-NVDA **−61.9%** (t = −3.53); ex-top-3 **−71.9%** (t = −3.55) — it *strengthens*
- BULL aligned −65.2% (n=22) vs +33.1% other; BEAR aligned −26.5% (n=48) vs −12.3%

**Family-wise p = 0.021.** Observed max |t| 4.76 against a permutation null with median
2.60 and 95th percentile 4.19. So the grid's best cell is not what 95 cells produce by
chance — but note the raw, unstratified version of the same grid gave max |t| 3.98 against
a null median of 3.26, **p = 0.181**. Removing the direction confound is what made the
result readable.

This is not one lucky cell. **71 of 95 cells are negative**, including 23 of the 24
state-only cells (mean −20.8%). The fresh-cross cells around the winner — 26/100 at 10
sessions, 5/200 at 20, 5/100 at 5 — all sit at t between −2.8 and −4.4. It is a coherent
region of the grid, which is what a real effect looks like and what a fluke does not.

---

## 4. Why this is the expected sign

`EDGE_TESTS.md` §4 already rejected the jump hypothesis: sweeps precede **smaller** moves
and **falling** volatility, and look like prints near the *end* of a volatility episode. A
fresh crossover says the move has already happened. Buying a sweep into a just-completed
breakout is buying the most extended version of an entry that was mediocre to begin with —
these two findings are the same fact seen twice.

---

## 5. Limits

- The winning arm is **70 trades, 20 tickers, 39 ticker-weeks**, and NVDA is 26 of the 70.
  The ex-top-3 result is reassuring but it is 32 trades.
- The underlying sweep edge **is not established** (`EDGE_TESTS.md`). This measures a
  contrast *within* a population whose own mean does not survive its placebo, so "avoid
  this subset" does not imply "trade the rest."
- Still a grid. The permutation p-value corrects the family, and p = 0.021 is modest for a
  result selected from 95 cells; it is a reason to pre-register, not to size.
- `fresh` is approximated by sessions since the EMA/SMA sign last flipped, so a state that
  flickers across the line resets the clock.
- The comparator arm is heterogeneous — aligned-but-stale and unaligned pooled together.

## 6. The one hypothesis worth carrying forward

Per `EDGE_TESTS.md` §5.4, stated in advance rather than discovered again: **sweeps within
5 sessions of a fresh EMA26/SMA100 cross in the sweep's own direction underperform the
rest of the log, direction-stratified and clustered by ticker.** One hypothesis, one
number, measured on trades the grid never saw.
