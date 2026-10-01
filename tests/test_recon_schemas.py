"""Tests for tools/recon_schemas.py — header-only schema census parsing.

These cover the delimiter sniffing and header extraction the census depends
on. Duplicate raw headers (HR, HR.1, ...) must survive parsing intact: the
census records them with occurrence indexes and never collapses them.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.recon_schemas import (
    decode_head,
    extract_columns,
    parse_header_line,
)


def test_semicolon_header_with_duplicate_columns_preserved():
    # BetterCare-style: repeated HR channels must NOT be collapsed.
    cols = parse_header_line("Time;HR;HR.1;HR.2;SpO2", None)
    assert cols == ["Time", "HR", "HR.1", "HR.2", "SpO2"]


def test_comma_header_sniffed():
    cols = parse_header_line("Time,HR,SpO2,NIBP", None)
    assert cols == ["Time", "HR", "SpO2", "NIBP"]


def test_explicit_delimiter_used_directly():
    cols = parse_header_line("a|b|c", "|")
    assert cols == ["a", "b", "c"]


def test_single_column_header_rejected():
    # A one-field "header" is not a real schema line.
    assert parse_header_line("just-a-title", None) is None


def test_decode_head_strips_bom():
    raw = "Time;HR\r\n".encode("utf-8-sig")
    assert decode_head(raw) == "Time;HR\r\n"


def test_decode_head_cp1252_fallback():
    raw = "Patienté;HR\r\n".encode("cp1252")
    text = decode_head(raw)
    assert text is not None and "Patient" in text


def test_extract_columns_header_row_offset():
    # Some exports put metadata lines above the real header.
    text = "Exported by device X\nversion=2\nTime;HR;SpO2\n"
    cols, reason = extract_columns(text, ";", 2)
    assert cols == ["Time", "HR", "SpO2"]
    assert reason == ""


def test_extract_columns_too_short_returns_none():
    cols, reason = extract_columns("only one line", ";", 3)
    assert cols is None
    assert reason == "short_head"


def test_extract_columns_empty_file():
    cols, reason = extract_columns("", ";", 0)
    assert cols is None
    assert reason == "empty_file"


def test_extract_columns_blank_header():
    cols, reason = extract_columns("\n", ";", 0)
    assert cols is None
    assert reason == "blank_header"
