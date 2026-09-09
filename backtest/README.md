# `backtest/`

A backtesting toolkit whose market data comes **solely from the Alpaca Market Data
API**. No yfinance, no scraped CSVs, no vendored price files — if a number appears
in a result, it came over the wire from Alpaca and passed through
[`data.py`](data.py).

```bash
source .venv/bin/activate

python -m backtest check                     # verify credentials + data access
python -m backtest strategies                # list what's available
python -m backtest run --symbols AAPL,MSFT,NVDA \
    --start 2019-01-01 --strategy sma_cross \
    --param fast=20 --param slow=100 --benchmark SPY
```

---

## Routing

This is the **historical** half of the repo-wide rule (*historical → Alpaca, live →
Robinhood*), enforced by [`.claude/skills/data-routing/SKILL.md`](../.claude/skills/data-routing/SKILL.md).

Everything that reads the past lives here. Everything live — quotes, positions, buying
power, orders — goes through the Robinhood MCP via [`execution/`](../execution/README.md).
A backtest may motivate a trade; it may never price one.

---

## Layout

| File | Responsibility |
|---|---|
| [`config.py`](config.py) | Credential resolution and settings. Reads no secrets from this repo. |
| [`client.py`](client.py) | The only module that touches the network. Auth, pagination, retries, SIP embargo handling. |
| [`data.py`](data.py) | Alpaca JSON → pandas `BarPanel`, plus the on-disk cache. |
| [`engine.py`](engine.py) | Portfolio simulation: order sizing, fills, costs, accounting. |
| [`metrics.py`](metrics.py) | Pure statistics on a return series. No I/O. |
| [`strategies/`](strategies) | The `Strategy` interface and three worked examples. |
| [`writers.py`](writers.py) | Tabular output. Parquet by default; CSV on request. |
| [`report.py`](report.py) | Chart + markdown + parquet artefacts under `out/`. |
| [`cli.py`](cli.py) | `python -m backtest`. |
| [`tests/`](tests) | 70 offline tests. The Alpaca client is stubbed; nothing hits the network. |

---

## Output format

Parquet, by default, for everything tabular — fetched bars and the `equity` / `fills` /
`weights` artefacts a run writes. See
[`.claude/skills/parquet-output/SKILL.md`](../.claude/skills/parquet-output/SKILL.md).

```bash
python -m backtest fetch --symbols META --start 2023-09-09 --timeframe 1Min
#   -> data/META_1Min.parquet
python -m backtest run ... --format csv     # opt out for a run
```

Parquet preserves the timezone-aware MultiIndex and dtypes that CSV drops, and is ~3x
smaller on minute bars. Write via [`writers.write_frame`](writers.py), not `to_csv`.

The on-disk bar cache under `data/alpaca_cache/` stays gzipped CSV — it is internal, and
switching it would orphan every existing entry for no user-visible gain.

---

## Credentials

Nothing secret lives in this repo. `config.py` resolves credentials from the first
source that supplies **both** a key and a secret:

1. Process environment — `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`, or the
   `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` aliases Alpaca's own SDKs use.
2. The repo's `.env`.
3. `~/.claude/mcp-servers/alpaca.env` — the file the Alpaca MCP server already
   reads. Override the path with `ALPACA_ENV_FILE`.

Source 3 is how this normally resolves on this machine, so the backtester and the
MCP server stay on one set of keys. `python -m backtest check` prints which source
won, along with a masked key id.

Optional settings, read from the same three sources:

| Key | Default | Effect |
|---|---|---|
| `ALPACA_DATA_FEED` | `sip` | Default stock feed. |
| `ALPACA_PAPER_TRADE` | `true` | Which trading host serves the calendar/asset endpoints. Market data is identical either way. |
| `BACKTEST_CACHE_DIR` | `data/alpaca_cache` | Where cached bars live. |
| `BACKTEST_OUTPUT_DIR` | `out` | Where reports are written. |

> This package **never places an order**. It only reads. The two trading-API calls
> it makes (`/v2/calendar`, `/v2/assets`) are reference data.

---

## Data

```python
from backtest import load_bars

panel = load_bars(["AAPL", "MSFT"], "2020-01-01", "2024-12-31", "1Day")

panel.close          # DataFrame: timestamps x symbols
panel.returns()      # bar-over-bar simple returns
panel.frame          # the long form: MultiIndex (timestamp, symbol)
panel.symbols        # ['AAPL', 'MSFT']
```

**Timestamps are timezone-aware `America/New_York`.** Alpaca serves UTC; daily bars
arrive stamped `05:00Z`, which is midnight ET, so ET is the honest presentation.

**Bars are split- and dividend-adjusted by default** (`adjustment="all"`). That is
what you want for a return series — raw prices put a fake -30% day wherever a
split happened. Pass `adjustment="raw"` if you specifically need unadjusted prints.

### Feeds

| Feed | What it is |
|---|---|
| `sip` | Full consolidated tape — every US exchange. The default. |
| `iex` | IEX only, roughly 2% of volume. Free, but its OHLC and volume differ from the real market. |

Free and basic Alpaca plans serve **full SIP history but embargo the last 15
minutes**. A request whose window runs into that embargo is rejected outright
rather than truncated, so `client.py` catches the specific error, pulls `end` back
to the embargo edge, and retries — still on SIP. It only falls back to IEX for a
genuine entitlement failure, and says so loudly when it does. Check which feed a
result used: `panel.feed`, or the "data feed" row in any generated report.

### Intraday granularity

**Yes — 1-minute bars, full SIP, back to the same 2016-01-04 floor as daily.**
Alpaca also serves `1Min`, `5Min`, `15Min`, `1Hour`, and any `<n>Min` multiple.

```bash
python -m backtest run --symbols SPY,QQQ --start 2026-08-01     --timeframe 1Min --strategy mean_reversion --slippage-bps 2
```

There is a trap here worth understanding. **Alpaca returns intraday bars for the
whole extended day, 04:00–20:00 ET — 865 one-minute bars per session, not 390.**
Those extra bars are not comparable to regular-session ones:

| | regular session | pre/post-market |
|---|---:|---:|
| bars per session (`1Min`) | 390 | 475 |
| median SPY volume per bar | 50,696 | 788 |

Left in, they do two kinds of damage: annualisation is wrong by a factor of 2.5
(the year has 98,280 regular minutes, not 241,920), and the engine cheerfully
"fills" orders in bars that traded a few hundred shares.

So `load_bars` filters to the **regular session by default** and sets the
annualisation factor to match:

```python
load_bars(["SPY"], "2026-08-01", "2026-09-08", "1Min")                      # 09:30-16:00 ET
load_bars(["SPY"], "2026-08-01", "2026-09-08", "1Min", session="extended")  # 04:00-20:00 ET
```

`--session extended` on the CLI does the same. Use it to study the open and close
auctions or overnight gaps — but treat modelled fills in those bars as fiction.
The setting is ignored for daily bars and for crypto, which trades continuously.

Two details of the session filter:

*It matches on the bar's **interval**, not its start time.* A bar is labelled by
when it opens but covers the span after it, so the hourly bar stamped 09:00 holds
the 09:30 open and ~4.6M SPY shares. Minute timeframes tile a 6.5-hour session
exactly (390 / 78 / 26 / 13 bars for 1/5/15/30Min) and the two rules agree; hourly
bars do not tile it, and start-time filtering would silently throw the open away.
Regular-session hourly therefore yields **7** bars, 09:00 through 15:00.

*The closing auction is not in the regular window.* It prints at 16:00, which
lands in the 16:00 bar, outside `[09:30, 16:00)`. So the last regular 1-minute bar
closes a cent or two away from the official daily close (765.94 vs 765.96 for SPY
on 2026-09-08). If you need the official close, use daily bars — they have it.

Volume to expect: one symbol-year of 1-minute regular-session bars is ~98k rows,
around 2 MB gzipped in the cache. Requests over the 10,000-bar page limit are
paginated automatically.

### Caching

Bars are cached per symbol under `data/alpaca_cache/stocks/<timeframe>/<feed>_<adjustment>/`,
each `.csv.gz` paired with a `.meta.json` recording the date range it covers. A
later run that asks for a subset is served from disk; one that extends the range
fetches only the missing window and merges. Widening on both sides at once
refetches the span rather than tracking disjoint intervals.

**Bars from the current session are returned but never written to disk**, so a
half-formed daily bar cannot poison a later run. Pass `use_cache=False` (or
`--no-cache`) to force a fresh pull.

The cache stores the **unfiltered** extended-hours superset, so switching
`session` is a read-time view change and never triggers a refetch.

### Other loaders

```python
from backtest import load_calendar
from backtest.data import load_universe

load_calendar("2024-01-01", "2024-12-31")   # official sessions, incl. half days
load_universe(exchanges=["NASDAQ", "NYSE"]) # tradable asset master
load_bars(["BTC/USD"], "2023-01-01", asset_class="crypto")
```

---

## The engine

### Execution model

A strategy sees the world **at the close of bar `t`**; its orders fill **at the
open of bar `t+1`**.

```
bar t         bar t+1
  │              │
  ├─ close ──────┼─ open ──────────
  │  decide      │  fill
```

That one-bar delay is enforced by the loop, not by strategy discipline, which
removes the most common source of look-ahead bias. `Context.history()` is likewise
hard-capped at the current bar — a strategy cannot read the future even by
accident. Two tests pin this down: `test_history_never_extends_past_current_bar`
and `test_fills_happen_at_the_next_bar_open`.

### What is modelled

- Fractional or whole-share sizing.
- Per-share and basis-point commissions with a per-order minimum.
- Slippage as a fixed spread cost in bps, **always adverse** — buys pay up, sells
  receive less.
- Interest earned on idle cash, charged on debit balances and short market value.
- A gross-exposure cap, applied by scaling the whole target vector down.
- Symbols with no print on a bar (halt, pre-IPO, delisted) are held, not traded.

### Costs

```python
from backtest import Costs, run

result = run(strategy, panel, initial_cash=100_000, costs=Costs(
    slippage_bps=5,             # 5bp given up to the spread on every trade
    commission_per_share=0.005,
    min_commission=1.00,
    cash_rate=0.04,             # earned on idle cash
    borrow_rate=0.06,           # charged on debits and shorts
))
```

The defaults (`slippage_bps=1`, no commission, no financing) describe a retail
account at a zero-commission broker. They are optimistic for anything that trades
small caps or trades often — **turn slippage up and see whether the edge survives.**

### Results

```python
result.equity            # per-bar equity Series
result.returns           # per-bar returns
result.drawdown          # fractional drawdown from the running peak
result.weights           # per-bar portfolio weights
result.positions         # per-bar share counts
result.fills             # every trade, with its slippage and commission
result.gross_exposure    # sum of |weights|
result.turnover          # traded notional / equity, per bar
result.to_frame()        # all of the per-bar series in one table

print(result.summary())              # the printed block
result.stats(benchmark_returns)      # the same numbers as a dict
```

---

## Writing a strategy

Answer one question per bar: *given everything known at this close, what should
the portfolio look like?* Answer with **target weights** — signed fractions of
equity.

```python
from backtest.engine import HOLD, Context
from backtest.strategies import Strategy, register

class Momentum(Strategy):
    """Hold the single best 60-day performer, reviewed monthly."""

    def __init__(self, lookback: int = 60):
        self.lookback = lookback
        self.warmup = lookback          # no decisions until the window is full

    def target_weights(self, ctx: Context):
        if ctx.i % 21:                  # only act once a month
            return HOLD
        window = ctx.history("close", self.lookback)
        performance = window.iloc[-1] / window.iloc[0] - 1
        return {performance.idxmax(): 1.0}

register("momentum", Momentum)          # now visible to the CLI
```

Three return values, three meanings:

| Return | Meaning |
|---|---|
| `{"AAPL": 0.6, "MSFT": 0.4}` | Target those weights. Unmentioned symbols go to zero. |
| `{}` | Go flat — sell everything. |
| `HOLD` | Change nothing. Place no orders. |

`HOLD` is how a strategy avoids paying slippage to rebalance away price drift it
does not care about. Returning `None` raises, so a forgotten `return` fails loudly
instead of silently liquidating.

What a strategy gets from `ctx`:

| | |
|---|---|
| `ctx.history(field, lookback)` | `timestamp x symbol` frame, capped at the current bar |
| `ctx.series(symbol, field, lookback)` | one symbol's history |
| `ctx.prices` | this bar's closes |
| `ctx.positions` / `ctx.weights` | what is held right now |
| `ctx.cash` / `ctx.equity` | account state |
| `ctx.i` / `ctx.timestamp` | where we are on the timeline |

`prepare(panel)` runs once before the loop, for precomputing indicators. Only put
**causal** transforms there — `rolling`, `shift`, `ewm`. A whole-sample mean or
z-score computed in `prepare` leaks the future into every decision, which is the
one way to defeat the engine's look-ahead protection.

### Bundled examples

| Name | Idea |
|---|---|
| `buy_and_hold` | Equal-weight basket, bought once. The bar everything else has to clear. |
| `sma_cross` | Hold symbols whose fast SMA is above their slow SMA. `long_short=True` shorts the rest. |
| `mean_reversion` | Buy the most oversold names by trailing z-score, hold until they revert. |

These exist to exercise the engine and to show the interface. **They are not
recommendations** — they are three of the most over-fit ideas in finance, and any
edge they show on mega-cap tech from 2019 is mostly the fact that mega-cap tech
went up.

---

## CLI

```bash
python -m backtest check          # credentials, feed, connectivity
python -m backtest strategies     # registered strategies and their parameters
python -m backtest fetch --symbols AAPL,MSFT --start 2020-01-01 --out data/tech.parquet
python -m backtest run  --symbols AAPL,MSFT --start 2020-01-01 --strategy sma_cross
```

Useful `run` flags:

| Flag | |
|---|---|
| `--param KEY=VALUE` | Strategy parameter; repeat for more. Types are inferred. |
| `--timeframe` | `1Day` (default), `1Hour`, `15Min`, `1Min`, `1Week`. |
| `--session` | Intraday only: `regular` (default, 09:30–16:00 ET) or `extended` (04:00–20:00 ET). |
| `--benchmark SPY` | Adds beta, alpha, and a comparison line to the chart. |
| `--slippage-bps`, `--commission-per-share`, `--commission-bps` | Cost model. |
| `--max-leverage` | Gross exposure cap. Default `1.0`. |
| `--whole-shares` | Disallow fractional sizing. |
| `--cash` | Starting equity. Default `100,000`. |
| `--no-cache` | Force a fresh pull from Alpaca. |
| `--no-report` | Print stats only; write nothing. |
| `--name` | Output directory name under `out/`. |
| `-v` | Log every Alpaca request. |

Each run writes `out/<name>/`: `equity.png` (equity, drawdown, exposure),
`summary.md`, `equity.png`, and `equity` / `fills` / `weights` as parquet
(or CSV with `--format csv`).

---

## Tests

```bash
.venv/bin/python -m pytest backtest/tests -q
```

70 tests, all offline — the client is stubbed with `httpx.MockTransport` and
panels are built from synthetic prices, so the suite is deterministic and needs no
credentials. It covers metric arithmetic against hand-computable cases, the
engine's execution timing and accounting identity, cache range arithmetic, and the
SIP-embargo retry path.

---

## Known limitations

Read these before trusting a number.

**Survivorship bias.** Alpaca's asset master is *today's* list. A universe picked
from it — or typed by hand from memory — excludes everything that delisted, so
backtests over it are biased upward. There is no clean fix within Alpaca's API;
the honest move is to know the bias is there and not treat a 5-symbol mega-cap
backtest as evidence of anything.

**Data history.** Alpaca's equity data starts in **2016**. Anything before that
returns empty, so no strategy here can be tested across 2008 or the dot-com bust.

**Point-in-time correctness.** Adjusted bars reflect *today's* split and dividend
factors. That is right for return series but means the prices are not what a
trader saw on the day.

**No corporate-action modelling.** Mergers, spin-offs, and ticker changes are not
handled; a symbol that changed hands mid-sample will have a discontinuity.

**Fills are optimistic.** Every order fills completely, at the open, at a fixed
slippage. No partial fills, no market impact that scales with size, no gapping
through a stop. Large size in a thin name would not behave this way.

**No borrow model.** Shorts are assumed available and cost only `borrow_rate`.
Hard-to-borrow names are neither flagged nor priced.

**Bar-level only.** With daily bars, intraday stops, limits, and the path within a
day are invisible. A strategy that depends on intraday sequencing needs intraday
bars (which are available down to `1Min` — see above) and then has to face the
next problem: at minute resolution, spread and impact dominate. A 1-minute mean
reversion smoke test over five weeks of SPY/QQQ paid $3,489 in slippage on
$100,000 at just 2bp per trade, turning a flat gross result into a loss. Costs are
the strategy at that horizon.

**Taxes and fees.** Not modelled. Neither are SEC/FINRA pass-through fees.

**The multiple-comparisons problem.** Sweeping parameters until one looks good
finds noise. If you tune on a sample, hold out a period you have not looked at,
and expect live results to fall short of the backtest.

---

## Relationship to the MCP servers

The Alpaca and Robinhood MCP servers are for *live* research and order flow, driven
conversationally. This package is for *historical* simulation, driven by code.
They share credentials (via `~/.claude/mcp-servers/alpaca.env`) but nothing else —
and this package places no orders, so the repo's `ALLOW_LIVE_ORDERS=false`
guardrail is not something it can violate.
