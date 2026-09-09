"""Command line interface: ``python -m backtest ...``

Examples
--------
::

    python -m backtest check
    python -m backtest strategies
    python -m backtest fetch --symbols AAPL,MSFT --start 2020-01-01
    python -m backtest run --symbols AAPL,MSFT,NVDA --start 2019-01-01 \\
        --strategy sma_cross --param fast=20 --param slow=100 --benchmark SPY
"""

from __future__ import annotations

import argparse
import inspect
import logging
import sys
from pathlib import Path

import pandas as pd

from backtest.client import AlpacaClient, AlpacaError
from backtest.config import ConfigError, get_settings
from backtest.data import DataError, load_bars, load_calendar
from backtest.engine import Costs, run as run_backtest
from backtest.report import slugify, write_report
from backtest.strategies import get_strategy, list_strategies
from backtest.writers import DEFAULT_FORMAT, FORMATS, suffix_for, with_format, write_frame


def _symbols(text: str) -> list[str]:
    return [s.strip().upper() for s in text.replace(" ", ",").split(",") if s.strip()]


def _coerce(value: str) -> object:
    """Turn a ``--param`` string into the obvious Python type."""
    lowered = value.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"none", "null"}:
        return None
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            continue
    return value


def _params(pairs: list[str] | None) -> dict[str, object]:
    out: dict[str, object] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--param expects key=value, got {pair!r}")
        key, _, value = pair.partition("=")
        out[key.strip()] = _coerce(value)
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backtest",
        description="Backtest strategies on Alpaca market data.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log every Alpaca request")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="verify credentials and data access")
    sub.add_parser("strategies", help="list registered strategies")

    data_args = argparse.ArgumentParser(add_help=False)
    data_args.add_argument("--symbols", required=True, help="comma-separated, e.g. AAPL,MSFT")
    data_args.add_argument("--start", required=True, help="YYYY-MM-DD, inclusive")
    data_args.add_argument("--end", default=None, help="YYYY-MM-DD, inclusive (default: today)")
    data_args.add_argument("--timeframe", default="1Day", help="1Day, 1Hour, 15Min, 1Week, ...")
    data_args.add_argument(
        "--session",
        default="regular",
        choices=["regular", "extended"],
        help="intraday only: 09:30-16:00 ET, or the full 04:00-20:00 ET day",
    )
    data_args.add_argument(
        "--adjustment", default="all", choices=["all", "split", "dividend", "raw"]
    )
    data_args.add_argument("--feed", default=None, help="override the configured feed")
    data_args.add_argument(
        "--asset-class", default="stocks", choices=["stocks", "crypto"]
    )
    data_args.add_argument("--no-cache", action="store_true", help="force a fresh pull")
    data_args.add_argument(
        "--format",
        default=DEFAULT_FORMAT,
        choices=list(FORMATS),
        help="tabular output format for written artefacts",
    )

    fetch = sub.add_parser("fetch", parents=[data_args], help="download bars to CSV")
    fetch.add_argument(
        "--out",
        default=None,
        help="output path; an explicit extension wins over --format "
             "(default: data/<symbols>_<timeframe>.parquet)",
    )

    run_cmd = sub.add_parser("run", parents=[data_args], help="run a backtest")
    run_cmd.add_argument("--strategy", required=True, choices=list_strategies())
    run_cmd.add_argument(
        "--param", action="append", metavar="KEY=VALUE", help="strategy parameter (repeatable)"
    )
    run_cmd.add_argument("--cash", type=float, default=100_000.0, help="starting equity")
    run_cmd.add_argument("--slippage-bps", type=float, default=1.0)
    run_cmd.add_argument("--commission-per-share", type=float, default=0.0)
    run_cmd.add_argument("--commission-bps", type=float, default=0.0)
    run_cmd.add_argument("--cash-rate", type=float, default=0.0, help="annual rate on idle cash")
    run_cmd.add_argument("--borrow-rate", type=float, default=0.0, help="annual rate on debits")
    run_cmd.add_argument("--max-leverage", type=float, default=1.0, help="gross exposure cap")
    run_cmd.add_argument(
        "--whole-shares", action="store_true", help="disallow fractional share sizing"
    )
    run_cmd.add_argument("--benchmark", default=None, help="symbol to compare against, e.g. SPY")
    run_cmd.add_argument("--name", default=None, help="run directory name under out/")
    run_cmd.add_argument("--no-report", action="store_true", help="print stats only, write nothing")

    return parser


def cmd_check() -> int:
    settings = get_settings()
    print(f"credentials     from {settings.credential_source}")
    print(f"key id          {settings.api_key[:4]}…{settings.api_key[-4:]}")
    print(f"data feed       {settings.stock_feed}")
    print(f"trading host    {settings.trading_base_url} ({'paper' if settings.paper else 'LIVE'})")
    print(f"cache dir       {settings.cache_dir}")
    print(f"output dir      {settings.output_dir}")

    with AlpacaClient(settings) as client:
        end = pd.Timestamp.now(tz="America/New_York").date()
        start = end - pd.Timedelta(days=10)
        bars = client.stock_bars(["SPY"], "1Day", start.isoformat(), end.isoformat())
        count = len(bars.get("SPY", []))
        print(f"\nSPY daily bars in the last 10 days: {count}")
        if count:
            last = bars["SPY"][-1]
            print(f"latest bar      {last['t']}  close={last['c']}")
        sessions = client.calendar(start.isoformat(), end.isoformat())
        print(f"calendar rows   {len(sessions)}")
    print("\nOK — Alpaca market data is reachable.")
    return 0


def cmd_strategies() -> int:
    for name in list_strategies():
        cls = get_strategy(name)
        signature = inspect.signature(cls.__init__)
        params = ", ".join(
            f"{p.name}={p.default!r}"
            for p in list(signature.parameters.values())[1:]
            if p.default is not inspect.Parameter.empty
        )
        doc = (cls.__doc__ or "").strip().splitlines()[0] if cls.__doc__ else ""
        print(f"{name}\n    {doc}\n    params: {params or 'none'}\n")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    symbols = _symbols(args.symbols)
    panel = load_bars(
        symbols,
        args.start,
        args.end,
        args.timeframe,
        session=args.session,
        adjustment=args.adjustment,
        feed=args.feed,
        asset_class=args.asset_class,
        use_cache=not args.no_cache,
    )
    settings = get_settings()
    stem = f"{'-'.join(symbols[:4])}_{panel.timeframe}"
    if args.out:
        # An explicit extension is honoured; a bare path takes --format.
        out = Path(args.out)
        if not out.suffix:
            out = with_format(out, args.format)
    else:
        out = settings.cache_dir.parent / f"{stem}{suffix_for(args.format)}"
    written = write_frame(panel.frame, out, index_label="timestamp")
    print(panel)
    print(f"wrote {len(panel.frame):,} rows -> {written}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    symbols = _symbols(args.symbols)
    panel = load_bars(
        symbols,
        args.start,
        args.end,
        args.timeframe,
        session=args.session,
        adjustment=args.adjustment,
        feed=args.feed,
        asset_class=args.asset_class,
        use_cache=not args.no_cache,
    )

    strategy_cls = get_strategy(args.strategy)
    strategy = strategy_cls(**_params(args.param))

    result = run_backtest(
        strategy,
        panel,
        initial_cash=args.cash,
        costs=Costs(
            slippage_bps=args.slippage_bps,
            commission_per_share=args.commission_per_share,
            commission_bps=args.commission_bps,
            cash_rate=args.cash_rate,
            borrow_rate=args.borrow_rate,
        ),
        allow_fractional=not args.whole_shares,
        max_leverage=args.max_leverage,
    )

    benchmark_returns = None
    if args.benchmark:
        bench_panel = load_bars(
            [args.benchmark],
            args.start,
            args.end,
            args.timeframe,
            session=args.session,
            adjustment=args.adjustment,
            feed=args.feed,
            use_cache=not args.no_cache,
        )
        benchmark_returns = (
            bench_panel.close[args.benchmark.upper()]
            .reindex(result.equity.index)
            .ffill()
            .pct_change()
            .fillna(0.0)
        )

    print()
    print(result.summary(benchmark_returns))

    if not args.no_report:
        settings = get_settings()
        run_name = args.name or slugify(f"{args.strategy}-{'-'.join(symbols[:3])}-{args.start}")
        run_dir = write_report(
            result,
            settings.output_dir,
            run_name=run_name,
            benchmark=benchmark_returns,
            benchmark_label=args.benchmark.upper() if args.benchmark else "benchmark",
            extra_notes={
                "max leverage": f"{args.max_leverage}",
                "fractional shares": str(not args.whole_shares),
                "strategy params": ", ".join(f"{k}={v}" for k, v in _params(args.param).items())
                or "defaults",
            },
            fmt=args.format,
        )
        print(f"\nreport -> {run_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        if args.command == "check":
            return cmd_check()
        if args.command == "strategies":
            return cmd_strategies()
        if args.command == "fetch":
            return cmd_fetch(args)
        if args.command == "run":
            return cmd_run(args)
    except (ConfigError, DataError, AlpacaError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
