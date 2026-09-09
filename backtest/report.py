"""Turn a :class:`~backtest.engine.BacktestResult` into files on disk.

Produces, under ``out/<run-name>/``:

* ``equity.png``   — equity curve, drawdown, and exposure, stacked
* ``summary.md``   — the stats table and run parameters
* ``equity.parquet``   — per-bar equity, cash, returns, drawdown, exposure, turnover
* ``fills.parquet``    — every simulated trade
* ``weights.parquet``  — per-bar target weights actually held

Tabular artefacts default to parquet; pass ``fmt="csv"`` for CSV.

Charts use the Agg backend, so this works headless.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from backtest.engine import BacktestResult  # noqa: E402
from backtest.writers import DEFAULT_FORMAT, suffix_for, write_frame

#: Percent-formatted stats, everything else prints as a plain number.
_PERCENT_KEYS = {
    "total_return",
    "cagr",
    "volatility",
    "max_drawdown",
    "win_rate",
    "var_95",
    "cvar_95",
    "best_period",
    "worst_period",
    "avg_turnover",
    "alpha",
    "benchmark_return",
    "excess_return",
}
_DOLLAR_KEYS = {"total_commission", "total_slippage", "final_equity"}


def slugify(text: str) -> str:
    """Filesystem-safe run name."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80] or "run"


def plot_equity(
    result: BacktestResult,
    path: Path,
    benchmark: pd.Series | None = None,
    benchmark_label: str = "benchmark",
) -> Path:
    """Three stacked panels: equity (log), drawdown, gross/net exposure."""
    fig, axes = plt.subplots(
        3, 1, figsize=(11, 9), sharex=True, height_ratios=[3, 1.2, 1.2], constrained_layout=True
    )

    ax = axes[0]
    ax.plot(result.equity.index, result.equity.values, lw=1.4, label=result.strategy_name)
    if benchmark is not None and len(benchmark):
        curve = (1 + benchmark.reindex(result.equity.index).fillna(0.0)).cumprod()
        ax.plot(
            curve.index,
            curve.values * result.initial_cash,
            lw=1.1,
            ls="--",
            alpha=0.8,
            label=benchmark_label,
        )
    ax.set_yscale("log")
    ax.set_ylabel("equity ($, log)")
    ax.set_title(f"{result.strategy_name} — Alpaca {result.panel.timeframe} bars")
    ax.legend(loc="upper left", frameon=False)
    ax.grid(alpha=0.25)

    ax = axes[1]
    dd = result.drawdown
    ax.fill_between(dd.index, dd.values * 100, 0, alpha=0.4, color="tab:red", lw=0)
    ax.set_ylabel("drawdown (%)")
    ax.grid(alpha=0.25)

    ax = axes[2]
    ax.plot(result.gross_exposure.index, result.gross_exposure.values, lw=1.0, label="gross")
    ax.plot(
        result.net_exposure.index, result.net_exposure.values, lw=1.0, ls="--", label="net"
    )
    ax.axhline(0, color="black", lw=0.6)
    ax.set_ylabel("exposure (x equity)")
    ax.set_xlabel("date")
    ax.legend(loc="upper left", frameon=False, ncols=2)
    ax.grid(alpha=0.25)

    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return path


def format_stats(stats: dict[str, float]) -> str:
    """Stats dict -> markdown table."""
    rows = ["| metric | value |", "| --- | ---: |"]
    for key, value in stats.items():
        if key in _PERCENT_KEYS:
            shown = f"{value * 100:,.2f}%"
        elif key in _DOLLAR_KEYS:
            shown = f"${value:,.2f}"
        elif abs(value) == float("inf"):
            shown = "∞"
        elif float(value).is_integer() and abs(value) < 1e9:
            shown = f"{int(value):,}"
        else:
            shown = f"{value:,.4f}"
        rows.append(f"| {key.replace('_', ' ')} | {shown} |")
    return "\n".join(rows)


def write_report(
    result: BacktestResult,
    output_dir: Path,
    *,
    run_name: str | None = None,
    benchmark: pd.Series | None = None,
    benchmark_label: str = "benchmark",
    extra_notes: dict[str, str] | None = None,
    fmt: str = DEFAULT_FORMAT,
) -> Path:
    """Write the full artefact set. Returns the run directory.

    Tabular artefacts are written as ``fmt`` (parquet by default).
    """
    run_name = run_name or slugify(result.strategy_name)
    run_dir = Path(output_dir) / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    ext = suffix_for(fmt)
    write_frame(result.to_frame(), run_dir / f"equity{ext}", fmt=fmt)
    write_frame(result.fills, run_dir / f"fills{ext}", fmt=fmt, index=False)
    write_frame(result.weights, run_dir / f"weights{ext}", fmt=fmt)
    plot_equity(result, run_dir / "equity.png", benchmark, benchmark_label)

    stats = result.stats(benchmark)
    start, end = result.equity.index[0], result.equity.index[-1]
    notes = extra_notes or {}

    lines = [
        f"# {result.strategy_name}",
        "",
        f"_Generated {datetime.now().astimezone():%Y-%m-%d %H:%M %Z} — "
        "all price data from the Alpaca Market Data API._",
        "",
        "## Run",
        "",
        "| setting | value |",
        "| --- | --- |",
        f"| period | {start:%Y-%m-%d} → {end:%Y-%m-%d} ({len(result.equity):,} bars) |",
        f"| timeframe | {result.panel.timeframe} |",
        f"| symbols | {', '.join(result.panel.symbols)} |",
        f"| data feed | `{result.panel.feed or 'crypto'}` |",
        f"| initial cash | ${result.initial_cash:,.2f} |",
    ]
    for key, value in asdict(result.costs).items():
        lines.append(f"| {key.replace('_', ' ')} | {value} |")
    for key, value in notes.items():
        lines.append(f"| {key} | {value} |")

    lines += [
        "",
        "## Performance",
        "",
        format_stats(stats),
        "",
        "## Equity curve",
        "",
        "![equity curve](equity.png)",
        "",
        "## Files",
        "",
        f"- `equity{ext}` — per-bar equity, cash, returns, drawdown, exposure, turnover",
        f"- `fills{ext}` — every simulated trade with its slippage and commission",
        f"- `weights{ext}` — per-bar portfolio weights",
    ]
    if not result.fills.empty:
        recent = result.fills.tail(10).copy()
        recent["timestamp"] = recent["timestamp"].dt.strftime("%Y-%m-%d %H:%M")
        lines += ["", "## Last 10 fills", "", _markdown_table(recent)]

    (run_dir / "summary.md").write_text("\n".join(lines) + "\n")
    return run_dir


def _markdown_table(frame: pd.DataFrame) -> str:
    """Render a small DataFrame as markdown.

    Hand-rolled rather than ``DataFrame.to_markdown`` so the package does not
    need ``tabulate`` just to print ten rows.
    """
    def cell(value: object) -> str:
        return f"{value:,.4f}" if isinstance(value, float) else str(value)

    header = "| " + " | ".join(str(c) for c in frame.columns) + " |"
    rule = "| " + " | ".join("---" for _ in frame.columns) + " |"
    body = [
        "| " + " | ".join(cell(v) for v in row) + " |"
        for row in frame.itertuples(index=False, name=None)
    ]
    return "\n".join([header, rule, *body])
