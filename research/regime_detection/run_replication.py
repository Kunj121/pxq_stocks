"""Produce every artefact the replication reports, as parquet under ``out/``.

    python -m research.regime_detection.run_replication

Reads ``data/regime_panel.parquet`` (written by ``fetch_data.py``) and writes,
one file per table the paper reports, so the notebook and the write-up read the
same numbers rather than each recomputing them:

    regimes.parquet             per-day regime label, PCA scores, features
    pca_variance.parquet        Table 2
    pca_loadings.parquet        Table A1
    k_selection.parquet         Table 3
    regime_stats.parquet        Table 4
    significance.parquet        Table 5
    transitions.parquet         Table 6
    episodes.parquet            Table 7 (one row per episode)
    episode_summary.parquet     Table 7 (as reported)
    forecast_scores.parquet     Table 8
    rf_importance.parquet       Table 9
    strategy.parquet            per-day equity curves, Figures 7-8
    performance.parquet         Table 10
    annual_returns.parquet      Table 11
    forward_returns.parquet     Table 12
    widened_k_selection.parquet Table 13
    widened_shift.parquet       Section XI.J's train/test distribution shift
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from research.regime_detection import pipeline as pl
from research.regime_detection.features import build_features, widened_features

REPO = Path(__file__).resolve().parents[2]
PANEL = REPO / "data" / "regime_panel.parquet"
OUT = REPO / "out" / "regime_detection"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    frame = build_features(pd.read_parquet(PANEL))
    fit = pl.fit_regimes(frame)
    labels = fit.labels

    written: dict[str, int] = {}

    def write(name: str, obj: pd.DataFrame | pd.Series) -> None:
        frame_obj = obj.to_frame() if isinstance(obj, pd.Series) else obj
        frame_obj.to_parquet(OUT / f"{name}.parquet")
        written[name] = len(frame_obj)

    write("regimes", frame.assign(regime=labels, cluster=fit.cluster).join(fit.scores))
    write("pca_variance", fit.explained_variance)
    write("pca_loadings", fit.loadings)
    write("k_selection", fit.selection)
    write("regime_stats", pl.regime_stats(frame, labels))
    write("significance", pl.significance_tests(frame, labels))
    write("transitions", pl.transition_matrix(labels))
    write("episodes", pl.episodes(labels))
    write("episode_summary", pl.episode_summary(labels))

    scores, importance = pl.forecast(frame, labels)
    write("forecast_scores", scores)
    write("rf_importance", importance)

    result = pl.tactical_strategy(frame, labels)
    write("strategy", result)
    write(
        "performance",
        pd.DataFrame(
            {
                "regime_strategy": pl.performance(result["strategy_return"]),
                "buy_and_hold": pl.performance(result["benchmark_return"]),
            }
        ),
    )
    write("annual_returns", pl.annual_returns(result))
    write("forward_returns", pl.forward_returns(frame, labels))

    # Section XI.J: restore the non-stationary levels and watch it degrade.
    wide_names = widened_features(frame)
    wide = pl.fit_regimes(frame, wide_names)
    write("widened_k_selection", wide.selection)
    wide4 = pl.fit_regimes(frame, wide_names, k=4)
    is_train = pl.train_mask(frame.index)
    write(
        "widened_shift",
        pd.DataFrame(
            {
                "train_share": wide4.labels[is_train].value_counts(normalize=True),
                "test_share": wide4.labels[~is_train].value_counts(normalize=True),
            }
        ).fillna(0.0),
    )

    summary = {
        "observations": int(len(frame)),
        "start": str(frame.index.min().date()),
        "end": str(frame.index.max().date()),
        "k": fit.k,
        "n_components": fit.n_components,
        "cumulative_variance": float(fit.explained_variance["cumulative"].iloc[fit.n_components - 1]),
        "train_test_split_date": str(fit.split_date.date()),
        "gmm_agreement": fit.gmm_agreement,
        "bootstrap": pl.bootstrap_return_gap(frame, labels),
        "position_changes": int(result["turnover"].sum()),
        "avg_exposure": float(result["exposure"].mean()),
        "widened_silhouette_best": float(wide.selection["silhouette"].max()),
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2))

    for name, rows in written.items():
        print(f"{rows:>6,} rows -> out/regime_detection/{name}.parquet")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
