"""Local pre-flight checks for a proposed order.

Touches no network and no market data. Every number it evaluates was already
fetched from the Robinhood MCP server and is passed in by hand. See README.md.

Exit code 0 = order passes, 1 = rejected.
"""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from journal import count_today

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env_float(name, default):
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        sys.exit(f"FATAL: {name}={raw!r} in .env is not a number")


def _env_int(name, default):
    return int(_env_float(name, default))


def _env_bool(name, default):
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def check(symbol, side, qty, price, multiplier):
    """Return a list of rejection reasons. Empty list means the order passes."""
    load_dotenv(REPO_ROOT / ".env")

    max_notional = _env_float("MAX_ORDER_NOTIONAL_USD", 500.0)
    max_daily = _env_int("MAX_DAILY_ORDERS", 10)
    allow_live = _env_bool("ALLOW_LIVE_ORDERS", False)

    notional = qty * price * multiplier
    placed_today = count_today()
    reasons = []

    if not allow_live:
        reasons.append(
            "ALLOW_LIVE_ORDERS=false in .env — local layer is in dry-run. "
            "Flip it deliberately to trade."
        )

    if notional > max_notional:
        reasons.append(
            f"notional ${notional:,.2f} exceeds MAX_ORDER_NOTIONAL_USD "
            f"${max_notional:,.2f} (over by ${notional - max_notional:,.2f})"
        )

    if placed_today >= max_daily:
        reasons.append(
            f"already placed {placed_today} order(s) today; "
            f"MAX_DAILY_ORDERS={max_daily}"
        )

    print(f"  symbol      {symbol}")
    print(f"  side        {side}")
    print(f"  quantity    {qty:g}" + (f" x{multiplier:g}" if multiplier != 1 else ""))
    print(f"  price       ${price:,.2f}")
    print(f"  notional    ${notional:,.2f}  (cap ${max_notional:,.2f})")
    print(f"  today       {placed_today} order(s)  (cap {max_daily})")
    print()

    return reasons


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, choices=["buy", "sell"])
    p.add_argument("--qty", required=True, type=float)
    p.add_argument(
        "--price",
        required=True,
        type=float,
        help="per-share price, or per-share option premium — from an MCP quote",
    )
    p.add_argument(
        "--multiplier",
        type=float,
        default=1,
        help="100 for option contracts, 1 for equities (default)",
    )
    args = p.parse_args()

    if args.qty <= 0:
        sys.exit("REJECTED: quantity must be positive")
    if args.price <= 0:
        sys.exit("REJECTED: price must be positive — did the MCP quote return null?")

    reasons = check(
        args.symbol.upper(), args.side, args.qty, args.price, args.multiplier
    )

    if reasons:
        print("REJECTED")
        for r in reasons:
            print(f"  - {r}")
        sys.exit(1)

    print("PASS — proceed to review_equity_order / review_option_order")
    print("       (review + explicit human confirmation are still required)")


if __name__ == "__main__":
    main()
