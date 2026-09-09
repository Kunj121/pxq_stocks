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
| `journal.py` | Append-only trade journal at `logs/trades.jsonl`. |
| `logs/` | Journal output. Gitignored — trading records stay local. |

---

## Quick use

```bash
source ../.venv/bin/activate

# pre-flight a proposed order (prices come from MCP, passed in by hand)
python guardrails.py --symbol AAPL --side buy --qty 2 --price 227.50

# record a fill after place_equity_order returns
python journal.py add --symbol AAPL --side buy --qty 2 --price 227.50 \
    --order-id abc-123 --account-last4 2690

# review
python journal.py today
python journal.py summary
```

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

## Notes on behavior

- **Environment overrides `.env`.** `load_dotenv` does not override variables already
  set in the process, so `ALLOW_LIVE_ORDERS=true python guardrails.py ...` overrides the
  file for a single invocation without editing `.env`.
- **The daily counter reads the journal.** `guardrails.py` counts today's entries in
  `logs/trades.jsonl`, so `MAX_DAILY_ORDERS` only binds if every placed order is
  actually journaled. Skipping `journal.py add` silently disables that cap.
- **Days are UTC**, not market time — a late-evening ET order counts against the next
  UTC day.
- **`journal.py summary` reports notional flow, not P&L.** It has no cost basis and no
  fill prices. For real P&L use the MCP tools `get_realized_pnl` and
  `get_pnl_trade_history`.
- Malformed journal lines are skipped with a warning rather than crashing the read.
