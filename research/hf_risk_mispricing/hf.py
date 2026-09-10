"""High-frequency risk decomposition and mispricing — Choi, Kim & Lee (2026).

Partial replication. See hf_risk_mispricing.md for what is and is not replicable
and why; in short, the paper's characteristic projection cannot be run here (no
firm-characteristic panel exists in this repo), so Step 2 uses plain PCA — the
Pelger (2020) predecessor the paper builds on. That makes this a test of whether
the characteristics are what generate the reported performance.

Pipeline:

    intraday 30-min returns  (overnight excluded)
        |
        |  Lee & Mykland (2008) jump test, per stock, trailing bipower window
        v
    R_C (continuous)  +  R_J (jump)          -> RV / CV / JV, paper Table 1C
        |
        |  PCA per month: K_C factors on R_C, K_J factors on R_J
        v
    betas, and alpha = per-stock intercept orthogonal to both factor sets
        |
        |  w proportional to alpha, scaled to 10% annualised vol in month m
        v
    held through month m+1, rolled forward -> out-of-sample arbitrage portfolio
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

TRADING_DAYS = 252
BARS_PER_DAY = 13          # 09:30..15:30 inclusive, 30-minute bars
RETURNS_PER_DAY = BARS_PER_DAY - 1   # the overnight return is excluded
INTERVALS_PER_YEAR = TRADING_DAYS * RETURNS_PER_DAY


@dataclass(frozen=True)
class HFConfig:
    """Parameters. Defaults follow the paper where it states a value."""

    k_diffusive: int = 4          # PER test gives K_C = 4
    k_jump: int = 2               # PER test gives K_J = 2
    lm_window: int = 78           # Lee-Mykland K for 30-minute sampling
    lm_alpha: float = 0.05        # "conservative 5% tail of the Gumbel"
    target_vol: float = 0.10      # portfolio scaled to 10% annualised vol
    min_obs_per_month: int = 100  # stock-months with less are dropped
    min_price: float = 5.0
    volume_pctile: float = 0.25   # drop below the 25th pct of monthly volume


# --------------------------------------------------------------------------- #
# returns
# --------------------------------------------------------------------------- #

def intraday_returns(close: pd.DataFrame) -> pd.DataFrame:
    """Log returns between consecutive 30-minute bars, overnight excluded.

    The first bar of each session has no within-day predecessor, so its return
    is dropped rather than computed against the previous close. The paper does
    the same: its estimator assumes a continuous-time diffusion *within* the
    session, and the overnight gap is not one.
    """
    r = np.log(close).diff()
    day = close.index.normalize()
    first_of_day = day != pd.Series(day, index=close.index).shift(1)
    r.loc[first_of_day.to_numpy()] = np.nan
    return r


# --------------------------------------------------------------------------- #
# Lee & Mykland (2008) jump detection
# --------------------------------------------------------------------------- #

_C_CONST = np.sqrt(2.0 / np.pi)


def gumbel_threshold(n: int, alpha: float) -> float:
    """Critical value for max |L| over n observations under the no-jump null.

    Lee & Mykland show that the maximum of the standardised statistic converges
    to a Gumbel law. With c = sqrt(2/pi),

        C_n = (2 log n)^(1/2) / c  -  [log(pi) + log(log n)] / (2c (2 log n)^(1/2))
        S_n = 1 / (c (2 log n)^(1/2))

    and the test rejects when |L| > C_n + S_n * (-log(-log(1-alpha))).
    """
    if n < 3:
        return np.inf
    log_n = np.log(n)
    sqrt_2logn = np.sqrt(2.0 * log_n)
    c_n = sqrt_2logn / _C_CONST - (np.log(np.pi) + np.log(log_n)) / (
        2.0 * _C_CONST * sqrt_2logn)
    s_n = 1.0 / (_C_CONST * sqrt_2logn)
    return c_n + s_n * (-np.log(-np.log(1.0 - alpha)))


def local_volatility(returns: pd.Series, window: int) -> pd.Series:
    """Bipower-variation spot volatility over a trailing window.

    sigma_hat_t^2 = (1 / (K-2)) * sum |r_j| |r_{j-1}|  over the K-2 pairs
    strictly before t.

    Bipower variation is used rather than realised variance precisely because it
    is robust to the jumps being tested for — a squared-return estimator would
    inflate itself with the very jump it is supposed to flag, and mask it.
    """
    abs_r = returns.abs()
    bipower = abs_r * abs_r.shift(1)
    # shift(1) so the window ends strictly before t: no self-inclusion.
    mean_bp = bipower.shift(1).rolling(window - 2, min_periods=(window - 2) // 2).mean()
    return np.sqrt(mean_bp)


def detect_jumps(returns: pd.DataFrame, cfg: HFConfig) -> pd.DataFrame:
    """Boolean mask of detected jumps, per stock per interval."""
    mask = pd.DataFrame(False, index=returns.index, columns=returns.columns)
    n = int(returns.notna().sum().median())
    thresh = gumbel_threshold(max(n, 3), cfg.lm_alpha)
    for sym in returns.columns:
        r = returns[sym]
        sigma = local_volatility(r, cfg.lm_window)
        stat = (r / sigma).abs()
        mask[sym] = (stat > thresh) & r.notna()
    return mask


def decompose(returns: pd.DataFrame, jumps: pd.DataFrame):
    """Split the panel into continuous and jump components."""
    r_j = returns.where(jumps, 0.0).where(returns.notna())
    r_c = returns.where(~jumps, 0.0).where(returns.notna())
    return r_c, r_j


def month_index(index: pd.DatetimeIndex) -> pd.PeriodIndex:
    """Month periods from a tz-aware intraday index, without the tz warning."""
    idx = index.tz_localize(None) if index.tz is not None else index
    return idx.to_period("M")


def variation_stats(returns: pd.DataFrame, jumps: pd.DataFrame) -> pd.DataFrame:
    """Per stock-month RV / CV / JV — the paper's Table 1 Panel C."""
    month = month_index(returns.index)
    sq = returns.pow(2)
    rv = sq.groupby(month).sum(min_count=1)
    jv = sq.where(jumps).groupby(month).sum(min_count=1).fillna(0.0)
    cnt = jumps.groupby(month).sum()
    absj = returns.abs().where(jumps).groupby(month).mean()
    nobs = returns.notna().groupby(month).sum()

    out = pd.concat({
        "rv": rv.stack(), "jv": jv.stack(), "n_obs": nobs.stack(),
        "jump_count": cnt.stack(), "mean_abs_jump": absj.stack(),
    }, axis=1)
    out.index.names = ["month", "symbol"]
    out["cv"] = out["rv"] - out["jv"]
    out["jump_share"] = out["jv"] / out["rv"].where(out["rv"] > 0)
    return out


# --------------------------------------------------------------------------- #
# factor structure and mispricing
# --------------------------------------------------------------------------- #

def pca_factors(panel: np.ndarray, k: int):
    """Top-k principal components of an (N x T) panel.

    Returns (factors k x T, loadings N x k). Uses the SVD of the panel directly,
    which is the standard high-frequency estimator: with N large relative to k
    the leading singular vectors recover the factor space consistently.
    """
    if k <= 0 or panel.size == 0:
        return np.zeros((0, panel.shape[1])), np.zeros((panel.shape[0], 0))
    x = np.nan_to_num(panel, nan=0.0)
    u, s, vt = np.linalg.svd(x, full_matrices=False)
    k = min(k, len(s))
    factors = vt[:k] * s[:k, None] / np.sqrt(x.shape[0])
    loadings = u[:, :k] * np.sqrt(x.shape[0])
    return factors, loadings


def estimate_month(r_c: pd.DataFrame, r_j: pd.DataFrame, r_all: pd.DataFrame,
                   cfg: HFConfig) -> pd.DataFrame:
    """One month: factors, betas, pseudo-R^2, and per-stock mispricing alpha.

    alpha is the intercept of stock i's intraday returns on the estimated
    diffusive and jump factors — the average return left unexplained by the
    factor structure, in return units per 30-minute interval.
    """
    cols = r_all.columns
    fc, bc = pca_factors(r_c.to_numpy().T, cfg.k_diffusive)
    fj, bj = pca_factors(r_j.to_numpy().T, cfg.k_jump)

    design = [np.ones(len(r_all))]
    if fc.shape[0]:
        design.append(fc.T)
    if fj.shape[0]:
        design.append(fj.T)
    X = np.column_stack(design)

    Y = np.nan_to_num(r_all.to_numpy(), nan=0.0)          # T x N
    coef, *_ = np.linalg.lstsq(X, Y, rcond=None)
    resid = Y - X @ coef
    alpha = coef[0]

    tot = (Y ** 2).sum(axis=0)
    sys_ = ((X[:, 1:] @ coef[1:]) ** 2).sum(axis=0)
    pseudo_r2 = np.divide(sys_, tot, out=np.zeros_like(tot), where=tot > 0)

    n_c = cfg.k_diffusive
    return pd.DataFrame({
        "alpha": alpha,
        "resid_vol": resid.std(axis=0, ddof=1),
        "pseudo_r2": pseudo_r2,
        "beta_c_1": coef[1] if n_c and coef.shape[0] > 1 else np.nan,
        "beta_j_1": coef[1 + n_c] if cfg.k_jump and coef.shape[0] > 1 + n_c else np.nan,
    }, index=cols)


# --------------------------------------------------------------------------- #
# the intraday arbitrage portfolio
# --------------------------------------------------------------------------- #

def build_weights(alpha: pd.Series, month_returns: pd.DataFrame,
                  cfg: HFConfig) -> pd.Series:
    """Dollar-neutral weights proportional to estimated mispricing.

    Scaled so the portfolio would have had `target_vol` annualised volatility
    **in the estimation month** — using that month's realised covariance only,
    never the holding month's.
    """
    w = alpha - alpha.mean()          # dollar neutral
    norm = w.abs().sum()
    if not np.isfinite(norm) or norm == 0:
        return pd.Series(0.0, index=alpha.index)
    w = w / norm

    port = month_returns.reindex(columns=w.index).fillna(0.0).to_numpy() @ w.to_numpy()
    realised = port.std(ddof=1) * np.sqrt(INTERVALS_PER_YEAR)
    if realised > 0:
        w = w * (cfg.target_vol / realised)
    return w
