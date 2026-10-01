"""Tests for recon_forensics fingerprinting (no Dropbox needed)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.recon_forensics import fingerprint, header_parseable


def test_undecodable():
    raw = bytes([0x80, 0x81, 0xFE, 0xFF] * 64)
    fp = fingerprint(raw)
    assert fp["strict_decode_ok"] is None
    assert fp["fingerprint_class"] == "undecodable"


def test_binary_embedded():
    raw = b"a,b,c\n" + b"\x00\x01\x02" * 2000
    fp = fingerprint(raw)
    assert fp["null_ratio"] > 0.05
    assert fp["fingerprint_class"] == "binary_embedded"


def test_single_line():
    raw = b"a;b;c;d" * 5000
    fp = fingerprint(raw)
    assert fp["line_ending"] == "none"
    assert fp["fingerprint_class"] == "single_line"


def test_no_delimiter():
    raw = b"just some text here\nmore text\n"
    fp = fingerprint(raw)
    assert fp["fingerprint_class"] == "no_delimiter"


def test_bom_detected():
    raw = "a;b\n1;2\n".encode("utf-8-sig")
    fp = fingerprint(raw)
    assert fp["bom"] == "utf-8-sig"


def test_header_parseable_true():
    assert header_parseable(b"Time;HR;SpO2\n0;72;98\n")


def test_header_parseable_false():
    assert not header_parseable(b"\x00\x01\x02 binary junk \x00" * 100)
