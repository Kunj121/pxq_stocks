# Golden Sweep — strategy specification

**Status: forward-test specification, not a validated strategy.** Ten observations
over six weeks, two of which supply most of the return. Every rule below carries
an evidence grade; act on the grades, not on the backtest number.

## Thesis

A Quant Data ISO sweep identifies a **direction and an underlying**. It does not
identify a contract worth owning: the institution's strike and expiry serve a
book we cannot see — hedges, stock replacements, structures. Take the direction,
build our own expression.

## 1. Trigger

| Rule | Grade |
|---|---|
| Quant Data `Type = ISO`, `Consolidation = SWEEP` | **definitional** — the screen's own filter, carries no ranking information |
| Direction = the sweep's call/put | **assumed** — untested; the whole strategy rests on it |

## 2. Filters

| Rule | Grade |
|---|---|
| **Reject if an earnings report falls between entry and expiry** | **evidenced (n=2)** — removed the two worst positions in the log. OKTA was swept 10 minutes before its print and gapped 25% the wrong way |
| No technical confirmation | **evidenced negative** — see §7 |

## 3. Contract — the core of the strategy

| Rule | Grade |
|---|---|
| **Do not buy the swept contract** | **evidenced (n=10)** — swept contracts returned -24.7% held; our own returned +47.1% |
| Strike: **at the money**, nearest listed | **evidenced (n=10)** — most move per dollar; the sweeps' own deltas ranged 0.22 to 0.97 and neither extreme performed |
| Expiry: **third-Friday monthly**, rolling to the next if under 7 days out | **evidenced (n=10)** — several swept contracts were 1-2 DTE and expired worthless while directionally right. TSLA: -98% swept, +4.6% on the monthly |
| **Exception**: if the sweep's own DTE <= 3 days, consider matching it | **counterexample (n=1)** — MU 9/11 made +103% on a 2-day contract and **-76%** on the monthly, because MU dipped and recovered before the monthly expired. Short theses need short contracts |

## 4. Sizing

| Rule | Grade |
|---|---|
| Whole contracts only, spend <= the per-position cap | mechanical |
| **A fixed dollar cap selects the cheapest contracts, which are the worst ones** | **evidenced (n=10)** — at $2k/position on the *swept* contracts, every one of the five excluded was profitable and four of the five affordable were losers. Cheap = far OTM or nearly expired |
| At $10k, a $2k cap is ~20% of the account at total-loss risk per position | arithmetic — this strategy is high-variance at this account size. Accept it, raise the cap and hold fewer, or use spreads |

Four of ten ATM monthlies still cost more than $2k (MU, SNDK, AMD — high-priced
underlyings). There is no sizing rule that makes a $10k account fit every signal.

## 5. Exit

| Rule | Grade |
|---|---|
| **Hold to expiry. No stop, no profit target.** | **evidenced (n=10)** — every alternative tested was worse |
| The earnings veto (§2) is what substitutes for a stop | inference — it removes the main source of overnight gap risk on a long option, which is why the sample contains no catastrophe for a stop to catch |

```
hold to expiry              +47.1%
half @+100%, rest hold      +32.8%
TP +100%                    +18.6%
exit @75% DTE               +10.8%
TP +40%                      +8.4%   (80% win rate, worst return)
-50% stop, else hold        -38.5%
```

Note the +40% target has the **highest win rate and the worst return** — the
signature of truncating a right tail. Comfort and expectancy point opposite ways
here, and the discipline this strategy demands is sitting through drawdowns, not
cutting them.

## 6. What this is NOT

Not a technical strategy. Three tests, at increasing rigour, found no technical
confirmation:

1. The nine §1/§2/§8 gates scored six sweeps — no correlation with outcome
   (Spearman -0.29, n=6).
2. A 190-event conditional study lost to **random entry** (+65.5% vs +78.6%).
3. A 32-feature screen with out-of-sample selection: three of four chosen
   features **flipped sign** between halves; the composite underperformed random
   by 69 points.

`%B` and distance-above-VWAP — the two rules the framework was built on — were
the weakest features measured, and VWAP distance had the wrong sign.

**This does not prove technicals cannot rank sweeps.** All three tests measure
ordinary bars; a sweep is a different conditional population, and six sweeps
cannot test it. It does mean there is no evidence for the gates today, and
running them has a measured cost — they rejected all six September sweeps.

## 7. What would falsify this

- Hold-to-expiry underperforming a -50% stop over the next 20 sweeps
- The ATM monthly underperforming the swept contract
- A logged sweep gapping catastrophically **without** an earnings report inside
  the window — that would reinstate the stop
- Gate scores correlating positively with outcome at n >= 40

## 8. Open questions

- Does the sweep's own direction actually predict? Never tested — no control.
- Are post-earnings sweeps a distinct setup? NBIS was swept the morning after its
  print, faded the pop, and returned +36% (+159% on the monthly). n=1.
- Does sweep premium size rank outcomes? Untested.
- Spreads to reach the unaffordable ITM/high-priced names at $10k.

## 9. Honest accounting of the backtest number

+47.1% on a $10k account across ten signals, Aug 7 - Sep 17 2026. **INTC (+190%)
and NBIS (+159%) supply nearly all of it.** Two positions remain open and are
marked to the 18 Sep close. ATM contracts are priced using the *swept* contract's
implied vol, which carries skew error — largest on NBIS, whose sweep IV was
106.29% on a 20%-ITM put. Remove either winner and the strategy is flat to
negative.
