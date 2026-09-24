"""Tests for the golden-sweep alerter. No network, no Slack.

    python -m pytest research/quantdata/test_sweep_alerts.py -q

The screen is the product here, so most of these are about which prints get
through it. The direction cases matter most: a CALL is not automatically
bullish, and getting that backwards inverts the majority of real signals.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import sweep_alerts as A  # noqa: E402


def print_row(**over):
    """One sweep print, qualifying by default. Override a field to break it."""
    row = {
        "id": "p1",
        "ts": pd.Timestamp("2026-09-22 13:27:20", tz="America/New_York"),
        "ticker": "NVDA",
        "contractType": "CALL",
        "tradeSideCode": "ASK",
        "strikePrice": 180.0,
        "expirationDate": "2026-10-16",
        "dte": 24,
        "greeks.delta": 0.52,
        "impliedVolatility": 72.813,
        "premium": 1_500_000.0,
        "optionPrice": 5.0,
        "stockPrice": 178.0,
        "size": 100,
        "volume": 250,
        "openInterest": 900,
        "isOpeningPosition": True,
        "sector": "Technology",
        "isETF": False,
        "isIndex": False,
        "isGoldenSweep": False,
        "isUnusual": False,
        "tradeType": "ISO",
        "tradeConsolidationType": "SWEEP",
        "session": "2026-09-22",
    }
    row.update(over)
    return row


def flow(*rows):
    return pd.DataFrame(list(rows) or [print_row()])


# -- the screen ------------------------------------------------------------

def test_a_qualifying_print_passes():
    assert len(A.screen(flow())) == 1


@pytest.mark.parametrize("delta", [0.29, 0.71, 0.05, 0.95])
def test_delta_outside_the_band_is_rejected(delta):
    assert len(A.screen(flow(print_row(**{"greeks.delta": delta})))) == 0


@pytest.mark.parametrize("delta", [0.30, 0.50, 0.70])
def test_delta_band_is_inclusive_at_both_ends(delta):
    assert len(A.screen(flow(print_row(**{"greeks.delta": delta})))) == 1


def test_negative_delta_is_compared_by_magnitude():
    """Puts carry a negative delta; the band is on |delta|."""
    row = print_row(contractType="PUT", **{"greeks.delta": -0.52})
    assert len(A.screen(flow(row))) == 1


def test_etf_is_excluded():
    assert len(A.screen(flow(print_row(ticker="QQQ", isETF=True)))) == 0


def test_index_is_excluded():
    assert len(A.screen(flow(print_row(ticker="NDX", isIndex=True)))) == 0


def test_funds_can_be_kept_deliberately():
    got = A.screen(flow(print_row(ticker="QQQ", isETF=True)), exclude_funds=False)
    assert len(got) == 1


def test_premium_below_the_floor_is_rejected():
    assert len(A.screen(flow(print_row(premium=999_999.0)))) == 0


def test_premium_floor_is_inclusive():
    assert len(A.screen(flow(print_row(premium=1_000_000.0)))) == 1


def test_mid_market_has_no_aggressor_and_is_dropped():
    """No side means no direction. Dropped, never guessed."""
    assert len(A.screen(flow(print_row(tradeSideCode="MID_MARKET")))) == 0


def test_named_tickers_can_be_suppressed():
    got = A.screen(flow(print_row(ticker="SNDK")), exclude_tickers=("SNDK",))
    assert len(got) == 0


def test_sndk_is_not_suppressed_by_default():
    """directional.triggers() drops SNDK on cost. An alert is not a position."""
    assert len(A.screen(flow(print_row(ticker="SNDK")))) == 1


def test_empty_input_survives():
    assert len(A.screen(pd.DataFrame())) == 0


# -- direction: contractType x tradeSideCode -------------------------------

@pytest.mark.parametrize("right,side,expected", [
    ("CALL", "ASK",       "BULL"),   # bought calls
    ("CALL", "ABOVE_ASK", "BULL"),
    ("CALL", "BID",       "BEAR"),   # sold calls — a CALL that is not bullish
    ("CALL", "BELOW_BID", "BEAR"),
    ("PUT",  "ASK",       "BEAR"),   # bought puts
    ("PUT",  "BID",       "BULL"),   # sold puts
])
def test_direction_reads_the_aggressor_side_not_just_call_put(right, side, expected):
    row = print_row(contractType=right, tradeSideCode=side,
                    **{"greeks.delta": 0.52 if right == "CALL" else -0.52})
    assert A.screen(flow(row)).iloc[0]["direction"] == expected


# -- message ----------------------------------------------------------------

def test_alert_names_ticker_strike_expiry_and_type():
    row = A.screen(flow()).iloc[0]
    text = A.format_alert(row)
    for expected in ("NVDA", "CALL", "$180", "2026-10-16", "BULL", "0.52"):
        assert expected in text, f"{expected!r} missing from {text!r}"


def test_alert_shows_a_sold_call_as_bearish():
    row = A.screen(flow(print_row(tradeSideCode="BID"))).iloc[0]
    text = A.format_alert(row)
    assert "BEAR" in text and "hit the bid" in text


def test_strike_drops_trailing_zeros_but_keeps_real_cents():
    whole = A.format_alert(A.screen(flow(print_row(strikePrice=180.0))).iloc[0])
    cents = A.format_alert(A.screen(flow(print_row(strikePrice=11.5))).iloc[0])
    assert "$180 " in whole
    assert "$11.5 " in cents


# -- ledger and dedup -------------------------------------------------------

def test_ledger_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "ALERT_DIR", tmp_path)
    A.append_ledger("2026-09-22", A.screen(flow()))
    assert set(A.load_ledger("2026-09-22")["id"]) == {"p1"}


def test_ledger_is_empty_for_an_unseen_session(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "ALERT_DIR", tmp_path)
    assert len(A.load_ledger("1999-01-01")) == 0


def test_poll_suppresses_ids_already_seen(monkeypatch):
    monkeypatch.setattr(A.F, "sweeps",
                        lambda *a, **k: flow(print_row(id="p1"), print_row(id="p2")))

    assert set(A.poll("2026-09-22", set(), 1_000_000)["id"]) == {"p1", "p2"}
    assert set(A.poll("2026-09-22", {"p1"}, 1_000_000)["id"]) == {"p2"}
    assert len(A.poll("2026-09-22", {"p1", "p2"}, 1_000_000)) == 0


def test_poll_on_an_empty_feed_returns_nothing(monkeypatch):
    monkeypatch.setattr(A.F, "sweeps", lambda *a, **k: pd.DataFrame())
    assert len(A.poll("2026-09-22", set(), 1_000_000)) == 0


# -- market hours -----------------------------------------------------------

@pytest.mark.parametrize("when,expected", [
    ("2026-09-22 09:29", False),   # Tuesday, pre-open
    ("2026-09-22 09:30", True),
    ("2026-09-22 12:00", True),
    ("2026-09-22 16:00", True),
    ("2026-09-22 16:01", False),
    ("2026-09-18 12:00", True),    # Friday
    ("2026-09-19 12:00", False),   # Saturday
    ("2026-09-20 12:00", False),   # Sunday
    ("2026-09-21 12:00", True),    # Monday
])
def test_market_hours(when, expected):
    now = pd.Timestamp(when, tz="America/New_York")
    assert A.market_is_open(now) is expected


# -- spot, size, volume, open interest --------------------------------------

def test_alert_carries_spot_size_volume_and_open_interest():
    row = A.screen(flow(print_row(stockPrice=178.42, size=100,
                                  volume=250, openInterest=900))).iloc[0]
    text = A.format_alert(row)
    assert "spot $178.42" in text
    assert "size 100" in text
    assert "vol 250 vs OI 900" in text


def test_large_counts_get_thousands_separators():
    row = A.screen(flow(print_row(size=2509, volume=3101,
                                  openInterest=19906))).iloc[0]
    text = A.format_alert(row)
    assert "size 2,509" in text and "vol 3,101 vs OI 19,906" in text


def test_spot_keeps_two_decimals():
    """A price is not a count: 87.2 must not render as 87."""
    row = A.screen(flow(print_row(stockPrice=87.2))).iloc[0]
    assert "spot $87.20" in A.format_alert(row)


def test_a_missing_field_degrades_to_a_dash_not_a_crash():
    """The vendor populated these on every print observed, but an alert must
    still send if one goes null — a missing count is not worth losing a signal."""
    import numpy as np
    row = A.screen(flow(print_row(volume=np.nan, openInterest=np.nan))).iloc[0]
    text = A.format_alert(row)
    assert "vol ? vs OI ?" in text
    assert "NVDA" in text and "CALL" in text


def test_alert_is_four_lines():
    text = A.format_alert(A.screen(flow()).iloc[0])
    assert len(text.split("\n")) == 4


def test_volume_is_recorded_in_the_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "ALERT_DIR", tmp_path)
    A.append_ledger("2026-09-22", A.screen(flow()))
    saved = A.load_ledger("2026-09-22")
    assert "volume" in saved.columns and int(saved.iloc[0]["volume"]) == 250


def test_size_is_the_trade_size_not_the_pandas_attribute():
    """`Series.size` is the element count. Reading it as the trade size gives a
    plausible wrong number, so the same row must format identically whether it
    arrives as a Series or as an itertuples namedtuple."""
    hits = A.screen(flow(print_row(size=2509)))
    as_series = A.format_alert(hits.iloc[0])
    as_tuple = A.format_alert(next(hits.itertuples()))
    assert as_series == as_tuple
    assert "size 2,509" in as_series


# -- implied volatility and cost per contract -------------------------------

def test_iv_renders_as_a_percent_not_a_decimal():
    """The vendor already scales it: 72.813 means 72.8%, not 7281%."""
    row = A.screen(flow(print_row(impliedVolatility=72.813))).iloc[0]
    assert "IV 72.8%" in A.format_alert(row)


def test_cost_per_contract_is_option_price_times_one_hundred():
    row = A.screen(flow(print_row(optionPrice=1.69))).iloc[0]
    text = A.format_alert(row)
    assert "$1.69 ea" in text
    assert "$169 per contract" in text


def test_cost_per_contract_makes_an_expensive_name_obvious():
    """The number that justifies --exclude SNDK, shown in the alert itself."""
    row = A.screen(flow(print_row(ticker="SNDK", optionPrice=135.50,
                                  premium=1_029_830.0))).iloc[0]
    assert "$13,550 per contract" in A.format_alert(row)


def test_missing_option_price_degrades_to_a_dash():
    import numpy as np
    row = A.screen(flow(print_row(optionPrice=np.nan))).iloc[0]
    text = A.format_alert(row)
    assert "$? ea" in text and "$? per contract" in text


def test_iv_is_recorded_in_the_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "ALERT_DIR", tmp_path)
    A.append_ledger("2026-09-22", A.screen(flow()))
    saved = A.load_ledger("2026-09-22")
    assert "impliedVolatility" in saved.columns
    assert round(float(saved.iloc[0]["impliedVolatility"]), 3) == 72.813


# -- volume / open interest -------------------------------------------------

@pytest.mark.parametrize("volume,oi,expected", [
    (100, 1000, "routine"),      # 0.1
    (400, 1000, "routine"),      # 0.4, just under
    (500, 1000, "notable"),      # 0.5, band edge
    (1500, 1000, "notable"),     # 1.5
    (2000, 1000, "fresh"),       # 2.0, band edge
    (4900, 1000, "fresh"),
    (5000, 1000, "investigate"), # 5.0, band edge
    (50000, 1000, "investigate"),
])
def test_vol_oi_bands(volume, oi, expected):
    row = A.screen(flow(print_row(volume=volume, openInterest=oi))).iloc[0]
    assert A.vol_oi_band(row["vol_oi"], row["openInterest"]) == expected


def test_zero_open_interest_is_a_new_strike_not_an_infinite_ratio():
    row = A.screen(flow(print_row(volume=119, openInterest=0))).iloc[0]
    assert pd.isna(row["vol_oi"]), "division by zero must not produce inf"
    assert A.vol_oi_band(row["vol_oi"], row["openInterest"]) == "new strike"
    assert "new strike" in A.format_alert(row)


def test_alert_shows_the_ratio_and_its_band():
    row = A.screen(flow(print_row(volume=7011, openInterest=927))).iloc[0]
    text = A.format_alert(row)
    assert "vol 7,011 vs OI 927" in text
    assert "7.6x" in text and "investigate" in text


def test_min_vol_oi_filters():
    low = flow(print_row(id="lo", volume=100, openInterest=1000))    # 0.1
    high = flow(print_row(id="hi", volume=5000, openInterest=1000))  # 5.0
    both = pd.concat([low, high], ignore_index=True)
    assert set(A.screen(both, min_vol_oi=2.0)["id"]) == {"hi"}
    assert set(A.screen(both)["id"]) == {"lo", "hi"}


def test_a_new_strike_passes_any_vol_oi_floor():
    """Undefined ratio, but the freshest thing on the tape — dropping it would
    invert what the filter is for."""
    got = A.screen(flow(print_row(volume=119, openInterest=0)), min_vol_oi=5.0)
    assert len(got) == 1


# -- days to expiry ---------------------------------------------------------

@pytest.mark.parametrize("dte,band,passes", [
    (24, (0, float("inf")), True),
    (24, (7, 60), True),
    (3, (7, 60), False),
    (115, (7, 60), False),
    (7, (7, 60), True),      # inclusive low
    (60, (7, 60), True),     # inclusive high
])
def test_dte_band(dte, band, passes):
    got = A.screen(flow(print_row(dte=dte)), dte_band=band)
    assert (len(got) == 1) is passes


def test_dte_and_delta_apply_together():
    """Their point: 45dte at 0.15 delta is a different signal from 45dte at 0.60."""
    thin = flow(print_row(id="thin", dte=45, **{"greeks.delta": 0.12}))
    fat = flow(print_row(id="fat", dte=45, **{"greeks.delta": 0.60}))
    both = pd.concat([thin, fat], ignore_index=True)
    got = A.screen(both, dte_band=(30, 60), delta_band=(0.35, 0.70))
    assert set(got["id"]) == {"fat"}


def test_delta_band_is_configurable():
    row = flow(print_row(**{"greeks.delta": 0.32}))
    assert len(A.screen(row, delta_band=(0.30, 0.70))) == 1
    assert len(A.screen(row, delta_band=(0.35, 0.70))) == 0


# -- repeated flow ----------------------------------------------------------

def history(*rows):
    return A.prepare(pd.DataFrame(list(rows)))


def test_recurrence_counts_only_earlier_prints():
    """Point-in-time: a print after the trigger cannot inform it."""
    trigger = A.screen(flow(print_row(id="t", ts=pd.Timestamp("2026-09-22 13:00", tz="America/New_York"))))
    past = print_row(id="a", ts=pd.Timestamp("2026-09-22 10:00", tz="America/New_York"))
    future = print_row(id="b", ts=pd.Timestamp("2026-09-22 15:00", tz="America/New_York"))
    got = A.recurrence(trigger, history(past, future))
    assert int(got.iloc[0]["prior_n"]) == 1


def test_recurrence_separates_agreeing_from_opposing_flow():
    trigger = A.screen(flow(print_row(id="t", ts=pd.Timestamp("2026-09-22 13:00", tz="America/New_York"))))
    agree = print_row(id="a", ts=pd.Timestamp("2026-09-22 10:00", tz="America/New_York"))
    oppose = print_row(id="o", tradeSideCode="BID",
                       ts=pd.Timestamp("2026-09-22 11:00", tz="America/New_York"))
    got = A.recurrence(trigger, history(agree, oppose)).iloc[0]
    assert int(got["same_dir_n"]) == 1
    assert int(got["opp_dir_n"]) == 1
    assert got["aligned"] == pytest.approx(0.0), "equal both ways is no conviction"


def test_conviction_is_positive_when_prior_flow_agrees():
    trigger = A.screen(flow(print_row(id="t", ts=pd.Timestamp("2026-09-22 13:00", tz="America/New_York"))))
    agree = [print_row(id=f"a{i}", ts=pd.Timestamp(f"2026-09-22 1{i}:00", tz="America/New_York"))
             for i in range(3)]
    got = A.recurrence(trigger, history(*agree)).iloc[0]
    assert got["aligned"] == pytest.approx(1.0)


def test_strike_neighbourhood_and_same_expiry_are_counted():
    trigger = A.screen(flow(print_row(id="t", strikePrice=180.0,
                                      ts=pd.Timestamp("2026-09-22 13:00", tz="America/New_York"))))
    near = print_row(id="n", strikePrice=185.0,   # inside 10%
                     ts=pd.Timestamp("2026-09-22 10:00", tz="America/New_York"))
    far = print_row(id="f", strikePrice=300.0, expirationDate="2027-01-15",
                    ts=pd.Timestamp("2026-09-22 11:00", tz="America/New_York"))
    got = A.recurrence(trigger, history(near, far)).iloc[0]
    assert int(got["near_strike_n"]) == 1
    assert int(got["same_expiry_n"]) == 1


def test_recurrence_without_history_yields_nulls_not_a_crash():
    trigger = A.screen(flow())
    got = A.recurrence(trigger, pd.DataFrame())
    assert len(got) == 1 and pd.isna(got.iloc[0]["prior_n"])


def test_alert_omits_the_recurrence_line_when_there_is_no_history():
    row = A.recurrence(A.screen(flow()), pd.DataFrame()).iloc[0]
    text = A.format_alert(row)
    assert "prior" not in text
    assert len(text.split("\n")) == 4


def test_alert_adds_a_fifth_line_when_history_exists():
    trigger = A.screen(flow(print_row(id="t", ts=pd.Timestamp("2026-09-22 13:00", tz="America/New_York"))))
    past = print_row(id="a", ts=pd.Timestamp("2026-09-22 10:00", tz="America/New_York"))
    row = A.recurrence(trigger, history(past)).iloc[0]
    text = A.format_alert(row)
    assert len(text.split("\n")) == 5
    assert "1 prior" in text and "1 same way" in text


# -- open interest floor ----------------------------------------------------

@pytest.mark.parametrize("oi,passes", [
    (0, False), (1, False), (2, True), (900, True),
])
def test_min_oi_of_two_means_greater_than_one(oi, passes):
    """Open interest is an integer, so "greater than 1" is exactly >= 2."""
    got = A.screen(flow(print_row(openInterest=oi)), min_oi=2)
    assert (len(got) == 1) is passes


def test_min_oi_is_off_by_default():
    assert len(A.screen(flow(print_row(openInterest=0)))) == 1


def test_min_oi_beats_the_new_strike_exemption():
    """min_vol_oi lets a zero-OI contract through; an explicit OI floor must
    still reject it, or the two filters contradict each other."""
    row = flow(print_row(openInterest=0, volume=500))
    assert len(A.screen(row, min_vol_oi=5.0)) == 1
    assert len(A.screen(row, min_vol_oi=5.0, min_oi=2)) == 0


def test_unknown_open_interest_is_not_treated_as_zero():
    import numpy as np
    row = flow(print_row(openInterest=np.nan))
    assert len(A.screen(row, min_oi=2)) == 1, "null is unknown, not a failure"


def test_dte_under_sixty_excludes_leaps():
    near = flow(print_row(id="near", dte=24))
    leap = flow(print_row(id="leap", dte=115))
    both = pd.concat([near, leap], ignore_index=True)
    assert set(A.screen(both, dte_band=(0, 60))["id"]) == {"near"}


# -- call side --------------------------------------------------------------

def sided(right, side, **over):
    delta = 0.52 if right == "CALL" else -0.52
    return flow(print_row(contractType=right, tradeSideCode=side,
                          **{"greeks.delta": delta}, **over))


@pytest.mark.parametrize("side,passes", [
    ("ABOVE_ASK", True), ("ASK", True),
    ("BID", False), ("BELOW_BID", False), ("MID_MARKET", False),
])
def test_call_side_bullish_requires_a_bought_call(side, passes):
    assert (len(A.screen(sided("CALL", side), call_side="bullish")) == 1) is passes


@pytest.mark.parametrize("side,passes", [
    ("ABOVE_ASK", True), ("ASK", False), ("BID", False),
])
def test_call_side_above_ask_is_stricter_still(side, passes):
    assert (len(A.screen(sided("CALL", side), call_side="above-ask")) == 1) is passes


@pytest.mark.parametrize("mode", ["any", "bullish", "above-ask"])
@pytest.mark.parametrize("side", ["ABOVE_ASK", "ASK", "BID", "BELOW_BID"])
def test_puts_are_never_constrained_by_call_side(mode, side):
    """The asymmetry is the request, not an oversight."""
    assert len(A.screen(sided("PUT", side), call_side=mode)) == 1


def test_call_side_any_is_the_default_and_changes_nothing():
    for side in ("ABOVE_ASK", "ASK", "BID", "BELOW_BID"):
        assert len(A.screen(sided("CALL", side))) == 1


def test_an_unknown_call_side_is_rejected_loudly():
    with pytest.raises(ValueError, match="call_side"):
        A.screen(flow(), call_side="whatever")


def test_above_ask_is_visually_distinct_in_the_alert():
    """The thing being asked about should not read as one phrase among many."""
    row = A.screen(sided("CALL", "ABOVE_ASK")).iloc[0]
    assert "THROUGH the offer" in A.format_alert(row)
    plain = A.format_alert(A.screen(sided("CALL", "ASK")).iloc[0])
    assert "THROUGH" not in plain


# -- absolute expiry bounds -------------------------------------------------

@pytest.mark.parametrize("lo,hi,passes", [
    (None, None, True),
    ("2026-10-01", None, True),
    (None, "2026-11-01", True),
    ("2026-10-01", "2026-11-01", True),
    ("2026-11-01", None, False),      # expiry 2026-10-16 is before the floor
    (None, "2026-10-01", False),
])
def test_absolute_expiry_bounds(lo, hi, passes):
    row = flow(print_row(expirationDate="2026-10-16"))
    got = A.screen(row, expiry_min=lo, expiry_max=hi)
    assert (len(got) == 1) is passes


def test_absolute_expiry_is_independent_of_dte():
    """One pins a cycle, the other is a rolling window; both can apply."""
    row = flow(print_row(expirationDate="2026-10-16", dte=24))
    assert len(A.screen(row, dte_band=(0, 60), expiry_max="2026-10-31")) == 1
    assert len(A.screen(row, dte_band=(0, 10), expiry_max="2026-10-31")) == 0


# -- signal date (when the print happened) ----------------------------------

def at(when):
    return flow(print_row(ts=pd.Timestamp(when, tz="America/New_York")))


@pytest.mark.parametrize("lo,hi,passes", [
    (None, None, True),
    ("2026-09-22", None, True),
    (None, "2026-09-22", True),
    ("2026-09-22", "2026-09-22", True),    # one date means that whole session
    ("2026-09-23", None, False),
    (None, "2026-09-21", False),
])
def test_signal_date_bounds(lo, hi, passes):
    got = A.screen(at("2026-09-22 13:27"), signal_min=lo, signal_max=hi)
    assert (len(got) == 1) is passes


def test_a_bare_upper_date_means_end_of_day_not_midnight():
    """Read literally, --signal-max 2026-09-22 would exclude the whole session
    it names. A filter that silently returns nothing is a bad filter."""
    late = at("2026-09-22 15:59")
    assert len(A.screen(late, signal_max="2026-09-22")) == 1


def test_signal_bounds_accept_a_time_of_day():
    morning = at("2026-09-22 09:45")
    afternoon = at("2026-09-22 15:45")
    both = pd.concat([morning, afternoon], ignore_index=True)
    got = A.screen(both, signal_min="2026-09-22 12:00")
    assert len(got) == 1
    assert got.iloc[0]["ts"].hour == 15


def test_signal_date_is_independent_of_expiry_date():
    """One is when it printed, the other is when it expires."""
    row = flow(print_row(ts=pd.Timestamp("2026-09-22 13:00", tz="America/New_York"),
                         expirationDate="2026-10-16"))
    assert len(A.screen(row, signal_max="2026-09-22", expiry_min="2026-10-01")) == 1
    assert len(A.screen(row, signal_max="2026-09-21", expiry_min="2026-10-01")) == 0


def test_alert_names_the_date_not_only_the_clock():
    """Scrolling back through Slack, a bare time cannot be placed in a session."""
    text = A.format_alert(A.screen(at("2026-09-22 13:27:20")).iloc[0])
    assert "Tue 22 Sep 13:27:20 ET" in text


# -- what counts as a golden sweep ------------------------------------------

def test_golden_modes_map_to_distinct_pulls():
    assert A.GOLDEN_MODES["iso"] == {"trade_types": ("ISO",), "golden": None}
    assert A.GOLDEN_MODES["badge"] == {"trade_types": None, "golden": True}
    assert A.GOLDEN_MODES["both"] == {"trade_types": ("ISO",), "golden": True}


@pytest.mark.parametrize("mode,expect_iso,expect_badge", [
    ("iso", True, False),
    ("badge", False, True),
    ("both", True, True),
])
def test_poll_asks_the_feed_for_the_right_population(monkeypatch, mode,
                                                     expect_iso, expect_badge):
    seen = {}

    def _sweeps(session, **kw):
        seen.update(kw)
        return flow()

    monkeypatch.setattr(A.F, "sweeps", _sweeps)
    A.poll("2026-09-22", set(), 1_000_000, golden=mode)
    assert (seen.get("trade_types") == ("ISO",)) is expect_iso
    assert (seen.get("golden") is True) is expect_badge


def test_an_unknown_golden_mode_is_rejected(monkeypatch):
    monkeypatch.setattr(A.F, "sweeps", lambda *a, **k: flow())
    with pytest.raises(KeyError):
        A.poll("2026-09-22", set(), 1_000_000, golden="whatever")


def test_context_follows_the_same_definition(monkeypatch):
    """Recurrence judged against a different population than the trigger would
    compare apples to oranges."""
    seen = {}

    def _sweeps(session=None, **kw):
        seen.update(kw)
        return pd.DataFrame()

    monkeypatch.setattr(A.F, "sweeps", _sweeps)
    A.context("2026-09-22", lookback=3, golden="badge")
    assert seen.get("golden") is True and seen.get("trade_types") is None


# -- contract type, side, and why there is no sentiment filter --------------

@pytest.mark.parametrize("keep,right,passes", [
    ((), "CALL", True), ((), "PUT", True),
    (("CALL",), "CALL", True), (("CALL",), "PUT", False),
    (("PUT",), "PUT", True), (("PUT",), "CALL", False),
    (("CALL", "PUT"), "PUT", True),
])
def test_contract_type_filter(keep, right, passes):
    delta = 0.52 if right == "CALL" else -0.52
    row = flow(print_row(contractType=right, **{"greeks.delta": delta}))
    assert (len(A.screen(row, contract_types=keep)) == 1) is passes


@pytest.mark.parametrize("keep,side,passes", [
    ((), "ASK", True),
    (("ABOVE_ASK",), "ABOVE_ASK", True),
    (("ABOVE_ASK",), "ASK", False),
    (("ASK", "ABOVE_ASK"), "ASK", True),
    (("BID", "BELOW_BID"), "BELOW_BID", True),
    (("BID", "BELOW_BID"), "ASK", False),
])
def test_side_filter(keep, side, passes):
    assert (len(A.screen(flow(print_row(tradeSideCode=side)), sides=keep)) == 1) is passes


def test_side_and_contract_compose():
    """Bought calls only, the long way round from --call-side."""
    rows = pd.concat([
        flow(print_row(id="ca", contractType="CALL", tradeSideCode="ABOVE_ASK")),
        flow(print_row(id="cb", contractType="CALL", tradeSideCode="BID")),
        flow(print_row(id="pa", contractType="PUT", tradeSideCode="ABOVE_ASK",
                       **{"greeks.delta": -0.52})),
    ], ignore_index=True)
    got = A.screen(rows, contract_types=("CALL",), sides=("ABOVE_ASK",))
    assert set(got["id"]) == {"ca"}


def test_direction_is_the_sentiment_filter():
    """The vendor's sentimentType is the same function of contract type and
    aggressor side, agreeing with `direction` on 100% of prints — so filtering
    direction is filtering sentiment, and a second control would be a lie."""
    bull = A.screen(flow(print_row(tradeSideCode="ASK")))
    bear = A.screen(flow(print_row(tradeSideCode="BID")))
    assert bull.iloc[0]["direction"] == "BULL"
    assert bear.iloc[0]["direction"] == "BEAR"
    # NEUTRAL is exactly the no-aggressor case, which carries no direction.
    assert len(A.screen(flow(print_row(tradeSideCode="MID_MARKET")))) == 0


# -- trade type -------------------------------------------------------------

def typed(t, **over):
    return flow(print_row(tradeType=t, **over))


@pytest.mark.parametrize("keep,actual,passes", [
    ((), "ISO", True), ((), "AUTO", True),
    (("ISO",), "ISO", True),
    (("ISO",), "AUTO", False),
    (("ISO",), "MULTI_AUTO_COB", False),
    (("ISO", "AUTO"), "AUTO", True),
    (("MULTI_AUTO_COB",), "MULTI_AUTO_COB", True),
])
def test_trade_type_filter(keep, actual, passes):
    assert (len(A.screen(typed(actual), trade_types=keep)) == 1) is passes


def test_trade_type_composes_with_golden_mode_rather_than_replacing_it():
    """The badge is a pull, the type is a screen. Requiring both is legitimate
    and very restrictive: only ~3% of badge prints route as ISO."""
    rows = pd.concat([
        typed("ISO", id="a"),
        typed("AUTO", id="b"),
        typed("MULTI_AUTO_COB", id="c"),
    ], ignore_index=True)
    assert len(A.screen(rows)) == 3
    assert set(A.screen(rows, trade_types=("ISO",))["id"]) == {"a"}


def test_trade_type_is_off_by_default():
    assert len(A.screen(typed("MULTI_AUTO_COB"))) == 1


def test_alert_names_the_routing_type():
    text = A.format_alert(A.screen(typed("MULTI_AUTO_COB")).iloc[0])
    assert "MULTI_AUTO_COB · BULL" in text


def test_iso_is_bold_and_nothing_else_is():
    """ISO is the one value that says the order genuinely swept multiple
    exchanges. Bolding everything would emphasise nothing."""
    iso = A.format_alert(A.screen(typed("ISO")).iloc[0])
    auto = A.format_alert(A.screen(typed("AUTO")).iloc[0])
    assert "*ISO* · BULL" in iso
    assert "*AUTO*" not in auto and "AUTO · BULL" in auto


def test_a_missing_routing_type_degrades_to_a_dash():
    import numpy as np
    text = A.format_alert(A.screen(typed(np.nan)).iloc[0])
    assert "? · BULL" in text


def test_routing_type_is_recorded_in_the_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "ALERT_DIR", tmp_path)
    A.append_ledger("2026-09-22", A.screen(typed("ISO")))
    assert A.load_ledger("2026-09-22").iloc[0]["tradeType"] == "ISO"
