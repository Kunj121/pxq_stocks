"""The paper's pipeline: standardise -> PCA -> K-Means -> label -> test -> trade.

Four steps that the paper insists are four separate questions, and that this
module keeps separate on purpose:

1. :func:`fit_regimes`      -- unsupervised identification (Sections V.B-V.F)
2. :func:`regime_stats`,
   :func:`transition_matrix`,
   :func:`episodes`          -- description (Sections VI.C-VI.F)
3. :func:`forecast`          -- one-step-ahead prediction vs persistence (V.I)
4. :func:`tactical_strategy` -- economic usefulness (Section V.J)

The 70/30 split is chronological, and every estimator -- scaler, PCA, K-Means,
GMM, classifiers -- is fit on the training slice alone and then applied without
re-estimation.  That is the paper's own design (Section V.B), and it is also the
limit of its causality: the labels remain *ex-post* descriptions, because the
Bull/Bear naming uses each cluster's full-sample mean return (Section V.F) and
the training window is a single block rather than an expanding one (XI.A).
:func:`tactical_strategy` therefore inherits a look-ahead the paper acknowledges
and does not remove; see ``regime_detection.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import f1_score, precision_score, recall_score
from sklearn.mixture import GaussianMixture
from sklearn.preprocessing import StandardScaler

from research.regime_detection.features import FEATURES

TRADING_DAYS = 252
BULL, BEAR = "Bull / Low-Risk", "Bear / High-Risk"
TRAIN_FRACTION = 0.70
RANDOM_STATE = 42
VARIANCE_TARGET = 0.90


# -- 1. unsupervised identification ------------------------------------------


@dataclass
class RegimeFit:
    """Everything the clustering step produced, on the full sample."""

    labels: pd.Series  # BULL / BEAR per date
    cluster: pd.Series  # raw K-Means integer per date
    scores: pd.DataFrame  # retained PCA scores per date
    n_components: int
    explained_variance: pd.DataFrame  # per-component and cumulative
    loadings: pd.DataFrame  # feature x component
    selection: pd.DataFrame  # K, inertia, silhouette on the training slice
    k: int
    gmm_labels: pd.Series
    gmm_agreement: float
    split_date: pd.Timestamp
    feature_names: list[str] = field(default_factory=list)


def train_mask(index: pd.Index, fraction: float = TRAIN_FRACTION) -> np.ndarray:
    """Boolean mask for the chronological first ``fraction`` of the sample."""
    cut = int(len(index) * fraction)
    mask = np.zeros(len(index), dtype=bool)
    mask[:cut] = True
    return mask


def select_k(
    train_scores: np.ndarray, k_range: range = range(2, 7), random_state: int = RANDOM_STATE
) -> pd.DataFrame:
    """Inertia and silhouette for each candidate K, on the training scores.

    Reproduces Table 3.  Silhouette is the paper's selection criterion.
    """
    from sklearn.metrics import calinski_harabasz_score, davies_bouldin_score, silhouette_score

    rows = []
    for k in k_range:
        model = KMeans(n_clusters=k, n_init=50, random_state=random_state).fit(train_scores)
        rows.append(
            {
                "K": k,
                "inertia": model.inertia_,
                "silhouette": silhouette_score(train_scores, model.labels_),
                "calinski_harabasz": calinski_harabasz_score(train_scores, model.labels_),
                "davies_bouldin": davies_bouldin_score(train_scores, model.labels_),
            }
        )
    return pd.DataFrame(rows).set_index("K")


def fit_regimes(
    frame: pd.DataFrame,
    feature_names: list[str] | tuple[str, ...] = FEATURES,
    k: int | None = None,
    variance_target: float = VARIANCE_TARGET,
    random_state: int = RANDOM_STATE,
) -> RegimeFit:
    """Standardise, reduce, cluster, and label -- Sections V.B through V.F.

    ``k=None`` selects K by the best training-period silhouette, as the paper
    does.  Pass ``k`` explicitly to force a K (used by the Section XI.J check,
    where the three internal metrics disagree).
    """
    feature_names = list(feature_names)
    matrix = frame[feature_names].to_numpy(dtype=float)
    is_train = train_mask(frame.index)

    scaler = StandardScaler().fit(matrix[is_train])
    scaled = scaler.transform(matrix)

    # Smallest component count reaching the variance target, chosen on train.
    probe = PCA(random_state=random_state).fit(scaled[is_train])
    cumulative = np.cumsum(probe.explained_variance_ratio_)
    n_components = int(np.searchsorted(cumulative, variance_target) + 1)

    pca = PCA(n_components=n_components, random_state=random_state).fit(scaled[is_train])
    scores = pca.transform(scaled)

    selection = select_k(scores[is_train], random_state=random_state)
    chosen = int(selection["silhouette"].idxmax()) if k is None else int(k)

    kmeans = KMeans(n_clusters=chosen, n_init=50, random_state=random_state).fit(scores[is_train])
    cluster = pd.Series(kmeans.predict(scores), index=frame.index, name="cluster")

    # Section V.F: the higher-mean-return cluster is Bull.  Ex-post by design.
    mean_return = frame["Return"].groupby(cluster).mean().sort_values(ascending=False)
    if chosen == 2:
        naming = {mean_return.index[0]: BULL, mean_return.index[-1]: BEAR}
    else:
        naming = {c: f"R{rank}" for rank, c in enumerate(mean_return.index)}
    labels = cluster.map(naming).rename("regime")

    gmm = GaussianMixture(
        n_components=chosen, covariance_type="full", n_init=5, random_state=random_state
    ).fit(scores[is_train])
    gmm_labels = pd.Series(gmm.predict(scores), index=frame.index, name="gmm_cluster")
    agreement = float((gmm_labels.to_numpy() == cluster.to_numpy()).mean())
    # Labels are arbitrary integers, so the complement is equally valid for K=2.
    if chosen == 2:
        agreement = max(agreement, 1.0 - agreement)

    variance = pd.DataFrame(
        {
            "explained_variance": probe.explained_variance_ratio_,
            "cumulative": cumulative,
        },
        index=[f"PC{i}" for i in range(1, len(cumulative) + 1)],
    )
    loadings = pd.DataFrame(
        pca.components_.T,
        index=feature_names,
        columns=[f"PC{i}" for i in range(1, n_components + 1)],
    )

    return RegimeFit(
        labels=labels,
        cluster=cluster,
        scores=pd.DataFrame(
            scores, index=frame.index, columns=[f"PC{i}" for i in range(1, n_components + 1)]
        ),
        n_components=n_components,
        explained_variance=variance,
        loadings=loadings,
        selection=selection,
        k=chosen,
        gmm_labels=gmm_labels,
        gmm_agreement=agreement,
        split_date=frame.index[is_train][-1],
        feature_names=feature_names,
    )


# -- 2. description ----------------------------------------------------------


def regime_stats(frame: pd.DataFrame, labels: pd.Series) -> pd.DataFrame:
    """Table 4: descriptive statistics per regime, full sample."""
    grouped = frame.assign(regime=labels).groupby("regime")
    out = pd.DataFrame(
        {
            "observations": grouped.size(),
            "share": grouped.size() / len(frame),
            "mean_daily_return": grouped["Return"].mean(),
            "median_daily_return": grouped["Return"].median(),
            "std_daily_return": grouped["Return"].std(),
            "annualised_volatility": grouped["Return"].std() * np.sqrt(TRADING_DAYS),
            "mean_vix": grouped["VIX"].mean(),
            "mean_drawdown": grouped["Drawdown"].mean(),
            "mean_momentum_20d": grouped["Momentum_20D"].mean(),
            "mean_momentum_60d": grouped["Momentum_60D"].mean(),
        }
    )
    order = [r for r in (BULL, BEAR) if r in out.index]
    return out.loc[order] if order else out


def cohens_d(a: np.ndarray, b: np.ndarray) -> float:
    """Pooled-standard-deviation effect size, sign following ``a - b``."""
    na, nb = len(a), len(b)
    pooled = np.sqrt(((na - 1) * a.var(ddof=1) + (nb - 1) * b.var(ddof=1)) / (na + nb - 2))
    return float((a.mean() - b.mean()) / pooled)


def significance_tests(
    frame: pd.DataFrame,
    labels: pd.Series,
    variables: list[str] | None = None,
) -> pd.DataFrame:
    """Table 5: Welch t, Mann-Whitney U, and Cohen's d, Bull versus Bear."""
    variables = variables or [
        "Return",
        "VIX",
        "Volatility_20D",
        "Volatility_60D",
        "Drawdown",
        "Momentum_20D",
        "Momentum_60D",
        "Distance_MA200",
    ]
    bull = frame[labels == BULL]
    bear = frame[labels == BEAR]
    rows = []
    for name in variables:
        a, b = bull[name].to_numpy(), bear[name].to_numpy()
        t_stat, t_p = stats.ttest_ind(a, b, equal_var=False)
        u_stat, u_p = stats.mannwhitneyu(a, b, alternative="two-sided")
        rows.append(
            {
                "variable": name,
                "bull_mean": a.mean(),
                "bear_mean": b.mean(),
                "welch_t": t_stat,
                "welch_p": t_p,
                "mannwhitney_p": u_p,
                "cohens_d": cohens_d(a, b),
            }
        )
    return pd.DataFrame(rows).set_index("variable")


def bootstrap_return_gap(
    frame: pd.DataFrame,
    labels: pd.Series,
    resamples: int = 5000,
    random_state: int = RANDOM_STATE,
) -> dict[str, float]:
    """Section VI.E: distribution-free CI on the Bull-minus-Bear mean return."""
    rng = np.random.default_rng(random_state)
    bull = frame.loc[labels == BULL, "Return"].to_numpy()
    bear = frame.loc[labels == BEAR, "Return"].to_numpy()
    draws = np.empty(resamples)
    for i in range(resamples):
        draws[i] = rng.choice(bull, bull.size, replace=True).mean() - rng.choice(
            bear, bear.size, replace=True
        ).mean()
    return {
        "observed": float(bull.mean() - bear.mean()),
        "ci_low": float(np.percentile(draws, 2.5)),
        "ci_high": float(np.percentile(draws, 97.5)),
    }


def transition_matrix(labels: pd.Series) -> pd.DataFrame:
    """Table 6: row-normalised first-order transition probabilities."""
    counts = pd.crosstab(labels.iloc[:-1].to_numpy(), labels.iloc[1:].to_numpy())
    matrix = counts.div(counts.sum(axis=1), axis=0)
    matrix.index.name, matrix.columns.name = "current", "next"
    order = [r for r in (BULL, BEAR) if r in matrix.index]
    return matrix.loc[order, order] if len(order) == 2 else matrix


def episodes(labels: pd.Series) -> pd.DataFrame:
    """Table 7: one row per contiguous run of the same regime label."""
    run_id = (labels != labels.shift()).cumsum()
    grouped = labels.groupby(run_id)
    return pd.DataFrame(
        {
            "regime": grouped.first(),
            "start": grouped.apply(lambda s: s.index[0]),
            "end": grouped.apply(lambda s: s.index[-1]),
            "trading_days": grouped.size(),
        }
    ).reset_index(drop=True)


def episode_summary(labels: pd.Series) -> pd.DataFrame:
    """Table 7 as the paper reports it: count, mean, median, min, max."""
    runs = episodes(labels)
    out = runs.groupby("regime")["trading_days"].agg(
        episodes="size", mean="mean", median="median", min="min", max="max"
    )
    order = [r for r in (BEAR, BULL) if r in out.index]
    return out.loc[order] if order else out


# -- 3. one-step-ahead forecasting -------------------------------------------


def forecast(
    frame: pd.DataFrame,
    labels: pd.Series,
    feature_names: list[str] | tuple[str, ...] = FEATURES,
    random_state: int = RANDOM_STATE,
) -> tuple[pd.DataFrame, pd.Series]:
    """Table 8: predict *tomorrow's* regime from *today's* features.

    The design that makes this non-circular (Section V.I): X is day *t*'s feature
    row, y is day *t+1*'s label.  Every model, and the persistence baseline, is
    scored on the identical held-out rows, so the comparison is like for like.

    Returns the score table and the Random Forest's feature importances (Table 9).
    """
    feature_names = list(feature_names)
    binary = (labels == BEAR).astype(int)  # 1 = Bear, so Bull is the positive class at 0

    x = frame[feature_names].iloc[:-1]
    y_next = binary.shift(-1).dropna().astype(int)
    y_today = binary.iloc[:-1]
    x, y_next, y_today = x.align(y_next, join="inner", axis=0)[0], y_next, y_today.loc[x.index]

    is_train = train_mask(x.index)
    x_train, x_test = x[is_train], x[~is_train]
    y_train, y_test = y_next[is_train], y_next[~is_train]

    scaler = StandardScaler().fit(x_train)
    x_train_s, x_test_s = scaler.transform(x_train), scaler.transform(x_test)

    models = {
        "Logistic Regression": (LogisticRegression(max_iter=2000, random_state=random_state), True),
        "Random Forest": (
            RandomForestClassifier(n_estimators=300, random_state=random_state, n_jobs=-1),
            False,
        ),
        "Gradient Boosting": (GradientBoostingClassifier(random_state=random_state), False),
    }

    # Positive class is Bull (label 0), matching the paper's precision == recall
    # figure for a baseline that is right about the majority class.
    def score(name: str, prediction: np.ndarray) -> dict[str, float | str]:
        truth = y_test.to_numpy()
        bull_truth, bull_pred = (truth == 0).astype(int), (prediction == 0).astype(int)
        return {
            "model": name,
            "accuracy": float((prediction == truth).mean()),
            "precision_bull": precision_score(bull_truth, bull_pred, zero_division=0),
            "recall_bull": recall_score(bull_truth, bull_pred, zero_division=0),
            "f1_bull": f1_score(bull_truth, bull_pred, zero_division=0),
            "recall_bear": recall_score(truth, prediction, zero_division=0),
        }

    rows = [score("Persistence Baseline", y_today[~is_train].to_numpy())]
    importances = pd.Series(dtype=float)
    for name, (model, scale) in models.items():
        model.fit(x_train_s if scale else x_train, y_train)
        prediction = model.predict(x_test_s if scale else x_test)
        rows.append(score(name, prediction))
        if name == "Random Forest":
            importances = pd.Series(
                model.feature_importances_, index=feature_names, name="importance"
            ).sort_values(ascending=False)

    return pd.DataFrame(rows).set_index("model"), importances


# -- 4. economic usefulness --------------------------------------------------


def tactical_strategy(
    frame: pd.DataFrame, labels: pd.Series, cost_bps: float = 10.0
) -> pd.DataFrame:
    """Section V.J: 100% invested after Bull, 0% after Bear, signal lagged a day.

    The lag is the whole point -- today's exposure is set by *yesterday's* label,
    so a day's own return never decides that day's position.  Cost is charged on
    every unit of position change.
    """
    exposure = (labels.shift(1) == BULL).astype(float)
    gross = exposure * frame["Return"]
    turnover = exposure.diff().abs().fillna(exposure.abs())
    net = gross - turnover * (cost_bps / 10_000.0)
    return pd.DataFrame(
        {
            "exposure": exposure,
            "benchmark_return": frame["Return"],
            "strategy_return": net,
            "turnover": turnover,
            "benchmark_equity": (1.0 + frame["Return"]).cumprod(),
            "strategy_equity": (1.0 + net).cumprod(),
        }
    )


def performance(returns: pd.Series, periods: int = TRADING_DAYS) -> dict[str, float]:
    """Total return, CAGR, volatility, Sharpe, max drawdown, Calmar, hit rate."""
    returns = returns.dropna()
    equity = (1.0 + returns).cumprod()
    years = len(returns) / periods
    total = float(equity.iloc[-1] - 1.0)
    cagr = float(equity.iloc[-1] ** (1.0 / years) - 1.0)
    volatility = float(returns.std() * np.sqrt(periods))
    drawdown = float((equity / equity.cummax() - 1.0).min())
    return {
        "total_return": total,
        "cagr": cagr,
        "annualised_volatility": volatility,
        "sharpe": float(returns.mean() / returns.std() * np.sqrt(periods)) if returns.std() else 0.0,
        "max_drawdown": drawdown,
        "calmar": cagr / abs(drawdown) if drawdown else np.nan,
        "pct_positive_days": float((returns > 0).mean()),
    }


def annual_returns(result: pd.DataFrame) -> pd.DataFrame:
    """Table 11: calendar-year strategy and benchmark returns, plus exposure."""
    by_year = result.groupby(result.index.year)
    return pd.DataFrame(
        {
            "strategy_return": by_year["strategy_return"].apply(lambda s: (1 + s).prod() - 1),
            "benchmark_return": by_year["benchmark_return"].apply(lambda s: (1 + s).prod() - 1),
            "avg_exposure": by_year["exposure"].mean(),
        }
    )


def forward_returns(
    frame: pd.DataFrame, labels: pd.Series, horizons: tuple[int, ...] = (1, 5, 20)
) -> pd.DataFrame:
    """Table 12: mean forward return and hit rate by regime and horizon.

    This is the paper's explanation for the strategy's shortfall, and it is a
    genuinely forward-looking measurement -- the regime is known on day *t*, the
    return is realised over *t+1 .. t+h*.
    """
    close = frame["close"]
    rows = []
    for horizon in horizons:
        forward = close.shift(-horizon) / close - 1.0
        for regime, group in forward.groupby(labels):
            group = group.dropna()
            rows.append(
                {
                    "regime": regime,
                    "horizon": horizon,
                    "mean_forward_return": group.mean(),
                    "pct_positive": float((group > 0).mean()),
                    "observations": int(group.size),
                }
            )
    return pd.DataFrame(rows).set_index(["regime", "horizon"]).sort_index()
