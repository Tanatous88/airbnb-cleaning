"""Holiday detection for the review generator."""
import pytest

from app.main import _seasonal_note


@pytest.mark.parametrize("checkin,checkout,expected", [
    ("2026-02-12", "2026-02-13", "Valentine"),
    ("2026-03-16", "2026-03-16", "Patrick"),
    ("2026-05-23", "2026-05-24", "Memorial"),     # Sat/Sun before Mon May 25
    ("2026-07-02", "2026-07-03", "Fourth of July"),
    ("2025-08-30", "2025-08-31", "Labor Day"),     # Labor Day 2025 is Sep 1
    ("2026-10-20", "2026-10-21", "Halloween"),
    ("2026-11-20", "2026-11-21", "Thanksgiving"),
    ("2026-12-10", "2026-12-11", "Christmas"),
    ("2026-12-30", "2027-01-01", "New Year"),
    ("2026-04-10", "2026-04-12", ""),
])
def test_seasonal_note(checkin, checkout, expected):
    note = _seasonal_note(checkin, checkout)
    if expected:
        assert expected in note
    else:
        assert note == ""
