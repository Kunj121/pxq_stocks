# quantdata — the options order-flow feed

The vendor behind the golden-sweep signal, reachable from Python.

| File | What it is |
|---|---|
| `explore_api.ipynb` | what the API serves, end to end, and what is still missing to backtest on it |
| `client.py` | JSON-RPC client — import this rather than re-deriving the transport |
| `feeds.py` | one function per pull — sweeps, bars, the Alpaca bulk lane |
| `sweep_alerts.py` | **real-time golden-sweep alerts to Slack** |
| `test_sweep_alerts.py` | tests for the screen. No network |
| `com.pxq.sweepalerts.plist` | launchd job for the watcher. Not installed |
| `dashboard.py` | **four-stream flow dashboard**, rebuilt every 2 minutes |
| `test_dashboard.py` | tests for the renderer. No network |

```python
from client import QuantData

qd = QuantData()
sweeps, meta = qd.frame(
    "qd_get_order_flow_consolidated",
    sessionDate="2026-09-21",
    tradeConsolidationTypes=["SWEEP"], tradeTypes=["ISO"],
    premiumRange={"min": 1_000_000},
)
```

## Why a client and not the MCP tools

The hosted server at `api.quantdata.us/mcp` is **stateless MCP over HTTPS** — no
handshake, no session id — so `tools/call` works from a bare POST and the feed is
scriptable, not just callable from Claude's tool layer. The REST surface recorded in
the original setup notes (`/v1/options/tool/<name>`) returns 404 as of 2026-09-22.

Credentials resolve from `QUANTDATA_API_KEY`, then from the bearer token in the
`quantdata` MCP registration in `~/.claude.json` — environment first, then the MCP
server's own config, the same order `backtest/config.py` uses for Alpaca.

## Two findings that move the strategy

**Direction is `contractType` x `tradeSideCode`, not call/put.** `sentimentType` is a
deterministic function of the two, and on the sweep screen **58% of qualifying ISO
sweeps print on the bid** — sold, not bought. §1 of
[`sweep_events/strategy.md`](../sweep_events/strategy.md) reads direction from
call/put alone, so it is inverted for the majority of signals. `build_log.py` does not
record the side, so the 38 existing rows cannot be re-signed without re-pulling.

**History runs at least 12 months**, not the four the setup note recorded, and
`qd_get_option_price_over_time` serves traded OHLC for individual contracts. Together
these retire the caveat in [`sweep_events/README.md`](../sweep_events/README.md) that
the log can only grow forward, and remove the need for the Black-Scholes price path in
`backtest_strategy.py`.

## Where it sits in the lanes

[`data-routing`](../../.claude/skills/data-routing/SKILL.md) has two lanes: historical
is Alpaca, live is Robinhood. Quant Data is a **third, read-only historical lane** for
what neither serves — sweep prints and per-contract option bars. It never prices the
underlying (Alpaca's job, and its bars are split-adjusted where the vendor's `spot` is
not) and it never touches execution.

---

## Real-time alerts

`sweep_alerts.py` watches the live tape and posts every qualifying sweep to
Slack, through [`execution/slack.py`](../../execution/slack.py).

```bash
python research/quantdata/sweep_alerts.py --dry-run --once --backfill  # see today, post nothing
python research/quantdata/sweep_alerts.py                              # watch and post
python research/quantdata/sweep_alerts.py --premium 500000             # louder
```

### The screen

| Condition | Value | Deployed |
|---|---|---|
| Consolidation | `SWEEP` | yes |
| Trade type | `ISO` | yes |
| Underlying | not an ETF, not an index | yes |
| `\|delta\|` | `--delta-min` to `--delta-max` | 0.30 – 0.70 |
| Aggressor side | known — `MID_MARKET` has none | yes |
| Premium | `>= --premium` | $1M |
| Days to expiry | `<= --dte-max` | 60 |
| Open interest | `>= --min-oi` | 2 |
| Calls | `--call-side` | `bullish` |
| Expiry date | `--expiry-min` / `--expiry-max` | off |
| Signal date | `--signal-min` / `--signal-max` | off |
| Ticker | not in `--exclude` | `SNDK` |

**`--call-side` exists but is no longer deployed.** It was removed on
2026-09-23: the dashboard's calls chip plus its bullish sentiment chip express
the same thing, and two controls for one question is a place for them to
disagree. Removing it added about 2.8 alerts a session, all sold calls.

The flag still works — `bullish` requires a call to have been bought, at the ask
or above; `above-ask` requires it to have paid through the offer. Puts are never
constrained, because a put already carries its meaning in its side crossed with
its type. **`above-ask` is rarer than it sounds**: 77 of 1,135 call prints over
15–22 September, and *none at all* among prints that cleared the live screen.

`--expiry-min` / `--expiry-max` bound the absolute expiration date and are
independent of `--dte-max`: one pins a cycle, the other is a rolling window.
Pinning 16 October took a session's golden stream from 159 prints to 33.

`--signal-min` / `--signal-max` bound **when the print happened**, not when it
expires. Both take `YYYY-MM-DD` or `YYYY-MM-DD HH:MM`, read as Eastern. A bare
date as the upper bound means the *end* of that day: read literally it would be
midnight, so `--signal-max 2026-09-22` would exclude the whole session it names,
and a filter that silently returns nothing is worse than one that errors.

Every alert now carries the signal date, not just the clock. Scrolling back
through Slack, a bare `13:27` cannot be placed in a session — and Slack's own
message timestamp is the *delivery* time, which differs from the print time
whenever the watcher restarts or catches up.

**Expiry is the second most aggressive filter after premium.** Capping it at 60
days removed **11 of 20** alerts over the 15–22 September window, all LEAPs and
long-dated calls including an NVDA call 821 days out. The surviving population
clusters at 28–31 days.

**The open interest floor currently removes nothing at the alert level.** Every
surviving alert already had open interest of 2 or more, because the two prints
that would have failed were SNDK, excluded by name. It does bite on the
dashboard's wider screen — 27 of 688 prints — and it is a real guard if the
ticker exclusion is ever lifted. Open interest is a whole number, so "greater
than 1" is `--min-oi 2`.

`--min-oi` overrides the new-strike exemption in `--min-vol-oi`: a zero-OI
contract passes any ratio floor, but an explicit open interest floor still
rejects it. An explicit filter should beat an exemption built into a different
one.

**`ISO` is the definition of "golden" here, not the vendor's `isGoldenSweep`
badge.** The badge is a loose unusual-activity marker — 121 prints against 13
for ISO + SWEEP on 2026-09-21, and 70 of the badge-only prints were `AUTO`,
ordinary electronic fills. ISO is a statement about how the order routed, which
is what the premise needs.

**SNDK is excluded in the deployed watcher**, via `--exclude SNDK`, matching
`directional.triggers()`. One SNDK contract costs a median $15,544, about 1.6x
the next worst name, so the print is real but unactionable at any sane position
size. It accounted for 2 of 4 qualifying alerts on 2026-09-22.

The *code* default is still no exclusions, because suppression is a
position-sizing judgement and does not belong in the screen itself. The flag is
where that judgement lives, so it is visible in `systemctl cat sweep-alerts`
rather than buried in a constant.

### What an alert says

```
🟢 *IBRX* CALL $11 exp 2027-01-15 (115dte)
BULL · lifted the ask · Δ 0.52 · IV 109.9%
$1,189,738 premium · $1.69 ea · $169 per contract · 13:27:20 ET
spot $9.19 · size 7,000 · vol 7,011 vs OI 927
```

Four lines, each answering a different question.

**What contract.** Ticker, type, strike, expiry, days to expiry.

**How it reads.** Direction, aggressor side, delta, implied volatility. This
line carries the part call/put alone gets wrong: 58% of qualifying ISO sweeps
print on the bid, so a call is often *bearish*, and an alert naming only the
contract type would invert most of them. Implied volatility arrives already
scaled as a percent — 72.8 means 72.8%, not 0.728.

**What it cost.** Premium, price per share, and **cost per contract**
(`optionPrice * 100`). That last number decides whether a signal is actionable
at all, and it is why `--exclude SNDK` exists: SNDK prints near $13,550 a
contract against IBRX at $169. `optionPrice * 100 * size` reconciles to
`premium` within a rounding error, the gap being that a sweep fills at several
prices while `optionPrice` reports one.

**In what context.** `size` is this print; `volume` is everything that contract
traded today. Together they say whether the sweep *is* the day's activity or
merely joins it — above, one print is 7,000 of the day's 7,011 contracts. Open
interest is the denominator that makes volume readable: volume far above open
interest is the standard read for positions being **opened** rather than closed,
and 7,011 against 927 is about as clear as that gets. Spot sits next to the
strike so moneyness is visible without arithmetic.

A field the vendor leaves empty renders as `?` rather than suppressing the
alert. A missing count is not worth losing a signal over.

### Fields deliberately left out

The feed carries 48 columns per print. Beyond what the alert shows, the
candidates are `moneyness.moneyType` and `moneyness.degreeInPercent`,
`bidAskSpread`, `isOpeningPosition`, `isUnusual`,
`isVolumeGreaterThanOpenInterest`, `exchange`, `sector` / `industry` (usually
`UNKNOWN`), and the twelve non-delta greeks. They are all in the frame and one
line away from being added; the alert stops here because thirteen greeks would
bury the four numbers that decide anything. Everything shown is also written to
the ledger, so nothing is lost for later analysis.

### Choosing a premium floor

Measured across the full 2026-09-22 session, after the delta and ETF cuts:

| Floor | Alerts that session |
|---|---|
| $0 | 472 |
| $100k | 122 |
| $250k | 47 |
| $500k | 13 |
| $1M | 3 |

$1M is the default and matches `directional.TRIGGER_PREMIUM`. The floor is
applied server-side, so a higher floor is also a cheaper poll.

### Cold start

The feed serves the whole session, so a first run at noon could otherwise replay
the entire morning. On a session it has not seen before, the watcher records
what has already printed and alerts only on what comes next. `--backfill`
overrides that.

Fired alerts are appended to `data/sweep_alerts/alerts_<session>.parquet`, which
doubles as the dedup ledger — so a restart cannot re-alert, and every alert stays
auditable afterwards.

### Running it continuously

**It runs on the droplet, not on the laptop.** A closed laptop means no poller,
and Quant Data has no webhook or subscription to cover the gap — all 31 tools
are pull-only, so something has to be awake and asking.

Deployed at `root@192.241.254.230:/opt/sweep-alerts`, as `sweep-alerts.service`:

```bash
ssh root@192.241.254.230 systemctl start sweep-alerts     # arm
ssh root@192.241.254.230 systemctl enable sweep-alerts    # and on boot
ssh root@192.241.254.230 systemctl stop sweep-alerts      # pause
ssh root@192.241.254.230 journalctl -u sweep-alerts -f    # watch
```

Only the files the watcher imports are deployed, in the same layout so no import
path changes: `client.py`, `feeds.py`, `sweep_alerts.py`, `directional.py` and
`execution/slack.py`. The venv is built `--system-site-packages` because the box
already carries pandas, numpy and pyarrow, so the whole install is 17 MB.

Two things that box needs which the laptop does not:

- **`QUANTDATA_API_KEY` set explicitly.** `client.resolve_key()` falls back to
  the bearer token in `~/.claude.json`, and there is no Claude Code there.
- **Its own `/opt/sweep-alerts/.env`, not `/root/pxq_sports/.env`.** That shared
  file already defines a `SLACK_BOT_TOKEN` belonging to another app; inheriting
  it would make these alerts post as that bot. The unit's `EnvironmentFile`
  points at the isolated one deliberately.

The unit caps memory at 384 MB (the watcher peaks near 113 MB). That box runs
live Kalshi bots with about 1 GB free, so a leak here should kill this service
rather than pressure those.

#### Running it on the laptop instead

`com.pxq.sweepalerts.plist` is a launchd job for the same watcher. It only fires
while the Mac is awake, so treat it as a development convenience, not coverage.

```bash
cp research/quantdata/com.pxq.sweepalerts.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.pxq.sweepalerts.plist     # arm
launchctl unload ~/Library/LaunchAgents/com.pxq.sweepalerts.plist   # pause
```

Slack delivery goes through an **incoming webhook**, not the bot token. The
app's `xoxb-` token was issued without `chat:write`, and a webhook needs no
scopes at all, so the webhook is what the service actually posts with. Both are
set in `/opt/sweep-alerts/.env`; `post()` prefers the webhook. Full setup for
either transport is in [`execution/README.md`](../../execution/README.md).

---

## The flow dashboard

`dashboard.py` renders four streams of option flow into one self-contained HTML
page. The alerter tells you when something crosses a line; this shows you what
the tape is doing either way.

```bash
python research/quantdata/dashboard.py                    # today, to out/
python research/quantdata/dashboard.py --session 2026-09-22
python research/quantdata/dashboard.py --fragment --out frag.html
```

### The four streams

| Stream | Screen | Floor | Typical session |
|---|---|---|---|
| Golden sweeps · ISO | `ISO` + `SWEEP` | $100k | ~320 prints |
| ISO sweeps over $500k | `ISO` + `SWEEP`, no delta band, no fund cut | $500k | ~36 prints |
| Unusual · ISO | `ISO` + `SWEEP` + `isUnusual` | $100k | ~110 prints |
| Opening · ISO | `ISO` + `SWEEP` + `isOpeningPosition` | $100k | ~240 prints |

### What counts as a golden sweep

`--golden` picks the definition. They are nearly disjoint, so this is a real
choice rather than a knob:

| Mode | Means | Alerts/session through the full screen |
|---|---|---|
| `iso` | `ISO` + `SWEEP` — the routing fact | ~1 |
| `badge` | the vendor's `isGoldenSweep` | ~13 |
| `both` | badge **and** ISO | far fewer than either |

**Deployed: `badge`**, at the user's explicit choice on 2026-09-23 after the
tradeoff was laid out.

The badge population is entirely `SWEEP` consolidation but only **3% ISO**: 52%
is `AUTO` (ordinary electronic fills) and 43% `MULTI_AUTO_COB` (multi-leg).

**The badge admits multi-leg prints, and that weakens direction.** A
`MULTI_*_COB` is one leg of a spread, so its aggressor side describes that leg
rather than the position. `direction` is still assigned to it and should be
trusted less there. Only 27 of 437 badge prints had no direction at all, so
this is not something the direction test screens out for you.

`--golden` changes **only the `golden` stream**. `iso500` stays ISO whatever it
says — following the mode made it an exact duplicate of `golden`, because the
badge already implies about $1M of premium and its $500k floor then does
nothing. It exists to be the ISO cut you can compare against.

### The badge streams are ISO, and what that implies

A vendor badge alone says nothing about how an order routed. On 2026-09-22 only
**2.9%** of `isUnusual` prints and **2.6%** of `isOpeningPosition` prints were
ISO sweeps; the rest were multi-leg `MULTI_*_COB` and `AUTO` electronic fills.

Restricting them to ISO therefore makes those two tabs **subsets of `golden`**,
not separate populations. All 110 ISO-sweep `isUnusual` prints and all 238
`isOpeningPosition` ones that session were already in the golden stream — which
is inevitable, since `golden` is ISO + SWEEP with nothing else on top. The
badges are *attributes of a print*, not kinds of flow.

They are kept as tabs because that is how they get read, and the same views are
reachable from the `Unusual only` / `Opening only` filters on any tab. Both
badges also travel as a column. Their floors now match `golden`: ISO is the
binding constraint, so the old $250k and $500k floors left almost nothing
behind it.

`feeds.flagged()` defaults to ISO + SWEEP; pass `trade_types=None` for the raw
badge population.

Each stream is
pulled with its own server-side filter, which is not an optimisation but a
correctness requirement: **the unfiltered feed pages out at 20,000 rows**, so
counting a badge by downloading everything and filtering locally silently
truncates. `feeds.flagged()` is the pull.

### ETFs and indices are excluded

Every stream drops funds by default, matching `sweep_alerts.py`. A QQQ, SPY or
BITX sweep is portfolio hedging or index expression, not a view on a company.
What that removes, on the 2026-09-22 session:

| Stream | With funds | Without | Removed |
|---|---|---|---|
| Golden sweeps · ISO | 322 | 209 | 113 (35%) |
| ISO over $500k | 36 | 25 | 11 (31%) |
| Unusual | 175 | 132 | 43 (25%) |
| Opening positions | 1,711 | 804 | **907 (53%)** |

Opening positions is over half funds, so reading that panel un-excluded is
mostly reading index hedging.

The dashboard also applies **expiry under 60 days** and **open interest at
least 2**, matching the alerter. A dashboard showing a wider population than the
thing alerting on it makes every "why didn't that alert?" question
unanswerable. ### Saving a view

**Save view** writes the whole control state — every input, every chip, the
active tab and the sort — to `localStorage`, and the page restores it on load.
**Reset** clears both the controls and the saved copy, so a reset that appeared
to work cannot be undone by the next reload.

It is per-browser and per-origin, not per-account: a convenience, not shared
state. Every access is wrapped, because `localStorage` comes back empty in a
private window and throws outright in some contexts, and the page has to render
correctly with nothing stored.

Two details that would otherwise bite:

- **A stale date range is dropped.** A range saved on an older session cannot
  overlap today's data, and restoring it faithfully would show an empty page
  that looks broken. If it cannot overlap, the current span is used and the page
  says so.
- **A control removed since the view was saved is skipped**, so views saved
  before `calls must be` was dropped still load.

### The page filters itself

**Every filter runs in the browser.** The server pulls each stream once and
ships the rows; the controls narrow that set client-side, so changing a
threshold is instant and needs no redeploy. That was the point of the rewrite:
tuning a filter should not mean an SSH session.

Controls: premium, delta band, days to expiry, open interest, volume over open
interest, call side, direction, ticker, signal date range, and an ETF toggle.
Column headings sort. Tabs carry a live count per stream under the current
filters, so you can see what each one would give you before switching to it.

Two consequences worth knowing:

- **The browser can only narrow what it was given.** Each panel states its own
  pull floor, and lowering a control past it shows no extra rows because those
  were never fetched. Lower the stream's `premium_min` and rebuild for that.
- **Funds are shipped, flagged, not dropped.** The page offers an ETF toggle,
  and a toggle over rows that were never sent would be a lie. They are hidden
  by default and the count of what is hidden is shown.

The controls open on the **deployed alerter's own settings**, so the first view
is the population Slack is actually sending; anything changed is a deviation
from that rather than an unrelated starting point.

`--start` and `--end` render a date range as one page. A week is about 14,500
rows and 2.1 MB, all of it filterable without another fetch.

```bash
python research/quantdata/dashboard.py --start 2026-09-15 --end 2026-09-22
```

### What the polling costs

Measured 2026-09-23. The server side is not the expensive part:

| Operation | Requests | Time |
|---|---|---|
| One dashboard rebuild, four streams | 5 | 0.5s |
| One alerter poll | 1 | 0.4s |
| One context pull, five sessions | 17 | 1.2s |

That is roughly 1,500 requests a day against a 345,600/day allowance, so there
is nothing to win by fetching less from the vendor.

**The waste was client-side.** The page polled with a cache-buster and
`no-store`, which forced a full payload download every minute even though the
file changes only every two, and kept doing it all night when it cannot change
at all. The static server answers `If-Modified-Since` with a 304 and zero
bytes, so the poll now revalidates instead: unchanged means no transfer.
A matching `generated` stamp also skips the redraw, so 200 table rows are not
re-rendered to show the same numbers.

Outside 09:30-16:00 Eastern the page polls every ten minutes rather than every
minute. Never zero, so it still picks up the closing rebuild and recovers by
itself the next morning.

The rebuild timer covers 13:00-20:59 UTC, which spans the session in both EDT
and EST. **Hour granularity is deliberate**: systemd cannot express a minute
range that spans hours, and `13:25..20:05` is not valid `OnCalendar` syntax —
it refuses to load the unit rather than warning.

### Rate limits

The server allows **240 requests a minute with a burst of 20 a second**, and
signals a breach as HTTP 200 with a 429 quoted in the body — so it has to be
recognised by text, not status. `client.call()` retries up to five times,
honouring the server's own `retryAfterSeconds` and backing off harder without
it. A validation failure or any non-429 error is never retried, because asking
again cannot fix a bad argument.

Give up and the error says it is pacing rather than a malformed request; the
two want different fixes.

The embedded JSON escapes `<` as `\u003c`. A `</script>` inside vendor data
would otherwise end the script element early whatever the JSON says, because
the HTML parser never sees the string as a string.

The filter runs locally rather than through the server's `IS_ETF` expression
for two reasons: `is_fund()` catches indices as well as ETFs, where `IS_ETF`
alone leaves NDX and RUT behind, and the API takes one `filterExpression` at a
time, already spent on the badge for two of the streams.

Each panel reports how many it hid, because a dashboard that quietly drops a
third of the tape invites you to read the remainder as the whole. Pass
`--include-funds` to keep them.

Floors differ per stream because the badges do. `opening` is the broadest thing
the feed carries, nearly 5,000 prints a session above $100k, so it needs a
higher floor to stay a dashboard rather than a firehose.

### "Golden sweep" means two different things

The streams above use **ISO**, the routing fact. The vendor's own
`isGoldenSweep` badge is counted separately in every panel, because the two
populations barely overlap. On 2026-09-22 the badge flagged **155 prints of
which only 3 were ISO** — 80 were multi-leg `MULTI_AUTO_COB` and 70 were `AUTO`,
ordinary electronic fills.

The badge also already implies about $1M of premium: its smallest print that
session was $1,000,273, which is why its count does not move when you lower the
floor. That is the badge's own threshold, not the filter being ignored.

### Where it runs

Rebuilt every two minutes on the droplet and served on **localhost only**, since
that box has no firewall and the page carries your screen.

```bash
ssh -N -L 8085:127.0.0.1:8085 root@192.241.254.230   # tunnel, leave running
# then open http://127.0.0.1:8085/flow.html
```

| Unit | Role |
|---|---|
| `flow-dashboard.timer` | fires every 2 min, weekdays 13:00-21:59 UTC |
| `flow-dashboard.service` | oneshot rebuild, about 1.5s |
| `flow-web.service` | static server bound to `127.0.0.1:8085` |

The timer window is stated in UTC on purpose: 13:00-21:59 covers 09:00-17:00
Eastern in both EDT and EST, so there is no DST arithmetic to get wrong.

`--fragment` omits the document wrapper, for publishing a snapshot as an
Artifact that can be read from a phone. The droplet copy is the live one.
