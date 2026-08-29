"""Smoke tests for the getShowtimes parsing path.

Everything here runs against a captured-shape payload fixture — nothing in
this file touches the network or the real Regal site.
"""

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import monitor  # noqa: E402


# A trimmed copy of a real getShowtimes response: one "shows" entry holding a
# Film list, each film with Performances carrying Auditorium, CalendarShowTime
# and PerformanceAttributes.
SHOWTIMES_PAYLOAD = {
    "shows": [
        {
            "Film": [
                {
                    "Title": "Dhurandhar The Revenge (Hindi)",
                    "Performances": [
                        {
                            "Auditorium": 7,
                            "CalendarShowTime": "2026-03-27T14:30:00",
                            "PerformanceAttributes": ["2D", "RPX", "CC"],
                        },
                        {
                            "Auditorium": 7,
                            "CalendarShowTime": "2026-03-27T09:05:00",
                            "PerformanceAttributes": ["2D", "RPX", "CC"],
                        },
                        {
                            "Auditorium": 3,
                            "CalendarShowTime": "2026-03-27T19:45:00",
                            "PerformanceAttributes": ["IMAX"],
                        },
                    ],
                },
                {
                    "Title": "Some Other Movie",
                    "Performances": [
                        {
                            "Auditorium": 1,
                            "CalendarShowTime": "2026-03-27T12:00:00",
                            "PerformanceAttributes": [],
                        }
                    ],
                },
            ]
        }
    ]
}

TARGET_DATE = date(2026, 3, 27)


@pytest.fixture
def logger():
    return MagicMock()


def test_find_movie_matches_case_insensitive_substring(logger):
    film = monitor.find_movie(SHOWTIMES_PAYLOAD, "dhurandhar", logger)
    assert film is not None
    assert film["Title"] == "Dhurandhar The Revenge (Hindi)"


def test_find_movie_returns_none_when_absent(logger):
    assert monitor.find_movie(SHOWTIMES_PAYLOAD, "Nonexistent Film", logger) is None


def test_find_movie_tolerates_empty_payload(logger):
    assert monitor.find_movie({}, "anything", logger) is None
    assert monitor.find_movie({"shows": []}, "anything", logger) is None


def test_film_titles_lists_the_whole_schedule():
    assert monitor._film_titles(SHOWTIMES_PAYLOAD) == [
        "Dhurandhar The Revenge (Hindi)",
        "Some Other Movie",
    ]
    assert monitor._film_titles({}) == []
    assert monitor._film_titles(None) == []


def test_parse_language_reads_trailing_parenthetical():
    assert monitor._parse_language("Dhurandhar The Revenge (Hindi)") == "Hindi"
    assert monitor._parse_language("Some Other Movie") == ""


def test_format_time_and_experience_label():
    assert monitor._format_time("2026-03-27T14:30:00") == "2:30 PM"
    assert monitor._experience_label(["2D", "RPX", "CC"]) == "2D · RPX"
    assert monitor._experience_label(["CC"]) == "Standard"


def test_build_email_renders_grouped_sorted_showtimes(logger):
    film = monitor.find_movie(SHOWTIMES_PAYLOAD, "Dhurandhar", logger)
    subject, plain, html = monitor.build_email(film, TARGET_DATE)

    assert subject == "Showtimes Live – Dhurandhar The Revenge (Hindi) | Mar 27"

    # Screens grouped and ordered; times sorted within a screen.
    assert "Screen 3 (IMAX): 7:45 PM" in plain
    assert "Screen 7 (2D · RPX): 9:05 AM, 2:30 PM" in plain
    assert plain.index("Screen 3") < plain.index("Screen 7")

    assert "Friday, March 27, 2026" in plain
    assert "date=03-27-2026" in plain

    assert html.startswith("<!DOCTYPE html>")
    assert "Hindi" in html
    assert "9:05 AM" in html and "7:45 PM" in html


def test_fetch_showtimes_prefers_the_direct_path(monkeypatch, logger):
    """The fast HTTPS path stays first; the browser is only a fallback."""
    called = []
    monkeypatch.setattr(
        monitor, "_fetch_direct", lambda d, lg: called.append("direct") or SHOWTIMES_PAYLOAD
    )
    monkeypatch.setattr(
        monitor, "_fetch_via_browser",
        lambda d, lg: pytest.fail("browser must not run when the direct path succeeds"),
    )
    assert monitor.fetch_showtimes(TARGET_DATE, logger) is SHOWTIMES_PAYLOAD
    assert called == ["direct"]


def test_fetch_showtimes_falls_back_to_browser(monkeypatch, logger):
    monkeypatch.setattr(monitor, "_fetch_direct", lambda d, lg: None)
    monkeypatch.setattr(monitor, "_fetch_via_browser", lambda d, lg: SHOWTIMES_PAYLOAD)
    assert monitor.fetch_showtimes(TARGET_DATE, logger) is SHOWTIMES_PAYLOAD


def test_profile_dir_lives_in_the_repo_under_a_known_name():
    assert monitor.BROWSER_PROFILE_DIR.name == ".browser-profile"
    assert monitor.BROWSER_PROFILE_DIR.parent == monitor.SCRIPT_DIR


def test_browser_context_uses_the_persistent_profile(logger, tmp_path, monkeypatch):
    """The fallback must reuse a profile dir so Cloudflare clearance survives."""
    profile = tmp_path / ".browser-profile"
    monkeypatch.setattr(monitor, "BROWSER_PROFILE_DIR", profile)

    playwright = MagicMock()
    monitor._open_browser_context(playwright, logger)

    playwright.chromium.launch.assert_not_called()
    kwargs = playwright.chromium.launch_persistent_context.call_args.kwargs
    assert kwargs["user_data_dir"] == str(profile)
    assert kwargs["headless"] is True
    assert profile.is_dir()


def test_browser_context_falls_back_when_profile_is_unusable(logger, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "BROWSER_PROFILE_DIR", tmp_path / ".browser-profile")
    playwright = MagicMock()
    playwright.chromium.launch_persistent_context.side_effect = RuntimeError("locked")

    context = monitor._open_browser_context(playwright, logger)

    playwright.chromium.launch.assert_called_once()
    assert context is playwright.chromium.launch.return_value.new_context.return_value
    logger.warning.assert_called_once()


def test_profile_dir_is_gitignored():
    gitignore = (Path(__file__).resolve().parents[1] / ".gitignore").read_text()
    assert ".browser-profile/" in gitignore
