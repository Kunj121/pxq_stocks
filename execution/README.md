# execution/

The order-execution layer. **Every market-facing operation goes through the Robinhood
MCP server.** No exceptions.

---

## The rule

> All market data, account state, and order flow comes from `mcp__robinhood-trading__*`
> tools. Nothing else is a valid source.

This is the **live** half of the repo-wide routing rule (*historical → Alpaca, live →
Robinhood*), enforced by [`.claude/skills/data-routing/SKILL.md`](../.claude/skills/data-routing/SKILL.md).

Specifically forbidden as data or execution paths:

| Forbidden | Why |
|---|---|
| `robin_stocks` | Unofficial API wrapper — bypasses MCP, separate auth, no agentic guardrails |
| `yfinance` | Delayed/adjusted data that silently disagrees with Robinhood's book |
| Scraped quotes, CSVs of prices | Stale by construction |
| Hardcoded prices, sizes, account numbers | Fabricated state |
| `backtest/` output, Alpaca bars | Historical simulation — see the boundary below |
| Cached MCP output reused across a session | Prices move; re-fetch |

If an MCP tool can answer the question, it **must** be the thing that answers it.

### The `backtest/` boundary

`backtest/` is a legitimate sibling lane, but it is **historical simulation only** and
its data comes from Alpaca, not Robinhood. The two must not cross here:

- A backtest may **motivate** a trade — "this strategy showed edge, so I want the position."
- A backtest may **never price** one. Every number that reaches an order — the quote,
  the size, the buying power, the position you already hold — comes from a live MCP call
  made now.

Alpaca bars are a different venue's history, adjusted differently, and as stale as the
last cache write. They are not the book you are about to trade against.

---

## Why there is no Python that places orders

MCP tools are invoked by **Claude**, not by your Python process. There is no MCP client
in this venv and one must not be added — a Python path to Robinhood would necessarily
route around the MCP server and its safety surface.

This produces a hard split:

```
┌─────────────────────────────────────────────────┐
│  MARKET-FACING  —  Claude + MCP tools only      │
│  quotes · fundamentals · positions · orders     │
│  → PROTOCOL.md defines the exact call sequence  │
└─────────────────────────────────────────────────┘
                      │
                      │  numbers passed down by hand
                      ▼
┌─────────────────────────────────────────────────┐
│  LOCAL  —  Python, no network, no market data   │
│  guardrails.py · journal.py                     │
└─────────────────────────────────────────────────┘
```

`guardrails.py` and `journal.py` never fetch anything. They take values that Claude
already pulled from MCP and do arithmetic and record-keeping on them. That is the only
role Python has in execution.

---

## Files

| File | Role |
|---|---|
| `PROTOCOL.md` | The exact MCP tool sequence for equity and option orders. Read before every trade. |
| `guardrails.py` | Local pre-flight check: notional cap, daily order count, live-order kill switch. |
| `journal.py` | Append-only trade journal at `logs/trades.jsonl` + `logs/trades.csv`. |
| `slack.py` | Posts run verdicts and files to a Slack channel. Stdlib only. |
| `test_slack.py` | Tests for the above. No network — `urlopen` is stubbed. |
| `logs/` | Journal output. Gitignored — trading records stay local. |

### Journal output

Two files, written together on every order:

| File | Shape |
|---|---|
| `logs/trades.jsonl` | Full record — one JSON object per order. `guardrails.py` counts today's orders from this. |
| `logs/trades.csv` | `date,time,symbol,ticker,side,price,qty,fees,strategy` |

`date`/`time` are **local wall-clock** (the clock the order was placed on), taken from
the broker's own `created_at` rather than from when the journal happened to run. The
jsonl `ts` stays UTC. `symbol` and `ticker` carry the same value — the CSV schema asks
for both columns.

The two files are kept in sync by `journal.py`; do not hand-edit either.

---

## Quick use

```bash
source ../.venv/bin/activate

# pre-flight a proposed order (prices come from MCP, passed in by hand)
python guardrails.py --symbol AAPL --side buy --qty 2 --price 227.50

# record a fill after place_equity_order returns
# (usually unnecessary — the hook below already did it)
python journal.py add --symbol AAPL --side buy --qty 2 --price 227.50 \
    --order-id abc-123 --account-last4 2690 --fees 0 --strategy test

# correct a row once the real fill is known
python journal.py amend --order-id abc-123 --price 227.61 --qty 2 --fees 0 \
    --state filled

# review
python journal.py today
python journal.py summary
```

---

## Automatic journaling

Journaling is **not** left to whoever remembers to run it. `.claude/settings.json`
registers a `PostToolUse` hook on `mcp__robinhood-trading__place_.*_order`:

```
place_equity_order / place_option_order returns
        │
        ▼
python3 execution/journal.py from-mcp --strategy test
        │
        ▼
logs/trades.jsonl  +  logs/trades.csv
```

`from-mcp` reads the hook's PostToolUse payload on stdin and shape-matches the order
object out of the MCP response, so it survives changes to the response's nesting. It
is deliberately unable to fail loudly — a journaling error must never be mistaken for
an order error — so it exits 0 on malformed input and ignores non-order tools.

Two things it cannot know at placement time, both of which need a follow-up `amend`:

| Unknown | Why | Fix |
|---|---|---|
| Fill price and fees | `average_price` and `fees` are null until the order fills | `journal.py amend --order-id <id> --price <avg> --fees <fees> --state filled` |
| Share count on a dollar-based order | Robinhood computes shares from the fill; `quantity` is null at placement | same `amend`, plus `--qty <filled>` |

A dollar-based order is journaled with the notional preserved and the row flagged
`pending-fill`, and the hook's message spells out the exact `amend` command to run.

`--strategy` defaults to `test`. Change the flag in `.claude/settings.json` when the
orders stop being tests.

Records are keyed by `order_id` and are **idempotent** — if the hook fires and the
order is then journaled by hand, the second write updates the existing row rather than
duplicating it.

---

## Guardrails

Read from the repo-root `.env`. These are **local** checks — the MCP server does not
know about them, so they only bind if the protocol in `PROTOCOL.md` is actually followed.

| Var | Meaning |
|---|---|
| `MAX_ORDER_NOTIONAL_USD` | Reject any single order above this dollar value |
| `MAX_DAILY_ORDERS` | Reject once today's journal hits this count |
| `ALLOW_LIVE_ORDERS` | `false` → `guardrails.py` fails every order. Flip to `true` deliberately. |

`ALLOW_LIVE_ORDERS=false` is the default and the safe state. It is a dry-run switch for
the local layer; it does **not** disable the MCP tools themselves, which is why rule 2 in
`PROTOCOL.md` (explicit human confirmation) is the real backstop.

---

## Slack notifications

`slack.py` posts messages and files to a Slack channel. It is standalone: no
other module in this repo imports it, and nothing calls it on a schedule. Wire
it into whatever you want reported, or use it from the shell.

**This does not breach the no-network rule above.** That rule governs *market*
data and order flow, which stay on the MCP path. `slack.py` is an outbound
notification sink: it reads no prices, reaches no broker, and nothing it
returns may be fed back into an order. Keep it that way — if a number is worth
posting to Slack, it was already fetched from MCP somewhere upstream.

### Two transports

| | Webhook | Bot token |
|---|---|---|
| Env var | `SLACK_WEBHOOK_URL` | `SLACK_BOT_TOKEN` |
| Scopes needed | none | `chat:write` |
| Channel | fixed at creation | any, via `SLACK_CHANNEL` |
| Upload files | no | yes, with `files:write` |
| Thread replies | no | yes |

`post()` prefers the webhook whenever one is set and the call names neither a
channel nor a thread, because that path cannot fail on a missing scope. Calls
the webhook cannot serve fall through to the token and say so if it is absent.

**Prefer the webhook unless you need a second channel or file uploads.** It is
one URL, it needs no scopes, no install, and no invite.

### Setup — webhook

api.slack.com/apps -> your app -> *Incoming Webhooks* -> add one, pick the
channel, copy the URL into `.env` as `SLACK_WEBHOOK_URL`. That is the whole
setup. The URL is a credential: anyone holding it can post to that channel.

### Setup — bot token

1. **OAuth & Permissions.** Under *Bot Token Scopes* add `chat:write`. Add
   `files:write` only for `upload()`, and `channels:read` only if you want to
   name an upload target `#like-this` instead of by its `C…` id.
2. **Install to the workspace** and copy the *Bot* User OAuth Token (`xoxb-…`).
   The user token (`xoxp-…`) posts as you rather than as the bot.
3. **Put it in `.env`** (gitignored) as `SLACK_BOT_TOKEN`, with `SLACK_CHANNEL`.
4. **Invite the bot to the channel**: `/invite @your-bot-name`.

Two failures worth recognising on sight. `not_in_channel` means step 4 was
skipped — the token is fine, the bot simply is not in the room. `missing_scope`
means the scope was added but the app was **not reinstalled**; a scope granted
after install does nothing until you reinstall, and the existing token keeps
whatever it was issued with.

### Use

```bash
python execution/slack.py --check                 # inspect the configured transport
python execution/slack.py "nightly refresh finished"
python execution/slack.py --file out/some_report.html --title "Report"
```

```python
from execution.slack import notify, Slack

notify("DONE", "nightly refresh finished")       # returns False on failure
Slack().post("something worth reading")          # raises SlackError instead
```

Use `notify()` from anything unattended and `Slack()` from anything you are
watching. The difference is deliberate: `notify()` swallows every failure, so a
Slack outage cannot take down the job it is reporting on, while `Slack()` raises
so an interactive mistake is not silent.

### Failure behavior

Slack answers a *rejected* call with HTTP 200 and `{"ok": false}`, so success is
read from the `ok` field, never from the status code. Rate limits (429) retry up
to three times against `Retry-After` and then give up rather than blocking the
caller indefinitely.

---

## Notes on behavior

- **Environment overrides `.env`.** `load_dotenv` does not override variables already
  set in the process, so `ALLOW_LIVE_ORDERS=true python guardrails.py ...` overrides the
  file for a single invocation without editing `.env`.
- **The daily counter reads the journal.** `guardrails.py` counts today's entries in
  `logs/trades.jsonl`, so `MAX_DAILY_ORDERS` only binds if every placed order is
  actually journaled. The PostToolUse hook is what makes that reliable; with hooks
  disabled (`disableAllHooks`, or `--settings` overriding the project file) the cap is
  only as good as the operator's memory.
- **Days are UTC**, not market time — a late-evening ET order counts against the next
  UTC day.
- **`journal.py summary` reports notional flow, not P&L.** It has no cost basis and no
  fill prices. For real P&L use the MCP tools `get_realized_pnl` and
  `get_pnl_trade_history`.
- Malformed journal lines are skipped with a warning rather than crashing the read.
