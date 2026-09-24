# Entry Gate Checks — Sweep Confirmation

**Date of the worked checks:** 2026-09-17 (all quotes intraday, times in ET)

The flow signal answers *"should I be bullish?"*. This document is the confirmation layer:
the tests a name and a contract must pass before the sweep becomes a trade, and what
happened when they were run against three live candidates.

**Primary source:** [`papers/intraday_signal.md`](../papers/intraday_signal.md) — the
1-hour regime + Bollinger pullback framework. Section references (§1, §2, §8) are to that
document.

**Flow source:** Quant Data (external). Trade-type reference in
[`papers/quantdata_trade_types.md`](../papers/quantdata_trade_types.md).

---

## 1. Provenance of every threshold

Recorded because an earlier version of this gate set presented invented numbers in the same
table as sourced ones. Do not let that recur.

| Condition | Threshold | Source |
|---|---|---|
| `C_d > SMA50_d > SMA200_d` | — | §1 |
| `P_t > VWAP_t` | — | §1 |
| `EMA20_1h > EMA50_1h` | — | §1 |
| `RSI14_1h` | 50–70 | §1 — also matches saved Robinhood scan `e55c277b` |
| `ADX14_1h` | ≥ 20 | §1 |
| `+DI14_1h > -DI14_1h` | — | §1 |
| `RVOL_TOD` | ≥ 1.5 | §1 (time-of-day adjusted) |
| Bollinger `%B_1h` | ≤ 0.40 | §3 default pullback rule |
| Contract delta | 0.40–0.70 | §8 |
| Spread / mid | ≤ 5–10% | §8 |
| **Sweep volume > contract OI** | — | **not in §; added here** |
| **DTE** | **≥ 21** | **not in §; invented — open to challenge** |
| **52-week range position** | **40th–85th pct** | **not in §; invented — weakly held** |
| **EPS beats, last 4 quarters** | **≥ 3 of 4** | **not in §; invented** |
| Earnings outside the contract's life | — | conventional, and a hard veto here |

MACD is **not** part of this framework. §1 uses the hourly EMA cross plus DI for that role.

---

## 2. The gate stack

1. **Flow (Gate 1)** — sweep is opening (volume > OI), aggressive, single-leg.
   Reject any Quant Data Type containing `COB`, `SPRD` or `TIED` — those are multi-leg and
   the direction of a single leg is unreadable. A true sweep is `ISO`.
2. **§1 Regime** — seven conditions above. Direction must match the sweep.
3. **§2/§3 Location** — `%B_1h <= 0.40`. Trend says *whether*; %B says *when*.
4. **§8 Contract quality** — delta band, spread, liquidity, IV sanity, limit at mid.
5. **Fundamental** — earnings outside expiry, EPS history, range position, valuation.
6. **Veto** — IV already elevated; prefer a spread (needs L3) or skip.

---

## 3. Worked checks, 2026-09-17

### NVDA — 145C, 10/02 expiry (deep ITM)

Spot 219.23 · 1:59 PM

| | 10/02 145C | 10/16 145C |
|---|---:|---:|
| Mark | 74.675 → $7,467 | 75.025 → $7,502 |
| Delta | **0.990** | 0.984 |
| Gamma / Vega | 0.0006 / 0.012 | 0.0009 / 0.025 |
| Time premium | **$0.45** | $0.80 |
| Theta as % of premium | 0.07%/day | 0.06%/day |
| **Volume / OI** | **1,200 / 37 = 32.4x** | 0 / 303 |
| Spread | 0.47% | 0.33% |

**Regime:** ✅ daily stack (219.23 > 213.58 > 197.95), ✅ VWAP (218.77), ✅ RSI 60.3,
✅ ADX 22.6 · ❌ `EMA20_1h` 215.87 < `EMA50_1h` 217.71 · **%B 0.84** ❌

**Fundamental:** next earnings 2026-11-17 (verified) — outside 10/16 ✅ · 8 of 8 recent
quarters beat ✅ · 52-week range 164.27–236.54, at **76th pct** ✅ · PE 27.04, PB 22.56 ✅

**Verdict: signal genuine, contract wrong, timing wrong.** Delta 0.99 with 45c of time
premium is a synthetic long — stock replacement at ~2.9x leverage, not an options bet. The
OI check is the cleanest of the three at 32x. But §8 caps delta at 0.70, and the regime
fails on the hourly EMA cross while %B sits at 0.84.

### OKLO — 45C, 10/16 expiry

Spot 39.91 (+12.0% on the day) · 3:46 PM

| | |
|---|---:|
| Mark | 1.685 → **$170** |
| Delta | 0.332 |
| IV | 76.9% |
| Theta as % of premium | **3.3%/day** |
| **Volume / OI** | **3,706 / 56,408 = 0.07x** |
| Breakeven | 46.69 → **+17.0% required** |
| Spread | 1.78% |

**Regime:** ❌ `C_d` 39.91 < `SMA50` 42.28 · ❌ `SMA50` 42.28 < `SMA200` 63.00 (death cross)
· ❌ `EMA20_1h` 37.81 < `EMA50_1h` 38.45 · ✅ VWAP 39.22 · ✅ RSI 65.98 · ✅ ADX 35.28
· ✅ RVOL 1.51x · **%B 0.80** ❌

**Fundamental:** PE **-37.90** (pre-revenue) · last 4 quarters miss/miss/in-line/miss —
**0 of 4** ❌ · losses widening -0.18 → -0.28 · 52-week range 34.38–193.84, at **3.5th pct**
❌, **-79.4%** from the high, low set 3 days prior (9/14) · earnings 2026-11-10 tentative,
outside expiry ✅

**Verdict: reject.** A bounce off a three-day-old 52-week low in a downtrended,
pre-revenue name, where the call needs +17% in 29 days at 3.3%/day decay. Affordable
because the market gives it a 20.6% chance of finishing ITM.

### AMD — 600C, 10/16 expiry

Spot 545.71 (+6.5% on the day) · 3:54 PM

| | 600C (asked) | 560C (§8-compliant) |
|---|---:|---:|
| Mark | 13.275 → $1,328 | 25.85 → $2,585 |
| **Delta** | **0.287** ❌ | **0.464** ✅ |
| Breakeven | **613.28** (+12.4%) | 585.85 (+7.4%) |
| Theta as % of premium | **3.6%/day** | 2.2%/day |
| **Volume / OI** | **5,677 / 4,386 = 1.29x** ✅ | 1,128 / 1,856 = 0.61x |
| IV | 51.8% | 51.5% |
| Spread | 1.88% | 1.16% |

**Regime:** ✅ daily stack (545.71 > 495.03 > 351.29) · ✅ VWAP 542.03 · ✅ `EMA20_1h`
524.00 > `EMA50_1h` 509.54, gap +14.46 widening · ✅ RSI 67.04 · ✅ ADX 26.28 rising
· ❌ RVOL 1.33x (vs the invented 1.5) · **%B 0.84 at the close** ❌ (see the band defect
in §4 — the as-returned value was 0.76)

**Fundamental:** 4 of 4 recent quarters beat ✅ · earnings 2026-11-03 tentative, outside
expiry ✅ · 52-week range 149.85–584.73, at **91st pct** ❌ (weakly held — 91st pct in a
name up 3.6x off its low reads as trend) · PE 131.53, PB 12.44

**Verdict: best candidate, but early and wrong strike.** Every trend condition passes.
Two fixable problems:

- **Early.** `%B <= 0.40` implies **~$513** on §2-compliant bands; hourly SMA20 closed at
  **$520.35**. Wait for that zone and take §5 confirmation there. Sweep on the 600C was
  later confirmed from the flow program; volume/OI of 1.29x corroborates it independently.
- **Wrong strike.** The 600C breakeven of 613.28 is **above the 52-week high of 584.73** —
  it needs a new high plus 4.9% in 29 days. That is what delta 0.287 means in cash terms.
  The 560C breaks even essentially at the old high.

---

## 4. Findings

**The $3,000 premium rule misfires on ITM contracts.** The standing rule is: third-Friday
monthly, unless premium exceeds $3,000, then take the next weekly. On NVDA's 145C the
monthly is $7,502, so the exception fires and sends you to a weekly at $7,467 — it saves
$35 and buys nothing. Cause: the test measures *total* premium, ~99% of which is intrinsic
on a deep ITM contract, and intrinsic is not a cost. **Fix: apply the threshold to
extrinsic value** — `premium - max(0, spot - strike)`. Identical behaviour on ATM
contracts, stops misfiring on ITM ones. Also unresolved: "next week" could mean the next
weekly from today or the week after the monthly.

**A high ADX is not a bullish reading.** OKLO printed the strongest ADX of the three
(35.28) while sitting below both daily averages with a death cross, three days off a
52-week low. ADX measures trend *strength*, direction-blind. This is exactly the failure
`+DI > -DI` exists to catch — and it is the one §1 condition that cannot currently be
computed. Strongest argument for building it.

**Deep ITM implied vol is unreadable.** NVDA's 145C shows 91.1% on 10/02 and 72.5% on
10/16. Both are artifacts of solving for IV when extrinsic value is 45c against a $74.68
price. Never feed these into an IV filter; the elevated-IV veto simply does not apply to
such contracts.

**The volume/OI check discriminates well.** Across the three: 32.4x (unambiguously
opening), 1.29x (opening), 0.07x (not opening — 3,706 contracts against a 56,408 base).
It cleanly separated real new positioning from noise, and it is computable from
`get_option_quotes` alone.

**The MCP's Bollinger middle band is an EMA20, not the SMA20 that §2 specifies.** Verified
on all three names — e.g. AMD's 18:00 hourly bar returned a middle of 523.9991976 against
an `ema(20)` of 523.9992004 and an `sma(20)` of 517.8213700. Standard deviation is
unaffected, so a compliant band is rebuildable as `sma(20) +/- (upper - middle)`. For AMD at
the close this moved %B from 0.76 to **0.84** and the pullback target from ~$519 to
**~$513**. No verdict in this document changes — every reading was far above 0.40 — but
**the %B figures recorded above for NVDA and OKLO were computed on EMA-centred bands and
need recomputation** before either is re-used. Always rebuild the band; never take `%B`
from the returned middle.

**§8's delta band is the translator.** All three sweeps sat outside 0.40–0.70 (0.99, 0.33,
0.29). That is not a contradiction: the sweep supplies *direction*, §8 supplies *the
contract that expresses it*. AMD is the clean illustration — take the signal, buy the 560C
at 0.46 rather than the 600C at 0.29.

---

## 5. Implementation gaps

| Missing | Why | Path |
|---|---|---|
| `+DI` / `-DI` | `get_equity_technical_indicators` type `adx` returns only the scalar | Compute directional movement from hourly bars |
| `RVOL_TOD` | No MCP primitive. Note this is a **third** definition — distinct from full-day-vs-30-day-average and from `orb.py`'s opening-range-vs-prior-14 | Bucket intraday volume by clock time over a trailing window |
| IV rank / percentile | §8 wants "not extreme relative to its own recent history"; only a spot IV is served | Trailing ATM IV series |
| SMA20-centred Bollinger bands | The served middle band is an EMA20 (see §4) | `sma(20)` + the served band half-width |

All three are computable from `get_equity_historicals`. Until they exist, three of the
gates are being eyeballed.

---

## 6. What these checks cannot tell you

Neither Alpaca nor Robinhood serves historical options sweep prints, so **this gate set
cannot be backtested**. There is no causal way to ask whether the gates add edge or merely
cut winners.

Forward-testing is the only path, and it only works if the **rejects are logged**: every
sweep that fires, with all gate values at that moment and the outcome at 30 days —
including the trades not taken. Without the rejects, whether §1 helped is unknowable.
`execution/journal.py` is the natural home.

Sample-size caveat: a gate set this tight may fire twice a month. A year is ~24
observations. Decide deliberately between tight-and-discretionary and
looser-and-measurable.

---

## 7. Open items

- **Quant Data `Type` field** on each candidate. `ISO` is a clean directional sweep;
  `COB` / `SPRD` / `TIED` is a multi-leg leg and the bullish read is off. At delta 0.99 the
  NVDA 145C is also the standard long leg of a collar or diagonal — the Type decides it.
- Sweep size and at-ask % versus the day's total volume. NVDA's 1,200 could be one ~$9M
  print or an aggregate.
- **Rewrite the EPS gate for pre-revenue names.** "≥3 of 4 beats" is meaningless for OKLO;
  marking it failed is not the same as testing anything.
- Pin down "next week" in the expiration rule.
- Decide whether the invented DTE ≥ 21 floor survives. It excluded the NVDA 10/02 sweep on
  grounds that short-dated flow skews toward hedges — untested.
