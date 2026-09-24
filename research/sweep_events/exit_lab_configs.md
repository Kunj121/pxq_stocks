# Exit Lab configs — saved 2026-09-23

Exports from the Sweep Exit Lab dashboard (https://claude.ai/artifact/6rd57Zm8tvCWR55iQKnuko),
kept as-is. Raw JSON and the signal tables (joined to the 56-column trade log) live in
`data/exit_lab/`. Reload either on the page via Presets → paste, or Export → load if
saved to Claude.

**Status: parked, not validated.** Both were selected on the same 16-week sample they
are scored on. Kept because there may be an economic reason the put side worked
this summer (the July selloff, the memory/AI unwind) that is worth checking before
the settings are thrown out or trusted.

| | PUT_CONFIG | call_params |
|---|---|---|
| exit | TP +100% / SL −56%, $3k, 1 contract if over | TP +100% / SL −40%, $5k |
| filters | swept PUT · BEARISH · bid/ask/above-ask · DTE 1–60 · no funds | swept CALL · BULLISH · BULL · ask/above-ask · DTE ≤ 60 · **vendor golden flag** · opening position · **not mega-cap** · size ≥ 2 · vol ≥ 2 · OI ≥ 1 |
| trades | 21 (19 independent) | 3 |
| rules | +$19,278 · win 52% · PF 2.1 · DD −50% | +$9,685 · PF 9.5 |
| hold to expiry, same trades | +$4,410 | +$23,271 |
| where the edge is | TP locked in 8 fades (+$28.9k); SL was a wash (+$5.8k saved, −$15.2k stopped recoveries) | none — TP cut NBIS at +107% on the way to +398% |
| bootstrap 95% CI on mean | [+5%, +89%] (19 indep.) | [−41%, +111%] |

## Open test

The mirror of PUT_CONFIG (swept CALL, BULLISH, else identical) is 27 trades. Held to
expiry they run 30% win / 14 worthless / 4 above +100%. Running them through the
same +100/−56 rule on the page is the one experiment that separates "the rule has
an edge" from "puts had a good summer". Not yet run.

## Economic-factor angle to check

If the put edge is real it should line up with something: the July drawdown
(SPY 52-week lows for many of these names on 2026-07-29), sector concentration
(semis/AI — MU, ARM, AMD, NBIS, IREN are 11 of the 21), or the regime the hedged
backtest found (share hedge made +$96.8k by being short SPY through July). Split
the 21 by month and by sector before believing the aggregate.
