"""FOMC decision days — the macro analogue of the earnings veto.

Source: federalreserve.gov/monetarypolicy/fomccalendars.htm (fetched 2026-09-22).

Each scheduled FOMC meeting runs two days; **only the second day is an event**.
The statement lands at 14:00 ET and the press conference follows at 14:30, so a
position opened at 10:00 and closed before 14:00 on a decision day never saw the
event, while one held overnight into it did. `DECISION_DAYS` records that second
day and nothing else — day one is an ordinary session.

Excluded deliberately: the 2025-08-22 notation vote on the Statement on
Longer-Run Goals. It is a published FOMC action but not a rate decision, carries
no statement release or press conference, and moves nothing.

Unlike earnings, this calendar applies to **every symbol at once** — which is the
point. An earnings veto removes one name's idiosyncratic risk; an FOMC day is
common exposure across the whole book, so a log full of positions spanning the
same decision is not diversified the way its position count suggests.
"""
from __future__ import annotations

import pandas as pd

# (decision day, has a Summary of Economic Projections + press conference)
# SEP meetings carry the dot plot and are the larger of the two kinds.
DECISION_DAYS: list[tuple[str, bool]] = [
    ("2025-01-29", False), ("2025-03-19", True),  ("2025-05-07", False),
    ("2025-06-18", True),  ("2025-07-30", False), ("2025-09-17", True),
    ("2025-10-29", False), ("2025-12-10", True),
    ("2026-01-28", False), ("2026-03-18", True),  ("2026-04-29", False),
    ("2026-06-17", True),  ("2026-07-29", False), ("2026-09-16", True),
    ("2026-10-28", False), ("2026-12-09", True),
    ("2027-01-27", False), ("2027-03-17", True),  ("2027-04-28", False),
    ("2027-06-09", True),  ("2027-07-28", False), ("2027-09-15", True),
    ("2027-10-27", False), ("2027-12-08", True),
]

STATEMENT_HOUR = 14  # 14:00 ET statement; press conference at 14:30


def moments(tz: str = "America/New_York") -> pd.DatetimeIndex:
    """The instant each decision becomes public, tz-aware."""
    stamps = [pd.Timestamp(d) + pd.Timedelta(hours=STATEMENT_HOUR) for d, _ in DECISION_DAYS]
    return pd.DatetimeIndex(stamps).tz_localize(tz)


def frame() -> pd.DataFrame:
    """One row per decision, with the derived event instant."""
    out = pd.DataFrame(DECISION_DAYS, columns=["decision_date", "sep"])
    out["decision_date"] = pd.to_datetime(out["decision_date"])
    out["statement_at"] = moments().tz_localize(None)
    out["year"] = out["decision_date"].dt.year
    return out


def _as_naive_ny(when) -> pd.Timestamp:
    """Accept naive or tz-aware input; compare in wall-clock New York time."""
    stamp = pd.Timestamp(when)
    if stamp.tz is not None:
        stamp = stamp.tz_convert("America/New_York").tz_localize(None)
    return stamp


def next_in_window(start, end) -> str | None:
    """The first decision date strictly after `start` and at or before `end`.

    Half-open the same way the earnings veto is: a decision that has already
    printed by the time the sweep hits is not in the window, and one landing
    exactly at expiry is.
    """
    a, b = _as_naive_ny(start), _as_naive_ny(end)
    naive = moments().tz_localize(None)
    hit = naive[(naive > a) & (naive <= b)]
    return hit[0].date().isoformat() if len(hit) else None


def count_in_window(start, end) -> int:
    """How many decisions a holding period spans. A monthly can straddle two."""
    a, b = _as_naive_ny(start), _as_naive_ny(end)
    naive = moments().tz_localize(None)
    return int(((naive > a) & (naive <= b)).sum())


def is_decision_day(when) -> bool:
    """Did this timestamp fall on a decision day, at any hour?"""
    return _as_naive_ny(when).date() in {pd.Timestamp(d).date() for d, _ in DECISION_DAYS}


def before_statement(when) -> bool | None:
    """True if on a decision day and ahead of the 14:00 release; None off-day.

    Flow printed at 09:45 on an FOMC day is positioning into the event; flow at
    15:30 is a reaction to it. They are not the same signal.
    """
    stamp = _as_naive_ny(when)
    if not is_decision_day(stamp):
        return None
    return stamp.hour < STATEMENT_HOUR


if __name__ == "__main__":
    from pathlib import Path

    days = frame()
    out = Path(__file__).resolve().parents[2] / "data" / "fomc_days.parquet"
    days.to_parquet(out, index=False)
    span = f"{days.decision_date.min():%Y-%m-%d} .. {days.decision_date.max():%Y-%m-%d}"
    print(f"wrote {len(days)} FOMC decision days ({int(days.sep.sum())} with SEP) -> {out}")
    print(f"span {span}")
