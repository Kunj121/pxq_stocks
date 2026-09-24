# jump_sweeps — strategy specification

**Status: the exit rule is a turnover result, not a return result.** Over 118
bearish sweeps it returns +12.6% a trade against +13.3% for simply holding to
expiry — a paired difference of **-0.7%**, 95% CI [-17.1%, +13.5%]. What it does
buy is time: the same return in 6.1 sessions instead of 12.3, so +2.07% a day
against +1.09%. Every rule below carries an evidence grade; act on the grades,
not on the $53,771.

## Thesis

A bearish ISO sweep marks an underlying that drifts *down* over the following one
to two weeks. Buy an at-the-money put, look at the position once after a session,
and sell into the move rather than carrying the contract to expiry.

The name is aspirational and §6 says so: the jump this strategy sells into is
barely more likely after a signal than at a random moment on the same ticker. The
edge such as it is, is directional drift measured over days — not a jump at the
print.

This is the second spec out of this directory. The first,
[`strategy.md`](strategy.md), is direction-agnostic and holds to expiry; jump_sweeps
takes its trigger, contract and filters and replaces its exit. Where a rule is
inherited rather than re-tested, the grade says so.

## 1. Trigger

`directional.triggers()` — ISO print, premium >= $1M, |delta| 0.30-0.70, funds and
SNDK excluded — **and direction = BEAR**.

| Rule | Grade |
|---|---|
| Direction is `contractType` x `tradeSideCode`, never call/put alone | **definitional** — and load-bearing: 142 of the 216 BEAR triggers were *calls*, sold into the bid. "Bearish" here is not "someone bought a put" |
| **Bearish signals only** | **evidenced (n=140)** — BEAR drifts +2.57% by +10d signed to the trade (+3.05% net of SPY, 62% positive); BULL runs -3.36% by +5d. Pooled across both, maximum favourable excursion is +9.37% against +9.58% for random entry — exactly nothing |
| Premium >= $1M, |delta| 0.30-0.70 | **definitional** — inherited from `directional.py`; no ranking power was tested within the band |
| The vendor's `isGoldenSweep` flag is **not** the screen | **evidenced** — it marks only 41 of the 216 BEAR triggers. The two populations barely overlap; see [`README.md`](README.md) |

The direction filter was chosen after seeing the split, on the same 16 weeks it is
scored on. That is the single largest threat to everything below, and §8 is about it.

## 2. Filters

| Rule | Grade |
|---|---|
| Reject if an earnings report falls between entry and expiry | **evidenced (n=2)** in [`strategy.md`](strategy.md) §2 — inherited here, not re-tested. Removes 44 of the 216 |
| Reject sweeps printing on an FOMC decision day | inference — inherited from `backtest_directional.py`. Removes 11 |
| No technical confirmation, no flow confirmation | **evidenced negative** — three tests in `strategy.md` §6 found none, and the same-day flow counts in `same_day_flow.py` separate nothing (confirmed vs unconfirmed, Mann-Whitney p=0.54) |

The universe, end to end:

```
441   directional triggers            2026-06-01 .. 2026-09-17
216   ... of which BEAR
140   ... with a real entry fill      (76 dropped: earnings 44, fomc 11,
                                       no settlement 9, no entry print 6,
                                       no earnings coverage 5, split 1)
118   ... affordable at $5,000/position
```

62 tickers, median sweep premium $1,440,915, median swept DTE 50.

## 3. Contract

| Rule | Grade |
|---|---|
| **Do not buy the swept contract** — buy our own | **evidenced (n=10)** in `strategy.md` §3; inherited, not re-tested here |
| Strike: at the money, nearest listed | **evidenced (n=10)** there; inherited |
| Expiry: third-Friday monthly, rolling on if under 7 days out | **evidenced (n=10)** there; inherited |
| Right: always a **put** | mechanical — the direction filter admits only BEAR signals, so the expression is always long a put, whatever the swept contract was |

## 4. Sizing

| Rule | Grade |
|---|---|
| $5,000 per position, whole contracts | arithmetic |
| Skip the signal when one contract costs more than the budget | mechanical — drops 22 of the 140. Raising the budget admits the expensive underlyings (MU, LITE) rather than improving anything |

## 5. Exit — the core of the strategy

One check, one session after entry; the target is chosen there and only there.

| Rule | Grade |
|---|---|
| **Check the mark one session after entry** | **evidenced (n=118)** — $53,771 at one session against $38,952 at a half, $34,580 at two, $37,214 at three. One session is a genuine peak, not a tie |
| **If green at the check, target +70%** | **assumed** — the level is not supported. Sweeping it gives 40 -> $65,281, 55 -> $73,839, **70 -> $53,771**, 85 -> $83,902, 100 -> $86,353, 125 -> $92,598, 150 -> $81,641. The surface is non-monotonic, which means noise, and +70 sits in a local trough |
| **If red at the check, target +20% — wait, do not cut** | **evidenced (n=66)** — waiting for a bounce loses $39,548 on the red branch; cutting at the check loses $98,938. The direction of this rule is solid. The *level* is not: +40 returns $71,510 against +20's $53,771 |
| No stop loss | **assumed** — no stop-only variant was run. The only tested alternative carrying a stop (flat +100/-56) also changes the target, and returns $9,661 |
| No time stop | **evidenced (n=118)** — closing at day 10 costs $12,258 ($41,513 against $53,771). The time stop books losses on positions that would have recovered |
| The target arms **at** the check, never before | mechanical — and a trap. The first implementation read the day-1 outcome and let the target fire on day 0, which is lookahead; it was corrected before any number in this document was produced |

Against the alternatives, on the same 118 trades:

```
adaptive 70/20, no time stop     $53,771   win 70%   mean +12.6%   PF 1.37   6.1 d
hold to expiry                   $57,253   win 42%   mean +13.3%   PF 1.23  12.3 d
flat +70                         $45,642   win 58%   mean +12.5%   PF 1.24
flat +20                         $33,155   win 81%   mean  +7.3%   PF 1.37
flat +100 / -56                   $9,661   win 37%   mean  +3.1%   PF 1.05
adaptive, cut red at the check    -$5,620  win 34%   mean  +0.6%   PF 0.96
```

## 6. What this is NOT

**Not a jump strategy at the print.** Against 20 matched random-entry windows per
signal on the same ticker, pooled across both directions, the underlying reaches a
20% favourable move within ten sessions **12.8%** of the time after a signal and
**12.7%** of the time at random. Jumps happen; the sweep does not make them more
likely. The name describes the exit, not an edge.

**Not a day-1 timing edge.** The check separates outcomes dramatically — across all
305 filled triggers, positions green at day 1 hold to +36.6% and reach +20% at some
point 97% of the time, while red ones hold to -31.4% with a -100% median. But acting
on the split adds nothing: the green branch returns +44.3% under the rule against
+43.9% held, and the red branch -12.4% against -10.8%. The check *describes* the
population; differentiated targets do not *improve* it.

**Not an improvement on holding.** $53,771 against $57,253, paired difference -0.7%
with a CI straddling zero either way. The rule is better on 43 of 118 trades. Its
case rests entirely on halving the holding period.

## 7. Where the edge actually is

Excursions over ten sessions, BEAR signals against matched random entry:

| | signal | random |
|---|---|---|
| best move, mean | +10.24% | +8.61% |
| worst move, mean | -8.19% | -9.36% |
| where it ends | +1.82% | -0.21% |
| reached +5% | 65.7% | 55.8% |
| reached +10% | 39.3% | 32.6% |
| reached +20% | 17.1% | 12.5% |

Every cell leans the right way, which is the reason this document exists. But
bootstrapped by ticker the difference in best-move is **+1.63%, 95% CI
[-0.29%, +3.49%]**, P(<=0) = 5.4%, and the endpoint difference is +2.03%, CI
[-0.50%, +5.51%]. Both straddle zero. This is a lean that has not yet become a
finding.

Reproducing this table means running `event_study.py` over the **whole** filled set
and slicing BEAR afterwards, which is what its `__main__` does. Calling
`excursions()` on a pre-filtered subset walks the random-number stream differently
and returns slightly different controls — the signal column is unaffected.

The tighter 21-trade subset in [`exit_lab_configs.md`](exit_lab_configs.md)
(PUT_CONFIG) reaches +20% in 38.1% of windows against 18.7% for its controls — the
strongest number in the whole study, on n=21 chosen with seven filters.

## 8. Regime risk — read this before the P&L

Nearly all of it is one quarter.

| | trades | P&L |
|---|---|---|
| Jun-Jul 2026 | 80 | **+$59,238** |
| Aug-Sep 2026 | 38 | **-$5,467** |

The five best trades (XOM, DASH, MRVL, TSLA, AMD) are $29,215 — **54%** of the
total. By sector, Electronic Technology is $31,898 of it across 50 trades;
Communications adds $14,875 on 7.

A strategy that is long puts on semiconductor and AI names, whose entire return
lands in the quarter those names sold off, has not been shown to have an edge
distinct from that selloff. The hedging work in `hedged_backtest.py` found the same
thing from the other side: a beta hedge on this population made +$96.8k over the
same window purely by being short SPY through July.

Until another quarter reports, **the bearish drift and the July drawdown are the
same fact.** See the economic-factor note in
[`exit_lab_configs.md`](exit_lab_configs.md).

## 9. What would falsify this

- The BEAR excursion edge (§7) still failing to clear zero at n >= 250.
- The adaptive rule failing to beat hold-to-expiry on **return per day** out of
  sample — the only dimension on which it currently wins.
- A bullish mirror turning positive. Direction is the load-bearing filter; if calls
  work too, the story is volatility, not drift.
- Another quarter like Aug-Sep (flat to negative on 38 trades) repeating.
- Slippage of 5% or more erasing the target advantage. In the 21-trade subset, 7 of
  11 take-profit exits cleared their target by less than 10%.
- The green/red check at day 1 ceasing to separate hold-to-expiry outcomes at all —
  that separation (§6) is the one robust observation here even though acting on it
  is neutral.

## 10. Honest accounting

**How the prices are made.** Entry is a real traded option fill from Quant Data.
After that the contract is marked with Black-Scholes at the vol *implied by that
fill*, held constant, along the underlying's 15-minute closes, settling at real
intrinsic. An exit books the mark on the bar where the threshold was breached, not
the threshold — a 15-minute bar can gap straight past a target, and one did: MU
exited at +194% on a +100% target. That overstates a limit fill. There is no
slippage, no commission and no bid-ask in any number here.

**Why the numbers are quotable at all.** `exit_rules.py` reproduces the Sweep Exit
Lab dashboard's own total for the PUT_CONFIG configuration to the cent ($19,278)
before any rule above was evaluated. Where the two disagreed, the dashboard was
right and the Python was fixed.

**Reproducibility gap — now closed, and it was hiding a bug.**
`data/dashboard_trades.parquet` had no producer; it was assembled once in a session
and never committed. [`build_dashboard_trades.py`](build_dashboard_trades.py) now
rebuilds it, and reproduces 38 of the original 55 columns exactly — including every
trade economic (`strike`, `entry_price`, `exit_price`, `ret`) and every `skip` count.

The columns that did *not* reproduce were the swept-print block, and there the
original was wrong. It joined each trigger to its own vendor print on
(ticker, timestamp) alone, but several prints share a timestamp: IBM's trigger on
2026-06-01 09:38:42 was a $3.47M print at K=330, while the log recorded K=340 — a
different $279k print at the same instant. Adding the trigger's own premium to the
join key makes it unique across all 99,737 (ticker, timestamp, premium) triples.
About 20 rows carried wrong `swept_*` values. The blast radius is small: the BEAR
call/put split is unchanged at 142/74, PUT_CONFIG still selects exactly the same 21
trades, and the only figure in this document that moved is the golden-flag count in
§1 (38 -> 41).

**Nothing here is wired to execution.** No file in `execution/` or `main.py`
references sweeps. The only sweep-to-execution link in the repo is
`research/quantdata/sweep_alerts.py` posting to Slack; it places no orders.

**Sample.** 118 trades, 62 tickers, 16 weeks, one market regime. The exit rule's
advantage over holding is a coin flip on this sample (P = 54%). Its holding-period
advantage is real but was not the thing it was built to deliver.

**Recommended next action: run the bullish mirror and accumulate another quarter
before treating the direction filter as a rule.** The mirror is cheap — the same
config with the direction flipped — and it is the one test that separates "bearish
sweeps predict" from "puts worked in July".

## 11. Open questions

- Does the +70/+20 pair survive a proper parameter-stability test? Both levels are
  currently un-supported by their own sweeps (§5).
- Is the day-1 check better used for **sizing** than for targeting — half size on
  entry, add on green — given that it separates outcomes but not returns?
- Does the drift extend past 10 sessions? Days-to-best-move piles up at the window
  edge, which means the measurement is truncating a move still in progress.
- Would the strategy be better expressed as a spread, given that §7's edge is in
  reaching a threshold rather than in where the move ends?
- What does the rule do on the 26 signals from the last two weeks of the sample,
  where the 10-day window ran past the end of the bar data?

## Running it

There is no `jump_sweeps.py`. The document describes a configuration of existing
parts:

```bash
source .venv/bin/activate
python research/sweep_events/event_study.py     # §1, §6, §7 tables
python research/sweep_events/exit_lab.py data/exit_lab/PUT_CONFIG.json
```

The exit rule itself runs through `exit_rules.Rule(decide_bars=26, tp_green=70,
tp_red=20)` over `exit_rules.build_paths()`, or interactively in the Sweep Exit Lab
dashboard (`out/dashboard/index.html`) with **Adaptive target** switched on.
