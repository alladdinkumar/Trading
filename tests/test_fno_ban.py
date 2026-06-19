"""Tests for trading.data.fno_ban — NSE F&O ban-list fetch/parse (F-010)."""

from __future__ import annotations

from trading.data.fno_ban import fetch_fno_ban_symbols, parse_fno_ban_csv

# Legacy NSE fo_secban.csv: a header line then "<serial>|<SYMBOL>" rows.
_SAMPLE = """Date|02-Jan-2026
1|IDEA
2|BANDHANBNK
3|HINDCOPPER
"""


def test_parse_extracts_symbols_in_order() -> None:
    assert parse_fno_ban_csv(_SAMPLE) == ["IDEA", "BANDHANBNK", "HINDCOPPER"]


def test_parse_skips_header_and_serials() -> None:
    # No bare 'DATE', serial number, or date value leaks through as a symbol.
    out = parse_fno_ban_csv(_SAMPLE)
    assert "DATE" not in out
    assert "1" not in out
    assert all(not s[0].isdigit() for s in out)


def test_parse_handles_comma_delimiter_and_blank_lines() -> None:
    text = "Sr.No.,Symbol\n1,TATASTEEL\n\n2,M&M\n"
    assert parse_fno_ban_csv(text) == ["TATASTEEL", "M&M"]


def test_parse_dedupes_preserving_order() -> None:
    assert parse_fno_ban_csv("1|IDEA\n2|IDEA\n3|GNFC\n") == ["IDEA", "GNFC"]


def test_parse_empty_or_garbage_returns_empty() -> None:
    assert parse_fno_ban_csv("") == []
    assert parse_fno_ban_csv("\n\n   \n") == []


class _FakeResp:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class _OkSession:
    def get(self, url: str, **_: object) -> _FakeResp:
        return _FakeResp("1|IDEA\n2|GNFC\n")


class _BoomSession:
    def get(self, url: str, **_: object) -> _FakeResp:
        raise RuntimeError("nse down")


def test_fetch_parses_session_body() -> None:
    assert fetch_fno_ban_symbols(session=_OkSession()) == ["IDEA", "GNFC"]


def test_fetch_is_best_effort_on_error() -> None:
    assert fetch_fno_ban_symbols(session=_BoomSession()) == []
