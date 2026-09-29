# Does a moving-average crossover add anything to the sweep signal?

*Grid over EMA(fast) × SMA(slow) × cross-recency, on ISO sweeps that **lifted the offer**.
Reproduce with `python research/sweep_events/ma_grid.py --boot 2000 --perm 2000` (~20 min).
Writes `out/sweep_events/ma_grid.parquet`.*

**Verdict: no positive synergy at any setting, and a large negative one.** A golden sweep
that fires just after a *fresh crossover in its own direction* is the worst subset in the
log. On the 435 buy-side ISO trades that clears the grid correction at **p = 0.004**; on
the 111 golden-badged ones it points the same way, twice as hard, but cannot clear it
(p = 0.091, n = 13 in the best cell).

---

## 1. Direction is the aggressor side, not the right

A sweep is directional only if you know who was the aggressor. Lifting the offer is
someone paying up to get long the contract, so a **call at or above the ask is bullish and
a put at or above the ask is bearish**. A print at or below the bid is the opposite trade —
a call sold there is bearish — which is why the right alone mislabels most of the log.

| `side` | n |
|---|---|
| ASK | 374 |
| ABOVE_ASK | 61 |
| BID | 478 |
| BELOW_BID | 194 |

Keeping only ASK / ABOVE_ASK leaves **435 trades**, and **111** once the golden badge is
applied, across 53 tickers.

Two invariants the script asserts rather than assumes:

- Inside the buy side, the vendor's own sentiment agrees with the right on **every** row
  (301 CALL→BULL, 134 PUT→BEAR, zero off-diagonal). The disagreements that motivated
  `quantdata-direction-is-side` live entirely on the bid side.
- The traded right equals the swept right there, so the stored `ret` is the trade this
  rule implies and needs no repricing.

The filter alone moves the mean: **all ISO +9.0% → buy-side +20.2% → golden + buy-side
+30.5%.** That is not tested against a placebo here and should not be read as an edge.

## 2. Method

Indicator state is read from daily split-adjusted closes (`sip_all`) **shifted one
session**, so a sweep printing during session *S* sees bars through *S−1*. Asserted, not
trusted. A trade is **aligned** when the crossover agrees with its direction; `fresh`
requires the cross within *N* sessions. Grid: fast ∈ {5,8,10,12,20,26,50} × slow ∈
{20,50,100,200} (fast < slow) × fresh ∈ {state-only,5,10,20}.

Three corrections, all load-bearing:

- **Direction stratification** — alignment is defined against the trade's own direction,
  so the aligned arm fills with BULL and the unaligned with BEAR. Unstratified differences
  recover that split and call it a crossover effect.
- **Cluster bootstrap by ticker** — NVDA, TSLA and MU dominate the log.
- **Family-wise permutation** — each trade keeps its ticker and return but is re-dated to
  a random session of the same ticker. Statistic is **max |t| across the whole grid**,
  2,000 draws.

---

## 3. The classic golden cross is nothing, everywhere

EMA50 / SMA100→200, state only, the cell most people would reach for:

| population | raw diff | t | **stratified** | t | clustered CI |
|---|---|---|---|---|---|
| golden + buy-side | +31.2% | 0.81 | **+8.1%** | 0.14 | [−0.85, +1.29] |
| buy-side, all ISO | +33.2% | 1.89 | **+9.8%** | 0.41 | [−0.38, +1.04] |
| all ISO (reference) | +30.1% | 3.21 | **+3.4%** | 0.25 | [−0.25, +0.81] |

The raw column looks best where the labelling is worst. Holding direction fixed it is
three to ten points of noise in all three populations.

**Every best-positive cell dies on the clustered CI or the top ticker.** The strongest is
EMA5/SMA20 fresh ≤ 5 on the buy side, +107.0% (t = 3.45) — but ex-top-ticker +30.7%
(t = 1.41) and bootstrap [−0.06, +2.23]. There is no positive setting worth carrying.

---

## 4. What survives: the fresh-cross penalty

**Buy-side, all ISO — EMA26 / SMA100, cross within 10 sessions:**

| | n | mean | median | win |
|---|---|---|---|---|
| aligned with a fresh cross | 30 | **−76.0%** | −100.0% | 13% |
| everything else | 402 | +27.7% | −88.1% | 39% |

- stratified **−105.6%, t = −7.65**; clustered CI **[−1.46, −0.57]**
- **both directions**: BULL −84.2% (n=17) vs +42.9%; BEAR −65.2% (n=13) vs −8.3%
- ex-top-1 −113.5% (t = −6.76); ex-top-3 −108.0% (t = −4.80, n=16)
- 14 tickers, 23 ticker-weeks; NVDA is 6 of 30
- **family-wise p = 0.0040** (observed max |t| 7.65; null median 2.61, 95th 4.92)

**Golden + buy-side — EMA8 / SMA50, cross within 5 sessions:**

| | n | mean | win |
|---|---|---|---|
| aligned with a fresh cross | 13 | **−74.5%** | 15% |
| everything else | 97 | +45.9% | 43% |

- stratified −153.0%, t = −4.43; clustered CI [−1.77, −0.44]; ex-top-3 −132.1%
- **family-wise p = 0.091** — does not clear the grid correction
- and it is effectively one-sided: 12 of the 13 aligned trades are BULL, 5 of 13 are ASML

So the badge subset agrees in sign and magnitude but is too small to establish anything on
its own. The claim rests on the 435-trade buy-side population.

It is not one cell. **54 of 83 buy-side cells are negative**, and the whole SMA50/SMA100
fresh-cross region sits at t between −2.9 and −7.7. A coherent region is what a real
effect looks like; a fluke is a spike.

---

## 5. Why this is the expected sign

`EDGE_TESTS.md` §4 already rejected the jump hypothesis: sweeps precede **smaller** moves
and **falling** volatility, and look like prints near the *end* of a volatility episode. A
fresh crossover says the move has already happened. Lifting the offer into a
just-completed breakout is the most extended version of an entry that was mediocre to
begin with. The two findings are one fact seen twice.

---

## 6. Limits

- The winning arm is **30 trades, 14 tickers, 23 ticker-weeks**. Ex-top-3 holds at n=16.
- The underlying sweep edge **is not established** (`EDGE_TESTS.md`). This is a contrast
  *within* a population whose own mean does not survive its placebo, so "avoid this
  subset" does not imply "trade the rest."
- Still a grid. p = 0.004 is a real correction over 83 cells, but it is one dataset and
  one specification of `fresh`.
- `fresh` counts sessions since the EMA/SMA sign last flipped, so a state that flickers
  across the line resets the clock.
- The comparator arm pools aligned-but-stale with unaligned.

## 7. The hypothesis to carry forward

Per `EDGE_TESTS.md` §5.4, stated in advance: **ISO sweeps filled at or above the ask,
within 10 sessions of a fresh EMA26/SMA100 cross in the sweep's own direction,
underperform the rest of the buy-side log — direction-stratified, clustered by ticker.**
One hypothesis, one number, on trades the grid never saw.
