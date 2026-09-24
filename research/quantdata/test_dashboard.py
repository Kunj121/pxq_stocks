"""Tests for the flow dashboard. No network: streams are stubbed.

    python -m pytest research/quantdata/test_dashboard.py -q

Filtering now happens in the browser, so these cover the two things Python is
still responsible for: shaping a correct payload, and emitting a page that can
actually boot. What the controls do to that payload is the JavaScript's job.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import dashboard as DB  # noqa: E402
from test_sweep_alerts import flow, print_row  # noqa: E402


def ix(name):
    return DB.COLUMNS.index(name)


def stub(monkeypatch, frame, fail: str | None = None):
    def _load(self, session, golden="iso"):
        if fail and self.key == fail:
            from client import QuantDataError
            raise QuantDataError("upstream refused")
        return frame
    monkeypatch.setattr(DB.Stream, "load", _load)


# -- money ------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (0, "$0"), (950, "$950"), (1_500, "$1.50k"),
    (101_430_000, "$101.43M"), (2_000_000, "$2M"), (3_100_000_000, "$3.10B"),
])
def test_money_scales(value, expected):
    assert DB.money(value) == expected


def test_money_handles_missing():
    assert DB.money(None) == "—"
    assert DB.money(float("nan")) == "—"


# -- client payload ---------------------------------------------------------

def test_empty_frame_yields_no_rows():
    assert DB.client_rows(pd.DataFrame()) == []


def test_every_row_has_one_cell_per_column():
    rows = DB.client_rows(flow())
    assert len(rows) == 1 and len(rows[0]) == len(DB.COLUMNS)


def test_row_carries_what_the_controls_filter_on():
    r = DB.client_rows(flow(print_row(ticker="NVDA", premium=1_500_000.0,
                                      openInterest=900, volume=250)))[0]
    assert r[ix("ticker")] == "NVDA"
    assert r[ix("premium")] == 1_500_000.0
    assert r[ix("oi")] == 900 and r[ix("volume")] == 250
    assert r[ix("cp")] == "C" and r[ix("dir")] == "BULL"


def test_puts_carry_a_positive_delta_magnitude():
    r = DB.client_rows(flow(print_row(contractType="PUT",
                                      **{"greeks.delta": -0.52})))[0]
    assert r[ix("cp")] == "P"
    assert r[ix("delta")] == pytest.approx(0.52)


def test_timestamps_travel_as_epoch_milliseconds():
    r = DB.client_rows(flow())[0]
    assert isinstance(r[ix("ts")], int)
    back = pd.Timestamp(r[ix("ts")], unit="ms", tz="UTC").tz_convert("America/New_York")
    assert back.strftime("%Y-%m-%d %H:%M") == "2026-09-22 13:27"


def test_funds_are_shipped_flagged_not_dropped():
    """The page offers an ETF toggle; a toggle over absent rows would be a lie."""
    rows = DB.client_rows(pd.concat([
        flow(print_row(id="a", ticker="NVDA")),
        flow(print_row(id="b", ticker="QQQ", isETF=True)),
    ], ignore_index=True))
    assert sorted(r[ix("fund")] for r in rows) == [False, True]


def test_indices_count_as_funds_too():
    r = DB.client_rows(flow(print_row(ticker="NDX", isIndex=True)))[0]
    assert r[ix("fund")] is True


def test_missing_values_travel_as_null_not_nan():
    """NaN is not valid JSON, and null lets the page tell absent from zero."""
    import numpy as np
    rows = DB.client_rows(flow(print_row(openInterest=np.nan)))
    assert rows[0][ix("oi")] is None
    json.loads(json.dumps(rows))


def test_the_whole_payload_is_json_serialisable(monkeypatch):
    """It is embedded in a script tag; anything numpy-typed breaks the page."""
    stub(monkeypatch, flow())
    payload = DB.build_payload("2026-09-22")
    json.loads(json.dumps(payload))


# -- build_payload ----------------------------------------------------------

def test_payload_has_a_row_set_per_stream(monkeypatch):
    stub(monkeypatch, flow())
    payload = DB.build_payload("2026-09-22")
    assert set(payload["rows"]) == {s.key for s in DB.STREAMS}
    assert [s["key"] for s in payload["streams"]] == [s.key for s in DB.STREAMS]


def test_a_broken_stream_becomes_empty_not_fatal(monkeypatch):
    stub(monkeypatch, flow(), fail="unusual")
    payload = DB.build_payload("2026-09-22")
    assert payload["rows"]["unusual"] == []
    assert payload["rows"]["golden"], "healthy streams still load"


def test_a_date_range_merges_every_session(monkeypatch):
    seen = []

    def _load(self, session, golden="iso"):
        seen.append(session)
        return flow(print_row(id=f"{self.key}-{session}"))

    monkeypatch.setattr(DB.Stream, "load", _load)
    payload = DB.build_payload("2026-09-22",
                               sessions=["2026-09-21", "2026-09-22"])
    assert len(payload["rows"]["golden"]) == 2
    assert "2026-09-21" in seen and "2026-09-22" in seen


def test_span_covers_the_data_present(monkeypatch):
    stub(monkeypatch, flow())
    assert DB.build_payload("2026-09-22")["span"] == ["2026-09-22", "2026-09-22"]


def test_span_falls_back_to_the_session_when_there_is_no_data(monkeypatch):
    stub(monkeypatch, pd.DataFrame())
    assert DB.build_payload("2026-09-22")["span"] == ["2026-09-22", "2026-09-22"]


def test_defaults_mirror_the_deployed_alerter(monkeypatch):
    """The page must open on what Slack is actually sending."""
    stub(monkeypatch, flow())
    d = DB.build_payload("2026-09-22")["defaults"]
    assert d["premium"] == 1_000_000
    assert (d["delta_min"], d["delta_max"]) == (0.30, 0.70)
    assert d["dte_max"] == 60.0 and d["min_oi"] == 2
    # No call_side: the chip groups express "calls, bought" directly, and a
    # second control for the same question is a place for the two to disagree.
    assert "call_side" not in d


# -- render -----------------------------------------------------------------

def test_page_embeds_the_payload_and_the_script(monkeypatch):
    stub(monkeypatch, flow())
    page = DB.render("2026-09-22", DB.build_payload("2026-09-22"))
    assert "window.FLOW=" in page
    assert "function draw()" in page


def test_every_stream_gets_a_tab(monkeypatch):
    stub(monkeypatch, flow())
    page = DB.render("2026-09-22", DB.build_payload("2026-09-22"))
    for stream in DB.STREAMS:
        assert f'id="tab-{stream.key}"' in page
        assert stream.title in page


def test_page_has_the_controls_the_script_reaches_for(monkeypatch):
    """Every id readControls() reads must exist, or the page throws on boot."""
    stub(monkeypatch, flow())
    page = DB.render("2026-09-22", DB.build_payload("2026-09-22"))
    for ident in ("f-premium", "f-dmin", "f-dmax", "f-dte", "f-oi", "f-voi",
                  "f-dir", "f-ticker", "f-from", "f-to",
                  "f-funds", "f-unusual", "f-opening", "reset", "stats",
                  "chart", "leaders", "prints", "panel-title", "panel-blurb"):
        assert f'id="{ident}"' in page, f"{ident} missing from the page"


def test_controls_open_on_the_deployed_settings(monkeypatch):
    stub(monkeypatch, flow())
    page = DB.render("2026-09-22", DB.build_payload("2026-09-22"))
    assert 'id="f-premium" type="number" step="50000" min="0" value="1000000"' in page
    assert 'id="f-dte" type="number" step="1" min="0" value="60"' in page


def test_page_declares_both_themes(monkeypatch):
    stub(monkeypatch, flow())
    page = DB.render("2026-09-22", DB.build_payload("2026-09-22"))
    assert "prefers-color-scheme:dark" in page
    assert "--ground:#F4F6F8" in page


def test_fragment_omits_the_document_wrapper(monkeypatch):
    """An Artifact supplies its own head and body; nesting breaks the page."""
    stub(monkeypatch, flow())
    frag = DB.render("2026-09-22", DB.build_payload("2026-09-22"), fragment=True)
    for tag in ("<!doctype", "<html", "<head>", "<body>"):
        assert tag not in frag.lower()
    assert "<title>" in frag and "window.FLOW=" in frag


def test_full_page_is_a_complete_document(monkeypatch):
    stub(monkeypatch, flow())
    page = DB.render("2026-09-22", DB.build_payload("2026-09-22"))
    assert page.startswith("<!doctype html>")
    assert page.rstrip().endswith("</html>")


def test_embedded_data_cannot_break_out_of_the_script_tag(monkeypatch):
    """A `</script>` in vendor data ends the element early, whatever the JSON
    says, because the HTML parser never sees it as a string."""
    stub(monkeypatch, flow(print_row(ticker="</script><script>evil()")))
    page = DB.render("2026-09-22", DB.build_payload("2026-09-22"))
    assert "</script><script>evil()" not in page
    assert "\\u003c/script" in page
    # Exactly two script elements: the payload and the behaviour.
    assert page.count("<script>") == 2


def test_iso500_stream_is_present_and_floored_at_500k():
    stream = next(s for s in DB.STREAMS if s.key == "iso500")
    assert stream.premium_min == 500_000


def test_badges_travel_as_columns():
    """Every row is an ISO sweep now, so the badges are what still tells them apart."""
    r = DB.client_rows(flow(print_row(isUnusual=True)))[0]
    assert r[ix("unusual")] is True
    assert r[ix("opening")] is True   # the fixture opens a position
    assert r[ix("gold")] is False


def test_badge_streams_are_floored_like_golden():
    """ISO is the binding constraint; the old higher floors left nothing behind it."""
    floors = {s.key: s.premium_min for s in DB.STREAMS}
    assert floors["unusual"] == floors["golden"] == 100_000
    assert floors["opening"] == 100_000


def test_the_page_says_which_definition_is_in_use(monkeypatch):
    """The one label a reader trusts most must not claim ISO when it is the
    vendor badge."""
    stub(monkeypatch, flow())
    payload = DB.build_payload("2026-09-22")

    iso_page = DB.render("2026-09-22", payload, golden="iso")
    assert "Golden sweep here means ISO" in iso_page

    badge_page = DB.render("2026-09-22", payload, golden="badge")
    assert "means the vendor" in badge_page
    assert "Direction is weaker here" in badge_page


def test_the_golden_tab_is_relabelled_in_badge_mode(monkeypatch):
    stub(monkeypatch, flow())
    iso = DB.build_payload("2026-09-22", golden="iso")
    badge = DB.build_payload("2026-09-22", golden="badge")

    title = lambda pl: next(s["title"] for s in pl["streams"] if s["key"] == "golden")
    assert title(iso) == "Golden sweeps · ISO"
    assert title(badge) == "Golden sweeps · vendor badge"

    others = lambda pl: [s["title"] for s in pl["streams"] if s["key"] != "golden"]
    assert others(iso) == others(badge), "only the golden tab changes"


def test_golden_mode_reaches_only_the_golden_stream(monkeypatch):
    """Exactly one stream follows the mode. The badge streams stay ISO because
    a badge alone is not a routing fact, and iso500 stays ISO because that is
    what its name promises."""
    sweeps, flagged = [], []

    def _sweeps(session, **kw):
        sweeps.append(kw)
        return flow()

    def _flagged(flag, session, **kw):
        flagged.append(kw)
        return flow()

    monkeypatch.setattr(DB.F, "sweeps", _sweeps)
    monkeypatch.setattr(DB.F, "flagged", _flagged)
    DB.build_payload("2026-09-22", golden="badge")

    following = [kw for kw in sweeps if kw.get("golden") is True]
    assert len(following) == 1
    assert following[0]["premium_min"] == 100_000
    assert all("golden" not in kw for kw in flagged)


def test_iso500_stays_iso_whatever_the_golden_mode(monkeypatch):
    """Following the mode made it an exact duplicate of `golden` in badge mode:
    the badge implies ~$1M premium, so its $500k floor then does nothing."""
    asked = []

    def _sweeps(session, **kw):
        asked.append(kw)
        return flow()

    monkeypatch.setattr(DB.F, "sweeps", _sweeps)
    monkeypatch.setattr(DB.F, "flagged", lambda flag, session, **kw: flow())
    DB.build_payload("2026-09-22", golden="badge")

    by_floor = {kw["premium_min"]: kw for kw in asked}
    assert by_floor[500_000].get("golden") is None, "iso500 must stay ISO"
    assert by_floor[100_000].get("golden") is True, "golden follows the mode"


# -- live refresh -----------------------------------------------------------

def test_a_served_page_polls_its_sidecar(monkeypatch):
    stub(monkeypatch, flow())
    page = DB.render("2026-09-22", DB.build_payload("2026-09-22"),
                     data_url="flow.json")
    assert '"dataUrl":"flow.json"' in page
    assert "setInterval(freshen" in page
    assert "refreshes every minute" in page


def test_a_snapshot_has_nothing_to_poll(monkeypatch):
    """A published fragment has no server beside it; polling would 404 forever."""
    stub(monkeypatch, flow())
    frag = DB.render("2026-09-22", DB.build_payload("2026-09-22"), fragment=True)
    assert '"dataUrl":null' in frag
    assert "does not refresh" in frag


def test_refresh_replaces_data_without_reloading(monkeypatch):
    """A meta refresh would throw away the filters, the sort and the scroll
    position, which is the whole reason this is a fetch."""
    stub(monkeypatch, flow())
    page = DB.render("2026-09-22", DB.build_payload("2026-09-22"),
                     data_url="flow.json")
    assert "http-equiv" not in page
    assert "location.reload" not in page
    assert "F.rows = fresh.rows" in page


def test_payload_is_stamped_so_the_header_can_change(monkeypatch):
    stub(monkeypatch, flow())
    payload = DB.build_payload("2026-09-22")
    assert "generated" in payload
    page = DB.render("2026-09-22", payload, data_url="flow.json")
    assert 'id="updated"' in page


def test_page_has_every_chip_group(monkeypatch):
    stub(monkeypatch, flow())
    page = DB.render("2026-09-22", DB.build_payload("2026-09-22"))
    for ident in ("f-cp", "f-side", "f-dir", "f-type"):
        assert f'id="{ident}"' in page
        assert f'"{ident}": new Set()' in page or f'"{ident}"' in page
    assert "Calls or puts" in page
    assert "Execution side" in page
    assert "Trade type" in page
    # Named sentiment, since that is what the user looks for.
    assert "Sentiment" in page


def test_chip_groups_start_empty_meaning_no_constraint(monkeypatch):
    """An untouched group must let everything through, not nothing."""
    stub(monkeypatch, flow())
    page = DB.render("2026-09-22", DB.build_payload("2026-09-22"))
    assert 'new Set()' in page
    assert "if (c.type.size &&" in page, "size 0 short-circuits the filter"


def test_chips_are_multi_select_not_radio(monkeypatch):
    """Clicking toggles membership of a Set; nothing deselects a sibling."""
    stub(monkeypatch, flow())
    page = DB.render("2026-09-22", DB.build_payload("2026-09-22"))
    assert "CHIPS[group].delete(v)" in page and "CHIPS[group].add(v)" in page
    assert 'aria-pressed' in page


def test_trade_types_are_built_from_the_data_not_hardcoded(monkeypatch):
    """The feed carries a dozen types and adds more; a stale list would
    quietly hide flow."""
    stub(monkeypatch, flow())
    page = DB.render("2026-09-22", DB.build_payload("2026-09-22"))
    assert "CHIP_DEFS[\"f-type\"] = [...counts.entries()]" in page
    assert '"MULTI_AUTO_COB"' not in page.split("window.FLOW=")[0]


def test_trade_type_reaches_the_browser():
    r = DB.client_rows(flow())[0]
    assert r[ix("type")] == "ISO"


def test_selected_chips_survive_a_data_refresh(monkeypatch):
    """Rebuilding the buttons clears their pressed state; silently widening the
    viewer's filter on every poll would be worse than not refreshing."""
    stub(monkeypatch, flow())
    page = DB.render("2026-09-22", DB.build_payload("2026-09-22"),
                     data_url="flow.json")
    assert "CHIPS[g].has(c.dataset.v)" in page


# -- polling efficiency -----------------------------------------------------

def test_poll_allows_a_304(monkeypatch):
    """The server answers If-Modified-Since with 304 and zero bytes. A
    cache-buster or no-store would defeat that and re-download every minute."""
    stub(monkeypatch, flow())
    page = DB.render("2026-09-22", DB.build_payload("2026-09-22"),
                     data_url="flow.json")
    assert '"?t=" + Date.now()' not in page
    assert 'cache: "no-store"' not in page
    assert 'cache: "no-cache"' in page
    assert "r.status === 304" in page


def test_identical_data_does_not_trigger_a_redraw(monkeypatch):
    """A 200 can still carry the same payload; re-rendering 200 rows to show
    the same numbers is work nobody asked for."""
    stub(monkeypatch, flow())
    page = DB.render("2026-09-22", DB.build_payload("2026-09-22"),
                     data_url="flow.json")
    assert "fresh.generated === lastStamp" in page


def test_polling_backs_off_outside_market_hours(monkeypatch):
    """Overnight the file is frozen, so a minute-by-minute poll spends hours
    asking a question whose answer cannot change."""
    stub(monkeypatch, flow())
    page = DB.render("2026-09-22", DB.build_payload("2026-09-22"),
                     data_url="flow.json")
    assert "function marketOpen" in page
    assert "open ? 60000 : 600000" in page


def test_a_snapshot_never_schedules_a_poll(monkeypatch):
    stub(monkeypatch, flow())
    frag = DB.render("2026-09-22", DB.build_payload("2026-09-22"), fragment=True)
    assert '"dataUrl":null' in frag


def test_the_calls_must_be_control_is_gone(monkeypatch):
    """Expressible as the calls chip plus the bullish sentiment chip; keeping
    both invites them to disagree."""
    stub(monkeypatch, flow())
    page = DB.render("2026-09-22", DB.build_payload("2026-09-22"))
    assert "f-callside" not in page
    assert "Calls must be" not in page
    assert "callSide" not in page


# -- saving the view --------------------------------------------------------

def test_page_offers_save_and_reset(monkeypatch):
    stub(monkeypatch, flow())
    page = DB.render("2026-09-22", DB.build_payload("2026-09-22"))
    assert 'id="save"' in page and 'id="reset"' in page
    assert "Save view" in page


def test_save_stores_controls_chips_tab_and_sort(monkeypatch):
    """Restoring half a view is worse than restoring none: the page would look
    like the saved one while filtering differently."""
    stub(monkeypatch, flow())
    page = DB.render("2026-09-22", DB.build_payload("2026-09-22"))
    assert "function saveView" in page and "function loadView" in page
    assert "{ inputs, chips, active, sortKey, sortDir }" in page


def test_storage_access_is_guarded(monkeypatch):
    """localStorage throws outright in some contexts, and comes back empty in
    others. The page has to render correctly with nothing stored."""
    stub(monkeypatch, flow())
    page = DB.render("2026-09-22", DB.build_payload("2026-09-22"))
    body = page.split("function saveView")[1].split("function boot")[0]
    assert body.count("try {") >= 2 and body.count("catch") >= 2
    assert "could not save" in page


def test_reset_clears_the_saved_view(monkeypatch):
    """Otherwise Reset appears to work and the old view returns on reload."""
    stub(monkeypatch, flow())
    page = DB.render("2026-09-22", DB.build_payload("2026-09-22"))
    assert "localStorage.removeItem(STORE)" in page


def test_a_stale_saved_date_range_is_dropped(monkeypatch):
    """A range saved on an older session would silently show an empty page."""
    stub(monkeypatch, flow())
    page = DB.render("2026-09-22", DB.build_payload("2026-09-22"))
    assert "from.value > F.span[1] || to.value < F.span[0]" in page
    assert "outside this data" in page


def test_a_removed_control_does_not_break_restore(monkeypatch):
    """Views saved before `calls must be` was dropped still load."""
    stub(monkeypatch, flow())
    page = DB.render("2026-09-22", DB.build_payload("2026-09-22"))
    assert "if (!el) return;" in page
